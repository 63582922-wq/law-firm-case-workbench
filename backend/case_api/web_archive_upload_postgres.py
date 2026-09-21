"""RLS-scoped durable state for Web ZIP material admission."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_archive_upload import ArchiveUploadStatus, WebMaterialArchiveBlocked, WebMaterialArchiveOperation, WebMaterialArchiveStore
from case_kernel.case_ledger_postgres import _authorize_and_lock_matter, _authorize_matter_read
from case_kernel.errors import VersionConflict
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebMaterialArchive
from case_kernel.web_zip_staging import AdmittedWebZip


class WebMaterialArchivePersistenceBlocked(WebMaterialArchiveBlocked):
    """The archive operation database cannot safely continue."""


_MAX_DSN = 4096
_MAX_NAME = 255
_KEY = re.compile(
    r"^material-archives/v1/"
    r"[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f-]{36}\.zip$"
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROLES = frozenset({Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})


class PostgresWebMaterialArchiveStore(WebMaterialArchiveStore):
    def __init__(self, dsn: str, *, connection_factory: Callable[[], Any] | None = None) -> None:
        if not isinstance(dsn, str) or not dsn or len(dsn) > _MAX_DSN or dsn != dsn.strip() or "\x00" in dsn:
            raise ValueError("Web material archive PostgreSQL DSN is invalid")
        self._dsn = dsn
        self._connection_factory = connection_factory or self._open_connection

    def reserve_archive(self, *, identity: ServerIdentityContext, matter_id: str, expected_matter_version: int,
                        archive_id: str, display_name: str, declared_content_length: int | None,
                        created_at: datetime, expires_at: datetime) -> WebMaterialArchiveOperation:
        actor = _validate_identity(identity, now=created_at)
        _validate_uuid(matter_id, "archive matter")
        _validate_uuid(archive_id, "archive id")
        _validate_version(expected_matter_version)
        _validate_name(display_name)
        _validate_length(declared_content_length)
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id,
                                            expected_version=expected_matter_version, allowed_roles=_ROLES)
                row = connection.execute(
                    """
                    INSERT INTO web_material_archive_uploads (
                        archive_id, firm_id, matter_id, actor_id, session_id,
                        expected_matter_version, display_name, declared_content_length,
                        status, created_at, expires_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'RESERVED', %s, %s, %s)
                    RETURNING *
                    """,
                    (archive_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id,
                     expected_matter_version, display_name, declared_content_length, created_at, expires_at, created_at),
                ).fetchone()
                operation = _operation_from_row(row)
                _event(connection, operation, "RESERVED", created_at)
                return operation
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None

    def claim_or_resume(self, *, identity: ServerIdentityContext, matter_id: str, archive_id: str,
                        now: datetime) -> WebMaterialArchiveOperation | None:
        actor = _validate_identity(identity, now=now)
        _validate_uuid(matter_id, "archive matter")
        _validate_uuid(archive_id, "archive id")
        attempt_id = str(uuid4())
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    """
                    UPDATE web_material_archive_uploads
                    SET status = 'CLAIMED', attempt_id = %s, attempt_count = 1,
                        updated_at = %s
                    WHERE archive_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND status = 'RESERVED'
                      AND expires_at > %s
                    RETURNING *
                    """,
                    (attempt_id, now, archive_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id, now),
                ).fetchone()
                if row is not None:
                    operation = _operation_from_row(row)
                    _event(connection, operation, "CLAIMED", now)
                    return operation
                row = connection.execute(
                    "SELECT * FROM web_material_archive_uploads WHERE archive_id = %s AND firm_id = %s AND matter_id = %s AND actor_id = %s AND session_id = %s FOR UPDATE",
                    (archive_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                if row is None:
                    return None
                operation = _operation_from_row(row)
                return operation if operation.status in {ArchiveUploadStatus.OBJECT_STORED, ArchiveUploadStatus.RECONCILIATION_REQUIRED} else None
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None

    def record_object_stored(self, *, identity: ServerIdentityContext, operation: WebMaterialArchiveOperation,
                             admitted: AdmittedWebZip, stored_object: StoredWebMaterialArchive,
                             now: datetime) -> WebMaterialArchiveOperation:
        actor = _validate_identity(identity, now=now)
        _validate_owned(operation, identity=identity)
        if operation.status is not ArchiveUploadStatus.CLAIMED or operation.attempt_id is None:
            raise WebMaterialArchivePersistenceBlocked("Web material archive operation is not claimable")
        _validate_admitted(admitted, display_name=operation.display_name)
        _validate_stored(stored_object, actor=actor, matter_id=operation.matter_id, admitted=admitted)
        reference = sha256(stored_object.object_key.encode("ascii")).hexdigest()
        inventory = [{"name": entry.name, "byte_size": entry.byte_size, "sha256": entry.content_sha256, "compressed_byte_size": entry.compressed_byte_size} for entry in admitted.entries]
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=operation.matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    """
                    UPDATE web_material_archive_uploads
                    SET status = 'OBJECT_STORED', archive_content_sha256 = %s,
                        archive_byte_size = %s, entry_count = %s, expanded_byte_size = %s,
                        inventory_json = %s::jsonb, source_object_key = %s,
                        source_object_version_id = %s, source_reference_hash = %s,
                        object_stored_at = %s, updated_at = %s
                    WHERE archive_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND attempt_id = %s
                      AND status = 'CLAIMED'
                    RETURNING *
                    """,
                    (admitted.content_sha256, admitted.byte_size, len(admitted.entries), admitted.expanded_byte_size,
                     json.dumps(inventory, ensure_ascii=False, separators=(",", ":")), stored_object.object_key,
                     stored_object.object_version_id, reference, now, now, operation.archive_id, actor.firm_id,
                     operation.matter_id, actor.actor_id, identity.session_id, operation.attempt_id),
                ).fetchone()
                updated = _operation_from_row(row)
                _event(connection, updated, "OBJECT_STORED", now)
                return updated
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None

    def get_operation(self, *, identity: ServerIdentityContext, matter_id: str,
                      archive_id: str) -> WebMaterialArchiveOperation | None:
        actor = _validate_identity(identity, now=datetime.now().astimezone())
        _validate_uuid(matter_id, "archive matter")
        _validate_uuid(archive_id, "archive id")
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    "SELECT * FROM web_material_archive_uploads WHERE archive_id = %s AND firm_id = %s AND matter_id = %s AND actor_id = %s AND session_id = %s",
                    (archive_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                return None if row is None else _operation_from_row(row)
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None

    def require_reconciliation(self, *, identity: ServerIdentityContext, operation: WebMaterialArchiveOperation,
                               now: datetime) -> None:
        actor = _validate_identity(identity, now=now)
        _validate_owned(operation, identity=identity)
        try:
            with self._transaction(actor.firm_id) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=operation.matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    """
                    UPDATE web_material_archive_uploads
                    SET status = 'RECONCILIATION_REQUIRED', reconciliation_required_at = %s, updated_at = %s
                    WHERE archive_id = %s AND firm_id = %s AND matter_id = %s
                      AND actor_id = %s AND session_id = %s AND attempt_id = %s
                      AND status = 'CLAIMED'
                    RETURNING *
                    """,
                    (now, now, operation.archive_id, actor.firm_id, operation.matter_id, actor.actor_id,
                     identity.session_id, operation.attempt_id),
                ).fetchone()
                updated = _operation_from_row(row)
                _event(connection, updated, "RECONCILIATION_REQUIRED", now)
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None

    def _open_connection(self):
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
                yield connection
        except (WebMaterialArchiveBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise WebMaterialArchivePersistenceBlocked("Web material archive store is unavailable") from None


def _event(connection: Any, operation: WebMaterialArchiveOperation, event_type: str, occurred_at: datetime) -> None:
    connection.execute(
        """INSERT INTO web_material_archive_upload_events
           (event_id, archive_id, firm_id, matter_id, actor_id, session_id, event_type, attempt_id, source_reference_hash, occurred_at)
           VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (operation.archive_id, operation.firm_id, operation.matter_id, operation.actor_id, operation.session_id,
         event_type, operation.attempt_id, operation.source_reference_hash, occurred_at),
    )


