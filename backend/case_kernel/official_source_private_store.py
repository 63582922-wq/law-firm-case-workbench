"""Matter-bound private storage and safe text reads for official sources.

The PostgreSQL legal ledger deliberately persists only the legacy
content-addressed locator ``aa/bb/<sha256>.lca``.  It is not a bucket key.
This adapter expands that locator inside the trusted server boundary to an
S3 key containing the firm, matter and exact plaintext hash.  Consequently a
locator copied from another firm or matter cannot address that tenant's
object.

The text reader implements ``VerifiedOfficialSourceTextPort`` structurally.
It re-reads the immutable private object, verifies S3 metadata, byte count,
media type and SHA-256 before and after the read, and extracts literal text
only.  HTML is parsed without resolving attributes or resources; PDF input is
limited to its existing text layer.  ``provision_locator`` is retained only as
a lawyer-reviewed source label and is never presented as an automatically
located or legally interpreted provision.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import io
import json
import re
from typing import Any, Iterator
from uuid import UUID

from pypdf import PdfReader

from .legal_source_postgres import PostgresLegalSourceStore
from .official_source_capture_postgres import PostgresOfficialSourceCaptureStore
from .managed_artifact_store import StoredArtifactObject
from .web_object_store import S3CompatibleClient, S3PrivateObjectStoreConfig


class OfficialSourceObjectStoreBlocked(ValueError):
    """An official-source object or text extraction failed an invariant."""


class OfficialSourceObjectStateUnknown(OfficialSourceObjectStoreBlocked):
    """A private-object write may have committed and must be reconciled."""


_LEDGER_KEY = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.lca$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ARCHIVE_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/xhtml+xml",
        "text/html",
        "text/plain",
    }
)
_TEXT_MEDIA_TYPES = frozenset(
    {"application/pdf", "application/xhtml+xml", "text/html", "text/plain"}
)
_DEFAULT_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_TEXT_CHARACTERS = 2_000_000
_DEFAULT_MAX_PDF_PAGES = 2_000


@dataclass(frozen=True)
class StoredOfficialSourceObject:
    """Ledger-safe receipt; the full bucket key is intentionally absent."""

    ledger_object_key: str
    content_sha256: str
    byte_size: int
    media_type: str
    object_version_id: str | None = field(default=None, repr=False)
    stored_at: datetime | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OfficialSourceTextReadBudget:
    max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES
    max_text_characters: int = _DEFAULT_MAX_TEXT_CHARACTERS
    max_pdf_pages: int = _DEFAULT_MAX_PDF_PAGES

    def validate(self) -> None:
        for value, minimum, maximum, label in (
            (self.max_source_bytes, 1, 64 * 1024 * 1024, "source byte"),
            (self.max_text_characters, 1, 20_000_000, "text character"),
            (self.max_pdf_pages, 1, 10_000, "PDF page"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise OfficialSourceObjectStoreBlocked(
                    f"official source {label} limit is invalid"
                )


class S3OfficialSourcePrivateObjectStore:
    """No-overwrite, matter-bound S3 storage for exact official-source bytes."""

    def __init__(
        self,
        config: S3PrivateObjectStoreConfig,
        *,
        client: S3CompatibleClient | None = None,
        max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES,
    ) -> None:
        if not isinstance(config, S3PrivateObjectStoreConfig):
            raise ValueError("official source object-store configuration is required")
        if (
            isinstance(max_source_bytes, bool)
            or not isinstance(max_source_bytes, int)
            or not 1 <= max_source_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("official source object byte limit is invalid")
        self._config = config
        self._client = client or _new_s3_client(config)
        self._max_source_bytes = max_source_bytes
        for method in ("put_object", "head_object", "get_object"):
            if not callable(getattr(self._client, method, None)):
                raise ValueError("official source object-store client is incomplete")

    def put_official_source(
        self,
        content: bytes,
        *,
        firm_id: str,
        matter_id: str,
        content_sha256: str,
        content_media_type: str,
    ) -> StoredOfficialSourceObject:
        firm = _uuid(firm_id, "firm_id")
        matter = _uuid(matter_id, "matter_id")
        digest = _hash(content_sha256, "content_sha256")
        media_type = _normalized_media_type(content_media_type)
        if not isinstance(content, bytes) or not 1 <= len(content) <= self._max_source_bytes:
            raise OfficialSourceObjectStoreBlocked(
                "official source bytes are empty or exceed the configured limit"
            )
        if sha256(content).hexdigest() != digest:
            raise OfficialSourceObjectStoreBlocked(
                "official source bytes differ from their declared hash"
            )
        _validate_content_shape(content, media_type)
        receipt = StoredOfficialSourceObject(
            ledger_object_key=_ledger_key(digest),
            content_sha256=digest,
            byte_size=len(content),
            media_type=media_type,
        )
        key = _full_object_key(firm, matter, digest)
        checksum = _checksum(digest)
        metadata = _metadata(
            firm_id=firm,
            matter_id=matter,
            content_sha256=digest,
            byte_size=len(content),
            media_type=media_type,
        )
        request: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": key,
            "Body": io.BytesIO(content),
            "ContentLength": len(content),
            "ContentType": media_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": checksum,
            "IfNoneMatch": "*",
            "Metadata": metadata,
            "ServerSideEncryption": self._config.server_side_encryption,
        }
        if self._config.kms_key_id is not None:
            request["SSEKMSKeyId"] = self._config.kms_key_id
        remote_started = False
        try:
            remote_started = True
            response = self._client.put_object(**request)
            version = _optional_version(
                response.get("VersionId") if isinstance(response, dict) else None
            )
            head_request = {"Bucket": self._config.bucket, "Key": key}
            if version is not None:
                head_request["VersionId"] = version
            head = self._client.head_object(**head_request)
            stored_at = _optional_object_timestamp(
                head.get("LastModified") if isinstance(head, dict) else None
            )
            checksum_attested = _validate_head(
                head,
                firm_id=firm,
                matter_id=matter,
                content_sha256=digest,
                byte_size=len(content),
                media_type=media_type,
                encryption=self._config.server_side_encryption,
                allow_missing_checksum=True,
            )
            if not checksum_attested:
                _reconcile_checksumless_object(
                    self._client,
                    first_head=head,
                    request=head_request,
                    firm_id=firm,
                    matter_id=matter,
                    content_sha256=digest,
                    byte_size=len(content),
                    media_type=media_type,
                    encryption=self._config.server_side_encryption,
                )
            return StoredOfficialSourceObject(
                ledger_object_key=receipt.ledger_object_key,
                content_sha256=digest,
                byte_size=len(content),
                media_type=media_type,
                object_version_id=version,
                stored_at=stored_at,
            )
        except OfficialSourceObjectStoreBlocked:
            raise
        except Exception as error:
            if remote_started:
                raise OfficialSourceObjectStateUnknown(
                    "official source object write requires read-only reconciliation"
                ) from error
            raise OfficialSourceObjectStoreBlocked(
                "official source object could not be prepared"
            ) from error

    def recover_official_source(
        self,
        *,
        firm_id: str,
        matter_id: str,
        content_sha256: str,
        content_media_type: str,
        byte_size: int,
    ) -> StoredOfficialSourceObject:
        """Look up the deterministic key after an indeterminate write; never write."""

        recovered = self.find_existing_official_source(
            firm_id=firm_id,
            matter_id=matter_id,
            content_sha256=content_sha256,
            content_media_type=content_media_type,
            byte_size=byte_size,
        )
        if recovered is None:
            raise OfficialSourceObjectStoreBlocked(
                "official source object could not be reconciled"
            )
        return recovered

    def find_existing_official_source(
        self,
        *,
        firm_id: str,
        matter_id: str,
        content_sha256: str,
        content_media_type: str,
        byte_size: int,
    ) -> StoredOfficialSourceObject | None:
        """Read one exact tenant-bound object, or return ``None`` only if absent.

        This permits a fixed, previously authenticated legal-source snapshot to
        be used after a transient public-site failure.  It never enumerates a
        bucket or weakens tenant binding; any existing object that fails a
        receipt check is still rejected rather than treated as missing.
        """

        firm = _uuid(firm_id, "firm_id")
        matter = _uuid(matter_id, "matter_id")
        digest = _hash(content_sha256, "content_sha256")
        media_type = _normalized_media_type(content_media_type)
        _byte_size(byte_size, self._max_source_bytes)
        key = _full_object_key(firm, matter, digest)
        try:
            head = self._client.head_object(Bucket=self._config.bucket, Key=key)
        except Exception as error:
            if _is_s3_object_not_found(error):
                return None
            raise OfficialSourceObjectStoreBlocked(
                "official source object could not be reconciled"
            ) from error
        try:
            version = _optional_version(
                head.get("VersionId") if isinstance(head, dict) else None
            )
            stored_at = _optional_object_timestamp(
                head.get("LastModified") if isinstance(head, dict) else None
            )
            request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
            if version is not None:
                request["VersionId"] = version
            checksum_attested = _validate_head(
                head,
                firm_id=firm,
                matter_id=matter,
                content_sha256=digest,
                byte_size=byte_size,
                media_type=media_type,
                encryption=self._config.server_side_encryption,
                allow_missing_checksum=True,
            )
            if not checksum_attested:
                _reconcile_checksumless_object(
                    self._client,
                    first_head=head,
                    request=request,
                    firm_id=firm,
                    matter_id=matter,
                    content_sha256=digest,
                    byte_size=byte_size,
                    media_type=media_type,
                    encryption=self._config.server_side_encryption,
                )
            return StoredOfficialSourceObject(
                ledger_object_key=_ledger_key(digest),
                content_sha256=digest,
                byte_size=byte_size,
                media_type=media_type,
                object_version_id=version,
                stored_at=stored_at,
            )
        except OfficialSourceObjectStoreBlocked:
            raise
        except Exception as error:
            raise OfficialSourceObjectStoreBlocked(
                "official source object could not be reconciled"
            ) from error

    def read_official_source(
        self,
        *,
        firm_id: str,
        matter_id: str,
        ledger_object_key: str,
        expected_sha256: str,
        expected_media_type: str | None = None,
    ) -> tuple[bytes, str]:
        """Return authenticated bytes and their object-bound canonical media type."""

        firm = _uuid(firm_id, "firm_id")
        matter = _uuid(matter_id, "matter_id")
        digest = _hash(expected_sha256, "expected_sha256")
        _validate_ledger_key(ledger_object_key, digest)
        expected_media = (
            _normalized_media_type(expected_media_type)
            if expected_media_type is not None
            else None
        )
        key = _full_object_key(firm, matter, digest)
        try:
            first_head = self._client.head_object(Bucket=self._config.bucket, Key=key)
            size, media_type, version = _receipt_from_head(
                first_head,
                firm_id=firm,
                matter_id=matter,
                content_sha256=digest,
                expected_media_type=expected_media,
                max_source_bytes=self._max_source_bytes,
                encryption=self._config.server_side_encryption,
            )
            request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
            if version is not None:
                request["VersionId"] = version
            response = self._client.get_object(**request)
            body = _read_bounded_body(response, max_bytes=size)
            if len(body) != size or sha256(body).hexdigest() != digest:
                raise OfficialSourceObjectStoreBlocked(
                    "official source object bytes differ from their immutable receipt"
                )
            _validate_content_shape(body, media_type)
            second_head = self._client.head_object(**request)
            _validate_head(
                second_head,
                firm_id=firm,
                matter_id=matter,
                content_sha256=digest,
                byte_size=size,
                media_type=media_type,
                encryption=self._config.server_side_encryption,
                allow_missing_checksum=True,
            )
            if _head_fingerprint(first_head) != _head_fingerprint(second_head):
                raise OfficialSourceObjectStoreBlocked(
                    "official source object changed while it was being read"
                )
            return body, media_type
        except OfficialSourceObjectStoreBlocked:
            raise
        except Exception as error:
            # Tenant existence and object existence are intentionally not
            # distinguishable across this boundary.
            raise OfficialSourceObjectStoreBlocked(
                "official source private object could not be authenticated"
            ) from error

    def bound_reader(
        self,
        *,
        firm_id: str,
        matter_id: str,
        expected_media_type: str | None = None,
    ) -> "MatterBoundOfficialSourceReader":
        return MatterBoundOfficialSourceReader(
            objects=self,
            firm_id=firm_id,
            matter_id=matter_id,
            expected_media_type=expected_media_type,
        )


class MatterBoundOfficialSourceReader:
    """Reader contract shared by the legal ledger and capture parsers.

    ``__call__`` matches ``PostgresLegalSourceStore``. ``read_bytes`` matches
    the deterministic official-source parsers.  Neither method accepts a firm
    or matter from a database row or model; that scope is fixed at creation.
    """

    def __init__(
        self,
        *,
        objects: S3OfficialSourcePrivateObjectStore,
        firm_id: str,
        matter_id: str,
        expected_media_type: str | None = None,
    ) -> None:
        if not isinstance(objects, S3OfficialSourcePrivateObjectStore):
            raise ValueError("official source private store is required")
        self._objects = objects
        self._firm_id = _uuid(firm_id, "firm_id")
        self._matter_id = _uuid(matter_id, "matter_id")
        self._expected_media_type = (
            _normalized_media_type(expected_media_type)
            if expected_media_type is not None
            else None
        )

    def __call__(self, object_key: str, expected_sha256: str) -> bytes:
        body, _media_type = self._objects.read_official_source(
            firm_id=self._firm_id,
            matter_id=self._matter_id,
            ledger_object_key=object_key,
            expected_sha256=expected_sha256,
            expected_media_type=self._expected_media_type,
        )
        return body

    def read_bytes(self, object_key: str, *, expected_sha256: str) -> bytes:
        return self(object_key, expected_sha256)


class ContextBoundOfficialSourceReader:
    """Concurrency-safe matter scope for the existing PostgreSQL store API."""

    def __init__(self, objects: S3OfficialSourcePrivateObjectStore) -> None:
        if not isinstance(objects, S3OfficialSourcePrivateObjectStore):
            raise ValueError("official source private store is required")
        self._objects = objects
        self._scope: ContextVar[tuple[str, str] | None] = ContextVar(
            "official_source_reader_scope", default=None
        )

    @contextmanager
    def bind(self, *, firm_id: str, matter_id: str) -> Iterator[None]:
        scope = (_uuid(firm_id, "firm_id"), _uuid(matter_id, "matter_id"))
        token: Token[tuple[str, str] | None] = self._scope.set(scope)
        try:
            yield
        finally:
            self._scope.reset(token)

    def __call__(self, object_key: str, expected_sha256: str) -> bytes:
        scope = self._scope.get()
        if scope is None:
            raise OfficialSourceObjectStoreBlocked(
                "official source reader has no authorized matter scope"
            )
        firm_id, matter_id = scope
        body, _media_type = self._objects.read_official_source(
            firm_id=firm_id,
            matter_id=matter_id,
            ledger_object_key=object_key,
            expected_sha256=expected_sha256,
        )
        return body


class S3BackedPostgresLegalSourceStore(PostgresLegalSourceStore):
    """Existing legal ledger with a per-command matter-bound S3 reader.

    This thin subclass is the production construction contract for the current
    keyword-only ``PostgresLegalSourceStore`` API.  It changes no ledger SQL or
    approval rule; it only ensures the two commands which authenticate source
    bytes run with a ContextVar-bound firm/matter reader.
    """

    def __init__(self, dsn: str, *, objects: S3OfficialSourcePrivateObjectStore) -> None:
        self._context_reader = ContextBoundOfficialSourceReader(objects)
        super().__init__(dsn, official_source_reader=self._context_reader)

    def register_official_source_snapshot(self, **kwargs):
        actor = kwargs.get("actor")
        matter_id = kwargs.get("matter_id")
        if actor is None or matter_id is None:
            raise OfficialSourceObjectStoreBlocked(
                "official source registration identity is incomplete"
            )
        with self._context_reader.bind(firm_id=actor.firm_id, matter_id=matter_id):
            return super().register_official_source_snapshot(**kwargs)

    def register_reviewed_capture_snapshot(self, **kwargs):
        actor = kwargs.get("actor")
        matter_id = kwargs.get("matter_id")
        if actor is None or matter_id is None:
            raise OfficialSourceObjectStoreBlocked(
                "reviewed capture registration identity is incomplete"
            )
        with self._context_reader.bind(firm_id=actor.firm_id, matter_id=matter_id):
            return super().register_reviewed_capture_snapshot(**kwargs)


class S3OfficialSourceCaptureArtifactStore:
    """Legacy capture/parser shape backed by one fixed firm and matter in S3."""

    def __init__(
        self,
        *,
        objects: S3OfficialSourcePrivateObjectStore,
        firm_id: str,
        matter_id: str,
    ) -> None:
        self._objects = objects
        self._firm_id = _uuid(firm_id, "firm_id")
        self._matter_id = _uuid(matter_id, "matter_id")
        self._reader = objects.bound_reader(firm_id=self._firm_id, matter_id=self._matter_id)

    def put_captured_official_source(
        self,
        content: bytes,
        *,
        expected_sha256: str,
        case_root: str,
        content_media_type: str,
    ) -> StoredArtifactObject:
        del case_root
        try:
            stored = self._objects.put_official_source(
                content,
                firm_id=self._firm_id,
                matter_id=self._matter_id,
                content_sha256=expected_sha256,
                content_media_type=content_media_type,
            )
        except OfficialSourceObjectStateUnknown:
            # A network timeout or an S3 no-overwrite response may occur after
            # the immutable object is committed. Re-read exactly that
            # tenant/matter/hash receipt instead of issuing another write or
            # treating the public-source task as an unexplained fetch failure.
            stored = self._objects.recover_official_source(
                firm_id=self._firm_id,
                matter_id=self._matter_id,
                content_sha256=expected_sha256,
                content_media_type=content_media_type,
                byte_size=len(content),
            )
        return StoredArtifactObject(
            object_key=stored.ledger_object_key,
            plaintext_sha256=stored.content_sha256,
            plaintext_bytes=stored.byte_size,
            key_id="s3-matter-bound",
        )

    def read_bytes(self, object_key: str, *, expected_sha256: str) -> bytes:
        return self._reader.read_bytes(object_key, expected_sha256=expected_sha256)


class S3BackedPostgresOfficialSourceCaptureStore(PostgresOfficialSourceCaptureStore):
    """Capture persistence that authenticates every completion in its matter scope."""

    def __init__(self, dsn: str, *, objects: S3OfficialSourcePrivateObjectStore) -> None:
        self._context_reader = ContextBoundOfficialSourceReader(objects)
        super().__init__(dsn, artifact_reader=self._context_reader)

    def complete_capture(self, **kwargs):
        actor = kwargs.get("actor")
        matter_id = kwargs.get("matter_id")
        if actor is None or matter_id is None:
            raise OfficialSourceObjectStoreBlocked("official source capture identity is incomplete")
        with self._context_reader.bind(firm_id=actor.firm_id, matter_id=matter_id):
            return super().complete_capture(**kwargs)


class S3VerifiedOfficialSourceTextPort:
    """Safe HTML/TXT/PDF text-layer implementation for dynamic documents."""

    def __init__(
        self,
        *,
        objects: S3OfficialSourcePrivateObjectStore,
        budget: OfficialSourceTextReadBudget | None = None,
    ) -> None:
        if not isinstance(objects, S3OfficialSourcePrivateObjectStore):
            raise ValueError("official source private store is required")
        selected_budget = budget or OfficialSourceTextReadBudget()
        if not isinstance(selected_budget, OfficialSourceTextReadBudget):
            raise ValueError("official source text budget is invalid")
        selected_budget.validate()
        self._objects = objects
        self._budget = selected_budget

    def read_verified_source_text(
        self,
        *,
        firm_id: str,
        matter_id: str,
        snapshot_id: str,
        storage_object_key: str,
        content_sha256: str,
        content_media_type: str,
        provision_locator: str,
    ) -> str:
        _uuid(snapshot_id, "snapshot_id")
        locator = _source_label(provision_locator)
        media_type = _normalized_media_type(content_media_type)
        if media_type not in _TEXT_MEDIA_TYPES:
            raise OfficialSourceObjectStoreBlocked(
                "official source media type has no safe document-text reader"
            )
        body, authenticated_media_type = self._objects.read_official_source(
            firm_id=firm_id,
            matter_id=matter_id,
            ledger_object_key=storage_object_key,
            expected_sha256=content_sha256,
            expected_media_type=media_type,
        )
        text = extract_literal_official_source_text(
            body=body,
            content_media_type=authenticated_media_type,
            budget=self._budget,
        )
        return (
            "来源定位标签（由律师核验；系统未自动定位到精确条款）："
            f"{locator}\n\n{text}"
        )


def extract_literal_official_source_text(
    *,
    body: bytes,
    content_media_type: str,
    budget: OfficialSourceTextReadBudget | None = None,
) -> str:
    """Return literal source text safe to cite in a source-bound document.

    Capturing an official shell page can be useful for an authorized lawyer's
    later review, but markers hidden in JavaScript, CSS, attributes, OCR-less
    scans, or remote application shells are not legal text for an Agent
    candidate or a reviewable document.
    """

    selected_budget = budget or OfficialSourceTextReadBudget()
    if not isinstance(selected_budget, OfficialSourceTextReadBudget):
        raise ValueError("official source text budget is invalid")
    selected_budget.validate()
    media_type = _normalized_media_type(content_media_type)
    if media_type not in _TEXT_MEDIA_TYPES:
        raise OfficialSourceObjectStoreBlocked(
            "official source media type has no safe document-text reader"
        )
    if not isinstance(body, bytes) or not body:
        raise OfficialSourceObjectStoreBlocked("official source body is empty")
    if len(body) > selected_budget.max_source_bytes:
        raise OfficialSourceObjectStoreBlocked(
            "official source exceeds the document-text byte limit"
        )
    _validate_content_shape(body, media_type)
    if media_type == "application/pdf":
        return _extract_pdf_text(body, budget=selected_budget)
    if media_type in {"text/html", "application/xhtml+xml"}:
        return _extract_html_text(
            body, max_characters=selected_budget.max_text_characters
        )
    return _extract_plain_text(body, max_characters=selected_budget.max_text_characters)


@dataclass(frozen=True)
class OfficialSourceS3ProductionAdapters:
    """Concrete Worker/API composition bundle; contains no ambient identity."""

    objects: S3OfficialSourcePrivateObjectStore
    verified_text: S3VerifiedOfficialSourceTextPort

    def legal_source_store(self, *, dsn: str) -> S3BackedPostgresLegalSourceStore:
        return S3BackedPostgresLegalSourceStore(dsn, objects=self.objects)

    def capture_reader(
        self, *, firm_id: str, matter_id: str
    ) -> MatterBoundOfficialSourceReader:
        return self.objects.bound_reader(firm_id=firm_id, matter_id=matter_id)

    def capture_store(self, *, dsn: str) -> S3BackedPostgresOfficialSourceCaptureStore:
        return S3BackedPostgresOfficialSourceCaptureStore(dsn, objects=self.objects)

    def capture_artifact_store(
        self, *, firm_id: str, matter_id: str
    ) -> S3OfficialSourceCaptureArtifactStore:
        return S3OfficialSourceCaptureArtifactStore(
            objects=self.objects,
            firm_id=firm_id,
            matter_id=matter_id,
        )


def compose_official_source_s3_adapters(
    config: S3PrivateObjectStoreConfig,
    *,
    client: S3CompatibleClient | None = None,
    budget: OfficialSourceTextReadBudget | None = None,
) -> OfficialSourceS3ProductionAdapters:
    """Build one shared private store plus its two least-authority readers.

    Production Worker composition injects ``verified_text`` into
    ``PostgresDynamicDocumentBindingPort``.  API composition obtains the
    current legal ledger through ``legal_source_store``.  Existing deterministic
    official-source parsers receive only the per-lease ``capture_reader``.
    """

    selected_budget = budget or OfficialSourceTextReadBudget()
    selected_budget.validate()
    objects = S3OfficialSourcePrivateObjectStore(
        config,
        client=client,
        max_source_bytes=selected_budget.max_source_bytes,
    )
    return OfficialSourceS3ProductionAdapters(
        objects=objects,
        verified_text=S3VerifiedOfficialSourceTextPort(
            objects=objects, budget=selected_budget
        ),
    )


class _SafeVisibleHtmlParser(HTMLParser):
    _IGNORED = frozenset(
        {
            "applet",
            "audio",
            "base",
            "button",
            "embed",
            "form",
            "iframe",
            "input",
            "link",
            "math",
            "meta",
            "noscript",
            "object",
            "option",
            "script",
            "select",
            "source",
            "style",
            "svg",
            "template",
            "textarea",
            "video",
        }
    )

    def __init__(self, *, max_characters: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_characters = max_characters
        self._ignored_depth = 0
        self._characters = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs  # URLs and executable attributes are never inspected or emitted.
        if tag.casefold() in self._IGNORED:
            self._ignored_depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not data.strip():
            return
        self._characters += len(data)
        if self._characters > self._max_characters:
            raise OfficialSourceObjectStoreBlocked(
                "official source HTML text exceeds the character limit"
            )
        self.parts.append(data)


def _extract_html_text(body: bytes, *, max_characters: int) -> str:
    source = _decode_utf8(body, "HTML")
    parser = _SafeVisibleHtmlParser(max_characters=max_characters)
    try:
        parser.feed(source)
        parser.close()
    except OfficialSourceObjectStoreBlocked:
        raise
    except Exception as error:
        raise OfficialSourceObjectStoreBlocked(
            "official source HTML could not be safely parsed"
        ) from error
    return _normalized_extracted_text(" ".join(parser.parts), max_characters=max_characters)


def _extract_plain_text(body: bytes, *, max_characters: int) -> str:
    return _normalized_extracted_text(
        _decode_utf8(body, "plain text"), max_characters=max_characters
    )


def _extract_pdf_text(body: bytes, *, budget: OfficialSourceTextReadBudget) -> str:
    try:
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise OfficialSourceObjectStoreBlocked(
                "encrypted official source PDF cannot enter document drafting"
            )
        if not 1 <= len(reader.pages) <= budget.max_pdf_pages:
            raise OfficialSourceObjectStoreBlocked(
                "official source PDF page count exceeds the text-layer limit"
            )
        parts: list[str] = []
        characters = 0
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            normalized = _normalized_extracted_text(
                page_text,
                max_characters=budget.max_text_characters,
                allow_empty=True,
            )
            if not normalized:
                raise OfficialSourceObjectStoreBlocked(
                    f"official source PDF page {page_number} has no authenticated text layer"
                )
            characters += len(normalized)
            if characters > budget.max_text_characters:
                raise OfficialSourceObjectStoreBlocked(
                    "official source PDF text exceeds the character limit"
                )
            parts.append(f"[PDF第{page_number}页] {normalized}")
        return "\n".join(parts)
    except OfficialSourceObjectStoreBlocked:
        raise
    except Exception as error:
        raise OfficialSourceObjectStoreBlocked(
            "official source PDF text layer could not be safely read"
        ) from error


def _decode_utf8(body: bytes, label: str) -> str:
    if b"\x00" in body:
        raise OfficialSourceObjectStoreBlocked(
            f"official source {label} contains binary NUL bytes"
        )
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise OfficialSourceObjectStoreBlocked(
            f"official source {label} is not verified UTF-8 text"
        ) from error


def _normalized_extracted_text(
    value: str, *, max_characters: int, allow_empty: bool = False
) -> str:
    if any(ord(character) < 32 and character not in "\t\n\r\f" for character in value):
        raise OfficialSourceObjectStoreBlocked(
            "official source extracted text contains unsafe control characters"
        )
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) > max_characters:
        raise OfficialSourceObjectStoreBlocked(
            "official source extracted text exceeds the character limit"
        )
    if not normalized and not allow_empty:
        raise OfficialSourceObjectStoreBlocked("official source has no readable text")
    return normalized


def _source_label(value: str) -> str:
    if not isinstance(value, str):
        raise OfficialSourceObjectStoreBlocked("official source provision label is invalid")
    normalized = re.sub(r"\s+", " ", value).strip()
    if (
        not 1 <= len(normalized) <= 1_000
        or any(ord(character) < 32 for character in normalized)
    ):
        raise OfficialSourceObjectStoreBlocked("official source provision label is invalid")
    return normalized


def _receipt_from_head(
    head: object,
    *,
    firm_id: str,
    matter_id: str,
    content_sha256: str,
    expected_media_type: str | None,
    max_source_bytes: int,
    encryption: str,
) -> tuple[int, str, str | None]:
    if not isinstance(head, dict):
        raise OfficialSourceObjectStoreBlocked(
            "official source object metadata is unavailable"
        )
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise OfficialSourceObjectStoreBlocked(
            "official source object metadata is unavailable"
        )
    normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
    try:
        size = int(normalized.get("lawcase-official-source-bytes", "0"))
    except ValueError as error:
        raise OfficialSourceObjectStoreBlocked(
            "official source object byte metadata is invalid"
        ) from error
    media_type = _normalized_media_type(
        normalized.get("lawcase-official-source-media-type", "")
    )
    if expected_media_type is not None and media_type != expected_media_type:
        raise OfficialSourceObjectStoreBlocked(
            "official source object media type differs from the legal ledger"
        )
    _byte_size(size, max_source_bytes)
    _validate_head(
        head,
        firm_id=firm_id,
        matter_id=matter_id,
        content_sha256=content_sha256,
        byte_size=size,
        media_type=media_type,
        encryption=encryption,
        allow_missing_checksum=True,
    )
    return size, media_type, _optional_version(head.get("VersionId"))


def _validate_head(
    head: object,
    *,
    firm_id: str,
    matter_id: str,
    content_sha256: str,
    byte_size: int,
    media_type: str,
    encryption: str,
    allow_missing_checksum: bool = False,
) -> bool:
    if not isinstance(head, dict):
        raise OfficialSourceObjectStoreBlocked(
            "official source object metadata is unavailable"
        )
    checksum = head.get("ChecksumSHA256")
    checksum_attested = checksum == _checksum(content_sha256)
    if (
        head.get("ContentLength") != byte_size
        or (not checksum_attested and not (allow_missing_checksum and checksum is None))
        or _normalized_media_type(str(head.get("ContentType", ""))) != media_type
        or head.get("ServerSideEncryption") != encryption
    ):
        raise OfficialSourceObjectStoreBlocked(
            "official source object integrity metadata differs"
        )
    metadata = head.get("Metadata")
    if not isinstance(metadata, dict):
        raise OfficialSourceObjectStoreBlocked(
            "official source object metadata is unavailable"
        )
    normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
    if normalized != _metadata(
        firm_id=firm_id,
        matter_id=matter_id,
        content_sha256=content_sha256,
        byte_size=byte_size,
        media_type=media_type,
    ):
        raise OfficialSourceObjectStoreBlocked(
            "official source object tenant or content binding differs"
        )
    return checksum_attested


def _reconcile_checksumless_object(
    client: S3CompatibleClient,
    *,
    first_head: object,
    request: dict[str, Any],
    firm_id: str,
    matter_id: str,
    content_sha256: str,
    byte_size: int,
    media_type: str,
    encryption: str,
) -> None:
    """Accept a missing S3 checksum header only after a stable full-byte proof.

    Some S3-compatible gateways persist the checksum supplied to ``PUT`` but
    omit ``ChecksumSHA256`` from ``HEAD``.  A missing header is never treated
    as an integrity proof by itself: the exact version is read once, bounded,
    re-hashed, shape-checked, and surrounded by matching authenticated heads.
    """

    response = client.get_object(**request)
    body = _read_bounded_body(response, max_bytes=byte_size)
    if len(body) != byte_size or sha256(body).hexdigest() != content_sha256:
        raise OfficialSourceObjectStoreBlocked(
            "official source object bytes differ from their immutable receipt"
        )
    _validate_content_shape(body, media_type)
    second_head = client.head_object(**request)
    if _validate_head(
        second_head,
        firm_id=firm_id,
        matter_id=matter_id,
        content_sha256=content_sha256,
        byte_size=byte_size,
        media_type=media_type,
        encryption=encryption,
        allow_missing_checksum=True,
    ):
        raise OfficialSourceObjectStoreBlocked(
            "official source checksum attestation changed during reconciliation"
        )
    if _head_fingerprint(first_head) != _head_fingerprint(second_head):
        raise OfficialSourceObjectStoreBlocked(
            "official source object changed while it was being reconciled"
        )


def _head_fingerprint(head: object) -> str:
    if not isinstance(head, dict):
        raise OfficialSourceObjectStoreBlocked(
            "official source object metadata is unavailable"
        )
    selected = {
        "ContentLength": head.get("ContentLength"),
        "ChecksumSHA256": head.get("ChecksumSHA256"),
        "ContentType": head.get("ContentType"),
        "ETag": head.get("ETag"),
        "VersionId": head.get("VersionId"),
        "ServerSideEncryption": head.get("ServerSideEncryption"),
        "Metadata": head.get("Metadata"),
    }
    return sha256(
        json.dumps(selected, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_bounded_body(response: object, *, max_bytes: int) -> bytes:
    if not isinstance(response, dict):
        raise OfficialSourceObjectStoreBlocked("official source object body is unavailable")
    stream = response.get("Body")
    if isinstance(stream, (bytes, bytearray)):
        body = bytes(stream)
    elif callable(getattr(stream, "read", None)):
        body = stream.read(max_bytes + 1)
    else:
        raise OfficialSourceObjectStoreBlocked("official source object body is unavailable")
    if not isinstance(body, bytes) or len(body) > max_bytes:
        raise OfficialSourceObjectStoreBlocked(
            "official source object body exceeds its immutable receipt"
        )
    return body


def _validate_content_shape(content: bytes, media_type: str) -> None:
    if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
        raise OfficialSourceObjectStoreBlocked("official source PDF signature is invalid")
    if media_type in {"text/html", "application/xhtml+xml"}:
        prefix = content[:4096].lstrip().lower()
        if b"<html" not in prefix and b"<!doctype html" not in prefix:
            raise OfficialSourceObjectStoreBlocked("official source HTML signature is invalid")
    if media_type == "text/plain":
        _decode_utf8(content, "plain text")
    if media_type == "application/json":
        try:
            json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OfficialSourceObjectStoreBlocked(
                "official source JSON structure is invalid"
            ) from error
    if media_type == "application/vnd.ms-excel" and not content.startswith(
        bytes.fromhex("d0cf11e0a1b11ae1")
    ):
        raise OfficialSourceObjectStoreBlocked("official source XLS signature is invalid")
    if (
        media_type
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        and not content.startswith(b"PK")
    ):
        raise OfficialSourceObjectStoreBlocked("official source XLSX signature is invalid")


def _metadata(
    *,
    firm_id: str,
    matter_id: str,
    content_sha256: str,
    byte_size: int,
    media_type: str,
) -> dict[str, str]:
    return {
        "lawcase-official-source-firm-id": firm_id,
        "lawcase-official-source-matter-id": matter_id,
        "lawcase-official-source-sha256": content_sha256,
        "lawcase-official-source-bytes": str(byte_size),
        "lawcase-official-source-media-type": media_type,
        "lawcase-official-source-object-schema": "v1",
    }


def _ledger_key(content_sha256: str) -> str:
    return f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca"


def _full_object_key(firm_id: str, matter_id: str, content_sha256: str) -> str:
    return (
        f"official-sources/v1/{firm_id}/{matter_id}/"
        f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.lca"
    )


def _validate_ledger_key(value: object, content_sha256: str) -> None:
    match = _LEDGER_KEY.fullmatch(value) if isinstance(value, str) else None
    if match is None or match.group(3) != content_sha256 or value != _ledger_key(content_sha256):
        raise OfficialSourceObjectStoreBlocked(
            "official source ledger locator differs from its content hash"
        )


def _normalized_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise OfficialSourceObjectStoreBlocked("official source media type is invalid")
    media_type = value.split(";", 1)[0].strip().casefold()
    if media_type not in _ARCHIVE_MEDIA_TYPES or _MEDIA_TYPE.fullmatch(media_type) is None:
        raise OfficialSourceObjectStoreBlocked("official source media type is unsupported")
    return media_type


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise OfficialSourceObjectStoreBlocked(f"official source {label} is invalid")
    return value


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise OfficialSourceObjectStoreBlocked(
            f"official source {label} is invalid"
        ) from error


def _byte_size(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise OfficialSourceObjectStoreBlocked("official source object byte size is invalid")
    return value


def _checksum(content_sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")


def _optional_version(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 512
        or any(ord(character) < 32 for character in value)
    ):
        raise OfficialSourceObjectStoreBlocked(
            "official source object version is invalid"
        )
    return value


def _optional_object_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OfficialSourceObjectStoreBlocked(
            "official source object timestamp is invalid"
        )
    return value.astimezone(timezone.utc)


def _is_s3_object_not_found(error: Exception) -> bool:
    """Recognize only S3's ordinary exact-key absence response.

    The result is consumed only inside an already-authorized firm/matter
    context.  Other gateway errors, malformed responses and authorization
    failures stay indistinguishable and fail closed.
    """

    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    detail = response.get("Error")
    if not isinstance(detail, dict):
        return False
    code = detail.get("Code")
    return isinstance(code, str) and code in {"404", "NoSuchKey", "NotFound"}


def _new_s3_client(config: S3PrivateObjectStoreConfig) -> S3CompatibleClient:
    try:
        import boto3
        from botocore.config import Config

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
                retries={"max_attempts": 0},
            ),
        )
    except Exception as error:  # pragma: no cover - deployment dependency.
        raise OfficialSourceObjectStoreBlocked(
            "official source S3 client could not be configured"
        ) from error


__all__ = (
    "ContextBoundOfficialSourceReader",
    "MatterBoundOfficialSourceReader",
    "OfficialSourceObjectStateUnknown",
    "OfficialSourceObjectStoreBlocked",
    "OfficialSourceS3ProductionAdapters",
    "OfficialSourceTextReadBudget",
    "S3BackedPostgresLegalSourceStore",
    "S3BackedPostgresOfficialSourceCaptureStore",
    "S3OfficialSourceCaptureArtifactStore",
    "S3OfficialSourcePrivateObjectStore",
    "S3VerifiedOfficialSourceTextPort",
    "StoredOfficialSourceObject",
    "compose_official_source_s3_adapters",
    "extract_literal_official_source_text",
)
