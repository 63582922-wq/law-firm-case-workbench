"""Immutable private-object storage for admitted non-PDF case materials.

The object key is server-derived from tenant, matter, material UUID and the
verified SHA-256.  ``If-None-Match: *`` prevents an original from being
overwritten.  Any exception after the remote write starts is reported as an
unknown state: callers must reconcile the deterministic key and must not ask
the browser to transmit the file again.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import re
from typing import Any, Protocol
from uuid import UUID

from .web_common_material_admission import (
    AdmittedCommonMaterial,
    CommonMaterialFormat,
    CommonMaterialRoute,
)
from .web_object_store import S3CompatibleClient, S3PrivateObjectStoreConfig


class CommonMaterialObjectStoreBlocked(ValueError):
    """A private object-store input or recovery receipt is invalid."""


class CommonMaterialObjectStateUnknown(CommonMaterialObjectStoreBlocked):
    """The remote write may have committed and must be reconciled, not retried."""


@dataclass(frozen=True)
class StoredCommonMaterialOriginal:
    object_key: str = field(repr=False)
    material_object_id: str
    content_sha256: str
    byte_size: int
    media_type: str
    admitted_format: CommonMaterialFormat
    route: CommonMaterialRoute
    inspection_hash: str
    object_version_id: str | None = field(default=None, repr=False)


class CommonMaterialPrivateObjectStorePort(Protocol):
    def put_immutable_common_material(
        self,
        admitted: AdmittedCommonMaterial,
        *,
        firm_id: str,
        matter_id: str,
    ) -> StoredCommonMaterialOriginal: ...

    def recover_immutable_common_material(
        self,
        *,
        firm_id: str,
        matter_id: str,
        material_object_id: str,
        content_sha256: str,
        byte_size: int,
        media_type: str,
        admitted_format: CommonMaterialFormat,
        route: CommonMaterialRoute,
        inspection_hash: str,
    ) -> StoredCommonMaterialOriginal: ...

    def materialize_common_material(
        self,
        stored: StoredCommonMaterialOriginal,
        *,
        destination: str | Path,
    ) -> Path: ...


class S3CommonMaterialPrivateObjectStore:
    """S3-compatible, no-overwrite storage and exact Worker materialization."""

    def __init__(
        self,
        config: S3PrivateObjectStoreConfig,
        *,
        client: S3CompatibleClient | None = None,
    ) -> None:
        if not isinstance(config, S3PrivateObjectStoreConfig):
            raise ValueError("common material object-store configuration is required")
        self._config = config
        self._client = client or _new_boto3_client(config)
        for method in ("put_object", "head_object", "get_object"):
            if not callable(getattr(self._client, method, None)):
                raise ValueError("common material object-store client is invalid")

    def put_immutable_common_material(
        self,
        admitted: AdmittedCommonMaterial,
        *,
        firm_id: str,
        matter_id: str,
    ) -> StoredCommonMaterialOriginal:
        _uuid(firm_id, "common material firm")
        _uuid(matter_id, "common material matter")
        if not isinstance(admitted, AdmittedCommonMaterial):
            raise CommonMaterialObjectStoreBlocked("common material admission receipt is invalid")
        admitted.validate()
        _verify_admitted_bytes(admitted)
        stored = StoredCommonMaterialOriginal(
            object_key=_object_key(
                firm_id=firm_id,
                matter_id=matter_id,
                material_object_id=admitted.material_object_id,
                content_sha256=admitted.content_sha256,
                admitted_format=admitted.admitted_format,
            ),
            material_object_id=admitted.material_object_id,
            content_sha256=admitted.content_sha256,
            byte_size=admitted.byte_size,
            media_type=admitted.media_type,
            admitted_format=admitted.admitted_format,
            route=admitted.route,
            inspection_hash=admitted.inspection_hash,
        )
        checksum = _checksum(admitted.content_sha256)
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": stored.object_key,
            "ContentLength": admitted.byte_size,
            "ContentType": admitted.media_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
            "Metadata": _metadata(stored),
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        remote_started = False
        try:
            with admitted.path.open("rb") as source:
                remote_started = True
                response = self._client.put_object(Body=source, **request)
            _verify_admitted_bytes(admitted)
            version = _object_version(response)
            stored = StoredCommonMaterialOriginal(
                object_key=stored.object_key,
                material_object_id=stored.material_object_id,
                content_sha256=stored.content_sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                admitted_format=stored.admitted_format,
                route=stored.route,
                inspection_hash=stored.inspection_hash,
                object_version_id=version,
            )
            _validate_remote(
                self._client.head_object(**_head_request(self._config.bucket, stored)),
                stored=stored,
                expected_encryption=self._config.server_side_encryption,
            )
            return stored
        except CommonMaterialObjectStateUnknown:
            raise
        except Exception as error:
            if remote_started:
                raise CommonMaterialObjectStateUnknown(
                    "common material object write requires reconciliation"
                ) from error
            raise CommonMaterialObjectStoreBlocked(
                "common material object could not be prepared"
            ) from error

    def recover_immutable_common_material(
        self,
        *,
        firm_id: str,
        matter_id: str,
        material_object_id: str,
        content_sha256: str,
        byte_size: int,
        media_type: str,
        admitted_format: CommonMaterialFormat,
        route: CommonMaterialRoute,
        inspection_hash: str,
    ) -> StoredCommonMaterialOriginal:
        """Read-only recovery of the one deterministic key after uncertainty."""

        for value, label in (
            (firm_id, "common material firm"),
            (matter_id, "common material matter"),
            (material_object_id, "common material object"),
        ):
            _uuid(value, label)
        stored = StoredCommonMaterialOriginal(
            object_key=_object_key(
                firm_id=firm_id,
                matter_id=matter_id,
                material_object_id=material_object_id,
                content_sha256=content_sha256,
                admitted_format=admitted_format,
            ),
            material_object_id=material_object_id,
            content_sha256=content_sha256,
            byte_size=byte_size,
            media_type=media_type,
            admitted_format=admitted_format,
            route=route,
            inspection_hash=inspection_hash,
        )
        _validate_stored(stored)
        try:
            head = self._client.head_object(**_head_request(self._config.bucket, stored))
            version = head.get("VersionId") if isinstance(head, dict) else None
            stored = StoredCommonMaterialOriginal(
                object_key=stored.object_key,
                material_object_id=stored.material_object_id,
                content_sha256=stored.content_sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                admitted_format=stored.admitted_format,
                route=stored.route,
                inspection_hash=stored.inspection_hash,
                object_version_id=_validated_optional_version(version),
            )
            _validate_remote(
                head,
                stored=stored,
                expected_encryption=self._config.server_side_encryption,
            )
            return stored
        except CommonMaterialObjectStoreBlocked:
            raise
        except Exception as error:
            raise CommonMaterialObjectStoreBlocked(
                "common material object could not be reconciled"
            ) from error

    def materialize_common_material(
        self,
        stored: StoredCommonMaterialOriginal,
        *,
        destination: str | Path,
    ) -> Path:
        """Materialize exact bytes into a caller-owned private Worker folder."""

        _validate_stored(stored)
        target = _prepare_destination(destination, admitted_format=stored.admitted_format)
        body: object | None = None
        try:
            request = _head_request(self._config.bucket, stored)
            _validate_remote(
                self._client.head_object(**request),
                stored=stored,
                expected_encryption=self._config.server_side_encryption,
            )
            response = self._client.get_object(**request)
            body = response.get("Body") if isinstance(response, dict) else None
            if not callable(getattr(body, "read", None)):
                raise CommonMaterialObjectStoreBlocked("common material object body is unavailable")
            digest = sha256()
            total = 0
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    block = body.read(min(1024 * 1024, stored.byte_size - total + 1))
                    if not isinstance(block, (bytes, bytearray)):
                        raise CommonMaterialObjectStoreBlocked("common material object stream is invalid")
                    if not block:
                        break
                    total += len(block)
                    if total > stored.byte_size:
                        raise CommonMaterialObjectStoreBlocked("common material object exceeds its receipt")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o600)
            if total != stored.byte_size or digest.hexdigest() != stored.content_sha256:
                raise CommonMaterialObjectStoreBlocked("common material object bytes differ from their receipt")
            _validate_remote(
                self._client.head_object(**request),
                stored=stored,
                expected_encryption=self._config.server_side_encryption,
            )
            return target
        except Exception as error:
            target.unlink(missing_ok=True)
            if isinstance(error, CommonMaterialObjectStoreBlocked):
                raise
            raise CommonMaterialObjectStoreBlocked(
                "common material object could not be materialized"
            ) from error
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _object_key(
    *,
    firm_id: str,
    matter_id: str,
    material_object_id: str,
    content_sha256: str,
    admitted_format: CommonMaterialFormat,
) -> str:
    _hex_sha(content_sha256, "common material hash")
    if admitted_format in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}:
        suffix = "jpg" if admitted_format is CommonMaterialFormat.JPEG else "png"
        return (
            f"original-images/v1/{firm_id}/{matter_id}/{content_sha256[:2]}/"
            f"{content_sha256}/{material_object_id}.{suffix}"
        )
    return f"case-materials/v1/{firm_id}/{matter_id}/{content_sha256[:2]}/{content_sha256}"


def common_material_object_key(
    *,
    firm_id: str,
    matter_id: str,
    material_object_id: str,
    content_sha256: str,
    admitted_format: CommonMaterialFormat,
) -> str:
    """Return the one server-derived locator used for unknown-state recovery."""

    if not isinstance(admitted_format, CommonMaterialFormat):
        raise CommonMaterialObjectStoreBlocked("common material format is invalid")
    return _object_key(
        firm_id=_uuid(firm_id, "common material firm"),
        matter_id=_uuid(matter_id, "common material matter"),
        material_object_id=_uuid(material_object_id, "common material object"),
        content_sha256=_hex_sha(content_sha256, "common material hash"),
        admitted_format=admitted_format,
    )


def _metadata(stored: StoredCommonMaterialOriginal) -> dict[str, str]:
    metadata = {
        "lawcase-material-object-id": stored.material_object_id,
        "lawcase-material-sha256": stored.content_sha256,
        "lawcase-material-bytes": str(stored.byte_size),
        "lawcase-material-media-type": stored.media_type,
        "lawcase-material-format": stored.admitted_format.value,
        "lawcase-material-route": stored.route.value,
        "lawcase-material-inspection-hash": stored.inspection_hash,
        "lawcase-material-review-status": "NEEDS_LAWYER_REVIEW",
    }
    if stored.route is CommonMaterialRoute.VISUAL_OCR:
        metadata.update(
            {
                "lawcase-source-sha256": stored.content_sha256,
                "lawcase-source-bytes": str(stored.byte_size),
                "lawcase-source-media-type": stored.media_type,
            }
        )
    return metadata


def _validate_stored(stored: object) -> None:
    if not isinstance(stored, StoredCommonMaterialOriginal):
        raise CommonMaterialObjectStoreBlocked("common material storage receipt is invalid")
    parts = stored.object_key.split("/") if isinstance(stored.object_key, str) else []
    if stored.route is CommonMaterialRoute.VISUAL_OCR:
        if len(parts) != 7 or parts[:2] != ["original-images", "v1"]:
            raise CommonMaterialObjectStoreBlocked("common material image key is invalid")
        firm_id, matter_id = parts[2:4]
        for value, label in (
            (firm_id, "common material firm"),
            (matter_id, "common material matter"),
            (stored.material_object_id, "common material object receipt"),
        ):
            _uuid(value, label)
        suffix = "jpg" if stored.admitted_format is CommonMaterialFormat.JPEG else "png"
        if (
            parts[4] != stored.content_sha256[:2]
            or parts[5] != stored.content_sha256
            or parts[6] != f"{stored.material_object_id}.{suffix}"
        ):
            raise CommonMaterialObjectStoreBlocked("common material image key differs from its receipt")
    else:
        if len(parts) != 6 or parts[:2] != ["case-materials", "v1"]:
            raise CommonMaterialObjectStoreBlocked("common material object key is invalid")
        firm_id, matter_id = parts[2:4]
        for value, label in (
            (firm_id, "common material firm"),
            (matter_id, "common material matter"),
            (stored.material_object_id, "common material object receipt"),
        ):
            _uuid(value, label)
        if parts[4] != stored.content_sha256[:2] or parts[5] != stored.content_sha256:
            raise CommonMaterialObjectStoreBlocked("common material object key differs from its receipt")
    _validate_stored_values(stored)


def _validate_stored_values(stored: StoredCommonMaterialOriginal) -> None:
    _hex_sha(stored.content_sha256, "common material hash")
    _hex_sha(stored.inspection_hash, "common material inspection hash")
    if type(stored.byte_size) is not int or not 1 <= stored.byte_size <= 1024**3:
        raise CommonMaterialObjectStoreBlocked("common material object size is invalid")
    if not isinstance(stored.admitted_format, CommonMaterialFormat) or not isinstance(stored.route, CommonMaterialRoute):
        raise CommonMaterialObjectStoreBlocked("common material object format is invalid")
    image_format = stored.admitted_format in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}
    if image_format != (stored.route is CommonMaterialRoute.VISUAL_OCR):
        raise CommonMaterialObjectStoreBlocked("common material object route differs from its format")
    if not isinstance(stored.media_type, str) or not 1 <= len(stored.media_type) <= 160:
        raise CommonMaterialObjectStoreBlocked("common material object media type is invalid")
    _validated_optional_version(stored.object_version_id)


def _validate_remote(
    head: object,
    *,
    stored: StoredCommonMaterialOriginal,
    expected_encryption: str,
) -> None:
    _validate_stored_values(stored)
    if not isinstance(head, dict):
        raise CommonMaterialObjectStoreBlocked("common material object metadata is unavailable")
    if head.get("ContentLength") != stored.byte_size or head.get("ChecksumSHA256") != _checksum(stored.content_sha256):
        raise CommonMaterialObjectStoreBlocked("common material object integrity differs")
    if head.get("ContentType") not in {None, stored.media_type}:
        raise CommonMaterialObjectStoreBlocked("common material object media type differs")
    if head.get("ServerSideEncryption") != expected_encryption:
        raise CommonMaterialObjectStoreBlocked("common material object encryption metadata differs")
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise CommonMaterialObjectStoreBlocked("common material object metadata is unavailable")
    normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
    if normalized != _metadata(stored):
        raise CommonMaterialObjectStoreBlocked("common material object metadata differs")


def _verify_admitted_bytes(admitted: AdmittedCommonMaterial) -> None:
    path = admitted.path
    if path.is_symlink() or not path.is_file() or path.stat().st_size != admitted.byte_size:
        raise CommonMaterialObjectStoreBlocked("admitted common material source changed")
    digest = sha256()
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise CommonMaterialObjectStoreBlocked("admitted common material source is unavailable") from error
    if digest.hexdigest() != admitted.content_sha256:
        raise CommonMaterialObjectStoreBlocked("admitted common material source changed")


def _head_request(bucket: str, stored: StoredCommonMaterialOriginal) -> dict[str, Any]:
    request: dict[str, Any] = {
        "Bucket": bucket,
        "Key": stored.object_key,
        "ChecksumMode": "ENABLED",
    }
    if stored.object_version_id is not None:
        request["VersionId"] = stored.object_version_id
    return request


def _prepare_destination(destination: str | Path, *, admitted_format: CommonMaterialFormat) -> Path:
    target = Path(destination)
    expected_suffix = {
        CommonMaterialFormat.DOCX: ".docx",
        CommonMaterialFormat.XLSX: ".xlsx",
        CommonMaterialFormat.PPTX: ".pptx",
        CommonMaterialFormat.RTF: ".rtf",
        CommonMaterialFormat.TXT: ".txt",
        CommonMaterialFormat.CSV: ".csv",
        CommonMaterialFormat.HTML: ".html",
        CommonMaterialFormat.EML: ".eml",
        CommonMaterialFormat.JPEG: ".jpg",
        CommonMaterialFormat.PNG: ".png",
    }[admitted_format]
    if not target.is_absolute() or target.suffix.casefold() != expected_suffix or target.exists():
        raise CommonMaterialObjectStoreBlocked("common material destination is invalid")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise CommonMaterialObjectStoreBlocked("common material destination folder is unsafe")
    resolved_parent = parent.resolve(strict=True)
    resolved_target = resolved_parent / target.name
    if resolved_target.exists():
        raise CommonMaterialObjectStoreBlocked("common material destination already exists")
    return resolved_target


def _object_version(response: object) -> str | None:
    version = response.get("VersionId") if isinstance(response, dict) else None
    return _validated_optional_version(version)


def _validated_optional_version(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 512
        or any(ord(character) < 32 for character in value)
    ):
        raise CommonMaterialObjectStoreBlocked("common material object version is invalid")
    return value


def _checksum(value: str) -> str:
    return base64.b64encode(bytes.fromhex(_hex_sha(value, "common material hash"))).decode("ascii")


def _hex_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CommonMaterialObjectStoreBlocked(f"{label} is invalid")
    return value


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise CommonMaterialObjectStoreBlocked(f"{label} is invalid") from error


def _new_boto3_client(config: S3PrivateObjectStoreConfig) -> S3CompatibleClient:
    try:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region_name,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 0}),
        )
    except Exception as error:  # pragma: no cover - deployment dependency
        raise CommonMaterialObjectStoreBlocked(
            "common material S3 client is unavailable"
        ) from error


__all__ = (
    "CommonMaterialObjectStateUnknown",
    "CommonMaterialObjectStoreBlocked",
    "CommonMaterialPrivateObjectStorePort",
    "S3CommonMaterialPrivateObjectStore",
    "StoredCommonMaterialOriginal",
    "common_material_object_key",
)