def _operation_from_row(row: object) -> WebMaterialArchiveOperation:
    if not isinstance(row, Mapping):
        raise WebMaterialArchivePersistenceBlocked("Web material archive row is unavailable")
    try:
        inventory = row.get("inventory_json") or []
        if not isinstance(inventory, list):
            raise ValueError("inventory")
        return WebMaterialArchiveOperation(
            archive_id=_uuid(row["archive_id"]), firm_id=_uuid(row["firm_id"]), matter_id=_uuid(row["matter_id"]),
            actor_id=_uuid(row["actor_id"]), session_id=_uuid(row["session_id"]),
            expected_matter_version=_positive(row["expected_matter_version"]), display_name=_name(row["display_name"]),
            declared_content_length=_length(row.get("declared_content_length")), status=ArchiveUploadStatus(row["status"]),
            created_at=_datetime(row["created_at"]), expires_at=_datetime(row["expires_at"]),
            attempt_id=_optional_uuid(row.get("attempt_id")), attempt_count=int(row.get("attempt_count", 0)),
            archive_content_sha256=_optional_sha(row.get("archive_content_sha256")),
            archive_byte_size=_length(row.get("archive_byte_size")), entry_count=_positive_optional(row.get("entry_count")),
            expanded_byte_size=_positive_optional(row.get("expanded_byte_size")), inventory=tuple(inventory),
            source_object_key=row.get("source_object_key"), source_object_version_id=row.get("source_object_version_id"),
            source_reference_hash=_optional_sha(row.get("source_reference_hash")), object_stored_at=_optional_datetime(row.get("object_stored_at")),
            reconciliation_required_at=_optional_datetime(row.get("reconciliation_required_at")),
        )
    except (KeyError, TypeError, ValueError, WebMaterialArchiveBlocked) as error:
        raise WebMaterialArchivePersistenceBlocked("Web material archive row is malformed") from error


