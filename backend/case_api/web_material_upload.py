"""Server-owned, replay-safe Web PDF material upload orchestration.

The browser may reserve one opaque upload slot and stream a PDF into it, but
it never learns a staging path, scanner result, object key, storage version,
or a database credential.  A slot is bound to the already-resolved server
identity's firm, actor, session and matter.  Its PostgreSQL state machine is
the recovery record for the otherwise non-transactional S3-to-ledger saga.

This module deliberately has no FastAPI route or composition root.  It imports
the two public browser-safe response types from :mod:`case_api.web_app` so a
future Web composition can use this implementation directly without adapting
or widening its response boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol
from unicodedata import category
from uuid import UUID, uuid4

from case_api.persistent_identity import (
    AuthenticationMethod,
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
)
from case_api.web_app import WebMaterialUploadStatusResponse, WebRequestBlocked, WebUploadReceipt, WebUploadSlotResponse
from case_kernel.evidence_manifest_postgres import RegisteredWebEvidenceOriginal
from case_kernel.errors import VersionConflict
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_upload_staging import AdmittedWebPdfUpload, WebUploadStagingArea


__all__ = (
    "PostObjectStoreReconciliationRequired",
    "UploadSlotFailureCode",
    "UploadSlotStatus",
    "WebMaterialUploadBlocked",
    "WebMaterialUploadPolicy",
    "WebMaterialUploadService",
    "WebMaterialUploadSlot",
    "WebMaterialUploadSlotStore",
)


_MAX_DECLARED_CONTENT_LENGTH = 256 * 1024 * 1024
_MAX_FILENAME_BYTES = 255
_UPLOAD_ROLES = frozenset({Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})


class WebMaterialUploadBlocked(WebRequestBlocked):
    """A Web material upload cannot safely progress.

    The exception text is intentionally generic: concrete scanner, object
    store and database details remain server-side operational data.
    """


class PostObjectStoreReconciliationRequired(WebMaterialUploadBlocked):
    """An uploaded private object must be reconciled before any retry.

    This is deliberately not a signal to delete an object.  The caller has an
    uncertain post-object-store state, so a server-only reconciler must first
    establish whether the immutable evidence binding committed.
    """


class UploadSlotStatus(str, Enum):
    """Persistent slot states; only the PostgreSQL transition trigger mutates them."""

    RESERVED = "RESERVED"
    CLAIMED = "CLAIMED"
    OBJECT_STORED = "OBJECT_STORED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class UploadSlotFailureCode(str, Enum):
    """Fixed non-sensitive terminal/recovery reasons retained server-side."""

    CONTENT_REJECTED = "CONTENT_REJECTED"
    OBJECT_STORE_FAILED = "OBJECT_STORE_FAILED"
    LEDGER_REJECTED = "LEDGER_REJECTED"
    OBJECT_STATE_UNKNOWN = "OBJECT_STATE_UNKNOWN"
    BINDING_STATE_UNKNOWN = "BINDING_STATE_UNKNOWN"
    COMPLETION_STATE_UNKNOWN = "COMPLETION_STATE_UNKNOWN"


@dataclass(frozen=True)
class WebMaterialUploadPolicy:
    """Small fixed limits for one private PDF upload reservation."""

    slot_lifetime: timedelta = timedelta(minutes=15)
    maximum_declared_content_length: int = _MAX_DECLARED_CONTENT_LENGTH

    def __post_init__(self) -> None:
        if not timedelta(minutes=1) <= self.slot_lifetime <= timedelta(hours=1):
            raise ValueError("Web material upload slot lifetime must be between one minute and one hour")
        if (
            type(self.maximum_declared_content_length) is not int
            or not 1 <= self.maximum_declared_content_length <= _MAX_DECLARED_CONTENT_LENGTH
        ):
            raise ValueError("Web material upload declared-size limit is invalid")


@dataclass(frozen=True)
class WebMaterialUploadSlot:
    """Server-only persistent upload slot and its private saga hand-off.

    ``source_object_key``, ``source_object_version_id`` and ``attempt_id``
    deliberately have redacted representations.  This record must never be
    serialized by an HTTP route, evidence snapshot, audit payload or outbox.
    """

    upload_id: str
    firm_id: str
    matter_id: str
    actor_id: str
    session_id: str
    expected_matter_version: int
    display_name: str
    declared_content_length: int | None
    status: UploadSlotStatus
    created_at: datetime
    expires_at: datetime
    attempt_id: str | None = field(default=None, repr=False)
    attempt_count: int = 0
    claimed_at: datetime | None = None
    admitted_upload_id: str | None = None
    admitted_content_sha256: str | None = None
    admitted_byte_size: int | None = None
    admitted_page_count: int | None = None
    admitted_inspection_hash: str | None = None
    scanner_name: str | None = None
    scanner_definitions_version: str | None = None
    source_object_key: str | None = field(default=None, repr=False)
    source_object_version_id: str | None = field(default=None, repr=False)
    source_reference_hash: str | None = None
    object_stored_at: datetime | None = None
    evidence_file_id: str | None = None
    evidence_matter_version: int | None = None
    evidence_audit_event_id: str | None = None
    completed_at: datetime | None = None
    failure_code: UploadSlotFailureCode | None = None
    failed_at: datetime | None = None
    reconciliation_required_at: datetime | None = None
    updated_at: datetime | None = None

    def completed_receipt(self) -> WebUploadReceipt:
        """Build the only result that may cross into the browser API."""

        if self.status is not UploadSlotStatus.COMPLETED:
            raise WebMaterialUploadBlocked("material upload has no completed receipt")
        if (
            self.evidence_file_id is None
            or self.admitted_content_sha256 is None
            or self.admitted_page_count is None
            or self.evidence_matter_version is None
        ):
            raise WebMaterialUploadBlocked("material upload completion record is invalid")
        return WebUploadReceipt(
            evidence_file_id=self.evidence_file_id,
            display_name=self.display_name,
            content_sha256=self.admitted_content_sha256,
            page_count=self.admitted_page_count,
            matter_version=self.evidence_matter_version,
        )

    def stored_object(self) -> StoredWebEvidenceOriginal:
        """Rehydrate an object handle only for server-side ledger recovery."""

        if (
            self.status not in {UploadSlotStatus.OBJECT_STORED, UploadSlotStatus.COMPLETED}
            or self.source_object_key is None
            or self.admitted_content_sha256 is None
            or self.admitted_byte_size is None
        ):
            raise WebMaterialUploadBlocked("material upload has no recoverable private object")
        return StoredWebEvidenceOriginal(
            object_key=self.source_object_key,
            content_sha256=self.admitted_content_sha256,
            byte_size=self.admitted_byte_size,
            object_version_id=self.source_object_version_id,
        )

    def admitted_metadata(self) -> AdmittedWebPdfUpload:
        """Rebuild validated metadata for an idempotent ledger bind retry.

        The evidence store does not read this path during registration; the
        object store has already reverified and durably recorded the immutable
        object.  A non-existent private sentinel therefore prevents an old
        staging path from being retained or accidentally reopened.
        """

        required = (
            self.admitted_upload_id,
            self.admitted_content_sha256,
            self.admitted_byte_size,
            self.admitted_page_count,
            self.admitted_inspection_hash,
            self.scanner_name,
            self.scanner_definitions_version,
        )
        if any(value is None for value in required):
            raise WebMaterialUploadBlocked("material upload admitted metadata is unavailable")
        return AdmittedWebPdfUpload(
            upload_id=self.admitted_upload_id or "",
            display_name=self.display_name,
            byte_size=self.admitted_byte_size or 0,
            content_sha256=self.admitted_content_sha256 or "",
            media_type="application/pdf",
            page_count=self.admitted_page_count or 0,
            inspection_hash=self.admitted_inspection_hash or "",
            scanner_name=self.scanner_name or "",
            scanner_definitions_version=self.scanner_definitions_version or "",
            path=Path("/private/web-material-upload-slot-metadata.pdf"),
        )


class WebMaterialUploadSlotStore(Protocol):
    """Persistent, tenant-scoped state machine for Web upload slots."""

    def reserve_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        upload_id: str,
        display_name: str,
        declared_content_length: int | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> WebMaterialUploadSlot: ...

    def claim_or_resume(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        now: datetime,
    ) -> WebMaterialUploadSlot | None: ...

    def record_object_stored(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        admitted: AdmittedWebPdfUpload,
        stored_object: StoredWebEvidenceOriginal,
        now: datetime,
    ) -> WebMaterialUploadSlot: ...

    def complete_slot(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        registered: RegisteredWebEvidenceOriginal,
        now: datetime,
    ) -> WebMaterialUploadSlot: ...

    def fail_slot(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
        now: datetime,
    ) -> None: ...

    def require_reconciliation(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
        now: datetime,
    ) -> None: ...

    def get_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> WebMaterialUploadSlot | None: ...


class _EvidenceUploadBindingPort(Protocol):
    def register_web_uploaded_pdf_original(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        upload: AdmittedWebPdfUpload,
        stored_object: StoredWebEvidenceOriginal,
    ) -> RegisteredWebEvidenceOriginal: ...

    def is_web_uploaded_object_bound(
        self,
        *,
        matter_id: str,
        actor: Actor,
        stored_object: StoredWebEvidenceOriginal,
    ) -> bool: ...


class _PrivateObjectStorePort(Protocol):
    def put_verified_pdf(
        self,
        upload: AdmittedWebPdfUpload,
        *,
        firm_id: str,
        matter_id: str,
    ) -> StoredWebEvidenceOriginal: ...

    def delete_unbound_upload_object(self, stored: StoredWebEvidenceOriginal) -> None: ...


class WebMaterialUploadService:
    """Coordinate one browser PDF upload through private server boundaries.

    Short database operations surround, but never span, the large stream,
    scanner and object-store calls.  Once an object has been stored, the slot
    first durably records its server-only hand-off and only then invokes the
    atomic evidence registration.  This permits a safe idempotent bind retry
    and prevents a browser retry from submitting a second body.
    """

    def __init__(
        self,
        *,
        slot_store: WebMaterialUploadSlotStore,
        staging: WebUploadStagingArea,
        scanner: object,
        object_store: _PrivateObjectStorePort,
        evidence_store: _EvidenceUploadBindingPort,
        system_worker_for_firm: Callable[[str], Actor],
        policy: WebMaterialUploadPolicy = WebMaterialUploadPolicy(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for method in (
            "reserve_slot",
            "claim_or_resume",
            "record_object_stored",
            "complete_slot",
            "fail_slot",
            "require_reconciliation",
            "get_slot",
        ):
            if not callable(getattr(slot_store, method, None)):
                raise ValueError("Web material upload slot store is invalid")
        if not isinstance(staging, WebUploadStagingArea):
            raise ValueError("Web material upload staging area is required")
        if not callable(getattr(scanner, "scan", None)):
            raise ValueError("Web material upload scanner is invalid")
        for method in ("put_verified_pdf", "delete_unbound_upload_object"):
            if not callable(getattr(object_store, method, None)):
                raise ValueError("Web material upload object store is invalid")
        for method in ("register_web_uploaded_pdf_original", "is_web_uploaded_object_bound"):
            if not callable(getattr(evidence_store, method, None)):
                raise ValueError("Web material upload evidence store is invalid")
        if not callable(system_worker_for_firm):
            raise ValueError("Web material upload system worker resolver is invalid")
        if not isinstance(policy, WebMaterialUploadPolicy):
            raise ValueError("Web material upload policy is invalid")
        self._slot_store = slot_store
        self._staging = staging
        self._scanner = scanner
        self._object_store = object_store
        self._evidence_store = evidence_store
        self._system_worker_for_firm = system_worker_for_firm
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        _server_now(self._clock())

    def create_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        client_filename: str,
        declared_content_length: int | None,
    ) -> WebUploadSlotResponse:
        """Reserve an opaque, short-lived, server-owned upload identifier."""

        now = _validate_upload_identity(identity=identity, clock=self._clock)
        display_name = _safe_display_name(client_filename)
        _validate_declared_content_length(
            declared_content_length,
            maximum=self._policy.maximum_declared_content_length,
        )
        if type(expected_version) is not int or expected_version < 1:
            raise WebMaterialUploadBlocked("material upload version is invalid")
        _validate_uuid(matter_id, label="material upload matter")
        expires_at = min(now + self._policy.slot_lifetime, _server_datetime(identity.expires_at, label="identity expiry"))
        if expires_at <= now:
            raise PersistentAuthenticationBlocked("Web material upload session has expired")
        slot = self._slot_store.reserve_slot(
            identity=identity,
            matter_id=matter_id,
            expected_matter_version=expected_version,
            upload_id=str(uuid4()),
            display_name=display_name,
            declared_content_length=declared_content_length,
            created_at=now,
            expires_at=expires_at,
        )
        _validate_reserved_slot(slot, identity=identity, matter_id=matter_id)
        return WebUploadSlotResponse(upload_id=slot.upload_id, expires_at=slot.expires_at)

    def read_status(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> WebMaterialUploadStatusResponse:
        now = _validate_upload_identity(identity=identity, clock=self._clock)
        _validate_uuid(matter_id, label="material upload matter")
        _validate_uuid(upload_id, label="material upload identifier")
        slot = self._slot_store.get_slot(identity=identity, matter_id=matter_id, upload_id=upload_id)
        if slot is None:
            raise WebMaterialUploadBlocked("material upload status is unavailable")
        _validate_owned_slot(slot, identity=identity, matter_id=matter_id)
        if slot.status is UploadSlotStatus.COMPLETED:
            return WebMaterialUploadStatusResponse(
                operation_id=slot.upload_id, kind="PDF", status="COMPLETED", retry_allowed=False,
                receipt=slot.completed_receipt(),
            )
        if slot.status is UploadSlotStatus.RECONCILIATION_REQUIRED:
            return WebMaterialUploadStatusResponse(
                operation_id=slot.upload_id, kind="PDF", status="RECONCILIATION_REQUIRED", retry_allowed=False,
            )
        if slot.status is UploadSlotStatus.FAILED:
            return WebMaterialUploadStatusResponse(
                operation_id=slot.upload_id, kind="PDF", status="REJECTED", retry_allowed=False,
            )
        if slot.expires_at <= now and slot.status is UploadSlotStatus.RESERVED:
            return WebMaterialUploadStatusResponse(
                operation_id=slot.upload_id, kind="PDF", status="EXPIRED", retry_allowed=False,
            )
        return WebMaterialUploadStatusResponse(
            operation_id=slot.upload_id, kind="PDF", status="PROCESSING", retry_allowed=False,
        )

    async def accept_content(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        chunks: AsyncIterable[bytes],
    ) -> WebUploadReceipt:
        """Consume at most one body and bind its admitted PDF into evidence.

        A completed slot returns its prior browser-safe receipt without
        consuming a retry body.  A stored-but-unbound object retries only the
        deterministic ledger bind; it never rereads or reuploads browser
        bytes.  In-progress, failed, expired and uncertain slots fail closed.
        """

        now = _validate_upload_identity(identity=identity, clock=self._clock)
        _validate_uuid(matter_id, label="material upload matter")
        _validate_uuid(upload_id, label="material upload identifier")
        if not hasattr(chunks, "__aiter__"):
            raise WebMaterialUploadBlocked("material upload content stream is invalid")
        slot = await asyncio.to_thread(
            self._slot_store.claim_or_resume,
            identity=identity,
            matter_id=matter_id,
            upload_id=upload_id,
            now=now,
        )
        if slot is None:
            raise WebMaterialUploadBlocked("material upload is unavailable, expired, or already being processed")
        _validate_owned_slot(slot, identity=identity, matter_id=matter_id)
        if slot.status is UploadSlotStatus.COMPLETED:
            return slot.completed_receipt()
        if slot.status is UploadSlotStatus.RECONCILIATION_REQUIRED:
            # A prior request already told the browser that its post-object
            # outcome is unknown.  Preserve that explicit pending-verification
            # signal on every same-session replay and, crucially, do not read
            # a second body which could create another original.
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            )
        if slot.status is UploadSlotStatus.OBJECT_STORED:
            return await self._bind_stored_object(identity=identity, slot=slot)
        if slot.status is not UploadSlotStatus.CLAIMED:
            raise WebMaterialUploadBlocked("material upload is unavailable, expired, or already being processed")
        return await self._stream_store_and_bind(identity=identity, slot=slot, chunks=chunks)

    async def _stream_store_and_bind(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        chunks: AsyncIterable[bytes],
    ) -> WebUploadReceipt:
        staged: object | None = None
        try:
            try:
                staged = await self._staging.stage_async_chunks(chunks, client_filename=slot.display_name)
                admitted = await asyncio.to_thread(self._staging.inspect_pdf, staged, scanner=self._scanner)
                staged = admitted
            except Exception:
                await self._record_known_failure(
                    identity=identity,
                    slot=slot,
                    code=UploadSlotFailureCode.CONTENT_REJECTED,
                )
                raise WebMaterialUploadBlocked("material upload could not be safely admitted") from None

            try:
                stored = await asyncio.to_thread(
                    self._object_store.put_verified_pdf,
                    admitted,
                    firm_id=identity.actor.firm_id,
                    matter_id=slot.matter_id,
                )
            except Exception:
                await self._record_known_failure(
                    identity=identity,
                    slot=slot,
                    code=UploadSlotFailureCode.OBJECT_STORE_FAILED,
                )
                raise WebMaterialUploadBlocked("material upload could not be safely stored") from None

            try:
                persisted = await asyncio.to_thread(
                    self._slot_store.record_object_stored,
                    identity=identity,
                    slot=slot,
                    admitted=admitted,
                    stored_object=stored,
                    now=_server_now(self._clock()),
                )
            except Exception:
                # An object now exists but the durable hand-off is unknown.
                # Never delete it: a transaction outcome may be ambiguous.
                await self._record_reconciliation(
                    identity=identity,
                    slot=slot,
                    code=UploadSlotFailureCode.OBJECT_STATE_UNKNOWN,
                )
                raise PostObjectStoreReconciliationRequired(
                    "material upload requires server-side reconciliation"
                ) from None
            _validate_owned_slot(persisted, identity=identity, matter_id=slot.matter_id)
            if persisted.status is not UploadSlotStatus.OBJECT_STORED:
                await self._record_reconciliation(
                    identity=identity,
                    slot=slot,
                    code=UploadSlotFailureCode.OBJECT_STATE_UNKNOWN,
                )
                raise PostObjectStoreReconciliationRequired(
                    "material upload requires server-side reconciliation"
                )
            return await self._bind_stored_object(identity=identity, slot=persisted)
        finally:
            if staged is not None:
                try:
                    await asyncio.to_thread(self._staging.discard, staged)
                except Exception:
                    # A private staging cleanup failure must not change a
                    # successfully committed evidence receipt into a browser
                    # error.  Deployment cleanup monitoring handles leftovers.
                    pass

    async def _bind_stored_object(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
    ) -> WebUploadReceipt:
        _validate_owned_slot(slot, identity=identity, matter_id=slot.matter_id)
        if slot.status is not UploadSlotStatus.OBJECT_STORED:
            raise WebMaterialUploadBlocked("material upload object is not bindable")
        # Revalidate a live session immediately before the state-changing
        # evidence command, not only before accepting a potentially long body.
        _validate_upload_identity(identity=identity, clock=self._clock)
        admitted = slot.admitted_metadata()
        stored = slot.stored_object()
        try:
            registered = await asyncio.to_thread(
                self._evidence_store.register_web_uploaded_pdf_original,
                matter_id=slot.matter_id,
                actor=identity.actor,
                expected_version=slot.expected_matter_version,
                idempotency_key=f"web-material-upload:{slot.upload_id}:bind",
                upload=admitted,
                stored_object=stored,
            )
            _validate_registered_binding(registered, slot=slot)
        except Exception as error:
            return await self._resolve_failed_binding(identity=identity, slot=slot, stored=stored, error=error)

        try:
            completed = await asyncio.to_thread(
                self._slot_store.complete_slot,
                identity=identity,
                slot=slot,
                registered=registered,
                now=_server_now(self._clock()),
            )
        except Exception:
            await self._record_reconciliation(
                identity=identity,
                slot=slot,
                code=UploadSlotFailureCode.COMPLETION_STATE_UNKNOWN,
            )
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            ) from None
        _validate_owned_slot(completed, identity=identity, matter_id=slot.matter_id)
        return completed.completed_receipt()

    async def _resolve_failed_binding(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        stored: StoredWebEvidenceOriginal,
        error: Exception,
    ) -> WebUploadReceipt:
        """Handle a ledger error without ever deleting an uncertain object."""

        try:
            worker = self._system_worker_for_firm(identity.actor.firm_id)
            _validate_system_worker(worker, firm_id=identity.actor.firm_id)
            is_bound = await asyncio.to_thread(
                self._evidence_store.is_web_uploaded_object_bound,
                matter_id=slot.matter_id,
                actor=worker,
                stored_object=stored,
            )
            if type(is_bound) is not bool:
                raise ValueError("evidence binding reconciliation result is invalid")
        except Exception:
            await self._record_reconciliation(
                identity=identity,
                slot=slot,
                code=UploadSlotFailureCode.BINDING_STATE_UNKNOWN,
            )
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            ) from None

        if is_bound:
            # Registration might have committed before a lost response.  The
            # private object is immutable evidence now, so deletion is never
            # an option; a server reconciler must obtain its safe receipt.
            await self._record_reconciliation(
                identity=identity,
                slot=slot,
                code=UploadSlotFailureCode.BINDING_STATE_UNKNOWN,
            )
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            ) from None

        try:
            await asyncio.to_thread(self._object_store.delete_unbound_upload_object, stored)
        except Exception:
            # A proven unbound object that could not be deleted is still an
            # orphan.  Preserve it for server cleanup rather than reusing or
            # overwriting it from a browser retry.
            await self._record_reconciliation(
                identity=identity,
                slot=slot,
                code=UploadSlotFailureCode.BINDING_STATE_UNKNOWN,
            )
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            ) from None
        await self._record_known_failure(
            identity=identity,
            slot=slot,
            code=UploadSlotFailureCode.LEDGER_REJECTED,
        )
        # Preserve established conflict/authorization/not-found semantics only
        # after the private object is proven unbound and terminalized.  All
        # other ledger internals remain a generic browser-safe rejection.
        if isinstance(error, (VersionConflict, PermissionError, KeyError, PersistentAuthenticationBlocked)):
            raise error
        raise WebMaterialUploadBlocked("material upload could not be bound to evidence")

    async def _record_known_failure(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._slot_store.fail_slot,
                identity=identity,
                slot=slot,
                code=code,
                now=_server_now(self._clock()),
            )
        except Exception:
            # If terminalization failed, keep the claim immutable and make no
            # optimistic assertion that the body can be replayed safely.
            raise PostObjectStoreReconciliationRequired(
                "material upload requires server-side reconciliation"
            ) from None

    async def _record_reconciliation(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._slot_store.require_reconciliation,
                identity=identity,
                slot=slot,
                code=code,
                now=_server_now(self._clock()),
            )
        except Exception:
            # The original state remains the safe outcome.  Do not replace an
            # uncertainty with a risky retry or private-object delete.
            pass


def _validate_upload_identity(
    *,
    identity: ServerIdentityContext,
    clock: Callable[[], datetime],
) -> datetime:
    now = _server_now(clock())
    if not isinstance(identity, ServerIdentityContext):
        raise PersistentAuthenticationBlocked("Web material upload requires a server identity")
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise PersistentAuthenticationBlocked("Web material upload requires OIDC MFA")
    try:
        identity.validate(now=now)
    except PersistentAuthenticationBlocked:
        raise
    actor = identity.actor
    _validate_uuid(actor.actor_id, label="material upload actor")
    _validate_uuid(actor.firm_id, label="material upload firm")
    _validate_uuid(identity.session_id, label="material upload session")
    if not actor.roles or not all(isinstance(role, Role) for role in actor.roles):
        raise PersistentAuthenticationBlocked("Web material upload actor roles are invalid")
    if Role.SYSTEM_WORKER in actor.roles:
        raise PersistentAuthenticationBlocked("Web material upload cannot use a system worker")
    if not actor.roles.intersection(_UPLOAD_ROLES):
        raise PermissionError("actor lacks a permitted role for Web material upload")
    return now


def _validate_system_worker(actor: Actor, *, firm_id: str) -> None:
    if not isinstance(actor, Actor):
        raise ValueError("Web material upload system worker is invalid")
    _validate_uuid(actor.actor_id, label="material upload system worker")
    if actor.firm_id != firm_id or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise ValueError("Web material upload system worker is invalid")


def _safe_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise WebMaterialUploadBlocked("material upload file name is invalid")
    candidate = PurePosixPath(value.replace("\\", "/")).name.strip()
    if candidate in {"", ".", ".."}:
        raise WebMaterialUploadBlocked("material upload file name is invalid")
    try:
        encoded = candidate.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WebMaterialUploadBlocked("material upload file name is invalid") from error
    if len(encoded) > _MAX_FILENAME_BYTES or any(
        character == "\x00" or category(character).startswith("C") for character in candidate
    ):
        raise WebMaterialUploadBlocked("material upload file name is invalid")
    return candidate


def _validate_declared_content_length(value: int | None, *, maximum: int) -> None:
    if value is None:
        return
    if type(value) is not int or value < 1 or value > maximum:
        raise WebMaterialUploadBlocked("material upload declared size is invalid")


def _validate_uuid(value: object, *, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebMaterialUploadBlocked(f"{label} is invalid") from error


def _server_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PersistentAuthenticationBlocked(f"{label} is invalid")
    normalized = value.astimezone(timezone.utc)
    try:
        normalized.timestamp()
    except (OverflowError, OSError, ValueError) as error:
        raise PersistentAuthenticationBlocked(f"{label} is invalid") from error
    return normalized


def _server_now(value: object) -> datetime:
    return _server_datetime(value, label="Web material upload clock")


def _validate_reserved_slot(
    slot: WebMaterialUploadSlot,
    *,
    identity: ServerIdentityContext,
    matter_id: str,
) -> None:
    _validate_owned_slot(slot, identity=identity, matter_id=matter_id)
    if slot.status is not UploadSlotStatus.RESERVED or slot.attempt_id is not None:
        raise WebMaterialUploadBlocked("material upload reservation is invalid")


def _validate_owned_slot(
    slot: WebMaterialUploadSlot,
    *,
    identity: ServerIdentityContext,
    matter_id: str,
) -> None:
    if not isinstance(slot, WebMaterialUploadSlot):
        raise WebMaterialUploadBlocked("material upload slot is invalid")
    for value, label in (
        (slot.upload_id, "material upload identifier"),
        (slot.firm_id, "material upload firm"),
        (slot.matter_id, "material upload matter"),
        (slot.actor_id, "material upload actor"),
        (slot.session_id, "material upload session"),
    ):
        _validate_uuid(value, label=label)
    if (
        slot.firm_id != identity.actor.firm_id
        or slot.actor_id != identity.actor.actor_id
        or slot.session_id != identity.session_id
        or slot.matter_id != matter_id
    ):
        raise WebMaterialUploadBlocked("material upload slot is unavailable")
    if type(slot.expected_matter_version) is not int or slot.expected_matter_version < 1:
        raise WebMaterialUploadBlocked("material upload slot is invalid")
    _safe_display_name(slot.display_name)
    _server_datetime(slot.created_at, label="material upload creation time")
    _server_datetime(slot.expires_at, label="material upload expiry")
    if slot.expires_at <= slot.created_at:
        raise WebMaterialUploadBlocked("material upload slot is invalid")


def _validate_registered_binding(registered: object, *, slot: WebMaterialUploadSlot) -> None:
    if not isinstance(registered, RegisteredWebEvidenceOriginal):
        raise WebMaterialUploadBlocked("evidence registration result is invalid")
    receipt = registered.receipt
    if (
        receipt.command_name != "REGISTER_WEB_UPLOADED_EVIDENCE_ORIGINAL"
        or receipt.matter_id != slot.matter_id
        or receipt.object_type != "EVIDENCE_ORIGINAL"
        or receipt.matter_version != slot.expected_matter_version + 1
        or slot.source_reference_hash is None
        or registered.source_reference_hash != slot.source_reference_hash
    ):
        raise WebMaterialUploadBlocked("evidence registration result is invalid")
    _validate_uuid(receipt.object_id, label="evidence original")
    _validate_uuid(receipt.audit_event_id, label="evidence audit event")
