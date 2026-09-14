"""Private S3-compatible storage for admitted Web evidence originals.

Originals are never addressed by a browser filename or a public URL.  The
server re-verifies the staged bytes, writes one opaque key to a private bucket,
and requires the object store to return the SHA-256 checksum it accepted.  A
later worker must materialize the object into its own private work directory;
this module intentionally does not create signed browser download links.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .web_upload_staging import AdmittedWebPdfUpload
from .web_zip_staging import AdmittedWebZip


class WebObjectStoreBlocked(ValueError):
    """The private object-store boundary cannot preserve evidence integrity."""


class S3CompatibleClient(Protocol):
    """The deliberately small subset used by the original-evidence adapter."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...


class _ChecksumAwareS3Client:
    """Request checksum metadata on every object HEAD verification.

    S3-compatible services such as MinIO may omit ``ChecksumSHA256`` unless
    the request explicitly opts into checksum mode.  Every validation method
    in this module treats that checksum as part of the immutable receipt, so
    make the opt-in an adapter invariant rather than relying on each caller
    to remember it.
    """

    def __init__(self, delegate: S3CompatibleClient) -> None:
        self._delegate = delegate

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        request = dict(kwargs)
        request.setdefault("ChecksumMode", "ENABLED")
        return self._delegate.head_object(**request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


_BUCKET = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")
_ACCESS_KEY = re.compile(r"^[A-Za-z0-9/+=,.@_-]{3,256}$")
_CASE_AGENT_CANDIDATE_MAX_BYTES = 64 * 1024 * 1024
_CASE_AGENT_RESEARCH_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_CASE_AGENT_RESEARCH_ARCHIVE_MAX_BYTES = 4 * 1024 * 1024
_CASE_AGENT_LAWYER_ANALYSIS_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_CASE_AGENT_LAWYER_ANALYSIS_ARCHIVE_MAX_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class S3PrivateObjectStoreConfig:
    """Explicit server-owned configuration; credentials never appear in repr."""

    endpoint_url: str
    region_name: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    server_side_encryption: str = "AES256"
    kms_key_id: str | None = field(default=None, repr=False)
    allow_insecure_internal_endpoint: bool = False

    def __post_init__(self) -> None:
        _validate_endpoint(self.endpoint_url, allow_insecure=self.allow_insecure_internal_endpoint)
        if not isinstance(self.region_name, str) or not re.fullmatch(r"[a-z0-9-]{2,32}", self.region_name):
            raise ValueError("Web object-store region is invalid")
        if not isinstance(self.bucket, str) or not _BUCKET.fullmatch(self.bucket) or ".." in self.bucket:
            raise ValueError("Web object-store bucket is invalid")
        if not isinstance(self.access_key_id, str) or not _ACCESS_KEY.fullmatch(self.access_key_id):
            raise ValueError("Web object-store access key is invalid")
        if not isinstance(self.secret_access_key, str) or not (16 <= len(self.secret_access_key) <= 1024):
            raise ValueError("Web object-store secret access key is invalid")
        if self.server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("Web object-store encryption mode is invalid")
        if self.server_side_encryption == "aws:kms":
            if not isinstance(self.kms_key_id, str) or not self.kms_key_id.strip() or len(self.kms_key_id) > 512:
                raise ValueError("Web object-store KMS key is required")
        elif self.kms_key_id is not None:
            raise ValueError("Web object-store KMS key is only valid with aws:kms")


@dataclass(frozen=True)
class StoredWebEvidenceOriginal:
    """Server-only locator.  Never serialize ``object_key`` to the browser."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredWebEvidenceDerivative:
    """Server-only locator for a verified derivative PDF."""

    object_key: str = field(repr=False)
    artifact_sha256: str
    page_count: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredWebMaterialArchive:
    """Server-only locator for an admitted ZIP awaiting child processing."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    entry_count: int
    expanded_byte_size: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredWebOfficeArtifact:
    """Server-only locator for a generated DOCX/XLSX review artifact."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    media_type: str
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredCaseAgentMaterial:
    """Server-only locator for one scanner-admitted Office source."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    media_type: str
    admitted_format: str
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredCaseAgentReviewCandidate:
    """Server-only locator for canonical JSON awaiting lawyer review."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredCaseAgentResearchResponse:
    """Server-only exact provider-response and egress-receipt archive."""

    object_key: str = field(repr=False)
    request_hash: str
    response_sha256: str
    response_bytes: int
    archive_sha256: str
    archive_bytes: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredCaseAgentLawyerAnalysisResponse:
    """Private, immutable response archive for one lawyer-analysis call."""

    object_key: str = field(repr=False)
    request_hash: str
    response_sha256: str
    response_bytes: int
    archive_sha256: str
    archive_bytes: int
    object_version_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StoredReviewableDocumentPackageObject:
    """Server-only receipt for one member of an immutable document package."""

    object_key: str = field(repr=False)
    content_sha256: str
    byte_size: int
    media_type: str
    object_version_id: str | None = field(default=None, repr=False)


class S3CompatiblePrivateObjectStore:
    """Put immutable evidence originals into a private S3-compatible bucket."""

    def __init__(self, config: S3PrivateObjectStoreConfig, *, client: S3CompatibleClient | None = None) -> None:
        if not isinstance(config, S3PrivateObjectStoreConfig):
            raise ValueError("Web object-store configuration is required")
        self._config = config
        self._client = _ChecksumAwareS3Client(client or _new_boto3_client(config))
        for method in ("put_object", "head_object", "get_object", "delete_object"):
            if not callable(getattr(self._client, method, None)):
                raise ValueError("Web object-store client is invalid")

    def put_verified_pdf(
        self,
        upload: AdmittedWebPdfUpload,
        *,
        firm_id: str,
        matter_id: str,
    ) -> StoredWebEvidenceOriginal:
        """Persist only a freshly re-verified admitted PDF under an opaque key."""

        _validate_uuid("firm_id", firm_id)
        _validate_uuid("matter_id", matter_id)
        _validate_upload(upload)
        _verify_upload_bytes(upload)
        object_key = _object_key(firm_id=firm_id, matter_id=matter_id, content_sha256=upload.content_sha256)
        checksum = base64.b64encode(bytes.fromhex(upload.content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": upload.byte_size,
            "ContentType": "application/pdf",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {
                "lawcase-source-sha256": upload.content_sha256,
                "lawcase-source-bytes": str(upload.byte_size),
                "lawcase-inspection-hash": upload.inspection_hash,
            },
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            with upload.path.open("rb") as source:
                response = self._client.put_object(Body=source, **request)
            _verify_upload_bytes(upload)
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            _validate_remote_object(head, upload=upload, expected_checksum=checksum)
        except WebObjectStoreBlocked:
            self._best_effort_delete(object_key)
            raise
        except Exception as error:
            self._best_effort_delete(object_key)
            raise WebObjectStoreBlocked("private evidence object could not be stored or verified") from error
        version = response.get("VersionId") if isinstance(response, dict) else None
        if version is not None and (not isinstance(version, str) or not version):
            self._best_effort_delete(object_key)
            raise WebObjectStoreBlocked("private evidence object version is invalid")
        return StoredWebEvidenceOriginal(
            object_key=object_key,
            content_sha256=upload.content_sha256,
            byte_size=upload.byte_size,
            object_version_id=version,
        )

    def put_verified_zip(
        self,
        archive: AdmittedWebZip,
        *,
        firm_id: str,
        matter_id: str,
    ) -> StoredWebMaterialArchive:
        """Store an admitted ZIP as an immutable pending-processing object.

        Child PDFs are not silently registered here.  A dedicated worker must
        later materialize this exact object, rescan each child and create the
        ordinary PDF evidence records under its own recoverable saga.
        """

        _validate_uuid("firm_id", firm_id)
        _validate_uuid("matter_id", matter_id)
        _validate_archive(archive)
        _verify_archive_bytes(archive)
        object_key = _archive_object_key(firm_id=firm_id, matter_id=matter_id, content_sha256=archive.content_sha256)
        checksum = base64.b64encode(bytes.fromhex(archive.content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": archive.byte_size,
            "ContentType": "application/zip",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {
                "lawcase-archive-sha256": archive.content_sha256,
                "lawcase-archive-bytes": str(archive.byte_size),
                "lawcase-archive-entries": str(len(archive.entries)),
                "lawcase-archive-expanded-bytes": str(archive.expanded_byte_size),
            },
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            with archive.path.open("rb") as source:
                response = self._client.put_object(Body=source, **request)
            _verify_archive_bytes(archive)
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            _validate_remote_archive(head, archive=archive, expected_checksum=checksum)
        except WebObjectStoreBlocked:
            self._best_effort_delete(object_key)
            raise
        except Exception as error:
            self._best_effort_delete(object_key)
            raise WebObjectStoreBlocked("private material archive could not be stored or verified") from error
        version = response.get("VersionId") if isinstance(response, dict) else None
        if version is not None and (not isinstance(version, str) or not version):
            self._best_effort_delete(object_key)
            raise WebObjectStoreBlocked("private material archive object version is invalid")
        return StoredWebMaterialArchive(
            object_key=object_key,
            content_sha256=archive.content_sha256,
            byte_size=archive.byte_size,
            entry_count=len(archive.entries),
            expanded_byte_size=archive.expanded_byte_size,
            object_version_id=version,
        )

    def put_verified_office_artifact(
        self,
        content: bytes,
        *,
        content_sha256: str,
        media_type: str,
    ) -> StoredWebOfficeArtifact:
        """Store one generated DOCX/XLSX artifact under a private hash key.

        This endpoint accepts bytes produced by the server document worker,
        never a browser path or filename.  The ledger receives only the
        content-addressed suffix; the real bucket key remains server-only.
        """
        if media_type not in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            raise WebObjectStoreBlocked("unsupported Office artifact media type")
        if not isinstance(content, bytes) or not content or len(content) > 64 * 1024 * 1024:
            raise WebObjectStoreBlocked("Office artifact size is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256) or sha256(content).hexdigest() != content_sha256:
            raise WebObjectStoreBlocked("Office artifact hash is invalid")
        if not content.startswith(b"PK"):
            raise WebObjectStoreBlocked("Office artifact is not a ZIP container")
        ledger_key = f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca"
        object_key = f"reviewable-office/v1/{content_sha256[:2]}/{content_sha256}.lca"
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": len(content),
            "ContentType": media_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {"lawcase-office-sha256": content_sha256, "lawcase-office-media-type": media_type},
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            self._client.put_object(Body=content, **request)
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            _validate_remote_office(head, content=content, content_sha256=content_sha256, media_type=media_type)
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked("private Office artifact could not be stored or verified") from error
        return StoredWebOfficeArtifact(ledger_key, content_sha256, len(content), media_type)

    def put_verified_review_pdf(self, content: bytes, *, content_sha256: str) -> StoredWebOfficeArtifact:
        """Store the rendered review PDF beside its editable pair."""
        if not isinstance(content, bytes) or not content or not content.startswith(b"%PDF-") or len(content) > 128 * 1024 * 1024:
            raise WebObjectStoreBlocked("review PDF content is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256) or sha256(content).hexdigest() != content_sha256:
            raise WebObjectStoreBlocked("review PDF hash is invalid")
        ledger_key = f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca"
        object_key = f"reviewable-office/v1/{content_sha256[:2]}/{content_sha256}.lca"
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket, "Key": object_key, "ContentLength": len(content),
            "ContentType": "application/pdf", "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": checksum,
            "Metadata": {"lawcase-review-pdf-sha256": content_sha256},
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            self._client.put_object(Body=content, **request)
            _validate_remote_review_pdf(self._client.head_object(Bucket=self._config.bucket, Key=object_key), content, content_sha256)
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked("private review PDF could not be stored or verified") from error
        return StoredWebOfficeArtifact(ledger_key, content_sha256, len(content), "application/pdf")

    def put_case_agent_review_candidate(
        self,
        content: bytes,
        *,
        firm_id: str,
        matter_id: str,
        artifact_id: str,
        content_sha256: str,
    ) -> StoredCaseAgentReviewCandidate:
        """Persist canonical review-only JSON under an immutable case key.

        The caller supplies bytes, not a path.  This method deliberately does
        not expose a download URL and does not promote the candidate into any
        formal case ledger.
        """

        _validate_uuid("firm_id", firm_id)
        _validate_uuid("matter_id", matter_id)
        _validate_uuid("artifact_id", artifact_id)
        if (
            not isinstance(content, bytes)
            or not 2 <= len(content) <= _CASE_AGENT_CANDIDATE_MAX_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
            or sha256(content).hexdigest() != content_sha256
        ):
            raise WebObjectStoreBlocked("case-Agent candidate integrity is invalid")
        try:
            decoded = content.decode("utf-8")
            value = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_object)
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise WebObjectStoreBlocked("case-Agent candidate JSON is invalid") from error
        if not isinstance(value, dict) or canonical != content:
            raise WebObjectStoreBlocked("case-Agent candidate must be a canonical JSON object")

        object_key = (
            f"case-agent-candidates/v1/{firm_id}/{matter_id}/"
            f"{artifact_id}/{content_sha256}.json"
        )
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": len(content),
            "ContentType": "application/json",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {
                "lawcase-candidate-sha256": content_sha256,
                "lawcase-candidate-bytes": str(len(content)),
                "lawcase-candidate-artifact-id": artifact_id,
                "lawcase-candidate-review-status": "NEEDS_LAWYER_REVIEW",
            },
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            response = self._client.put_object(Body=io.BytesIO(content), **request)
            version = response.get("VersionId") if isinstance(response, dict) else None
            if version is not None and (not isinstance(version, str) or not version):
                raise WebObjectStoreBlocked("case-Agent candidate object version is invalid")
            head_request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": object_key,
            }
            if version is not None:
                head_request["VersionId"] = version
            _validate_remote_case_agent_candidate(
                self._client.head_object(**head_request),
                artifact_id=artifact_id,
                byte_size=len(content),
                content_sha256=content_sha256,
                expected_checksum=checksum,
            )
        except WebObjectStoreBlocked:
            # The key is content addressed and may already be referenced by a
            # concurrent idempotent write.  Never delete it from this path.
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent candidate could not be stored or verified"
            ) from error
        return StoredCaseAgentReviewCandidate(
            object_key=object_key,
            content_sha256=content_sha256,
            byte_size=len(content),
            object_version_id=version,
        )

    def put_reviewable_document_object(
        self,
        content: bytes,
        *,
        firm_id: str,
        matter_id: str,
        package_id: str,
        object_role: str,
        content_sha256: str,
        media_type: str,
    ) -> StoredReviewableDocumentPackageObject:
        """Bridge the shared private store to the exact document-package key.

        The caller is a server Worker and supplies an already generated byte
        object, never a browser filename/path.  Tenant, matter, package, role,
        digest, media type and remote checksum are all rechecked here.
        """

        for label, value in (
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("package_id", package_id),
        ):
            _validate_uuid(label, value)
        role_media = {
            "candidate": {"application/json"},
            "editable": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            "pdf-preview": {"application/pdf"},
        }
        role_limit = {
            "candidate": 4 * 1024 * 1024,
            "editable": 64 * 1024 * 1024,
            "pdf-preview": 128 * 1024 * 1024,
        }
        if (
            object_role not in role_media
            or media_type not in role_media[object_role]
            or not isinstance(content, bytes)
            or not 1 <= len(content) <= role_limit[object_role]
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or sha256(content).hexdigest() != content_sha256
            or (object_role == "candidate" and not content.startswith(b"{"))
            or (object_role == "editable" and not content.startswith(b"PK"))
            or (object_role == "pdf-preview" and not content.startswith(b"%PDF-"))
        ):
            raise WebObjectStoreBlocked(
                "reviewable document package object is invalid"
            )
        object_key = (
            f"case-agent-document-packages/v1/{firm_id}/{matter_id}/"
            f"{package_id}/{object_role}/{content_sha256}.lca"
        )
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        metadata = {
            "lawcase-document-package-id": package_id,
            "lawcase-document-role": object_role,
            "lawcase-document-sha256": content_sha256,
            "lawcase-document-bytes": str(len(content)),
            "lawcase-document-media-type": media_type,
        }
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "Body": io.BytesIO(content),
            "ContentLength": len(content),
            "ContentType": media_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": metadata,
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            response = self._client.put_object(**request)
            version = response.get("VersionId") if isinstance(response, dict) else None
            if version is not None and (
                not isinstance(version, str) or not version.strip() or len(version) > 512
            ):
                raise WebObjectStoreBlocked(
                    "reviewable document package object version is invalid"
                )
            head_request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": object_key,
            }
            if version is not None:
                head_request["VersionId"] = version
            _validate_remote_reviewable_document_package_object(
                self._client.head_object(**head_request),
                expected_size=len(content),
                expected_checksum=checksum,
                expected_media_type=media_type,
                expected_metadata=metadata,
                expected_encryption=self._config.server_side_encryption,
            )
        except WebObjectStoreBlocked:
            # This key is deterministic and may already be referenced by a
            # concurrent idempotent attempt; never delete it from this bridge.
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "reviewable document package object could not be stored or verified"
            ) from error
        return StoredReviewableDocumentPackageObject(
            object_key=object_key,
            content_sha256=content_sha256,
            byte_size=len(content),
            media_type=media_type,
            object_version_id=version,
        )

    def read_reviewable_document_object(
        self, receipt: Any
    ) -> bytes:
        """Authenticate and read one server-authorized package member."""

        key = getattr(receipt, "object_key", None)
        digest = getattr(receipt, "content_sha256", None)
        byte_size = getattr(receipt, "byte_size", None)
        media_type = getattr(receipt, "media_type", None)
        version = getattr(receipt, "object_version_id", None)
        parts = key.split("/") if isinstance(key, str) else ()
        role_media = {
            "candidate": {"application/json"},
            "editable": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            "pdf-preview": {"application/pdf"},
        }
        role_limit = {
            "candidate": 4 * 1024 * 1024,
            "editable": 64 * 1024 * 1024,
            "pdf-preview": 128 * 1024 * 1024,
        }
        if (
            len(parts) != 7
            or parts[:2] != ["case-agent-document-packages", "v1"]
            or not _is_uuid_value(parts[2])
            or not _is_uuid_value(parts[3])
            or not _is_uuid_value(parts[4])
            or parts[5] not in role_media
            or media_type not in role_media[parts[5]]
            or re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None
            or parts[6] != f"{digest}.lca"
            or type(byte_size) is not int
            or not 1 <= byte_size <= role_limit.get(parts[5], 0)
            or (version is not None and (
                not isinstance(version, str)
                or not version.strip()
                or len(version) > 512
            ))
        ):
            raise WebObjectStoreBlocked(
                "reviewable document package receipt is invalid"
            )
        metadata = {
            "lawcase-document-package-id": parts[4],
            "lawcase-document-role": parts[5],
            "lawcase-document-sha256": str(digest),
            "lawcase-document-bytes": str(byte_size),
            "lawcase-document-media-type": str(media_type),
        }
        checksum = base64.b64encode(bytes.fromhex(str(digest))).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
        }
        if version is not None:
            request["VersionId"] = version
        try:
            _validate_remote_reviewable_document_package_object(
                self._client.head_object(**request),
                expected_size=byte_size,
                expected_checksum=checksum,
                expected_media_type=str(media_type),
                expected_metadata=metadata,
                expected_encryption=self._config.server_side_encryption,
            )
            response = self._client.get_object(**request)
            body = response.get("Body") if isinstance(response, dict) else None
            if callable(getattr(body, "read", None)):
                content = body.read(byte_size + 1)
            elif isinstance(body, (bytes, bytearray)):
                content = bytes(body)
            else:
                raise WebObjectStoreBlocked(
                    "reviewable document package body is unavailable"
                )
            if (
                not isinstance(content, bytes)
                or len(content) != byte_size
                or sha256(content).hexdigest() != digest
            ):
                raise WebObjectStoreBlocked(
                    "reviewable document package bytes differ from their receipt"
                )
            _validate_remote_reviewable_document_package_object(
                self._client.head_object(**request),
                expected_size=byte_size,
                expected_checksum=checksum,
                expected_media_type=str(media_type),
                expected_metadata=metadata,
                expected_encryption=self._config.server_side_encryption,
            )
            return content
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "reviewable document package object could not be authenticated"
            ) from error

    def put_case_agent_research_response(
        self,
        response_body: bytes,
        *,
        receipt_payload: dict[str, object],
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> StoredCaseAgentResearchResponse:
        """Archive exact untrusted provider bytes plus their exact egress receipt.

        The content-addressed receipt envelope is private and deterministic for
        one authorized request.  It exists so a crash after S3 durability but
        before the PostgreSQL outcome can be recovered without another network
        call.  Neither the object key nor the provider body is browser-visible.
        """

        for label, value in (
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("external_request_id", external_request_id),
        ):
            _validate_uuid(label, value)
        if (
            not isinstance(request_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
            or not isinstance(response_body, bytes)
            or not 2 <= len(response_body) <= _CASE_AGENT_RESEARCH_RESPONSE_MAX_BYTES
            or not isinstance(receipt_payload, dict)
        ):
            raise WebObjectStoreBlocked("case-Agent research archive input is invalid")
        response_hash = sha256(response_body).hexdigest()
        try:
            envelope = json.dumps(
                {
                    "schema_version": "case-agent-public-research-archive-v1",
                    "external_request_id": external_request_id,
                    "request_hash": request_hash,
                    "response_sha256": response_hash,
                    "response_bytes": len(response_body),
                    "response_base64": base64.b64encode(response_body).decode("ascii"),
                    "egress_receipt": receipt_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WebObjectStoreBlocked(
                "case-Agent research receipt is not canonical JSON"
            ) from error
        if not 2 <= len(envelope) <= _CASE_AGENT_RESEARCH_ARCHIVE_MAX_BYTES:
            raise WebObjectStoreBlocked("case-Agent research archive is oversized")
        archive_hash = sha256(envelope).hexdigest()
        object_key = (
            f"case-agent-research/v1/{firm_id}/{matter_id}/"
            f"{external_request_id}/{request_hash}.json"
        )
        checksum = base64.b64encode(bytes.fromhex(archive_hash)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": len(envelope),
            "ContentType": "application/json",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {
                "lawcase-research-request-id": external_request_id,
                "lawcase-research-request-hash": request_hash,
                "lawcase-research-response-sha256": response_hash,
                "lawcase-research-response-bytes": str(len(response_body)),
                "lawcase-research-archive-sha256": archive_hash,
                "lawcase-research-archive-bytes": str(len(envelope)),
            },
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            response = self._client.put_object(Body=io.BytesIO(envelope), **request)
            version = response.get("VersionId") if isinstance(response, dict) else None
            if version is not None and (not isinstance(version, str) or not version):
                raise WebObjectStoreBlocked("case-Agent research object version is invalid")
            head_request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": object_key,
            }
            if version is not None:
                head_request["VersionId"] = version
            _validate_remote_case_agent_research_archive(
                self._client.head_object(**head_request),
                external_request_id=external_request_id,
                request_hash=request_hash,
                response_sha256=response_hash,
                response_bytes=len(response_body),
                archive_sha256=archive_hash,
                archive_bytes=len(envelope),
                expected_checksum=checksum,
            )
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent research response could not be stored or verified"
            ) from error
        return StoredCaseAgentResearchResponse(
            object_key=object_key,
            request_hash=request_hash,
            response_sha256=response_hash,
            response_bytes=len(response_body),
            archive_sha256=archive_hash,
            archive_bytes=len(envelope),
            object_version_id=version,
        )

    def read_case_agent_research_response(
        self,
        stored: StoredCaseAgentResearchResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> tuple[bytes, dict[str, object]]:
        """Re-read and authenticate one exact research archive for recovery."""

        _validate_stored_case_agent_research_response(
            stored,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=external_request_id,
        )
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            _validate_remote_case_agent_research_archive(
                self._client.head_object(**request),
                external_request_id=external_request_id,
                request_hash=stored.request_hash,
                response_sha256=stored.response_sha256,
                response_bytes=stored.response_bytes,
                archive_sha256=stored.archive_sha256,
                archive_bytes=stored.archive_bytes,
                expected_checksum=base64.b64encode(
                    bytes.fromhex(stored.archive_sha256)
                ).decode("ascii"),
            )
            value = self._client.get_object(**request)
            body = value.get("Body") if isinstance(value, dict) else None
            if hasattr(body, "read"):
                archive = body.read(_CASE_AGENT_RESEARCH_ARCHIVE_MAX_BYTES + 1)
            elif isinstance(body, (bytes, bytearray)):
                archive = bytes(body)
            else:
                raise WebObjectStoreBlocked("case-Agent research archive body is unavailable")
            if (
                not isinstance(archive, bytes)
                or len(archive) != stored.archive_bytes
                or sha256(archive).hexdigest() != stored.archive_sha256
            ):
                raise WebObjectStoreBlocked("case-Agent research archive bytes differ")
            payload = json.loads(
                archive.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_object,
            )
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if not isinstance(payload, dict) or canonical != archive:
                raise WebObjectStoreBlocked("case-Agent research archive is not canonical")
            expected_keys = {
                "schema_version", "external_request_id", "request_hash",
                "response_sha256", "response_bytes", "response_base64",
                "egress_receipt",
            }
            if set(payload) != expected_keys or (
                payload["schema_version"] != "case-agent-public-research-archive-v1"
                or payload["external_request_id"] != external_request_id
                or payload["request_hash"] != stored.request_hash
                or payload["response_sha256"] != stored.response_sha256
                or payload["response_bytes"] != stored.response_bytes
                or not isinstance(payload["egress_receipt"], dict)
            ):
                raise WebObjectStoreBlocked("case-Agent research archive binding differs")
            response_body = base64.b64decode(
                payload["response_base64"], validate=True
            )
            if (
                len(response_body) != stored.response_bytes
                or sha256(response_body).hexdigest() != stored.response_sha256
            ):
                raise WebObjectStoreBlocked("case-Agent research response bytes differ")
            _validate_remote_case_agent_research_archive(
                self._client.head_object(**request),
                external_request_id=external_request_id,
                request_hash=stored.request_hash,
                response_sha256=stored.response_sha256,
                response_bytes=stored.response_bytes,
                archive_sha256=stored.archive_sha256,
                archive_bytes=stored.archive_bytes,
                expected_checksum=base64.b64encode(
                    bytes.fromhex(stored.archive_sha256)
                ).decode("ascii"),
            )
            return response_body, payload["egress_receipt"]
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent research response could not be authenticated"
            ) from error

    def recover_case_agent_research_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> tuple[StoredCaseAgentResearchResponse, bytes, dict[str, object]]:
        """Recover a deterministic archive after an indeterminate DB commit.

        This method never creates or sends anything.  It can only discover the
        one key implied by the already-durable request identity, authenticate
        its metadata/bytes, and return it to the reconciliation path.
        """

        for label, value in (
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("external_request_id", external_request_id),
        ):
            _validate_uuid(label, value)
        if not isinstance(request_hash, str) or re.fullmatch(
            r"[0-9a-f]{64}", request_hash
        ) is None:
            raise WebObjectStoreBlocked("case-Agent research request hash is invalid")
        object_key = (
            f"case-agent-research/v1/{firm_id}/{matter_id}/"
            f"{external_request_id}/{request_hash}.json"
        )
        try:
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            if not isinstance(head, dict):
                raise WebObjectStoreBlocked("case-Agent research archive metadata is unavailable")
            metadata = head.get("Metadata")
            if not isinstance(metadata, dict):
                raise WebObjectStoreBlocked("case-Agent research archive metadata is unavailable")
            normalized = {str(key).lower(): value for key, value in metadata.items()}
            version = head.get("VersionId")
            if version is not None and (
                not isinstance(version, str) or not version.strip()
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent research archive version is invalid"
                )
            stored = StoredCaseAgentResearchResponse(
                object_key=object_key,
                request_hash=request_hash,
                response_sha256=str(normalized.get("lawcase-research-response-sha256", "")),
                response_bytes=int(normalized.get("lawcase-research-response-bytes", "0")),
                archive_sha256=str(normalized.get("lawcase-research-archive-sha256", "")),
                archive_bytes=int(normalized.get("lawcase-research-archive-bytes", "0")),
                # S3-compatible HEAD may omit VersionId.  If present, bind all
                # subsequent reads to it; otherwise the deterministic key plus
                # two checksum/metadata fences is the recovery identity.
                object_version_id=version,
            )
            body, receipt = self.read_case_agent_research_response(
                stored,
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=external_request_id,
            )
            return stored, body, receipt
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent research archive is not recoverable"
            ) from error

    def put_case_agent_lawyer_analysis_response(
        self,
        response_body: bytes,
        *,
        receipt_payload: dict[str, object],
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> StoredCaseAgentLawyerAnalysisResponse:
        """Archive one exact lawyer-analysis response before Worker success.

        The deterministic, private object is the recovery fence for an
        indeterminate database commit. Recovery may read this key, but no code
        on that path is allowed to submit the provider request again.
        """

        for label, value in (
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("external_request_id", external_request_id),
        ):
            _validate_uuid(label, value)
        if (
            not isinstance(request_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
            or not isinstance(response_body, bytes)
            or not 2
            <= len(response_body)
            <= _CASE_AGENT_LAWYER_ANALYSIS_RESPONSE_MAX_BYTES
            or not isinstance(receipt_payload, dict)
        ):
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis archive input is invalid"
            )
        response_hash = sha256(response_body).hexdigest()
        try:
            envelope = json.dumps(
                {
                    "schema_version": (
                        "case-agent-lawyer-analysis-response-archive-v1"
                    ),
                    "external_request_id": external_request_id,
                    "request_hash": request_hash,
                    "response_sha256": response_hash,
                    "response_bytes": len(response_body),
                    "response_base64": base64.b64encode(response_body).decode(
                        "ascii"
                    ),
                    "transport_receipt": receipt_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis receipt is not canonical JSON"
            ) from error
        if not 2 <= len(envelope) <= _CASE_AGENT_LAWYER_ANALYSIS_ARCHIVE_MAX_BYTES:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis archive is oversized"
            )
        archive_hash = sha256(envelope).hexdigest()
        object_key = (
            f"case-agent-lawyer-analysis/v1/{firm_id}/{matter_id}/"
            f"{external_request_id}/{request_hash}.json"
        )
        checksum = base64.b64encode(bytes.fromhex(archive_hash)).decode("ascii")
        metadata = {
            "lawcase-lawyer-request-id": external_request_id,
            "lawcase-lawyer-request-hash": request_hash,
            "lawcase-lawyer-response-sha256": response_hash,
            "lawcase-lawyer-response-bytes": str(len(response_body)),
            "lawcase-lawyer-archive-sha256": archive_hash,
            "lawcase-lawyer-archive-bytes": str(len(envelope)),
        }
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": len(envelope),
            "ContentType": "application/json",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": metadata,
            "ServerSideEncryption": self._config.server_side_encryption,
            "IfNoneMatch": "*",
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            response = self._client.put_object(Body=io.BytesIO(envelope), **request)
            version = response.get("VersionId") if isinstance(response, dict) else None
            if version is not None and (
                not isinstance(version, str) or not version.strip()
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis object version is invalid"
                )
            head_request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": object_key,
            }
            if version is not None:
                head_request["VersionId"] = version
            _validate_remote_case_agent_lawyer_analysis_archive(
                self._client.head_object(**head_request),
                external_request_id=external_request_id,
                request_hash=request_hash,
                response_sha256=response_hash,
                response_bytes=len(response_body),
                archive_sha256=archive_hash,
                archive_bytes=len(envelope),
                expected_checksum=checksum,
                expected_encryption=self._config.server_side_encryption,
            )
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis response could not be stored or verified"
            ) from error
        return StoredCaseAgentLawyerAnalysisResponse(
            object_key=object_key,
            request_hash=request_hash,
            response_sha256=response_hash,
            response_bytes=len(response_body),
            archive_sha256=archive_hash,
            archive_bytes=len(envelope),
            object_version_id=version,
        )

    def read_case_agent_lawyer_analysis_response(
        self,
        stored: StoredCaseAgentLawyerAnalysisResponse,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
    ) -> tuple[bytes, dict[str, object]]:
        """Authenticate and read one exact private lawyer-analysis archive."""

        _validate_stored_case_agent_lawyer_analysis_response(
            stored,
            firm_id=firm_id,
            matter_id=matter_id,
            external_request_id=external_request_id,
        )
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        checksum = base64.b64encode(
            bytes.fromhex(stored.archive_sha256)
        ).decode("ascii")
        try:
            _validate_remote_case_agent_lawyer_analysis_archive(
                self._client.head_object(**request),
                external_request_id=external_request_id,
                request_hash=stored.request_hash,
                response_sha256=stored.response_sha256,
                response_bytes=stored.response_bytes,
                archive_sha256=stored.archive_sha256,
                archive_bytes=stored.archive_bytes,
                expected_checksum=checksum,
                expected_encryption=self._config.server_side_encryption,
            )
            value = self._client.get_object(**request)
            body = value.get("Body") if isinstance(value, dict) else None
            if callable(getattr(body, "read", None)):
                archive = body.read(_CASE_AGENT_LAWYER_ANALYSIS_ARCHIVE_MAX_BYTES + 1)
            elif isinstance(body, (bytes, bytearray)):
                archive = bytes(body)
            else:
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis archive body is unavailable"
                )
            if (
                not isinstance(archive, bytes)
                or len(archive) != stored.archive_bytes
                or sha256(archive).hexdigest() != stored.archive_sha256
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis archive bytes differ"
                )
            payload = json.loads(
                archive.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_object,
            )
            if (
                not isinstance(payload, dict)
                or json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                != archive
                or set(payload)
                != {
                    "schema_version",
                    "external_request_id",
                    "request_hash",
                    "response_sha256",
                    "response_bytes",
                    "response_base64",
                    "transport_receipt",
                }
                or payload["schema_version"]
                != "case-agent-lawyer-analysis-response-archive-v1"
                or payload["external_request_id"] != external_request_id
                or payload["request_hash"] != stored.request_hash
                or payload["response_sha256"] != stored.response_sha256
                or payload["response_bytes"] != stored.response_bytes
                or not isinstance(payload["transport_receipt"], dict)
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis archive binding differs"
                )
            try:
                response_body = base64.b64decode(
                    payload["response_base64"], validate=True
                )
            except (TypeError, ValueError) as error:
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis response encoding is invalid"
                ) from error
            if (
                len(response_body) != stored.response_bytes
                or sha256(response_body).hexdigest() != stored.response_sha256
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis response bytes differ"
                )
            _validate_remote_case_agent_lawyer_analysis_archive(
                self._client.head_object(**request),
                external_request_id=external_request_id,
                request_hash=stored.request_hash,
                response_sha256=stored.response_sha256,
                response_bytes=stored.response_bytes,
                archive_sha256=stored.archive_sha256,
                archive_bytes=stored.archive_bytes,
                expected_checksum=checksum,
                expected_encryption=self._config.server_side_encryption,
            )
            return response_body, payload["transport_receipt"]
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis response could not be authenticated"
            ) from error

    def recover_case_agent_lawyer_analysis_response(
        self,
        *,
        firm_id: str,
        matter_id: str,
        external_request_id: str,
        request_hash: str,
    ) -> tuple[
        StoredCaseAgentLawyerAnalysisResponse, bytes, dict[str, object]
    ] | None:
        """Look up the deterministic archive; never sends or creates data."""

        for label, value in (
            ("firm_id", firm_id),
            ("matter_id", matter_id),
            ("external_request_id", external_request_id),
        ):
            _validate_uuid(label, value)
        if not isinstance(request_hash, str) or re.fullmatch(
            r"[0-9a-f]{64}", request_hash
        ) is None:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis request hash is invalid"
            )
        object_key = (
            f"case-agent-lawyer-analysis/v1/{firm_id}/{matter_id}/"
            f"{external_request_id}/{request_hash}.json"
        )
        try:
            head = self._client.head_object(
                Bucket=self._config.bucket, Key=object_key
            )
        except Exception as error:
            if _is_s3_object_missing(error):
                return None
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis archive lookup failed closed"
            ) from error
        try:
            if not isinstance(head, dict) or not isinstance(
                head.get("Metadata"), dict
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent lawyer-analysis archive metadata is unavailable"
                )
            normalized = {
                str(key).lower(): value
                for key, value in head["Metadata"].items()
            }
            version = head.get("VersionId")
            stored = StoredCaseAgentLawyerAnalysisResponse(
                object_key=object_key,
                request_hash=request_hash,
                response_sha256=str(
                    normalized.get("lawcase-lawyer-response-sha256", "")
                ),
                response_bytes=int(
                    normalized.get("lawcase-lawyer-response-bytes", "0")
                ),
                archive_sha256=str(
                    normalized.get("lawcase-lawyer-archive-sha256", "")
                ),
                archive_bytes=int(
                    normalized.get("lawcase-lawyer-archive-bytes", "0")
                ),
                object_version_id=version,
            )
            body, receipt = self.read_case_agent_lawyer_analysis_response(
                stored,
                firm_id=firm_id,
                matter_id=matter_id,
                external_request_id=external_request_id,
            )
            return stored, body, receipt
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent lawyer-analysis archive is not recoverable"
            ) from error

    def materialize_case_agent_material(
        self,
        stored: StoredCaseAgentMaterial,
        *,
        destination: str | Path,
    ) -> Path:
        """Materialize one exact scanner-admitted DOCX/XLSX for a Worker."""

        _validate_case_agent_material(stored)
        suffix = ".docx" if stored.admitted_format == "DOCX" else ".xlsx"
        target = _prepare_private_materialization_destination(destination, suffix=suffix)
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            checksum = base64.b64encode(bytes.fromhex(stored.content_sha256)).decode("ascii")
            _validate_remote_case_agent_material(
                self._client.head_object(**request), stored=stored, expected_checksum=checksum
            )
            _stream_verified_object_to_private_file(
                self._client.get_object(**request),
                target=target,
                expected_byte_size=stored.byte_size,
                expected_sha256=stored.content_sha256,
                label="case-Agent material",
            )
            _validate_remote_case_agent_material(
                self._client.head_object(**request), stored=stored, expected_checksum=checksum
            )
        except WebObjectStoreBlocked:
            _remove_private_file(target)
            raise
        except Exception as error:
            _remove_private_file(target)
            raise WebObjectStoreBlocked(
                "case-Agent material could not be materialized safely"
            ) from error
        return target

    def verify_case_agent_review_candidate(
        self, stored: StoredCaseAgentReviewCandidate, *, artifact_id: str
    ) -> None:
        """Fail closed unless an existing staged candidate is still intact."""

        _validate_uuid("artifact_id", artifact_id)
        if not isinstance(stored, StoredCaseAgentReviewCandidate):
            raise WebObjectStoreBlocked("case-Agent candidate locator is invalid")
        expected_suffix = f"/{artifact_id}/{stored.content_sha256}.json"
        if (
            not re.fullmatch(r"[0-9a-f]{64}", stored.content_sha256)
            or not 2 <= stored.byte_size <= _CASE_AGENT_CANDIDATE_MAX_BYTES
            or not stored.object_key.startswith("case-agent-candidates/v1/")
            or not stored.object_key.endswith(expected_suffix)
        ):
            raise WebObjectStoreBlocked("case-Agent candidate locator is not hash-bound")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            _validate_remote_case_agent_candidate(
                self._client.head_object(**request),
                artifact_id=artifact_id,
                byte_size=stored.byte_size,
                content_sha256=stored.content_sha256,
                expected_checksum=base64.b64encode(
                    bytes.fromhex(stored.content_sha256)
                ).decode("ascii"),
            )
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent candidate could not be authenticated"
            ) from error

    def read_case_agent_review_candidate(
        self, stored: StoredCaseAgentReviewCandidate, *, artifact_id: str
    ) -> bytes:
        """Re-read one exact review candidate for independent verification.

        This is a server-only object operation.  The locator is supplied by an
        independently authorised PostgreSQL projection, never by a browser or
        by the execution adapter's in-memory result.
        """

        self.verify_case_agent_review_candidate(stored, artifact_id=artifact_id)
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
        }
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            response = self._client.get_object(**request)
            body = response.get("Body") if isinstance(response, dict) else None
            if hasattr(body, "read"):
                content = body.read(_CASE_AGENT_CANDIDATE_MAX_BYTES + 1)
            elif isinstance(body, (bytes, bytearray)):
                content = bytes(body)
            else:
                raise WebObjectStoreBlocked("case-Agent candidate body is unavailable")
            if (
                not isinstance(content, bytes)
                or len(content) != stored.byte_size
                or len(content) > _CASE_AGENT_CANDIDATE_MAX_BYTES
                or sha256(content).hexdigest() != stored.content_sha256
            ):
                raise WebObjectStoreBlocked(
                    "case-Agent candidate bytes differ from their receipt"
                )
            # Detect metadata/version changes around GET as well as corrupt
            # bytes.  The canonical JSON verifier performs a separate format
            # check after this storage-level authentication.
            self.verify_case_agent_review_candidate(stored, artifact_id=artifact_id)
            return content
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "case-Agent candidate could not be independently read"
            ) from error

    def read_verified_office_artifact(self, object_key: str, expected_sha256: str) -> bytes:
        """Read one hash-bound Office artifact for a server worker only."""
        if object_key != f"{expected_sha256[:2]}/{expected_sha256[2:4]}/{expected_sha256}.lca":
            raise WebObjectStoreBlocked("Office artifact locator is not hash-bound")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise WebObjectStoreBlocked("Office artifact hash is invalid")
        return self.read_verified_review_artifact(object_key, expected_sha256)

    def read_verified_review_artifact(self, object_key: str, expected_sha256: str) -> bytes:
        if object_key != f"{expected_sha256[:2]}/{expected_sha256[2:4]}/{expected_sha256}.lca" or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise WebObjectStoreBlocked("review artifact locator is not hash-bound")
        object_key = f"reviewable-office/v1/{expected_sha256[:2]}/{expected_sha256}.lca"
        try:
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            response = self._client.get_object(Bucket=self._config.bucket, Key=object_key)
            body = response.get("Body") if isinstance(response, dict) else None
            if hasattr(body, "read"):
                content = body.read(64 * 1024 * 1024 + 1)
            elif isinstance(body, (bytes, bytearray)):
                content = bytes(body)
            else:
                raise WebObjectStoreBlocked("Office artifact body is unavailable")
            if not isinstance(content, bytes) or len(content) > 64 * 1024 * 1024:
                raise WebObjectStoreBlocked("Office artifact body is oversized")
            if sha256(content).hexdigest() != expected_sha256:
                raise WebObjectStoreBlocked("review artifact content hash differs")
            metadata = head.get("Metadata") if isinstance(head, dict) else None
            if not isinstance(metadata, dict):
                raise WebObjectStoreBlocked("review artifact metadata is unavailable")
            return content
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked("private Office artifact could not be authenticated") from error

    def delete_unbound_upload_archive(self, stored: StoredWebMaterialArchive) -> None:
        """Delete an archive only before it has been bound to an operation."""

        _validate_stored_archive(stored)
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": stored.object_key}
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            self._client.delete_object(**request)
        except Exception as error:
            raise WebObjectStoreBlocked("unbound private material archive could not be removed") from error

    def materialize_verified_zip(
        self,
        stored: StoredWebMaterialArchive,
        *,
        destination: str | Path,
    ) -> Path:
        """Materialize one exact archive for a server-only child worker."""

        _validate_stored_archive(stored)
        target = _prepare_private_materialization_destination(destination, suffix=".zip")
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": stored.object_key}
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            checksum = base64.b64encode(bytes.fromhex(stored.content_sha256)).decode("ascii")
            _validate_remote_stored_archive(self._client.head_object(**request), stored=stored, expected_checksum=checksum)
            _stream_verified_object_to_private_file(
                self._client.get_object(**request),
                target=target,
                expected_byte_size=stored.byte_size,
                expected_sha256=stored.content_sha256,
                label="private material archive",
            )
            _validate_remote_stored_archive(self._client.head_object(**request), stored=stored, expected_checksum=checksum)
        except WebObjectStoreBlocked:
            _remove_private_file(target)
            raise
        except Exception as error:
            _remove_private_file(target)
            raise WebObjectStoreBlocked("private material archive could not be materialized safely") from error
        return target

    def delete_unbound_upload_object(self, stored: StoredWebEvidenceOriginal) -> None:
        """Delete a just-uploaded object when the later ledger transaction fails.

        This is intentionally not a general evidence deletion API.  Once an
        object is bound to an immutable evidence-original record, retention and
        supersession rules belong to a separate controlled workflow.
        """

        _validate_stored_object(stored)
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": stored.object_key}
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            self._client.delete_object(**request)
        except Exception as error:
            raise WebObjectStoreBlocked("unbound private evidence object could not be removed") from error

    def materialize_verified_pdf(
        self,
        stored: StoredWebEvidenceOriginal,
        *,
        destination: str | Path,
    ) -> Path:
        """Download one original into a private Worker file and rehash it.

        The returned path is a server-worker-only value.  Calling code must
        pass it directly into a materialized-source worker record and must not
        serialize it to a response, lineage file, audit payload, or browser.
        """

        _validate_stored_object(stored)
        target = _prepare_private_materialization_destination(destination)
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": stored.object_key}
        if stored.object_version_id is not None:
            request["VersionId"] = stored.object_version_id
        try:
            expected_checksum = base64.b64encode(bytes.fromhex(stored.content_sha256)).decode("ascii")
            _validate_remote_stored_object(
                self._client.head_object(**request),
                stored=stored,
                expected_checksum=expected_checksum,
            )
            response = self._client.get_object(**request)
            _stream_object_to_private_file(response, target=target, stored=stored)
            # Detect a version/metadata change between HEAD and GET.  The
            # downloaded bytes are separately rehashed before worker use.
            _validate_remote_stored_object(
                self._client.head_object(**request),
                stored=stored,
                expected_checksum=expected_checksum,
            )
        except WebObjectStoreBlocked:
            _remove_private_file(target)
            raise
        except Exception as error:
            _remove_private_file(target)
            raise WebObjectStoreBlocked("private evidence object could not be materialized safely") from error
        return target

    def read_verified_native_image(self, locator: Any) -> bytes:
        """Re-read one server-bound native evidence image without a URL/path.

        ``locator`` is intentionally duck-typed to avoid making the object
        store depend on the Agent adapter module.  Every locator field and
        remote metadata value is nevertheless checked before and after GET.
        """

        key = getattr(locator, "object_key", None)
        digest = getattr(locator, "content_sha256", None)
        firm_id = getattr(locator, "firm_id", None)
        matter_id = getattr(locator, "matter_id", None)
        evidence_file_id = getattr(locator, "evidence_file_id", None)
        byte_size = getattr(locator, "byte_size", None)
        media_type = getattr(locator, "media_type", None)
        version = getattr(locator, "object_version_id", None)
        reference_hash = getattr(locator, "source_reference_hash", None)
        parts = key.split("/") if isinstance(key, str) else ()
        if (
            not _is_uuid_value(firm_id)
            or not _is_uuid_value(matter_id)
            or not _is_uuid_value(evidence_file_id)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(byte_size) is not int
            or not 1 <= byte_size <= 64 * 1024 * 1024
            or media_type not in {"image/jpeg", "image/png"}
            or len(parts) != 7
            or parts[:2] != ["original-images", "v1"]
            or parts[2] != firm_id
            or parts[3] != matter_id
            or parts[4] != digest[:2]
            or parts[5] != digest
            or re.fullmatch(r"[0-9a-f-]{36}\.(jpg|png)", parts[6]) is None
            or (media_type == "image/jpeg" and not parts[6].endswith(".jpg"))
            or (media_type == "image/png" and not parts[6].endswith(".png"))
            or sha256(key.encode("utf-8")).hexdigest() != reference_hash
        ):
            raise WebObjectStoreBlocked("native evidence image locator is invalid")
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
        if version is not None:
            request["VersionId"] = version
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        try:
            first = self._client.head_object(**request)
            _validate_remote_native_image(
                first,
                byte_size=byte_size,
                content_sha256=digest,
                media_type=media_type,
                expected_checksum=checksum,
            )
            response = self._client.get_object(**request)
            body = response.get("Body") if isinstance(response, dict) else None
            if not callable(getattr(body, "read", None)):
                raise WebObjectStoreBlocked(
                    "native evidence image download response is invalid"
                )
            content = body.read(byte_size + 1)
            if (
                not isinstance(content, bytes)
                or len(content) != byte_size
                or sha256(content).hexdigest() != digest
                or body.read(1) not in {b"", None}
            ):
                raise WebObjectStoreBlocked("native evidence image bytes differ")
            _validate_remote_native_image(
                self._client.head_object(**request),
                byte_size=byte_size,
                content_sha256=digest,
                media_type=media_type,
                expected_checksum=checksum,
            )
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked(
                "native evidence image could not be read safely"
            ) from error
        return content

    def put_verified_derivative(
        self,
        source_path: str | Path,
        *,
        firm_id: str,
        matter_id: str,
        artifact_type: str,
        artifact_sha256: str,
        page_count: int,
    ) -> StoredWebEvidenceDerivative:
        """Store a worker-verified derivative under a content-addressed private key."""

        _validate_uuid("firm_id", firm_id)
        _validate_uuid("matter_id", matter_id)
        if artifact_type not in {"RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"}:
            raise WebObjectStoreBlocked("unsupported Web evidence derivative type")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) or page_count < 1:
            raise WebObjectStoreBlocked("Web evidence derivative integrity metadata is invalid")
        source = Path(source_path).expanduser().resolve(strict=True)
        if source.is_symlink() or not source.is_file() or source.stat().st_size < 1:
            raise WebObjectStoreBlocked("Web evidence derivative is unavailable")
        if _file_sha256(source) != artifact_sha256:
            raise WebObjectStoreBlocked("Web evidence derivative hash differs before storage")
        # The ledger deliberately stores only the managed content-addressed
        # suffix.  Tenant/matter scope stays in the private object-store key
        # and is reconstructed by the server-only derivative delivery path.
        ledger_object_key = f"{artifact_sha256[:2]}/{artifact_sha256[2:4]}/{artifact_sha256}.lca"
        object_key = f"derivatives/v1/{firm_id}/{matter_id}/{artifact_sha256[:2]}/{artifact_sha256}.lca"
        checksum = base64.b64encode(bytes.fromhex(artifact_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": object_key,
            "ContentLength": source.stat().st_size,
            "ContentType": "application/pdf",
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "Metadata": {
                "lawcase-derivative-sha256": artifact_sha256,
                "lawcase-derivative-type": artifact_type,
                "lawcase-derivative-pages": str(page_count),
            },
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        try:
            with source.open("rb") as stream:
                response = self._client.put_object(Body=stream, **request)
            head = self._client.head_object(Bucket=self._config.bucket, Key=object_key)
            _validate_remote_derivative(head, byte_size=source.stat().st_size, artifact_sha256=artifact_sha256, page_count=page_count)
        except WebObjectStoreBlocked:
            raise
        except Exception as error:
            raise WebObjectStoreBlocked("verified Web evidence derivative could not be stored") from error
        version = response.get("VersionId") if isinstance(response, dict) else None
        if version is not None and (not isinstance(version, str) or not version):
            raise WebObjectStoreBlocked("Web evidence derivative object version is invalid")
        return StoredWebEvidenceDerivative(
            object_key=ledger_object_key,
            artifact_sha256=artifact_sha256,
            page_count=page_count,
            object_version_id=version,
        )

    def materialize_verified_derivative(
        self,
        *,
        firm_id: str,
        matter_id: str,
        storage_object_key: str,
        artifact_sha256: str,
        page_count: int,
        destination: str | Path,
    ) -> Path:
        """Materialize one verified derivative for a server download response."""
        _validate_uuid("firm_id", firm_id)
        _validate_uuid("matter_id", matter_id)
        if not re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca", storage_object_key):
            raise WebObjectStoreBlocked("private derivative storage key is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) or storage_object_key != f"{artifact_sha256[:2]}/{artifact_sha256[2:4]}/{artifact_sha256}.lca":
            raise WebObjectStoreBlocked("private derivative hash binding is invalid")
        if type(page_count) is not int or page_count < 1:
            raise WebObjectStoreBlocked("private derivative page count is invalid")
        target = _prepare_private_materialization_destination(destination)
        object_key = f"derivatives/v1/{firm_id}/{matter_id}/{artifact_sha256[:2]}/{artifact_sha256}.lca"
        request = {"Bucket": self._config.bucket, "Key": object_key}
        try:
            head = self._client.head_object(**request)
            if not isinstance(head, dict) or type(head.get("ContentLength")) is not int:
                raise WebObjectStoreBlocked("private derivative metadata is unavailable")
            _validate_remote_derivative(
                head,
                byte_size=head["ContentLength"],
                artifact_sha256=artifact_sha256,
                page_count=page_count,
            )
            _stream_verified_object_to_private_file(
                self._client.get_object(**request),
                target=target,
                expected_byte_size=head["ContentLength"],
                expected_sha256=artifact_sha256,
                label="private derivative",
            )
            latest = self._client.head_object(**request)
            _validate_remote_derivative(
                latest,
                byte_size=target.stat().st_size,
                artifact_sha256=artifact_sha256,
                page_count=page_count,
            )
        except WebObjectStoreBlocked:
            _remove_private_file(target)
            raise
        except Exception as error:
            _remove_private_file(target)
            raise WebObjectStoreBlocked("private derivative could not be materialized safely") from error
        return target

    def _best_effort_delete(self, object_key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._config.bucket, Key=object_key)
        except Exception:
            # The original failure remains the caller-visible result.  A
            # deployment must monitor orphan cleanup separately; no browser
            # route can learn object keys from this path.
            pass


def _new_boto3_client(config: S3PrivateObjectStoreConfig) -> S3CompatibleClient:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:  # pragma: no cover - exercised in deployment composition.
        raise WebObjectStoreBlocked("the S3-compatible object-store client is not installed") from error
    try:
        return boto3.session.Session().client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region_name,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=120,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    except Exception as error:  # pragma: no cover - environment-specific client setup.
        raise WebObjectStoreBlocked("the S3-compatible object-store client could not be configured") from error


def _validate_endpoint(value: str, *, allow_insecure: bool) -> None:
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError("Web object-store endpoint is invalid")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Web object-store endpoint is invalid")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Web object-store endpoint is invalid")
    if parsed.scheme == "http" and not allow_insecure:
        raise ValueError("Web object-store endpoint must use HTTPS unless explicitly internal")


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise WebObjectStoreBlocked(f"Web object-store {label} is invalid") from error


def _is_uuid_value(value: object) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _validate_upload(upload: AdmittedWebPdfUpload) -> None:
    if not isinstance(upload, AdmittedWebPdfUpload):
        raise WebObjectStoreBlocked("Web evidence upload is invalid")
    if upload.media_type != "application/pdf" or upload.page_count < 1:
        raise WebObjectStoreBlocked("Web evidence upload is not an admitted PDF")
    if upload.byte_size < 1 or len(upload.content_sha256) != 64 or any(char not in "0123456789abcdef" for char in upload.content_sha256):
        raise WebObjectStoreBlocked("Web evidence upload integrity metadata is invalid")
    if len(upload.inspection_hash) != 64 or any(char not in "0123456789abcdef" for char in upload.inspection_hash):
        raise WebObjectStoreBlocked("Web evidence inspection metadata is invalid")


def _validate_stored_object(stored: StoredWebEvidenceOriginal) -> None:
    if not isinstance(stored, StoredWebEvidenceOriginal):
        raise WebObjectStoreBlocked("stored Web evidence object is invalid")
    if not isinstance(stored.byte_size, int) or stored.byte_size < 1:
        raise WebObjectStoreBlocked("stored Web evidence object is invalid")
    if len(stored.content_sha256) != 64 or any(char not in "0123456789abcdef" for char in stored.content_sha256):
        raise WebObjectStoreBlocked("stored Web evidence object is invalid")
    parts = stored.object_key.split("/")
    if (
        len(parts) != 7
        or parts[0:2] != ["originals", "v1"]
        or parts[4] != stored.content_sha256[:2]
        or parts[5] != stored.content_sha256
        or not parts[6].endswith(".pdf")
    ):
        raise WebObjectStoreBlocked("stored Web evidence object is invalid")
    try:
        UUID(parts[2])
        UUID(parts[3])
        UUID(parts[6][:-4])
    except (ValueError, TypeError) as error:
        raise WebObjectStoreBlocked("stored Web evidence object is invalid") from error
    if stored.object_version_id is not None and (
        not isinstance(stored.object_version_id, str) or not stored.object_version_id
    ):
        raise WebObjectStoreBlocked("stored Web evidence object is invalid")


def _validate_archive(archive: AdmittedWebZip) -> None:
    if not isinstance(archive, AdmittedWebZip) or archive.byte_size < 1 or not archive.entries:
        raise WebObjectStoreBlocked("Web material archive is invalid")
    if len(archive.content_sha256) != 64 or any(char not in "0123456789abcdef" for char in archive.content_sha256):
        raise WebObjectStoreBlocked("Web material archive integrity metadata is invalid")
    if archive.expanded_byte_size < 1 or any(
        not entry.name.casefold().endswith(".pdf") or entry.byte_size < 1 or len(entry.content_sha256) != 64
        for entry in archive.entries
    ):
        raise WebObjectStoreBlocked("Web material archive entry metadata is invalid")


def _validate_stored_archive(stored: StoredWebMaterialArchive) -> None:
    if not isinstance(stored, StoredWebMaterialArchive) or stored.byte_size < 1 or stored.entry_count < 1:
        raise WebObjectStoreBlocked("stored Web material archive is invalid")
    if len(stored.content_sha256) != 64 or any(char not in "0123456789abcdef" for char in stored.content_sha256):
        raise WebObjectStoreBlocked("stored Web material archive integrity metadata is invalid")
    parts = stored.object_key.split("/")
    if (
        len(parts) != 7
        or parts[:2] != ["material-archives", "v1"]
        or parts[4] != stored.content_sha256[:2]
        or parts[5] != stored.content_sha256
        or not parts[6].endswith(".zip")
    ):
        raise WebObjectStoreBlocked("stored Web material archive is invalid")
    try:
        UUID(parts[2])
        UUID(parts[3])
        UUID(parts[6][:-4])
    except (ValueError, TypeError) as error:
        raise WebObjectStoreBlocked("stored Web material archive is invalid") from error
    if stored.object_version_id is not None and (not isinstance(stored.object_version_id, str) or not stored.object_version_id):
        raise WebObjectStoreBlocked("stored Web material archive is invalid")


def _verify_archive_bytes(archive: AdmittedWebZip) -> None:
    _validate_archive(archive)
    if archive.path.is_symlink() or not archive.path.is_file() or archive.path.stat().st_size != archive.byte_size:
        raise WebObjectStoreBlocked("Web material archive changed before object-store handoff")
    digest = sha256()
    try:
        with archive.path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise WebObjectStoreBlocked("Web material archive is unavailable") from error
    if digest.hexdigest() != archive.content_sha256:
        raise WebObjectStoreBlocked("Web material archive changed before object-store handoff")


def _verify_upload_bytes(upload: AdmittedWebPdfUpload) -> None:
    if upload.path.is_symlink() or not upload.path.is_file() or upload.path.stat().st_size != upload.byte_size:
        raise WebObjectStoreBlocked("Web evidence upload changed before object-store handoff")
    digest = sha256()
    try:
        with upload.path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise WebObjectStoreBlocked("Web evidence upload is unavailable") from error
    if digest.hexdigest() != upload.content_sha256:
        raise WebObjectStoreBlocked("Web evidence upload changed before object-store handoff")


def _object_key(*, firm_id: str, matter_id: str, content_sha256: str) -> str:
    # No browser filename, local path, counterparty name or material title is
    # embedded in a storage key.  The UUID makes same-hash source instances
    # independently auditable without exposing their human-facing labels.
    return f"originals/v1/{firm_id}/{matter_id}/{content_sha256[:2]}/{content_sha256}/{uuid4()}.pdf"


def _archive_object_key(*, firm_id: str, matter_id: str, content_sha256: str) -> str:
    return f"material-archives/v1/{firm_id}/{matter_id}/{content_sha256[:2]}/{content_sha256}/{uuid4()}.zip"


def _validate_remote_object(
    head: Any,
    *,
    upload: AdmittedWebPdfUpload,
    expected_checksum: str,
) -> None:
    if not isinstance(head, dict):
        raise WebObjectStoreBlocked("private evidence object verification response is invalid")
    if head.get("ContentLength") != upload.byte_size:
        raise WebObjectStoreBlocked("private evidence object byte size differs after upload")
    if head.get("ChecksumSHA256") != expected_checksum:
        raise WebObjectStoreBlocked("private evidence object SHA-256 checksum is unavailable or differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private evidence object metadata is unavailable")
    normalized_metadata = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized_metadata.get("lawcase-source-sha256") != upload.content_sha256
        or normalized_metadata.get("lawcase-source-bytes") != str(upload.byte_size)
        or normalized_metadata.get("lawcase-inspection-hash") != upload.inspection_hash
    ):
        raise WebObjectStoreBlocked("private evidence object metadata differs after upload")


def _validate_remote_archive(head: Any, *, archive: AdmittedWebZip, expected_checksum: str) -> None:
    if not isinstance(head, dict) or head.get("ContentLength") != archive.byte_size or head.get("ChecksumSHA256") != expected_checksum:
        raise WebObjectStoreBlocked("private material archive integrity differs after upload")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private material archive metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized.get("lawcase-archive-sha256") != archive.content_sha256
        or normalized.get("lawcase-archive-bytes") != str(archive.byte_size)
        or normalized.get("lawcase-archive-entries") != str(len(archive.entries))
        or normalized.get("lawcase-archive-expanded-bytes") != str(archive.expanded_byte_size)
    ):
        raise WebObjectStoreBlocked("private material archive metadata differs after upload")


def _validate_remote_office(
    head: Any,
    *,
    content: bytes,
    content_sha256: str,
    media_type: str | None,
) -> None:
    if not isinstance(head, dict) or head.get("ContentLength") != len(content):
        raise WebObjectStoreBlocked("private Office artifact byte size differs after storage")
    if head.get("ChecksumSHA256") != base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii"):
        raise WebObjectStoreBlocked("private Office artifact checksum is unavailable or differs")
    if sha256(content).hexdigest() != content_sha256:
        raise WebObjectStoreBlocked("private Office artifact content hash differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private Office artifact metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if normalized.get("lawcase-office-sha256") != content_sha256:
        raise WebObjectStoreBlocked("private Office artifact metadata differs")
    if media_type is not None and normalized.get("lawcase-office-media-type") != media_type:
        raise WebObjectStoreBlocked("private Office artifact media type differs")


def _validate_remote_review_pdf(head: Any, content: bytes, content_sha256: str) -> None:
    if not isinstance(head, dict) or head.get("ContentLength") != len(content):
        raise WebObjectStoreBlocked("private review PDF byte size differs after storage")
    if head.get("ChecksumSHA256") != base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii"):
        raise WebObjectStoreBlocked("private review PDF checksum is unavailable or differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict) or {str(key).lower(): value for key, value in metadata.items()}.get("lawcase-review-pdf-sha256") != content_sha256:
        raise WebObjectStoreBlocked("private review PDF metadata differs")


def _validate_remote_stored_archive(head: Any, *, stored: StoredWebMaterialArchive, expected_checksum: str) -> None:
    if not isinstance(head, dict) or head.get("ContentLength") != stored.byte_size or head.get("ChecksumSHA256") != expected_checksum:
        raise WebObjectStoreBlocked("private material archive integrity differs before processing")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private material archive metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized.get("lawcase-archive-sha256") != stored.content_sha256
        or normalized.get("lawcase-archive-bytes") != str(stored.byte_size)
        or normalized.get("lawcase-archive-entries") != str(stored.entry_count)
        or normalized.get("lawcase-archive-expanded-bytes") != str(stored.expanded_byte_size)
    ):
        raise WebObjectStoreBlocked("private material archive metadata differs before processing")


def _validate_remote_stored_object(
    head: Any,
    *,
    stored: StoredWebEvidenceOriginal,
    expected_checksum: str,
) -> None:
    if not isinstance(head, dict):
        raise WebObjectStoreBlocked("private evidence object verification response is invalid")
    if head.get("ContentLength") != stored.byte_size:
        raise WebObjectStoreBlocked("private evidence object byte size differs before materialization")
    if head.get("ChecksumSHA256") != expected_checksum:
        raise WebObjectStoreBlocked("private evidence object SHA-256 checksum is unavailable or differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private evidence object metadata is unavailable")
    normalized_metadata = {str(key).lower(): value for key, value in metadata.items()}
    if normalized_metadata.get("lawcase-source-sha256") != stored.content_sha256 or normalized_metadata.get(
        "lawcase-source-bytes"
    ) != str(stored.byte_size):
        raise WebObjectStoreBlocked("private evidence object metadata differs before materialization")


def _validate_case_agent_material(stored: StoredCaseAgentMaterial) -> None:
    if not isinstance(stored, StoredCaseAgentMaterial):
        raise WebObjectStoreBlocked("case-Agent material locator is invalid")
    _validate_uuid("material firm_id", stored.object_key.split("/")[2] if len(stored.object_key.split("/")) > 2 else "")
    _validate_uuid("material matter_id", stored.object_key.split("/")[3] if len(stored.object_key.split("/")) > 3 else "")
    expected_media = {
        "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    if stored.admitted_format not in expected_media or stored.media_type != expected_media[stored.admitted_format]:
        raise WebObjectStoreBlocked("case-Agent material format is invalid")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", stored.content_sha256)
        or not 1 <= stored.byte_size <= 100 * 1024 * 1024
        or stored.object_key
        != (
            f"case-materials/v1/{stored.object_key.split('/')[2]}/"
            f"{stored.object_key.split('/')[3]}/{stored.content_sha256[:2]}/"
            f"{stored.content_sha256}"
        )
    ):
        raise WebObjectStoreBlocked("case-Agent material locator is not hash-bound")
    if stored.object_version_id is not None and (
        not isinstance(stored.object_version_id, str)
        or stored.object_version_id != stored.object_version_id.strip()
        or not 1 <= len(stored.object_version_id) <= 512
        or any(ord(character) < 32 for character in stored.object_version_id)
    ):
        raise WebObjectStoreBlocked("case-Agent material object version is invalid")


def _validate_remote_case_agent_material(
    head: Any,
    *,
    stored: StoredCaseAgentMaterial,
    expected_checksum: str,
) -> None:
    if (
        not isinstance(head, dict)
        or head.get("ContentLength") != stored.byte_size
        or head.get("ChecksumSHA256") != expected_checksum
    ):
        raise WebObjectStoreBlocked("case-Agent material integrity differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("case-Agent material metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized.get("lawcase-material-sha256") != stored.content_sha256
        or normalized.get("lawcase-material-bytes") != str(stored.byte_size)
        or normalized.get("lawcase-material-media-type") != stored.media_type
        or normalized.get("lawcase-material-format") != stored.admitted_format
    ):
        raise WebObjectStoreBlocked("case-Agent material metadata differs")


def _validate_remote_case_agent_candidate(
    head: Any,
    *,
    artifact_id: str,
    byte_size: int,
    content_sha256: str,
    expected_checksum: str,
) -> None:
    if (
        not isinstance(head, dict)
        or head.get("ContentLength") != byte_size
        or head.get("ChecksumSHA256") != expected_checksum
    ):
        raise WebObjectStoreBlocked("case-Agent candidate integrity differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("case-Agent candidate metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized.get("lawcase-candidate-sha256") != content_sha256
        or normalized.get("lawcase-candidate-bytes") != str(byte_size)
        or normalized.get("lawcase-candidate-artifact-id") != artifact_id
        or normalized.get("lawcase-candidate-review-status") != "NEEDS_LAWYER_REVIEW"
    ):
        raise WebObjectStoreBlocked("case-Agent candidate metadata differs")


def _validate_remote_reviewable_document_package_object(
    head: Any,
    *,
    expected_size: int,
    expected_checksum: str,
    expected_media_type: str,
    expected_metadata: dict[str, str],
    expected_encryption: str,
) -> None:
    if not isinstance(head, dict) or (
        head.get("ContentLength") != expected_size
        or head.get("ChecksumSHA256") != expected_checksum
        or head.get("ContentType") != expected_media_type
        or head.get("ServerSideEncryption") != expected_encryption
    ):
        raise WebObjectStoreBlocked(
            "reviewable document package remote receipt differs"
        )
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict) or {
        str(key).lower(): str(value) for key, value in metadata.items()
    } != expected_metadata:
        raise WebObjectStoreBlocked(
            "reviewable document package remote metadata differs"
        )


def _validate_stored_case_agent_research_response(
    stored: StoredCaseAgentResearchResponse,
    *,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
) -> None:
    for label, value in (
        ("firm_id", firm_id),
        ("matter_id", matter_id),
        ("external_request_id", external_request_id),
    ):
        _validate_uuid(label, value)
    expected = (
        f"case-agent-research/v1/{firm_id}/{matter_id}/"
        f"{external_request_id}/{stored.request_hash}.json"
    )
    if (
        not isinstance(stored, StoredCaseAgentResearchResponse)
        or stored.object_key != expected
        or re.fullmatch(r"[0-9a-f]{64}", stored.request_hash) is None
        or re.fullmatch(r"[0-9a-f]{64}", stored.response_sha256) is None
        or not 2 <= stored.response_bytes <= _CASE_AGENT_RESEARCH_RESPONSE_MAX_BYTES
        or re.fullmatch(r"[0-9a-f]{64}", stored.archive_sha256) is None
        or not 2 <= stored.archive_bytes <= _CASE_AGENT_RESEARCH_ARCHIVE_MAX_BYTES
        or (
            stored.object_version_id is not None
            and (
                not isinstance(stored.object_version_id, str)
                or stored.object_version_id != stored.object_version_id.strip()
                or not 1 <= len(stored.object_version_id) <= 512
                or any(ord(character) < 32 for character in stored.object_version_id)
            )
        )
    ):
        raise WebObjectStoreBlocked("case-Agent research archive locator is invalid")


def _validate_remote_case_agent_research_archive(
    head: Any,
    *,
    external_request_id: str,
    request_hash: str,
    response_sha256: str,
    response_bytes: int,
    archive_sha256: str,
    archive_bytes: int,
    expected_checksum: str,
) -> None:
    if (
        not isinstance(head, dict)
        or head.get("ContentLength") != archive_bytes
        or head.get("ChecksumSHA256") != expected_checksum
    ):
        raise WebObjectStoreBlocked("case-Agent research archive integrity differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("case-Agent research archive metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if normalized != {
        "lawcase-research-request-id": external_request_id,
        "lawcase-research-request-hash": request_hash,
        "lawcase-research-response-sha256": response_sha256,
        "lawcase-research-response-bytes": str(response_bytes),
        "lawcase-research-archive-sha256": archive_sha256,
        "lawcase-research-archive-bytes": str(archive_bytes),
    }:
        raise WebObjectStoreBlocked("case-Agent research archive metadata differs")


def _validate_stored_case_agent_lawyer_analysis_response(
    stored: StoredCaseAgentLawyerAnalysisResponse,
    *,
    firm_id: str,
    matter_id: str,
    external_request_id: str,
) -> None:
    for label, value in (
        ("firm_id", firm_id),
        ("matter_id", matter_id),
        ("external_request_id", external_request_id),
    ):
        _validate_uuid(label, value)
    if not isinstance(stored, StoredCaseAgentLawyerAnalysisResponse):
        raise WebObjectStoreBlocked(
            "case-Agent lawyer-analysis archive locator is invalid"
        )
    expected = (
        f"case-agent-lawyer-analysis/v1/{firm_id}/{matter_id}/"
        f"{external_request_id}/{stored.request_hash}.json"
    )
    if (
        stored.object_key != expected
        or re.fullmatch(r"[0-9a-f]{64}", stored.request_hash) is None
        or re.fullmatch(r"[0-9a-f]{64}", stored.response_sha256) is None
        or not 2
        <= stored.response_bytes
        <= _CASE_AGENT_LAWYER_ANALYSIS_RESPONSE_MAX_BYTES
        or re.fullmatch(r"[0-9a-f]{64}", stored.archive_sha256) is None
        or not 2
        <= stored.archive_bytes
        <= _CASE_AGENT_LAWYER_ANALYSIS_ARCHIVE_MAX_BYTES
        or (
            stored.object_version_id is not None
            and (
                not isinstance(stored.object_version_id, str)
                or stored.object_version_id != stored.object_version_id.strip()
                or not 1 <= len(stored.object_version_id) <= 512
                or any(
                    ord(character) < 32
                    for character in stored.object_version_id
                )
            )
        )
    ):
        raise WebObjectStoreBlocked(
            "case-Agent lawyer-analysis archive locator is invalid"
        )


def _validate_remote_case_agent_lawyer_analysis_archive(
    head: Any,
    *,
    external_request_id: str,
    request_hash: str,
    response_sha256: str,
    response_bytes: int,
    archive_sha256: str,
    archive_bytes: int,
    expected_checksum: str,
    expected_encryption: str,
) -> None:
    if (
        not isinstance(head, dict)
        or head.get("ContentLength") != archive_bytes
        or head.get("ChecksumSHA256") != expected_checksum
        or head.get("ContentType") != "application/json"
        or head.get("ServerSideEncryption") != expected_encryption
    ):
        raise WebObjectStoreBlocked(
            "case-Agent lawyer-analysis archive integrity differs"
        )
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked(
            "case-Agent lawyer-analysis archive metadata is unavailable"
        )
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if normalized != {
        "lawcase-lawyer-request-id": external_request_id,
        "lawcase-lawyer-request-hash": request_hash,
        "lawcase-lawyer-response-sha256": response_sha256,
        "lawcase-lawyer-response-bytes": str(response_bytes),
        "lawcase-lawyer-archive-sha256": archive_sha256,
        "lawcase-lawyer-archive-bytes": str(archive_bytes),
    }:
        raise WebObjectStoreBlocked(
            "case-Agent lawyer-analysis archive metadata differs"
        )


def _is_s3_object_missing(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error")
    if not isinstance(details, dict):
        return False
    return str(details.get("Code", "")) in {"404", "NoSuchKey", "NotFound"}


def _reject_duplicate_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _validate_remote_derivative(head: Any, *, byte_size: int, artifact_sha256: str, page_count: int) -> None:
    if not isinstance(head, dict) or head.get("ContentLength") != byte_size:
        raise WebObjectStoreBlocked("private derivative byte size differs after upload")
    expected_checksum = base64.b64encode(bytes.fromhex(artifact_sha256)).decode("ascii")
    if head.get("ChecksumSHA256") != expected_checksum:
        raise WebObjectStoreBlocked("private derivative checksum is unavailable or differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("private derivative metadata is unavailable")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if normalized.get("lawcase-derivative-sha256") != artifact_sha256 or normalized.get("lawcase-derivative-pages") != str(page_count):
        raise WebObjectStoreBlocked("private derivative metadata differs after upload")


def _validate_remote_native_image(
    head: Any,
    *,
    byte_size: int,
    content_sha256: str,
    media_type: str,
    expected_checksum: str,
) -> None:
    if (
        not isinstance(head, dict)
        or head.get("ContentLength") != byte_size
        or head.get("ChecksumSHA256") != expected_checksum
        or head.get("ContentType") != media_type
    ):
        raise WebObjectStoreBlocked("native evidence image metadata differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise WebObjectStoreBlocked("native evidence image metadata is absent")
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    if (
        normalized.get("lawcase-source-sha256") != content_sha256
        or normalized.get("lawcase-source-bytes") != str(byte_size)
        or normalized.get("lawcase-source-media-type") != media_type
    ):
        raise WebObjectStoreBlocked("native evidence image metadata differs")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise WebObjectStoreBlocked("private derivative could not be read") from error
    return digest.hexdigest()


def _prepare_private_materialization_destination(value: str | Path, *, suffix: str = ".pdf") -> Path:
    try:
        destination = Path(value)
    except TypeError as error:
        raise WebObjectStoreBlocked("private evidence materialization destination is invalid") from error
    if not destination.is_absolute() or destination.suffix.lower() != suffix:
        raise WebObjectStoreBlocked("private evidence materialization destination is invalid")
    _assert_no_symlink_components(destination.parent)
    try:
        parent_metadata = os.lstat(destination.parent)
    except OSError as error:
        raise WebObjectStoreBlocked("private evidence materialization directory is unavailable") from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise WebObjectStoreBlocked("private evidence materialization directory is invalid")
    if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise WebObjectStoreBlocked("private evidence materialization directory must be private")
    if destination.exists() or destination.is_symlink():
        raise WebObjectStoreBlocked("private evidence materialization destination already exists")
    return destination


def _assert_no_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise WebObjectStoreBlocked("private evidence materialization path is invalid")
    current = Path(path.anchor)
    for component in path.parts[len(current.parts) :]:
        if component in {"", ".", ".."}:
            raise WebObjectStoreBlocked("private evidence materialization path is invalid")
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            raise WebObjectStoreBlocked("private evidence materialization directory is unavailable") from None
        except OSError as error:
            raise WebObjectStoreBlocked("private evidence materialization directory is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise WebObjectStoreBlocked("private evidence materialization path cannot use symbolic links")


def _stream_object_to_private_file(
    response: Any,
    *,
    target: Path,
    stored: StoredWebEvidenceOriginal,
) -> None:
    if not isinstance(response, dict):
        raise WebObjectStoreBlocked("private evidence object download response is invalid")
    body = response.get("Body")
    if not callable(getattr(body, "read", None)):
        raise WebObjectStoreBlocked("private evidence object download response is invalid")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = sha256()
    byte_size = 0
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            while True:
                block = body.read(1024 * 1024)
                if not isinstance(block, (bytes, bytearray)):
                    raise WebObjectStoreBlocked("private evidence object download body is invalid")
                if not block:
                    break
                byte_size += len(block)
                if byte_size > stored.byte_size:
                    raise WebObjectStoreBlocked("private evidence object exceeds its recorded byte size")
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        if byte_size != stored.byte_size or digest.hexdigest() != stored.content_sha256:
            raise WebObjectStoreBlocked("private evidence object bytes differ during materialization")
        os.link(temporary, target)
        target.chmod(0o600)
    except WebObjectStoreBlocked:
        raise
    except OSError as error:
        raise WebObjectStoreBlocked("private evidence object could not be materialized safely") from error
    finally:
        _remove_private_file(temporary)


def _stream_verified_object_to_private_file(
    response: Any,
    *,
    target: Path,
    expected_byte_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    if not isinstance(response, dict):
        raise WebObjectStoreBlocked(f"{label} download response is invalid")
    body = response.get("Body")
    if not callable(getattr(body, "read", None)):
        raise WebObjectStoreBlocked(f"{label} download response is invalid")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = sha256()
    byte_size = 0
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            while True:
                block = body.read(1024 * 1024)
                if not isinstance(block, (bytes, bytearray)):
                    raise WebObjectStoreBlocked(f"{label} download body is invalid")
                if not block:
                    break
                byte_size += len(block)
                if byte_size > expected_byte_size:
                    raise WebObjectStoreBlocked(f"{label} exceeds its recorded byte size")
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        if byte_size != expected_byte_size or digest.hexdigest() != expected_sha256:
            raise WebObjectStoreBlocked(f"{label} bytes differ during materialization")
        os.link(temporary, target)
        target.chmod(0o600)
    except WebObjectStoreBlocked:
        raise
    except OSError as error:
        raise WebObjectStoreBlocked(f"{label} could not be materialized") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _remove_private_file(temporary)


def _remove_private_file(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError:
        pass