def _validate_identity(identity: ServerIdentityContext, *, now: datetime) -> Actor:
    if not isinstance(identity, ServerIdentityContext) or identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise WebMaterialArchivePersistenceBlocked("Web material archive requires OIDC MFA")
    identity.validate(now=now)
    actor = identity.actor
    if not isinstance(actor, Actor) or Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_ROLES):
        raise PermissionError("actor lacks a permitted role for material archive upload")
    _validate_uuid(actor.actor_id, "actor")
    _validate_uuid(actor.firm_id, "firm")
    _validate_uuid(identity.session_id, "session")
    return actor


def _validate_owned(operation: WebMaterialArchiveOperation, *, identity: ServerIdentityContext) -> None:
    if operation.firm_id != identity.actor.firm_id or operation.actor_id != identity.actor.actor_id or operation.session_id != identity.session_id:
        raise WebMaterialArchivePersistenceBlocked("Web material archive ownership is invalid")


def _validate_admitted(admitted: AdmittedWebZip, *, display_name: str) -> None:
    if not isinstance(admitted, AdmittedWebZip) or admitted.display_name != display_name or not admitted.entries:
        raise WebMaterialArchivePersistenceBlocked("Web material archive admission is invalid")
    if not _SHA.fullmatch(admitted.content_sha256) or not 1 <= admitted.byte_size <= 256 * 1024 * 1024:
        raise WebMaterialArchivePersistenceBlocked("Web material archive admission is invalid")


def _validate_stored(stored: StoredWebMaterialArchive, *, actor: Actor, matter_id: str, admitted: AdmittedWebZip) -> None:
    if not isinstance(stored, StoredWebMaterialArchive) or stored.content_sha256 != admitted.content_sha256 or stored.byte_size != admitted.byte_size:
        raise WebMaterialArchivePersistenceBlocked("Web material archive object differs from admission")
    if not _KEY.fullmatch(stored.object_key):
        raise WebMaterialArchivePersistenceBlocked("Web material archive object key is invalid")
    parts = stored.object_key.split("/")
    if parts[2] != actor.firm_id or parts[3] != matter_id or parts[5] != admitted.content_sha256:
        raise WebMaterialArchivePersistenceBlocked("Web material archive object is outside its server scope")


def _uuid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as error:
        raise WebMaterialArchivePersistenceBlocked("archive row UUID is invalid") from error


def _validate_uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError) as error:
        raise WebMaterialArchivePersistenceBlocked(f"archive {label} is invalid") from error


def _validate_version(value: object) -> None:
    if type(value) is not int or value < 1:
        raise WebMaterialArchivePersistenceBlocked("archive expected version is invalid")


def _validate_name(value: object) -> None:
    if not isinstance(value, str) or value != value.strip() or not value.lower().endswith(".zip") or len(value.encode()) > _MAX_NAME or any(ord(char) < 32 for char in value):
        raise WebMaterialArchivePersistenceBlocked("archive display name is invalid")


def _validate_length(value: object) -> None:
    if value is not None and (type(value) is not int or not 1 <= value <= 256 * 1024 * 1024):
        raise WebMaterialArchivePersistenceBlocked("archive declared length is invalid")


def _positive(value: object) -> int:
    if type(value) is not int or value < 1:
        raise WebMaterialArchivePersistenceBlocked("archive row positive integer is invalid")
    return value


def _positive_optional(value: object) -> int | None:
    return None if value is None else _positive(value)


def _length(value: object) -> int | None:
    return None if value is None else _positive(value)


def _optional_sha(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if not _SHA.fullmatch(normalized):
        raise WebMaterialArchivePersistenceBlocked("archive row hash is invalid")
    return normalized


def _name(value: object) -> str:
    _validate_name(value)
    return str(value)


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebMaterialArchivePersistenceBlocked("archive row timestamp is invalid")
    return value


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)
