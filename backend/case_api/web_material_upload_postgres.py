"""PostgreSQL state machine for private Web material-upload slots.

This adapter uses the normal tenant application database role, never the
cookie/session gateway role.  Every transaction installs the server-derived
firm context before reading or changing a tenant row, and every operation
matches the actor and opaque server session stored at reservation time.

Private object keys are retained only while a server-owned upload saga needs
them for recovery.  They are never returned by the browser response types and
must not be selected into evidence snapshots, audit/outbox payloads or logs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_material_upload import (
    UploadSlotFailureCode,
    UploadSlotStatus,
    WebMaterialUploadBlocked,
    WebMaterialUploadSlot,
    WebMaterialUploadSlotStore,
)
from case_kernel.case_ledger_postgres import (
    CaseLedgerPersistenceBlocked,
    _authorize_and_lock_matter,
    _authorize_matter_read,
)
from case_kernel.evidence_manifest_postgres import RegisteredWebEvidenceOriginal
from case_kernel.errors import VersionConflict
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_upload_staging import AdmittedWebPdfUpload


__all__ = (
    "PostgresWebMaterialUploadSlotStore",
    "WebMaterialUploadPersistenceBlocked",
)


_MAX_DSN_LENGTH = 4_096
_MAX_FILENAME_BYTES = 255
_MAX_SCANNER_TEXT_BYTES = 160
_MAX_OBJECT_KEY_BYTES = 512
_MAX_OBJECT_VERSION_BYTES = 512
_UPLOAD_ROLES = frozenset({Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY = re.compile(
    r"^originals/v1/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{2}/[0-9a-f]{64}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$"
)

_SLOT_COLUMNS = """
    upload_id, firm_id, matter_id, actor_id, session_id,
    expected_matter_version, display_name, declared_content_length,
    status, attempt_id, attempt_count, created_at, expires_at, claimed_at,
    admitted_upload_id, admitted_content_sha256, admitted_byte_size,
    admitted_page_count, admitted_inspection_hash, scanner_name,
    scanner_definitions_version, source_object_key, source_object_version_id,
    source_reference_hash, object_stored_at, evidence_file_id,
    evidence_matter_version, evidence_audit_event_id, completed_at,
    failure_code, failed_at, reconciliation_required_at, updated_at
