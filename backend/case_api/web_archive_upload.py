"""Recoverable Web ZIP material admission.

An accepted archive is an immutable private object with a server-side
inventory.  This vertical intentionally stops at ``STORED_PENDING_PROCESSING``:
the child-PDF worker must later rescan and bind each child through the ordinary
PDF upload/evidence saga.  A browser retry never sends a second archive body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid4

from case_api.persistent_identity import AuthenticationMethod, PersistentAuthenticationBlocked, ServerIdentityContext
from case_api.web_app import WebMaterialArchiveReceipt, WebMaterialArchiveSlotResponse, WebMaterialUploadStatusResponse, WebRequestBlocked
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebMaterialArchive
from case_kernel.web_zip_staging import AdmittedWebZip, WebZipStagingArea


class WebMaterialArchiveBlocked(WebRequestBlocked):
    """The archive cannot safely enter the server-owned queue."""


class ArchiveObjectStateUnknown(WebMaterialArchiveBlocked):
    """The private object or its durable hand-off has an unknown outcome."""


class ArchiveUploadStatus(str, Enum):
    RESERVED = "RESERVED"
    CLAIMED = "CLAIMED"
    OBJECT_STORED = "OBJECT_STORED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class WebMaterialArchiveOperation:
    archive_id: str
    firm_id: str
    matter_id: str
    actor_id: str
    session_id: str
    expected_matter_version: int
    display_name: str
    declared_content_length: int | None
    status: ArchiveUploadStatus
    created_at: datetime
    expires_at: datetime
    attempt_id: str | None = field(default=None, repr=False)
    attempt_count: int = 0
    archive_content_sha256: str | None = None
    archive_byte_size: int | None = None
    entry_count: int | None = None
    expanded_byte_size: int | None = None
    inventory: tuple[dict[str, object], ...] = ()
    source_object_key: str | None = field(default=None, repr=False)
    source_object_version_id: str | None = field(default=None, repr=False)
    source_reference_hash: str | None = None
    object_stored_at: datetime | None = None
    reconciliation_required_at: datetime | None = None

    def receipt(self) -> WebMaterialArchiveReceipt:
        if self.status is not ArchiveUploadStatus.OBJECT_STORED:
            raise WebMaterialArchiveBlocked("material archive has no completed server receipt")
        required = (self.archive_content_sha256, self.archive_byte_size, self.entry_count, self.expanded_byte_size)
        if any(value is None for value in required):
            raise WebMaterialArchiveBlocked("material archive completion record is invalid")
        return WebMaterialArchiveReceipt(
            archive_id=self.archive_id,
            display_name=self.display_name,
            content_sha256=self.archive_content_sha256,
            byte_size=self.archive_byte_size,
            entry_count=self.entry_count,
            expanded_byte_size=self.expanded_byte_size,
            processing_status="STORED_PENDING_PROCESSING",
        )

    def stored_object(self) -> StoredWebMaterialArchive:
        if self.status is not ArchiveUploadStatus.OBJECT_STORED or self.source_object_key is None:
            raise WebMaterialArchiveBlocked("material archive has no recoverable private object")
        required = (self.archive_content_sha256, self.archive_byte_size, self.entry_count, self.expanded_byte_size)
        if any(value is None for value in required):
            raise WebMaterialArchiveBlocked("material archive object metadata is invalid")
        return StoredWebMaterialArchive(
            object_key=self.source_object_key,
            content_sha256=self.archive_content_sha256,
            byte_size=self.archive_byte_size,
            entry_count=self.entry_count,
            expanded_byte_size=self.expanded_byte_size,
            object_version_id=self.source_object_version_id,
        )


class WebMaterialArchiveStore(Protocol):
    def reserve_archive(self, *, identity: ServerIdentityContext, matter_id: str, expected_matter_version: int,
                        archive_id: str, display_name: str, declared_content_length: int | None,
                        created_at: datetime, expires_at: datetime) -> WebMaterialArchiveOperation: ...

    def claim_or_resume(self, *, identity: ServerIdentityContext, matter_id: str, archive_id: str,
                        now: datetime) -> WebMaterialArchiveOperation | None: ...

    def record_object_stored(self, *, identity: ServerIdentityContext, operation: WebMaterialArchiveOperation,
                             admitted: AdmittedWebZip, stored_object: StoredWebMaterialArchive,
                             now: datetime) -> WebMaterialArchiveOperation: ...

    def require_reconciliation(self, *, identity: ServerIdentityContext, operation: WebMaterialArchiveOperation,
                               now: datetime) -> None: ...

    def get_operation(self, *, identity: ServerIdentityContext, matter_id: str,
                      archive_id: str) -> WebMaterialArchiveOperation | None: ...


class _ArchiveObjectStorePort(Protocol):
    def put_verified_zip(self, archive: AdmittedWebZip, *, firm_id: str, matter_id: str) -> StoredWebMaterialArchive: ...


class WebMaterialArchiveUploadService:
    def __init__(self, *, store: WebMaterialArchiveStore, staging: WebZipStagingArea,
                 object_store: _ArchiveObjectStorePort, slot_lifetime: timedelta = timedelta(minutes=15),
                 clock: Callable[[], datetime] | None = None) -> None:
        if not isinstance(staging, WebZipStagingArea):
            raise ValueError("ZIP staging area is required")
        for method in ("reserve_archive", "claim_or_resume", "record_object_stored", "require_reconciliation", "get_operation"):
            if not callable(getattr(store, method, None)):
                raise ValueError("ZIP archive store is invalid")
        if not callable(getattr(object_store, "put_verified_zip", None)):
            raise ValueError("ZIP archive object store is invalid")
        if not timedelta(minutes=1) <= slot_lifetime <= timedelta(hours=1):
            raise ValueError("ZIP archive slot lifetime is invalid")
        self._store, self._staging, self._object_store = store, staging, object_store
        self._slot_lifetime, self._clock = slot_lifetime, clock or (lambda: datetime.now(timezone.utc))

    def create_slot(self, *, identity: ServerIdentityContext, matter_id: str, expected_version: int,
                    client_filename: str, declared_content_length: int | None) -> WebMaterialArchiveSlotResponse:
        now = _validate_identity(identity, self._clock)
        if type(expected_version) is not int or expected_version < 1:
            raise WebMaterialArchiveBlocked("material archive version is invalid")
        _validate_uuid(matter_id, "material archive matter")
        expires = min(now + self._slot_lifetime, _aware(identity.expires_at, "identity expiry"))
        if expires <= now:
            raise PersistentAuthenticationBlocked("Web material archive session has expired")
        operation = self._store.reserve_archive(
            identity=identity, matter_id=matter_id, expected_matter_version=expected_version,
            archive_id=str(uuid4()), display_name=client_filename.strip(),
            declared_content_length=declared_content_length, created_at=now, expires_at=expires,
        )
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        return WebMaterialArchiveSlotResponse(archive_id=operation.archive_id, expires_at=operation.expires_at)

    def read_status(self, *, identity: ServerIdentityContext, matter_id: str,
                    archive_id: str) -> WebMaterialUploadStatusResponse:
        now = _validate_identity(identity, self._clock)
        _validate_uuid(matter_id, "material archive matter")
        _validate_uuid(archive_id, "material archive identifier")
        operation = self._store.get_operation(identity=identity, matter_id=matter_id, archive_id=archive_id)
        if operation is None:
            raise WebMaterialArchiveBlocked("material archive status is unavailable")
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        if operation.status is ArchiveUploadStatus.OBJECT_STORED:
            return WebMaterialUploadStatusResponse(
                operation_id=operation.archive_id, kind="ZIP", status="STORED_PENDING_PROCESSING", retry_allowed=False,
                receipt=operation.receipt(),
            )
        if operation.status is ArchiveUploadStatus.RECONCILIATION_REQUIRED:
            return WebMaterialUploadStatusResponse(
                operation_id=operation.archive_id, kind="ZIP", status="RECONCILIATION_REQUIRED", retry_allowed=False,
            )
        if operation.expires_at <= now and operation.status is ArchiveUploadStatus.RESERVED:
            status = "EXPIRED"
        else:
            status = "PROCESSING"
        return WebMaterialUploadStatusResponse(operation_id=operation.archive_id, kind="ZIP", status=status, retry_allowed=False)

    async def accept_content(self, *, identity: ServerIdentityContext, matter_id: str, archive_id: str,
                             chunks: AsyncIterable[bytes]) -> WebMaterialArchiveReceipt:
        now = _validate_identity(identity, self._clock)
        _validate_uuid(matter_id, "material archive matter")
        _validate_uuid(archive_id, "material archive identifier")
        if not hasattr(chunks, "__aiter__"):
            raise WebMaterialArchiveBlocked("material archive stream is invalid")
        operation = await asyncio.to_thread(self._store.claim_or_resume, identity=identity, matter_id=matter_id,
                                            archive_id=archive_id, now=now)
        if operation is None:
            raise WebMaterialArchiveBlocked("material archive is unavailable, expired, or already processing")
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        if operation.status is ArchiveUploadStatus.OBJECT_STORED:
            return operation.receipt()
        if operation.status is ArchiveUploadStatus.RECONCILIATION_REQUIRED:
            raise ArchiveObjectStateUnknown("material archive requires server-side reconciliation")
        if operation.status is not ArchiveUploadStatus.CLAIMED:
            raise WebMaterialArchiveBlocked("material archive is unavailable or already processing")
        staged: object | None = None
        try:
            try:
                staged = await self._staging.stage_async_chunks(chunks, client_filename=operation.display_name)
                admitted = await asyncio.to_thread(self._staging.inspect_zip, staged)
                staged = admitted
            except Exception:
                raise WebMaterialArchiveBlocked("material archive could not be safely admitted") from None
            try:
                stored = await asyncio.to_thread(self._object_store.put_verified_zip, admitted,
                                                 firm_id=identity.actor.firm_id, matter_id=matter_id)
            except Exception:
                raise WebMaterialArchiveBlocked("material archive could not be safely stored") from None
            try:
                persisted = await asyncio.to_thread(self._store.record_object_stored, identity=identity,
                                                    operation=operation, admitted=admitted, stored_object=stored,
                                                    now=_aware(self._clock(), "server clock"))
            except Exception:
                await asyncio.to_thread(self._store.require_reconciliation, identity=identity,
                                        operation=operation, now=_aware(self._clock(), "server clock"))
                raise ArchiveObjectStateUnknown("material archive requires server-side reconciliation") from None
            _validate_owned(persisted, identity=identity, matter_id=matter_id)
            if persisted.status is not ArchiveUploadStatus.OBJECT_STORED:
                raise ArchiveObjectStateUnknown("material archive requires server-side reconciliation")
            return persisted.receipt()
        finally:
            if staged is not None:
                try:
                    await asyncio.to_thread(self._staging.discard, staged)
                except Exception:
                    pass


def _validate_identity(identity: ServerIdentityContext, clock: Callable[[], datetime]) -> datetime:
    if not isinstance(identity, ServerIdentityContext) or identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise PersistentAuthenticationBlocked("Web material archive requires OIDC MFA")
    now = _aware(clock(), "server clock")
    identity.validate(now=now)
    actor = identity.actor
    _validate_uuid(actor.actor_id, "material archive actor")
    _validate_uuid(actor.firm_id, "material archive firm")
    _validate_uuid(identity.session_id, "material archive session")
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection({Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER}):
        raise PermissionError("actor lacks a permitted role for material archive upload")
    return now


def _validate_owned(operation: WebMaterialArchiveOperation, *, identity: ServerIdentityContext, matter_id: str) -> None:
    if not isinstance(operation, WebMaterialArchiveOperation) or operation.matter_id != matter_id:
        raise WebMaterialArchiveBlocked("material archive operation is invalid")
    if operation.firm_id != identity.actor.firm_id or operation.actor_id != identity.actor.actor_id or operation.session_id != identity.session_id:
        raise WebMaterialArchiveBlocked("material archive operation is not owned by this session")


def _validate_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise WebMaterialArchiveBlocked(f"{label} is invalid") from error


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebMaterialArchiveBlocked(f"{label} is invalid")
    return value.astimezone(timezone.utc)
