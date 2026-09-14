"""Recoverable Web orchestration for admitted common case materials.

This module is intentionally route-free.  A future FastAPI composition should
bind the current OIDC/MFA identity, ``expected_version`` and two
``Idempotency-Key`` values (reservation and content hand-off), then pass the
raw request stream to :class:`WebCommonMaterialUploadService`.

The service owns a durable saga:

``RESERVED -> CLAIMED -> OBJECT_STORED -> COMPLETED``

After the private-object call begins, any unknown result becomes
``RECONCILIATION_REQUIRED``.  A browser replay never consumes another body.
Completion registers only an immutable ``material-object`` review candidate:
Office/text/email routes to ``CommonDocumentReader`` and JPEG/PNG routes to
the controlled visual/OCR skill.  No result from this service is a formal
fact, transaction, legal conclusion, evidence decision or court-ready file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from case_api.persistent_identity import (
    AuthenticationMethod,
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
)
from case_kernel.common_material_object_store import (
    CommonMaterialObjectStateUnknown,
    CommonMaterialObjectStoreBlocked,
    CommonMaterialPrivateObjectStorePort,
    StoredCommonMaterialOriginal,
)
from case_kernel.models import Role
from case_kernel.web_common_material_admission import (
    AdmittedCommonMaterial,
    CommonMaterialAdmissionPort,
    CommonMaterialAgentStatus,
    CommonMaterialContentRejected,
    CommonMaterialFormat,
    CommonMaterialReviewStatus,
    CommonMaterialRoute,
)


class WebCommonMaterialUploadBlocked(ValueError):
    """The common-material upload cannot safely progress."""


class CommonMaterialUploadReconciliationRequired(WebCommonMaterialUploadBlocked):
    """The prior object/ledger result is unknown; browser retransmit is forbidden."""


class CommonMaterialUploadStatus(StrEnum):
    RESERVED = "RESERVED"
    CLAIMED = "CLAIMED"
    OBJECT_STORED = "OBJECT_STORED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class CommonMaterialUploadFailureCode(StrEnum):
    CONTENT_REJECTED = "CONTENT_REJECTED"
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"
    OBJECT_STATE_UNKNOWN = "OBJECT_STATE_UNKNOWN"
    OBJECT_HANDOFF_UNKNOWN = "OBJECT_HANDOFF_UNKNOWN"
    REGISTRATION_STATE_UNKNOWN = "REGISTRATION_STATE_UNKNOWN"


@dataclass(frozen=True)
class WebCommonMaterialUploadPolicy:
    slot_lifetime: timedelta = timedelta(minutes=15)
    maximum_declared_byte_size: int = 100 * 1024 * 1024

    def __post_init__(self) -> None:
        if not timedelta(minutes=1) <= self.slot_lifetime <= timedelta(hours=1):
            raise ValueError("common material upload slot lifetime is invalid")
        if type(self.maximum_declared_byte_size) is not int or not 1 <= self.maximum_declared_byte_size <= 1024**3:
            raise ValueError("common material upload byte limit is invalid")


@dataclass(frozen=True)
class CommonMaterialUploadReservationReceipt:
    upload_id: str
    expires_at: datetime


@dataclass(frozen=True)
class CommonMaterialAdmissionReceipt:
    material_object_id: str
    display_name: str
    admitted_format: CommonMaterialFormat
    media_type: str
    byte_size: int
    content_sha256: str
    route: CommonMaterialRoute
    review_status: CommonMaterialReviewStatus
    agent_status: CommonMaterialAgentStatus
    agent_source_ref: str | None
    matter_version: int
    formal_fact: bool = False
    formal_transaction: bool = False
    legal_conclusion: bool = False
    evidence_decision: bool = False
    court_ready: bool = False


@dataclass(frozen=True)
class CommonMaterialUploadStatusReceipt:
    upload_id: str
    status: str
    retry_allowed: bool
    receipt: CommonMaterialAdmissionReceipt | None = None


@dataclass(frozen=True)
class CommonMaterialUploadOperation:
    """Server-only recovery record; private object values never cross HTTP."""

    upload_id: str
    material_object_id: str
    firm_id: str
    matter_id: str
    actor_id: str
    session_id: str
    expected_matter_version: int
    display_name: str
    declared_byte_size: int
    declared_media_type: str
    reserve_idempotency_key: str = field(repr=False)
    reserve_request_hash: str
    status: CommonMaterialUploadStatus
    created_at: datetime
    expires_at: datetime
    content_idempotency_key: str | None = field(default=None, repr=False)
    attempt_id: str | None = field(default=None, repr=False)
    attempt_count: int = 0
    claimed_at: datetime | None = None
    admitted_format: CommonMaterialFormat | None = None
    canonical_kind: str | None = None
    admitted_media_type: str | None = None
    route: CommonMaterialRoute | None = None
    admitted_byte_size: int | None = None
    admitted_content_sha256: str | None = None
    admitted_inspection_hash: str | None = None
    scanner_name: str | None = None
    scanner_definitions_version: str | None = None
    review_flags: tuple[str, ...] = ()
    source_object_key: str | None = field(default=None, repr=False)
    source_object_version_id: str | None = field(default=None, repr=False)
    source_reference_hash: str | None = None
    object_stored_at: datetime | None = None
    result_matter_version: int | None = None
    agent_status: CommonMaterialAgentStatus | None = None
    agent_source_ref: str | None = None
    audit_event_id: str | None = None
    outbox_id: str | None = None
    completed_at: datetime | None = None
    failure_code: CommonMaterialUploadFailureCode | None = None
    terminal_at: datetime | None = None

    def browser_receipt(self) -> CommonMaterialAdmissionReceipt:
        if self.status is not CommonMaterialUploadStatus.COMPLETED:
            raise WebCommonMaterialUploadBlocked("common material has no completed receipt")
        if (
            self.admitted_format is None
            or self.admitted_media_type is None
            or self.route is None
            or self.admitted_byte_size is None
            or self.admitted_content_sha256 is None
            or self.result_matter_version is None
            or self.agent_status is None
        ):
            raise WebCommonMaterialUploadBlocked("common material completion record is incomplete")
        if self.agent_status is CommonMaterialAgentStatus.AGENT_READY:
            if self.admitted_format in {CommonMaterialFormat.DOCX, CommonMaterialFormat.XLSX}:
                if self.agent_source_ref != f"material-object:{self.material_object_id}":
                    raise WebCommonMaterialUploadBlocked("common material Agent source binding differs")
            elif self.admitted_format in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}:
                prefix = "evidence-page:"
                if not isinstance(self.agent_source_ref, str) or not self.agent_source_ref.startswith(prefix):
                    raise WebCommonMaterialUploadBlocked("common material visual source binding is missing")
                _uuid(self.agent_source_ref.removeprefix(prefix), "common material evidence page")
            else:
                raise WebCommonMaterialUploadBlocked("common material format has no enabled Agent adapter")
        elif (
            self.agent_status is not CommonMaterialAgentStatus.INGESTED_PENDING_ADAPTER
            or self.agent_source_ref is not None
        ):
            raise WebCommonMaterialUploadBlocked("common material Agent readiness record is incomplete")
        return CommonMaterialAdmissionReceipt(
            material_object_id=self.material_object_id,
            display_name=self.display_name,
            admitted_format=self.admitted_format,
            media_type=self.admitted_media_type,
            byte_size=self.admitted_byte_size,
            content_sha256=self.admitted_content_sha256,
            route=self.route,
            review_status=CommonMaterialReviewStatus.NEEDS_LAWYER_REVIEW,
            agent_status=self.agent_status,
            agent_source_ref=self.agent_source_ref,
            matter_version=self.result_matter_version,
        )


class CommonMaterialUploadStorePort(Protocol):
    """PostgreSQL contract for the Web/API composition and reconciler."""

    def reserve_upload(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        upload_id: str,
        material_object_id: str,
        display_name: str,
        declared_byte_size: int,
        declared_media_type: str,
        idempotency_key: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> CommonMaterialUploadOperation: ...

    def claim_or_resume(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        content_idempotency_key: str,
        now: datetime,
    ) -> CommonMaterialUploadOperation | None: ...

    def record_object_stored(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        admitted: AdmittedCommonMaterial,
        stored: StoredCommonMaterialOriginal,
        now: datetime,
    ) -> CommonMaterialUploadOperation: ...

    def complete_registration(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        now: datetime,
    ) -> CommonMaterialUploadOperation: ...

    def fail_upload(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        failure_code: CommonMaterialUploadFailureCode,
        now: datetime,
    ) -> None: ...

    def require_reconciliation(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        failure_code: CommonMaterialUploadFailureCode,
        admitted: AdmittedCommonMaterial | None = None,
        stored: StoredCommonMaterialOriginal | None = None,
        now: datetime,
    ) -> None: ...

    def get_upload(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> CommonMaterialUploadOperation | None: ...


class WebCommonMaterialUploadService:
    def __init__(
        self,
        *,
        store: CommonMaterialUploadStorePort,
        staging: CommonMaterialAdmissionPort,
        scanner: object,
        object_store: CommonMaterialPrivateObjectStorePort,
        policy: WebCommonMaterialUploadPolicy = WebCommonMaterialUploadPolicy(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for method in (
            "reserve_upload", "claim_or_resume", "record_object_stored",
            "complete_registration", "fail_upload", "require_reconciliation",
            "get_upload",
        ):
            if not callable(getattr(store, method, None)):
                raise ValueError("common material upload store is invalid")
        for method in ("stage_async_chunks", "admit", "discard"):
            if not callable(getattr(staging, method, None)):
                raise ValueError("common material staging adapter is invalid")
        if not callable(getattr(scanner, "scan", None)):
            raise ValueError("common material scanner is invalid")
        if not callable(getattr(object_store, "put_immutable_common_material", None)):
            raise ValueError("common material object store is invalid")
        if not isinstance(policy, WebCommonMaterialUploadPolicy):
            raise ValueError("common material upload policy is invalid")
        self._store = store
        self._staging = staging
        self._scanner = scanner
        self._object_store = object_store
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        _server_time(self._clock())

    def create_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        client_filename: str,
        declared_byte_size: int,
        declared_media_type: str,
        idempotency_key: str,
    ) -> CommonMaterialUploadReservationReceipt:
        now = _validate_identity(identity, clock=self._clock)
        _uuid(matter_id, "common material matter")
        if type(expected_version) is not int or expected_version < 1:
            raise WebCommonMaterialUploadBlocked("common material expected version is invalid")
        if type(declared_byte_size) is not int or not 1 <= declared_byte_size <= self._policy.maximum_declared_byte_size:
            raise WebCommonMaterialUploadBlocked("common material declared size is invalid")
        _idempotency_key(idempotency_key)
        expires_at = min(now + self._policy.slot_lifetime, _server_time(identity.expires_at))
        if expires_at <= now:
            raise PersistentAuthenticationBlocked("common material upload session has expired")
        operation = self._store.reserve_upload(
            identity=identity,
            matter_id=matter_id,
            expected_matter_version=expected_version,
            upload_id=str(uuid4()),
            material_object_id=str(uuid4()),
            display_name=client_filename,
            declared_byte_size=declared_byte_size,
            declared_media_type=declared_media_type,
            idempotency_key=idempotency_key,
            created_at=now,
            expires_at=expires_at,
        )
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        if operation.status is not CommonMaterialUploadStatus.RESERVED:
            raise WebCommonMaterialUploadBlocked("common material reservation is invalid")
        return CommonMaterialUploadReservationReceipt(operation.upload_id, operation.expires_at)

    def read_status(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> CommonMaterialUploadStatusReceipt:
        now = _validate_identity(identity, clock=self._clock)
        _uuid(matter_id, "common material matter")
        _uuid(upload_id, "common material upload")
        operation = self._store.get_upload(
            identity=identity,
            matter_id=matter_id,
            upload_id=upload_id,
        )
        if operation is None:
            raise WebCommonMaterialUploadBlocked("common material upload status is unavailable")
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        if operation.status is CommonMaterialUploadStatus.COMPLETED:
            return CommonMaterialUploadStatusReceipt(
                upload_id=upload_id,
                status="COMPLETED",
                retry_allowed=False,
                receipt=operation.browser_receipt(),
            )
        if operation.status is CommonMaterialUploadStatus.RECONCILIATION_REQUIRED:
            return CommonMaterialUploadStatusReceipt(upload_id, "RECONCILIATION_REQUIRED", False)
        if operation.status is CommonMaterialUploadStatus.FAILED:
            public_status = (
                "ADMISSION_UNAVAILABLE"
                if operation.failure_code is CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE
                else "REJECTED"
            )
            return CommonMaterialUploadStatusReceipt(upload_id, public_status, False)
        if operation.status is CommonMaterialUploadStatus.RESERVED and operation.expires_at <= now:
            return CommonMaterialUploadStatusReceipt(upload_id, "EXPIRED", False)
        return CommonMaterialUploadStatusReceipt(upload_id, "PROCESSING", False)

    async def accept_content(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        idempotency_key: str,
        chunks: AsyncIterable[bytes],
    ) -> CommonMaterialAdmissionReceipt:
        now = _validate_identity(identity, clock=self._clock)
        _uuid(matter_id, "common material matter")
        _uuid(upload_id, "common material upload")
        _idempotency_key(idempotency_key)
        if not hasattr(chunks, "__aiter__"):
            raise WebCommonMaterialUploadBlocked("common material stream is invalid")
        operation = await asyncio.to_thread(
            self._store.claim_or_resume,
            identity=identity,
            matter_id=matter_id,
            upload_id=upload_id,
            content_idempotency_key=idempotency_key,
            now=now,
        )
        if operation is None:
            raise WebCommonMaterialUploadBlocked("common material upload is unavailable or already processing")
        _validate_owned(operation, identity=identity, matter_id=matter_id)
        if operation.status is CommonMaterialUploadStatus.COMPLETED:
            return operation.browser_receipt()
        if operation.status is CommonMaterialUploadStatus.RECONCILIATION_REQUIRED:
            raise CommonMaterialUploadReconciliationRequired(
                "common material upload requires server reconciliation"
            )
        if operation.status is CommonMaterialUploadStatus.OBJECT_STORED:
            return await self._complete(identity=identity, operation=operation)
        if operation.status is not CommonMaterialUploadStatus.CLAIMED:
            raise WebCommonMaterialUploadBlocked("common material upload cannot consume another body")
        return await self._stream_store_complete(
            identity=identity,
            operation=operation,
            chunks=chunks,
        )

    async def _stream_store_complete(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        chunks: AsyncIterable[bytes],
    ) -> CommonMaterialAdmissionReceipt:
        staged: object | None = None
        try:
            try:
                staged = await self._staging.stage_async_chunks(
                    chunks,
                    client_filename=operation.display_name,
                    declared_byte_size=operation.declared_byte_size,
                    declared_media_type=operation.declared_media_type,
                )
                admitted = await asyncio.to_thread(
                    self._staging.admit,
                    staged,
                    material_object_id=operation.material_object_id,
                    scanner=self._scanner,
                )
                staged = admitted
            except CommonMaterialContentRejected:
                await self._known_failure(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.CONTENT_REJECTED,
                )
                raise WebCommonMaterialUploadBlocked(
                    "common material content could not be safely admitted"
                ) from None
            except Exception:
                await self._known_failure(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE,
                )
                raise WebCommonMaterialUploadBlocked(
                    "common material admission service is unavailable"
                ) from None

            try:
                stored = await asyncio.to_thread(
                    self._object_store.put_immutable_common_material,
                    admitted,
                    firm_id=identity.actor.firm_id,
                    matter_id=operation.matter_id,
                )
            except CommonMaterialObjectStoreBlocked as error:
                if isinstance(error, CommonMaterialObjectStateUnknown):
                    await self._unknown_state(
                        identity=identity,
                        operation=operation,
                        code=CommonMaterialUploadFailureCode.OBJECT_STATE_UNKNOWN,
                        admitted=admitted,
                    )
                    raise CommonMaterialUploadReconciliationRequired(
                        "common material object state is unknown"
                    ) from None
                await self._known_failure(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE,
                )
                raise WebCommonMaterialUploadBlocked(
                    "common material object service is unavailable"
                ) from None
            except Exception:
                await self._unknown_state(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.OBJECT_STATE_UNKNOWN,
                    admitted=admitted,
                )
                raise CommonMaterialUploadReconciliationRequired(
                    "common material object state is unknown"
                ) from None

            try:
                persisted = await asyncio.to_thread(
                    self._store.record_object_stored,
                    identity=identity,
                    operation=operation,
                    admitted=admitted,
                    stored=stored,
                    now=_server_time(self._clock()),
                )
            except Exception:
                await self._unknown_state(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.OBJECT_HANDOFF_UNKNOWN,
                    admitted=admitted,
                    stored=stored,
                )
                raise CommonMaterialUploadReconciliationRequired(
                    "common material object hand-off is unknown"
                ) from None
            if persisted.status is not CommonMaterialUploadStatus.OBJECT_STORED:
                await self._unknown_state(
                    identity=identity,
                    operation=operation,
                    code=CommonMaterialUploadFailureCode.OBJECT_HANDOFF_UNKNOWN,
                    admitted=admitted,
                    stored=stored,
                )
                raise CommonMaterialUploadReconciliationRequired(
                    "common material object hand-off is unknown"
                )
            return await self._complete(identity=identity, operation=persisted)
        finally:
            if staged is not None:
                try:
                    await asyncio.to_thread(self._staging.discard, staged)
                except Exception:
                    pass

    async def _complete(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
    ) -> CommonMaterialAdmissionReceipt:
        _validate_identity(identity, clock=self._clock)
        try:
            completed = await asyncio.to_thread(
                self._store.complete_registration,
                identity=identity,
                operation=operation,
                now=_server_time(self._clock()),
            )
        except Exception:
            await self._unknown_state(
                identity=identity,
                operation=operation,
                code=CommonMaterialUploadFailureCode.REGISTRATION_STATE_UNKNOWN,
            )
            raise CommonMaterialUploadReconciliationRequired(
                "common material registration state is unknown"
            ) from None
        _validate_owned(completed, identity=identity, matter_id=operation.matter_id)
        return completed.browser_receipt()

    async def _known_failure(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        code: CommonMaterialUploadFailureCode,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._store.fail_upload,
                identity=identity,
                operation=operation,
                failure_code=code,
                now=_server_time(self._clock()),
            )
        except Exception:
            raise CommonMaterialUploadReconciliationRequired(
                "common material failure state requires reconciliation"
            ) from None

    async def _unknown_state(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        code: CommonMaterialUploadFailureCode,
        admitted: AdmittedCommonMaterial | None = None,
        stored: StoredCommonMaterialOriginal | None = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._store.require_reconciliation,
                identity=identity,
                operation=operation,
                failure_code=code,
                admitted=admitted,
                stored=stored,
                now=_server_time(self._clock()),
            )
        except Exception:
            pass


def _validate_identity(
    identity: object,
    *,
    clock: Callable[[], datetime],
) -> datetime:
    now = _server_time(clock())
    if not isinstance(identity, ServerIdentityContext):
        raise PersistentAuthenticationBlocked("common material upload requires a server identity")
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise PersistentAuthenticationBlocked("common material upload requires OIDC MFA")
    identity.validate(now=now)
    actor = identity.actor
    _uuid(actor.actor_id, "common material actor")
    _uuid(actor.firm_id, "common material firm")
    _uuid(identity.session_id, "common material session")
    allowed = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
    if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(allowed):
        raise PermissionError("actor lacks a permitted common material upload role")
    return now


def _validate_owned(
    operation: object,
    *,
    identity: ServerIdentityContext,
    matter_id: str,
) -> None:
    if not isinstance(operation, CommonMaterialUploadOperation):
        raise WebCommonMaterialUploadBlocked("common material upload operation is invalid")
    for value, label in (
        (operation.upload_id, "common material upload"),
        (operation.material_object_id, "common material object"),
        (operation.firm_id, "common material firm"),
        (operation.matter_id, "common material matter"),
        (operation.actor_id, "common material actor"),
        (operation.session_id, "common material session"),
    ):
        _uuid(value, label)
    if (
        operation.firm_id != identity.actor.firm_id
        or operation.matter_id != matter_id
        or operation.actor_id != identity.actor.actor_id
        or operation.session_id != identity.session_id
    ):
        raise WebCommonMaterialUploadBlocked("common material upload operation is unavailable")
    if type(operation.expected_matter_version) is not int or operation.expected_matter_version < 1:
        raise WebCommonMaterialUploadBlocked("common material upload expected version is invalid")
    _server_time(operation.created_at)
    _server_time(operation.expires_at)


def _idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 8 <= len(value.encode("ascii", errors="ignore")) <= 160
        or len(value.encode("ascii", errors="ignore")) != len(value)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise WebCommonMaterialUploadBlocked("common material idempotency key is invalid")
    return value


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebCommonMaterialUploadBlocked(f"{label} is invalid") from error


def _server_time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PersistentAuthenticationBlocked("common material server time is invalid")
    normalized = value.astimezone(timezone.utc)
    try:
        normalized.timestamp()
    except (OverflowError, OSError, ValueError) as error:
        raise PersistentAuthenticationBlocked("common material server time is invalid") from error
    return normalized


__all__ = (
    "CommonMaterialAdmissionReceipt",
    "CommonMaterialUploadFailureCode",
    "CommonMaterialUploadOperation",
    "CommonMaterialUploadReconciliationRequired",
    "CommonMaterialUploadReservationReceipt",
    "CommonMaterialUploadStatus",
    "CommonMaterialUploadStatusReceipt",
    "CommonMaterialUploadStorePort",
    "WebCommonMaterialUploadBlocked",
    "WebCommonMaterialUploadPolicy",
    "WebCommonMaterialUploadService",
)