"""


class WebMaterialUploadPersistenceBlocked(WebMaterialUploadBlocked):
    """The slot database cannot safely honor a browser upload request."""


class PostgresWebMaterialUploadSlotStore(WebMaterialUploadSlotStore):
    """RLS-scoped durable saga state for one server-owned PDF upload.

    ``connection_factory`` is injected only for controlled composition and
    unit tests.  Production uses ``psycopg`` with mapping rows.  The adapter
    never opens a browser-controlled DSN or accepts client firm/role fields.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if (
            not isinstance(dsn, str)
            or not dsn
            or len(dsn) > _MAX_DSN_LENGTH
            or dsn != dsn.strip()
            or "\x00" in dsn
        ):
            raise ValueError("Web material upload PostgreSQL DSN is invalid")
        if connection_factory is not None and not callable(connection_factory):
            raise ValueError("Web material upload connection factory is invalid")
        self._dsn = dsn
        self._connection_factory = connection_factory or self._open_connection

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
    ) -> WebMaterialUploadSlot:
        actor = _validate_identity(identity, now=created_at)
        _validate_uuid(matter_id, label="Web material upload matter")
        _validate_uuid(upload_id, label="Web material upload identifier")
        _validate_version(expected_matter_version)
        _validate_display_name(display_name)
        _validate_declared_length(declared_content_length)
        _validate_timestamps(created_at=created_at, expires_at=expires_at)
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_and_lock_matter(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    expected_version=expected_matter_version,
                    allowed_roles=_UPLOAD_ROLES,
                )
                row = connection.execute(
                    f"""
                    INSERT INTO web_material_upload_slots (
                        upload_id, firm_id, matter_id, actor_id, session_id,
                        expected_matter_version, display_name,
                        declared_content_length, status, created_at, expires_at,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, 'RESERVED', %s, %s, %s
                    )
                    RETURNING {_SLOT_COLUMNS}
                    """,
                    (
                        upload_id,
                        actor.firm_id,
                        matter_id,
                        actor.actor_id,
                        identity.session_id,
                        expected_matter_version,
                        display_name,
                        declared_content_length,
                        created_at,
                        expires_at,
                        created_at,
                    ),
                ).fetchone()
                slot = _slot_from_row(row)
                _insert_slot_event(connection, slot=slot, event_type="RESERVED", occurred_at=created_at)
                return slot
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def claim_or_resume(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        now: datetime,
    ) -> WebMaterialUploadSlot | None:
        _validate_uuid(matter_id, label="Web material upload matter")
        _validate_uuid(upload_id, label="Web material upload identifier")
        current = _aware_datetime(now, label="Web material upload clock")
        actor = _validate_identity(identity, now=current)
        attempt_id = str(uuid4())
        try:
            with self._transaction(actor.firm_id) as connection:
                # Database membership is refreshed before the state CAS so a
                # revoked actor cannot start receiving a large stream.
                _authorize_matter_read(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    allowed_roles=_UPLOAD_ROLES,
                )
                claimed = connection.execute(
                    f"""
                    UPDATE web_material_upload_slots
                    SET status = 'CLAIMED', attempt_id = %s,
                        attempt_count = attempt_count + 1, claimed_at = %s,
                        updated_at = %s
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s
                      AND status = 'RESERVED' AND expires_at > %s
                    RETURNING {_SLOT_COLUMNS}
                    """,
                    (
                        attempt_id,
                        current,
                        current,
                        upload_id,
                        actor.firm_id,
                        matter_id,
                        actor.actor_id,
                        identity.session_id,
                        current,
                    ),
                ).fetchone()
                if claimed is not None:
                    slot = _slot_from_row(claimed)
                    _insert_slot_event(connection, slot=slot, event_type="CLAIMED", occurred_at=current)
                    return slot

                row = connection.execute(
                    f"""
                    SELECT {_SLOT_COLUMNS}
                    FROM web_material_upload_slots
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s
                    FOR UPDATE
                    """,
                    (upload_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                if row is None:
                    return None
                slot = _slot_from_row(row)
                if slot.status in {
                    UploadSlotStatus.COMPLETED,
                    UploadSlotStatus.OBJECT_STORED,
                    UploadSlotStatus.RECONCILIATION_REQUIRED,
                }:
                    return slot
                # ``CLAIMED`` intentionally remains unavailable rather than
                # consuming a second body after a lost request/process state.
                return None
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def get_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> WebMaterialUploadSlot | None:
        _validate_uuid(matter_id, label="Web material upload matter")
        _validate_uuid(upload_id, label="Web material upload identifier")
        actor = _validate_identity(identity)
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=_UPLOAD_ROLES)
                row = connection.execute(
                    f"""
                    SELECT {_SLOT_COLUMNS}
                    FROM web_material_upload_slots
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s
                    """,
                    (upload_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                return None if row is None else _slot_from_row(row)
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def record_object_stored(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        admitted: AdmittedWebPdfUpload,
        stored_object: StoredWebEvidenceOriginal,
        now: datetime,
    ) -> WebMaterialUploadSlot:
        current = _aware_datetime(now, label="Web material upload clock")
        actor = _validate_identity(identity, now=current)
        _validate_slot_ownership(slot, identity=identity)
        if slot.status is not UploadSlotStatus.CLAIMED or slot.attempt_id is None:
            raise WebMaterialUploadPersistenceBlocked("Web material upload slot is not claimable for object persistence")
        _validate_admitted_upload(admitted, display_name=slot.display_name)
        _validate_stored_object(stored_object, actor=actor, matter_id=slot.matter_id, admitted=admitted)
        source_reference_hash = sha256(stored_object.object_key.encode("ascii")).hexdigest()
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(
                    connection,
                    actor=actor,
                    matter_id=slot.matter_id,
                    allowed_roles=_UPLOAD_ROLES,
                )
                row = connection.execute(
                    f"""
                    UPDATE web_material_upload_slots
                    SET status = 'OBJECT_STORED', admitted_upload_id = %s,
                        admitted_content_sha256 = %s, admitted_byte_size = %s,
                        admitted_page_count = %s, admitted_inspection_hash = %s,
                        scanner_name = %s, scanner_definitions_version = %s,
                        source_object_key = %s, source_object_version_id = %s,
                        source_reference_hash = %s, object_stored_at = %s,
                        updated_at = %s
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND attempt_id = %s
                      AND status = 'CLAIMED'
                    RETURNING {_SLOT_COLUMNS}
                    """,
                    (
                        admitted.upload_id,
                        admitted.content_sha256,
                        admitted.byte_size,
                        admitted.page_count,
                        admitted.inspection_hash,
                        admitted.scanner_name,
                        admitted.scanner_definitions_version,
                        stored_object.object_key,
                        stored_object.object_version_id,
                        source_reference_hash,
                        current,
                        current,
                        slot.upload_id,
                        actor.firm_id,
                        slot.matter_id,
                        actor.actor_id,
                        identity.session_id,
                        slot.attempt_id,
                    ),
                ).fetchone()
                persisted = _slot_from_row(row)
                _insert_slot_event(
                    connection,
                    slot=persisted,
                    event_type="OBJECT_STORED",
                    occurred_at=current,
                )
                return persisted
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def complete_slot(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        registered: RegisteredWebEvidenceOriginal,
        now: datetime,
    ) -> WebMaterialUploadSlot:
        current = _aware_datetime(now, label="Web material upload clock")
        actor = _validate_identity(identity, now=current)
        _validate_slot_ownership(slot, identity=identity)
        if slot.status is not UploadSlotStatus.OBJECT_STORED or slot.attempt_id is None:
            raise WebMaterialUploadPersistenceBlocked("Web material upload slot is not ready for completion")
        _validate_registered(registered, slot=slot)
        receipt = registered.receipt
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(
                    connection,
                    actor=actor,
                    matter_id=slot.matter_id,
                    allowed_roles=_UPLOAD_ROLES,
                )
                row = connection.execute(
                    f"""
                    UPDATE web_material_upload_slots
                    SET status = 'COMPLETED', evidence_file_id = %s,
                        evidence_matter_version = %s, evidence_audit_event_id = %s,
                        completed_at = %s, updated_at = %s
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND attempt_id = %s
                      AND status = 'OBJECT_STORED'
                    RETURNING {_SLOT_COLUMNS}
                    """,
                    (
                        receipt.object_id,
                        receipt.matter_version,
                        receipt.audit_event_id,
                        current,
                        current,
                        slot.upload_id,
                        actor.firm_id,
                        slot.matter_id,
                        actor.actor_id,
                        identity.session_id,
                        slot.attempt_id,
                    ),
                ).fetchone()
                completed = _slot_from_row(row)
                _insert_slot_event(connection, slot=completed, event_type="COMPLETED", occurred_at=current)
                return completed
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def fail_slot(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
        now: datetime,
    ) -> None:
        self._terminalize(
            identity=identity,
            slot=slot,
            code=_validated_terminal_code(code),
            now=now,
            target=UploadSlotStatus.FAILED,
        )

    def require_reconciliation(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
        now: datetime,
    ) -> None:
        self._terminalize(
            identity=identity,
            slot=slot,
            code=_validated_terminal_code(code),
            now=now,
            target=UploadSlotStatus.RECONCILIATION_REQUIRED,
        )

    def _terminalize(
        self,
        *,
        identity: ServerIdentityContext,
        slot: WebMaterialUploadSlot,
        code: UploadSlotFailureCode,
        now: datetime,
        target: UploadSlotStatus,
    ) -> None:
        current = _aware_datetime(now, label="Web material upload clock")
        actor = _validate_identity(identity, now=current)
        _validate_slot_ownership(slot, identity=identity)
        if slot.status not in {UploadSlotStatus.CLAIMED, UploadSlotStatus.OBJECT_STORED} or slot.attempt_id is None:
            raise WebMaterialUploadPersistenceBlocked("Web material upload slot cannot be terminalized")
        if target is UploadSlotStatus.FAILED:
            update = """
                SET status = 'FAILED', failure_code = %s, failed_at = %s,
                    updated_at = %s
            """
            event_type = "FAILED"
            parameters: tuple[object, ...] = (code.value, current, current)
        elif target is UploadSlotStatus.RECONCILIATION_REQUIRED:
            update = """
                SET status = 'RECONCILIATION_REQUIRED', failure_code = %s,
                    reconciliation_required_at = %s, updated_at = %s
            """
            event_type = "RECONCILIATION_REQUIRED"
            parameters = (code.value, current, current)
        else:  # pragma: no cover - private caller invariant.
            raise WebMaterialUploadPersistenceBlocked("Web material upload terminal state is invalid")
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(
                    connection,
                    actor=actor,
                    matter_id=slot.matter_id,
                    allowed_roles=_UPLOAD_ROLES,
                )
                row = connection.execute(
                    f"""
                    UPDATE web_material_upload_slots
                    {update}
                    WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND attempt_id = %s
                      AND status IN ('CLAIMED', 'OBJECT_STORED')
                    RETURNING {_SLOT_COLUMNS}
                    """,
                    parameters
                    + (
                        slot.upload_id,
                        actor.firm_id,
                        slot.matter_id,
                        actor.actor_id,
                        identity.session_id,
                        slot.attempt_id,
                    ),
                ).fetchone()
                terminal = _slot_from_row(row)
                _insert_slot_event(connection, slot=terminal, event_type=event_type, occurred_at=current)
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None

    def _open_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
                yield connection
        except (WebMaterialUploadBlocked, PermissionError, KeyError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialUploadPersistenceBlocked("Web material upload store is unavailable") from None


def _insert_slot_event(
    connection: Any,
    *,
    slot: WebMaterialUploadSlot,
    event_type: str,
    occurred_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO web_material_upload_slot_events (
            event_id, upload_id, firm_id, matter_id, actor_id, session_id,
            event_type, attempt_id, source_reference_hash, terminal_code,
            occurred_at
        ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            slot.upload_id,
            slot.firm_id,
            slot.matter_id,
            slot.actor_id,
            slot.session_id,
            event_type,
            slot.attempt_id,
            slot.source_reference_hash,
            slot.failure_code.value if slot.failure_code is not None else None,
            occurred_at,
        ),
    )


def _validate_identity(identity: ServerIdentityContext, *, now: datetime | None = None) -> Actor:
    if not isinstance(identity, ServerIdentityContext):
        raise WebMaterialUploadPersistenceBlocked("Web material upload identity is invalid")
    if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebMaterialUploadPersistenceBlocked("Web material upload requires OIDC MFA")
    try:
        identity.validate(now=_aware_datetime(now, label="Web material upload clock") if now is not None else None)
    except Exception:
        raise WebMaterialUploadPersistenceBlocked("Web material upload identity is invalid") from None
    actor = identity.actor
    if not isinstance(actor, Actor):
        raise WebMaterialUploadPersistenceBlocked("Web material upload actor is invalid")
    _validate_uuid(actor.actor_id, label="Web material upload actor")
    _validate_uuid(actor.firm_id, label="Web material upload firm")
    _validate_uuid(identity.session_id, label="Web material upload session")
    if (
        not actor.roles
        or not all(isinstance(role, Role) for role in actor.roles)
        or Role.SYSTEM_WORKER in actor.roles
        or not actor.roles.intersection(_UPLOAD_ROLES)
    ):
        raise PermissionError("actor lacks a permitted role for Web material upload")
    return actor


def _validate_slot_ownership(slot: WebMaterialUploadSlot, *, identity: ServerIdentityContext) -> None:
    if not isinstance(slot, WebMaterialUploadSlot):
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot is invalid")
    actor = identity.actor
    if (
        slot.firm_id != actor.firm_id
        or slot.actor_id != actor.actor_id
        or slot.session_id != identity.session_id
    ):
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot ownership is invalid")
    for value, label in (
        (slot.upload_id, "Web material upload identifier"),
        (slot.matter_id, "Web material upload matter"),
    ):
        _validate_uuid(value, label=label)
    if slot.attempt_id is not None:
        _validate_uuid(slot.attempt_id, label="Web material upload attempt")


def _validate_version(value: int) -> None:
    if type(value) is not int or value < 1:
        raise WebMaterialUploadPersistenceBlocked("Web material upload expected version is invalid")


def _validate_display_name(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WebMaterialUploadPersistenceBlocked("Web material upload display name is invalid")
    try:
        byte_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise WebMaterialUploadPersistenceBlocked("Web material upload display name is invalid") from error
    if byte_length > _MAX_FILENAME_BYTES or "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise WebMaterialUploadPersistenceBlocked("Web material upload display name is invalid")


def _validate_declared_length(value: object) -> None:
    if value is not None and (type(value) is not int or not 1 <= value <= 256 * 1024 * 1024):
        raise WebMaterialUploadPersistenceBlocked("Web material upload declared length is invalid")


def _validate_timestamps(*, created_at: datetime, expires_at: datetime) -> None:
    created = _aware_datetime(created_at, label="Web material upload creation time")
    expiry = _aware_datetime(expires_at, label="Web material upload expiry")
    if expiry <= created:
        raise WebMaterialUploadPersistenceBlocked("Web material upload expiry is invalid")


def _validate_admitted_upload(upload: object, *, display_name: str) -> None:
    if not isinstance(upload, AdmittedWebPdfUpload):
        raise WebMaterialUploadPersistenceBlocked("Web material upload admitted record is invalid")
    _validate_uuid(upload.upload_id, label="Web material staged identifier")
    if upload.display_name != display_name or upload.media_type != "application/pdf":
        raise WebMaterialUploadPersistenceBlocked("Web material upload admitted record is invalid")
    if type(upload.byte_size) is not int or not 1 <= upload.byte_size <= 256 * 1024 * 1024:
        raise WebMaterialUploadPersistenceBlocked("Web material upload admitted size is invalid")
    if type(upload.page_count) is not int or not 1 <= upload.page_count <= 10_000:
        raise WebMaterialUploadPersistenceBlocked("Web material upload admitted page count is invalid")
    for value in (upload.content_sha256, upload.inspection_hash):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise WebMaterialUploadPersistenceBlocked("Web material upload admitted integrity is invalid")
    for value in (upload.scanner_name, upload.scanner_definitions_version):
        _validate_private_text(value, maximum=_MAX_SCANNER_TEXT_BYTES, label="Web material scanner metadata")


def _validate_stored_object(
    stored: object,
    *,
    actor: Actor,
    matter_id: str,
    admitted: AdmittedWebPdfUpload,
) -> None:
    if not isinstance(stored, StoredWebEvidenceOriginal):
        raise WebMaterialUploadPersistenceBlocked("Web material private object is invalid")
    if stored.content_sha256 != admitted.content_sha256 or stored.byte_size != admitted.byte_size:
        raise WebMaterialUploadPersistenceBlocked("Web material private object differs from admitted content")
    if not isinstance(stored.object_key, str) or len(stored.object_key) > _MAX_OBJECT_KEY_BYTES or not _OBJECT_KEY.fullmatch(stored.object_key):
        raise WebMaterialUploadPersistenceBlocked("Web material private object is invalid")
    parts = stored.object_key.split("/")
    if parts[2] != actor.firm_id or parts[3] != matter_id or parts[4] != admitted.content_sha256[:2] or parts[5] != admitted.content_sha256:
        raise WebMaterialUploadPersistenceBlocked("Web material private object is outside its server scope")
    if stored.object_version_id is not None:
        _validate_private_text(
            stored.object_version_id,
            maximum=_MAX_OBJECT_VERSION_BYTES,
            label="Web material private object version",
        )


def _validate_registered(registered: object, *, slot: WebMaterialUploadSlot) -> None:
    if not isinstance(registered, RegisteredWebEvidenceOriginal):
        raise WebMaterialUploadPersistenceBlocked("Web evidence registration result is invalid")
    receipt = registered.receipt
    if (
        receipt.command_name != "REGISTER_WEB_UPLOADED_EVIDENCE_ORIGINAL"
        or receipt.matter_id != slot.matter_id
        or receipt.object_type != "EVIDENCE_ORIGINAL"
        or receipt.matter_version != slot.expected_matter_version + 1
        or registered.source_reference_hash != slot.source_reference_hash
    ):
        raise WebMaterialUploadPersistenceBlocked("Web evidence registration result is invalid")
    _validate_uuid(receipt.object_id, label="Web evidence original")
    _validate_uuid(receipt.audit_event_id, label="Web evidence audit event")


def _validated_terminal_code(value: object) -> UploadSlotFailureCode:
    if not isinstance(value, UploadSlotFailureCode):
        raise WebMaterialUploadPersistenceBlocked("Web material upload terminal code is invalid")
    return value


def _slot_from_row(row: object) -> WebMaterialUploadSlot:
    if not isinstance(row, Mapping):
        raise WebMaterialUploadPersistenceBlocked("Web material upload store returned no slot")
    try:
        status = UploadSlotStatus(row["status"])
        failure_code = row.get("failure_code")
        return WebMaterialUploadSlot(
            upload_id=_row_uuid(row, "upload_id"),
            firm_id=_row_uuid(row, "firm_id"),
            matter_id=_row_uuid(row, "matter_id"),
            actor_id=_row_uuid(row, "actor_id"),
            session_id=_row_uuid(row, "session_id"),
            expected_matter_version=_row_positive_int(row, "expected_matter_version"),
            display_name=_row_display_name(row, "display_name"),
            declared_content_length=_row_optional_length(row, "declared_content_length"),
            status=status,
            attempt_id=_row_optional_uuid(row, "attempt_id"),
            attempt_count=_row_nonnegative_int(row, "attempt_count"),
            created_at=_row_datetime(row, "created_at"),
            expires_at=_row_datetime(row, "expires_at"),
            claimed_at=_row_optional_datetime(row, "claimed_at"),
            admitted_upload_id=_row_optional_uuid(row, "admitted_upload_id"),
            admitted_content_sha256=_row_optional_sha256(row, "admitted_content_sha256"),
            admitted_byte_size=_row_optional_length(row, "admitted_byte_size"),
            admitted_page_count=_row_optional_positive_int(row, "admitted_page_count"),
            admitted_inspection_hash=_row_optional_sha256(row, "admitted_inspection_hash"),
            scanner_name=_row_optional_private_text(row, "scanner_name", maximum=_MAX_SCANNER_TEXT_BYTES),
            scanner_definitions_version=_row_optional_private_text(
                row, "scanner_definitions_version", maximum=_MAX_SCANNER_TEXT_BYTES
            ),
            source_object_key=_row_optional_object_key(row),
            source_object_version_id=_row_optional_private_text(
                row, "source_object_version_id", maximum=_MAX_OBJECT_VERSION_BYTES
            ),
            source_reference_hash=_row_optional_sha256(row, "source_reference_hash"),
            object_stored_at=_row_optional_datetime(row, "object_stored_at"),
            evidence_file_id=_row_optional_uuid(row, "evidence_file_id"),
            evidence_matter_version=_row_optional_positive_int(row, "evidence_matter_version"),
            evidence_audit_event_id=_row_optional_uuid(row, "evidence_audit_event_id"),
            completed_at=_row_optional_datetime(row, "completed_at"),
            failure_code=UploadSlotFailureCode(failure_code) if failure_code is not None else None,
            failed_at=_row_optional_datetime(row, "failed_at"),
            reconciliation_required_at=_row_optional_datetime(row, "reconciliation_required_at"),
            updated_at=_row_optional_datetime(row, "updated_at"),
        )
    except (KeyError, TypeError, ValueError, WebMaterialUploadBlocked) as error:
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed") from error


def _row_uuid(row: Mapping[str, Any], field: str) -> str:
    return _validate_uuid(row[field], label=f"Web material upload row {field}")


def _row_optional_uuid(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    return None if value is None else _validate_uuid(value, label=f"Web material upload row {field}")


def _row_positive_int(row: Mapping[str, Any], field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 1:
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_optional_positive_int(row: Mapping[str, Any], field: str) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_nonnegative_int(row: Mapping[str, Any], field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 0:
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_optional_length(row: Mapping[str, Any], field: str) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 1 or value > 256 * 1024 * 1024:
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_display_name(row: Mapping[str, Any], field: str) -> str:
    value = row[field]
    _validate_display_name(value)
    return value


def _row_optional_sha256(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_optional_private_text(row: Mapping[str, Any], field: str, *, maximum: int) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    _validate_private_text(value, maximum=maximum, label="Web material upload slot row")
    return value


def _row_optional_object_key(row: Mapping[str, Any]) -> str | None:
    value = row.get("source_object_key")
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_OBJECT_KEY_BYTES or not _OBJECT_KEY.fullmatch(value):
        raise WebMaterialUploadPersistenceBlocked("Web material upload slot row is malformed")
    return value


def _row_datetime(row: Mapping[str, Any], field: str) -> datetime:
    return _aware_datetime(row[field], label="Web material upload slot row")


def _row_optional_datetime(row: Mapping[str, Any], field: str) -> datetime | None:
    value = row.get(field)
    return None if value is None else _aware_datetime(value, label="Web material upload slot row")


def _validate_uuid(value: object, *, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise WebMaterialUploadPersistenceBlocked(f"{label} is invalid") from error


def _validate_private_text(value: object, *, maximum: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise WebMaterialUploadPersistenceBlocked(f"{label} is invalid")


def _aware_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebMaterialUploadPersistenceBlocked(f"{label} is invalid")
    return value.astimezone(timezone.utc)
