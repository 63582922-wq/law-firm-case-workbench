"""RLS-scoped PostgreSQL store for common-material Web admission.

Large byte streams and S3 calls never run inside a database transaction.  The
store persists the object hand-off first, then atomically registers the
immutable ``case_material_objects`` row, increments the exact matter version,
appends audit/outbox records and completes the upload saga.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_common_material_upload import (
    CommonMaterialUploadFailureCode,
    CommonMaterialUploadOperation,
    CommonMaterialUploadStatus,
    CommonMaterialUploadStorePort,
    WebCommonMaterialUploadBlocked,
)
from case_kernel.case_ledger_postgres import _authorize_and_lock_matter, _authorize_matter_read
from case_kernel.common_material_object_store import (
    StoredCommonMaterialOriginal,
    common_material_object_key,
)
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.models import Actor, Role
from case_kernel.web_common_material_admission import (
    AdmittedCommonMaterial,
    CommonMaterialAgentStatus,
    CommonMaterialFormat,
    CommonMaterialRoute,
    common_material_agent_status,
)


class CommonMaterialUploadPersistenceBlocked(WebCommonMaterialUploadBlocked):
    """The common-material admission ledger cannot safely progress."""


_ROLES = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER})
_SHA = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY = re.compile(r"^[!-~]{8,160}$")
_MAX_DSN_BYTES = 4096


class PostgresCommonMaterialUploadStore(CommonMaterialUploadStorePort):
    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if (
            not isinstance(dsn, str)
            or dsn != dsn.strip()
            or not 1 <= len(dsn.encode("utf-8")) <= _MAX_DSN_BYTES
            or "\x00" in dsn
        ):
            raise ValueError("common material PostgreSQL DSN is invalid")
        self._dsn = dsn
        self._connection_factory = connection_factory or self._open_connection

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
    ) -> CommonMaterialUploadOperation:
        actor = _validate_identity(identity, now=created_at)
        for value, label in (
            (matter_id, "matter"),
            (upload_id, "upload"),
            (material_object_id, "material object"),
        ):
            _uuid(value, label)
        _positive(expected_matter_version, "expected matter version")
        normalized_name = _display_name(display_name)
        normalized_type = _declared_media_type(declared_media_type)
        _byte_size(declared_byte_size, "declared byte size")
        _idempotency_key(idempotency_key)
        created = _aware(created_at, "creation time")
        expiry = _aware(expires_at, "expiry time")
        if expiry <= created:
            raise CommonMaterialUploadPersistenceBlocked("common material upload expiry is invalid")
        request_hash = _canonical_hash(
            {
                "schema_version": "reserve-common-material-upload-v1",
                "matter_id": matter_id,
                "expected_matter_version": expected_matter_version,
                "display_name": normalized_name,
                "declared_byte_size": declared_byte_size,
                "declared_media_type": normalized_type,
            }
        )
        try:
            with self._transaction(actor) as connection:
                _authorize_and_lock_matter(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    expected_version=expected_matter_version,
                    allowed_roles=_ROLES,
                )
                prior = connection.execute(
                    """
                    SELECT request_hash, response_json
                      FROM command_idempotency
                     WHERE firm_id = %s AND matter_id = %s AND actor_id = %s
                       AND command_name = 'RESERVE_COMMON_MATERIAL_UPLOAD'
                       AND idempotency_key = %s
                    """,
                    (actor.firm_id, matter_id, actor.actor_id, idempotency_key),
                ).fetchone()
                if prior is not None:
                    if str(prior["request_hash"]) != request_hash:
                        raise IdempotencyConflict(
                            "common material reservation idempotency key was reused with different input"
                        )
                    response = prior["response_json"]
                    if not isinstance(response, Mapping):
                        raise CommonMaterialUploadPersistenceBlocked("common material reservation replay is invalid")
                    replay_id = _uuid(response.get("upload_id"), "replayed upload")
                    row = connection.execute(
                        """SELECT * FROM web_common_material_uploads
                           WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                             AND actor_id = %s AND session_id = %s""",
                        (replay_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                    ).fetchone()
                    return _operation(row)
                row = connection.execute(
                    """
                    INSERT INTO web_common_material_uploads (
                        upload_id, material_object_id, firm_id, matter_id, actor_id,
                        session_id, expected_matter_version, display_name,
                        declared_byte_size, declared_media_type,
                        reserve_idempotency_key, reserve_request_hash, status,
                        created_at, expires_at, updated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'RESERVED',%s,%s,%s
                    ) RETURNING *
                    """,
                    (
                        upload_id, material_object_id, actor.firm_id, matter_id,
                        actor.actor_id, identity.session_id, expected_matter_version,
                        normalized_name, declared_byte_size, normalized_type,
                        idempotency_key, request_hash, created, expiry, created,
                    ),
                ).fetchone()
                operation = _operation(row)
                _event(connection, operation=operation, event_type="RESERVED", occurred_at=created)
                connection.execute(
                    """
                    INSERT INTO command_idempotency (
                        firm_id, matter_id, actor_id, command_name,
                        idempotency_key, request_hash, response_json
                    ) VALUES (%s,%s,%s,'RESERVE_COMMON_MATERIAL_UPLOAD',%s,%s,%s)
                    """,
                    (
                        actor.firm_id, matter_id, actor.actor_id, idempotency_key,
                        request_hash,
                        Jsonb({
                            "upload_id": upload_id,
                            "material_object_id": material_object_id,
                            "expected_matter_version": expected_matter_version,
                        }),
                    ),
                )
                return operation
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict, IdempotencyConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def claim_or_resume(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        content_idempotency_key: str,
        now: datetime,
    ) -> CommonMaterialUploadOperation | None:
        actor = _validate_identity(identity, now=now)
        _uuid(matter_id, "matter")
        _uuid(upload_id, "upload")
        _idempotency_key(content_idempotency_key)
        current = _aware(now, "claim time")
        try:
            with self._transaction(actor) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    """
                    SELECT * FROM web_common_material_uploads
                     WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                       AND actor_id = %s AND session_id = %s
                     FOR UPDATE
                    """,
                    (upload_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                if row is None:
                    return None
                operation = _operation(row)
                if operation.content_idempotency_key is not None and operation.content_idempotency_key != content_idempotency_key:
                    raise IdempotencyConflict(
                        "common material content idempotency key differs from the first attempt"
                    )
                if operation.status in {
                    CommonMaterialUploadStatus.OBJECT_STORED,
                    CommonMaterialUploadStatus.COMPLETED,
                    CommonMaterialUploadStatus.RECONCILIATION_REQUIRED,
                }:
                    return operation
                if operation.status is not CommonMaterialUploadStatus.RESERVED or operation.expires_at <= current:
                    return None
                attempt_id = str(uuid4())
                claimed = connection.execute(
                    """
                    UPDATE web_common_material_uploads
                       SET status = 'CLAIMED', content_idempotency_key = %s,
                           attempt_id = %s, attempt_count = 1, claimed_at = %s,
                           updated_at = %s
                     WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                       AND actor_id = %s AND session_id = %s AND status = 'RESERVED'
                       AND expires_at > %s
                     RETURNING *
                    """,
                    (
                        content_idempotency_key, attempt_id, current, current,
                        upload_id, actor.firm_id, matter_id, actor.actor_id,
                        identity.session_id, current,
                    ),
                ).fetchone()
                if claimed is None:
                    return None
                operation = _operation(claimed)
                _event(connection, operation=operation, event_type="CLAIMED", occurred_at=current)
                return operation
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict, IdempotencyConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def record_object_stored(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        admitted: AdmittedCommonMaterial,
        stored: StoredCommonMaterialOriginal,
        now: datetime,
    ) -> CommonMaterialUploadOperation:
        actor = _validate_identity(identity, now=now)
        _owned(operation, identity=identity)
        if operation.status is not CommonMaterialUploadStatus.CLAIMED or operation.attempt_id is None:
            raise CommonMaterialUploadPersistenceBlocked("common material upload is not claimed")
        _validate_admitted(admitted, operation=operation)
        _validate_stored(stored, admitted=admitted, operation=operation)
        current = _aware(now, "object hand-off time")
        reference_hash = sha256(stored.object_key.encode("ascii")).hexdigest()
        try:
            with self._transaction(actor) as connection:
                _authorize_matter_read(
                    connection, actor=actor, matter_id=operation.matter_id, allowed_roles=_ROLES
                )
                row = connection.execute(
                    """
                    UPDATE web_common_material_uploads
                       SET status = 'OBJECT_STORED', admitted_format = %s,
                           canonical_kind = %s, admitted_media_type = %s, route = %s,
                           admitted_byte_size = %s, admitted_content_sha256 = %s,
                           admitted_inspection_hash = %s, scanner_name = %s,
                           scanner_definitions_version = %s, review_flags = %s,
                           review_status = 'NEEDS_LAWYER_REVIEW', formal_fact = false,
                           formal_transaction = false, legal_conclusion = false,
                           evidence_decision = false, court_ready = false,
                           source_object_key = %s, source_object_version_id = %s,
                           source_reference_hash = %s, object_stored_at = %s,
                           updated_at = %s
                     WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                       AND actor_id = %s AND session_id = %s AND attempt_id = %s
                       AND status = 'CLAIMED'
                     RETURNING *
                    """,
                    (
                        admitted.admitted_format.value, admitted.canonical_kind.value,
                        admitted.media_type, admitted.route.value, admitted.byte_size,
                        admitted.content_sha256, admitted.inspection_hash,
                        admitted.scanner_name, admitted.scanner_definitions_version,
                        Jsonb(list(admitted.review_flags)), stored.object_key,
                        stored.object_version_id, reference_hash, current, current,
                        operation.upload_id, actor.firm_id, operation.matter_id,
                        actor.actor_id, identity.session_id, operation.attempt_id,
                    ),
                ).fetchone()
                if row is None:
                    raise CommonMaterialUploadPersistenceBlocked(
                        "common material object hand-off does not match the active claim"
                    )
                updated = _operation(row)
                _event(connection, operation=updated, event_type="OBJECT_STORED", occurred_at=current)
                return updated
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def complete_registration(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        now: datetime,
    ) -> CommonMaterialUploadOperation:
        actor = _validate_identity(identity, now=now)
        _owned(operation, identity=identity)
        current = _aware(now, "registration time")
        try:
            with self._transaction(actor) as connection:
                row = connection.execute(
                    """SELECT * FROM web_common_material_uploads
                       WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                         AND actor_id = %s AND session_id = %s FOR UPDATE""",
                    (
                        operation.upload_id, actor.firm_id, operation.matter_id,
                        actor.actor_id, identity.session_id,
                    ),
                ).fetchone()
                current_operation = _operation(row)
                if current_operation.status is CommonMaterialUploadStatus.COMPLETED:
                    return current_operation
                if current_operation.status is not CommonMaterialUploadStatus.OBJECT_STORED:
                    raise CommonMaterialUploadPersistenceBlocked(
                        "only a stored common material can be registered"
                    )
                _authorize_and_lock_matter(
                    connection,
                    actor=actor,
                    matter_id=current_operation.matter_id,
                    expected_version=current_operation.expected_matter_version,
                    allowed_roles=_ROLES,
                )
                next_version = current_operation.expected_matter_version + 1
                required = (
                    current_operation.admitted_format,
                    current_operation.canonical_kind,
                    current_operation.admitted_media_type,
                    current_operation.route,
                    current_operation.admitted_byte_size,
                    current_operation.admitted_content_sha256,
                    current_operation.admitted_inspection_hash,
                    current_operation.scanner_name,
                    current_operation.scanner_definitions_version,
                    current_operation.source_object_key,
                    current_operation.source_reference_hash,
                    current_operation.object_stored_at,
                )
                if any(value is None for value in required):
                    raise CommonMaterialUploadPersistenceBlocked(
                        "stored common material metadata is incomplete"
                    )
                agent_status = common_material_agent_status(current_operation.admitted_format)
                evidence_page_id = (
                    str(uuid4())
                    if current_operation.admitted_format in {
                        CommonMaterialFormat.JPEG,
                        CommonMaterialFormat.PNG,
                    }
                    else None
                )
                agent_source_ref = (
                    f"evidence-page:{evidence_page_id}"
                    if evidence_page_id is not None
                    else (
                        f"material-object:{current_operation.material_object_id}"
                        if current_operation.admitted_format in {
                            CommonMaterialFormat.DOCX,
                            CommonMaterialFormat.XLSX,
                        }
                        else None
                    )
                )
                connection.execute(
                    """
                    INSERT INTO case_material_objects (
                        material_object_id, firm_id, matter_id, source_upload_id,
                        original_display_name, admitted_format, canonical_kind,
                        media_type, content_sha256, byte_size, inspection_hash,
                        scanner_name, scanner_definitions_version, route,
                        review_flags, status, record_version, original_locked,
                        formal_fact, formal_transaction, legal_conclusion,
                        evidence_decision, court_ready, agent_status,
                        agent_source_ref, source_object_key,
                        source_object_version_id, source_reference_hash,
                        created_matter_version, created_by, created_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'NEEDS_LAWYER_REVIEW',1,true,false,false,false,false,false,
                        %s,%s,%s,%s,%s,%s,%s,%s
                    )
                    """,
                    (
                        current_operation.material_object_id, actor.firm_id,
                        current_operation.matter_id, current_operation.upload_id,
                        current_operation.display_name,
                        current_operation.admitted_format.value,
                        current_operation.canonical_kind,
                        current_operation.admitted_media_type,
                        current_operation.admitted_content_sha256,
                        current_operation.admitted_byte_size,
                        current_operation.admitted_inspection_hash,
                        current_operation.scanner_name,
                        current_operation.scanner_definitions_version,
                        current_operation.route.value,
                        Jsonb(list(current_operation.review_flags)),
                        agent_status.value,
                        agent_source_ref,
                        current_operation.source_object_key,
                        current_operation.source_object_version_id,
                        current_operation.source_reference_hash,
                        next_version, actor.actor_id, current,
                    ),
                )
                if current_operation.admitted_format in {
                    CommonMaterialFormat.DOCX,
                    CommonMaterialFormat.XLSX,
                }:
                    connection.execute(
                        """
                        INSERT INTO case_agent_material_objects (
                            material_object_id, firm_id, matter_id, admitted_format,
                            media_type, content_sha256, byte_size, source_object_key,
                            source_object_version_id, source_reference_hash,
                            scanner_name, scanner_definitions_version, inspection_hash,
                            admitted_by, created_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            current_operation.material_object_id, actor.firm_id,
                            current_operation.matter_id,
                            current_operation.admitted_format.value,
                            current_operation.admitted_media_type,
                            current_operation.admitted_content_sha256,
                            current_operation.admitted_byte_size,
                            current_operation.source_object_key,
                            current_operation.source_object_version_id,
                            current_operation.source_reference_hash,
                            current_operation.scanner_name,
                            current_operation.scanner_definitions_version,
                            current_operation.admitted_inspection_hash,
                            actor.actor_id, current,
                        ),
                    )
                elif evidence_page_id is not None:
                    connection.execute(
                        """
                        INSERT INTO evidence_original_files (
                            evidence_file_id, firm_id, matter_id, original_label,
                            original_file_sha256, byte_size, media_type, page_count,
                            source_scan_fingerprint, supersedes_file_id, created_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,NULL,%s)
                        """,
                        (
                            current_operation.material_object_id, actor.firm_id,
                            current_operation.matter_id, current_operation.display_name,
                            current_operation.admitted_content_sha256,
                            current_operation.admitted_byte_size,
                            current_operation.admitted_media_type,
                            current_operation.admitted_inspection_hash, current,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO evidence_pages (
                            evidence_page_id, firm_id, matter_id, evidence_file_id,
                            page_number, rendered_page_sha256, created_at
                        ) VALUES (%s,%s,%s,%s,1,NULL,%s)
                        """,
                        (
                            evidence_page_id, actor.firm_id,
                            current_operation.matter_id,
                            current_operation.material_object_id, current,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO web_evidence_native_image_source_objects (
                            evidence_file_id, firm_id, matter_id, source_object_key,
                            source_object_version_id, source_object_sha256,
                            source_object_bytes, source_media_type,
                            source_reference_hash, admitted_by, created_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            current_operation.material_object_id, actor.firm_id,
                            current_operation.matter_id,
                            current_operation.source_object_key,
                            current_operation.source_object_version_id,
                            current_operation.admitted_content_sha256,
                            current_operation.admitted_byte_size,
                            current_operation.admitted_media_type,
                            current_operation.source_reference_hash,
                            actor.actor_id, current,
                        ),
                    )
                    _invalidate_image_evidence_outputs(
                        connection,
                        firm_id=actor.firm_id,
                        matter_id=current_operation.matter_id,
                        now=current,
                    )
                updated_matter = connection.execute(
                    """UPDATE matters SET version = version + 1, updated_at = %s
                       WHERE matter_id = %s AND firm_id = %s AND version = %s
                       RETURNING version""",
                    (
                        current, current_operation.matter_id, actor.firm_id,
                        current_operation.expected_matter_version,
                    ),
                ).fetchone()
                if updated_matter is None or int(updated_matter["version"]) != next_version:
                    raise VersionConflict("matter changed before common material registration")
                audit_event_id = str(uuid4())
                request_id = str(uuid4())
                audit_payload = {
                    "schema_version": "common-material-admitted-audit-v1",
                    "material_object_id": current_operation.material_object_id,
                    "admitted_format": current_operation.admitted_format.value,
                    "content_sha256": current_operation.admitted_content_sha256,
                    "byte_size": current_operation.admitted_byte_size,
                    "inspection_hash": current_operation.admitted_inspection_hash,
                    "route": current_operation.route.value,
                    "review_status": "NEEDS_LAWYER_REVIEW",
                    "agent_status": agent_status.value,
                    "agent_source_ref": agent_source_ref,
                    "formal_fact": False,
                    "formal_transaction": False,
                    "legal_conclusion": False,
                    "evidence_decision": False,
                    "court_ready": False,
                }
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, firm_id, matter_id, actor_id, event_type,
                        input_version, output_version, request_id, payload, occurred_at
                    ) VALUES (%s,%s,%s,%s,'COMMON_MATERIAL_ADMITTED',%s,%s,%s,%s,%s)
                    """,
                    (
                        audit_event_id, actor.firm_id, current_operation.matter_id,
                        actor.actor_id, current_operation.expected_matter_version,
                        next_version, request_id, Jsonb(audit_payload), current,
                    ),
                )
                outbox_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO outbox_events (
                        outbox_id, firm_id, matter_id, aggregate_version,
                        event_type, payload, created_at
                    ) VALUES (%s,%s,%s,%s,'COMMON_MATERIAL_ADMITTED',%s,%s)
                    """,
                    (
                        outbox_id, actor.firm_id, current_operation.matter_id,
                        next_version,
                        Jsonb({
                            "material_object_id": current_operation.material_object_id,
                            "admitted_format": current_operation.admitted_format.value,
                            "route": current_operation.route.value,
                            "review_status": "NEEDS_LAWYER_REVIEW",
                            "agent_status": agent_status.value,
                            "agent_source_ref": agent_source_ref,
                        }),
                        current,
                    ),
                )
                completed = connection.execute(
                    """
                    UPDATE web_common_material_uploads
                       SET status = 'COMPLETED', result_matter_version = %s,
                           agent_status = %s, agent_source_ref = %s,
                           audit_event_id = %s, outbox_id = %s, completed_at = %s,
                           updated_at = %s
                     WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                       AND actor_id = %s AND session_id = %s
                       AND status = 'OBJECT_STORED'
                     RETURNING *
                    """,
                    (
                        next_version, agent_status.value, agent_source_ref,
                        audit_event_id, outbox_id, current, current,
                        current_operation.upload_id, actor.firm_id,
                        current_operation.matter_id, actor.actor_id, identity.session_id,
                    ),
                ).fetchone()
                result = _operation(completed)
                _event(
                    connection,
                    operation=result,
                    event_type="COMPLETED",
                    occurred_at=current,
                    matter_version=next_version,
                )
                command_key = f"common-material:{current_operation.upload_id}:register"
                command_hash = _canonical_hash(
                    {
                        "schema_version": "register-common-material-v1",
                        "upload_id": current_operation.upload_id,
                        "material_object_id": current_operation.material_object_id,
                        "expected_matter_version": current_operation.expected_matter_version,
                        "content_sha256": current_operation.admitted_content_sha256,
                        "inspection_hash": current_operation.admitted_inspection_hash,
                        "source_reference_hash": current_operation.source_reference_hash,
                        "agent_status": agent_status.value,
                        "agent_source_ref": agent_source_ref,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO command_idempotency (
                        firm_id, matter_id, actor_id, command_name,
                        idempotency_key, request_hash, response_json, completed_at
                    ) VALUES (%s,%s,%s,'REGISTER_COMMON_MATERIAL_OBJECT',%s,%s,%s,%s)
                    """,
                    (
                        actor.firm_id, current_operation.matter_id, actor.actor_id,
                        command_key, command_hash,
                        Jsonb({
                            "material_object_id": current_operation.material_object_id,
                            "matter_version": next_version,
                            "agent_status": agent_status.value,
                            "agent_source_ref": agent_source_ref,
                            "audit_event_id": audit_event_id,
                            "outbox_id": outbox_id,
                        }),
                        current,
                    ),
                )
                return result
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict, IdempotencyConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def fail_upload(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        failure_code: CommonMaterialUploadFailureCode,
        now: datetime,
    ) -> None:
        if failure_code not in {
            CommonMaterialUploadFailureCode.CONTENT_REJECTED,
            CommonMaterialUploadFailureCode.ADMISSION_UNAVAILABLE,
        }:
            raise CommonMaterialUploadPersistenceBlocked("known common material failure code is invalid")
        self._terminalize(
            identity=identity,
            operation=operation,
            status=CommonMaterialUploadStatus.FAILED,
            failure_code=failure_code,
            now=now,
        )

    def require_reconciliation(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        failure_code: CommonMaterialUploadFailureCode,
        admitted: AdmittedCommonMaterial | None = None,
        stored: StoredCommonMaterialOriginal | None = None,
        now: datetime,
    ) -> None:
        if failure_code not in {
            CommonMaterialUploadFailureCode.OBJECT_STATE_UNKNOWN,
            CommonMaterialUploadFailureCode.OBJECT_HANDOFF_UNKNOWN,
            CommonMaterialUploadFailureCode.REGISTRATION_STATE_UNKNOWN,
        }:
            raise CommonMaterialUploadPersistenceBlocked("common material reconciliation code is invalid")
        if failure_code in {
            CommonMaterialUploadFailureCode.OBJECT_STATE_UNKNOWN,
            CommonMaterialUploadFailureCode.OBJECT_HANDOFF_UNKNOWN,
        }:
            _validate_admitted(admitted, operation=operation)
            if failure_code is CommonMaterialUploadFailureCode.OBJECT_HANDOFF_UNKNOWN:
                _validate_stored(stored, admitted=admitted, operation=operation)
            elif stored is not None:
                raise CommonMaterialUploadPersistenceBlocked(
                    "unknown common material object state cannot claim a storage receipt"
                )
        elif admitted is not None or stored is not None:
            raise CommonMaterialUploadPersistenceBlocked(
                "registration reconciliation must use its persisted object hand-off"
            )
        self._terminalize(
            identity=identity,
            operation=operation,
            status=CommonMaterialUploadStatus.RECONCILIATION_REQUIRED,
            failure_code=failure_code,
            admitted=admitted,
            stored=stored,
            now=now,
        )

    def _terminalize(
        self,
        *,
        identity: ServerIdentityContext,
        operation: CommonMaterialUploadOperation,
        status: CommonMaterialUploadStatus,
        failure_code: CommonMaterialUploadFailureCode,
        admitted: AdmittedCommonMaterial | None = None,
        stored: StoredCommonMaterialOriginal | None = None,
        now: datetime,
    ) -> None:
        actor = _validate_identity(identity, now=now)
        _owned(operation, identity=identity)
        current = _aware(now, "terminal time")
        allowed_from = (
            (CommonMaterialUploadStatus.CLAIMED,)
            if status is CommonMaterialUploadStatus.FAILED
            else (CommonMaterialUploadStatus.CLAIMED, CommonMaterialUploadStatus.OBJECT_STORED)
        )
        try:
            with self._transaction(actor) as connection:
                _authorize_matter_read(
                    connection, actor=actor, matter_id=operation.matter_id, allowed_roles=_ROLES
                )
                row = connection.execute(
                    """SELECT * FROM web_common_material_uploads
                       WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                         AND actor_id = %s AND session_id = %s FOR UPDATE""",
                    (
                        operation.upload_id, actor.firm_id, operation.matter_id,
                        actor.actor_id, identity.session_id,
                    ),
                ).fetchone()
                current_operation = _operation(row)
                if current_operation.status in {
                    CommonMaterialUploadStatus.COMPLETED,
                    CommonMaterialUploadStatus.FAILED,
                    CommonMaterialUploadStatus.RECONCILIATION_REQUIRED,
                }:
                    return
                if current_operation.status not in allowed_from:
                    raise CommonMaterialUploadPersistenceBlocked(
                        "common material upload cannot enter the requested terminal state"
                    )
                if admitted is None:
                    updated = connection.execute(
                        """
                        UPDATE web_common_material_uploads
                           SET status = %s, failure_code = %s, terminal_at = %s,
                               updated_at = %s
                         WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                           AND actor_id = %s AND session_id = %s AND status = %s
                         RETURNING *
                        """,
                        (
                            status.value, failure_code.value, current, current,
                            operation.upload_id, actor.firm_id, operation.matter_id,
                            actor.actor_id, identity.session_id,
                            current_operation.status.value,
                        ),
                    ).fetchone()
                else:
                    object_key = (
                        stored.object_key
                        if stored is not None
                        else common_material_object_key(
                            firm_id=operation.firm_id,
                            matter_id=operation.matter_id,
                            material_object_id=operation.material_object_id,
                            content_sha256=admitted.content_sha256,
                            admitted_format=admitted.admitted_format,
                        )
                    )
                    object_version = None if stored is None else stored.object_version_id
                    object_time = None if stored is None else current
                    reference_hash = sha256(object_key.encode("ascii")).hexdigest()
                    _validate_existing_recovery_metadata(
                        current_operation,
                        admitted=admitted,
                        object_key=object_key,
                        reference_hash=reference_hash,
                    )
                    updated = connection.execute(
                        """
                        UPDATE web_common_material_uploads
                           SET status = %s, failure_code = %s, terminal_at = %s,
                               admitted_format = %s, canonical_kind = %s,
                               admitted_media_type = %s, route = %s,
                               admitted_byte_size = %s, admitted_content_sha256 = %s,
                               admitted_inspection_hash = %s, scanner_name = %s,
                               scanner_definitions_version = %s, review_flags = %s,
                               review_status = 'NEEDS_LAWYER_REVIEW', formal_fact = false,
                               formal_transaction = false, legal_conclusion = false,
                               evidence_decision = false, court_ready = false,
                               source_object_key = %s, source_object_version_id = %s,
                               source_reference_hash = %s, object_stored_at = %s,
                               updated_at = %s
                         WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                           AND actor_id = %s AND session_id = %s AND status = %s
                         RETURNING *
                        """,
                        (
                            status.value, failure_code.value, current,
                            admitted.admitted_format.value, admitted.canonical_kind.value,
                            admitted.media_type, admitted.route.value, admitted.byte_size,
                            admitted.content_sha256, admitted.inspection_hash,
                            admitted.scanner_name, admitted.scanner_definitions_version,
                            Jsonb(list(admitted.review_flags)), object_key, object_version,
                            reference_hash, object_time, current,
                            operation.upload_id, actor.firm_id, operation.matter_id,
                            actor.actor_id, identity.session_id,
                            current_operation.status.value,
                        ),
                    ).fetchone()
                terminal = _operation(updated)
                _event(
                    connection,
                    operation=terminal,
                    event_type=status.value,
                    occurred_at=current,
                    failure_code=failure_code,
                )
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def get_upload(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> CommonMaterialUploadOperation | None:
        actor = _validate_identity(identity, now=datetime.now(timezone.utc))
        _uuid(matter_id, "matter")
        _uuid(upload_id, "upload")
        try:
            with self._transaction(actor) as connection:
                _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=_ROLES)
                row = connection.execute(
                    """SELECT * FROM web_common_material_uploads
                       WHERE upload_id = %s AND firm_id = %s AND matter_id = %s
                         AND actor_id = %s AND session_id = %s""",
                    (upload_id, actor.firm_id, matter_id, actor.actor_id, identity.session_id),
                ).fetchone()
                return None if row is None else _operation(row)
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None

    def _open_connection(self) -> Any:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _transaction(self, actor: Actor) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                connection.execute("SELECT set_config('app.firm_id', %s, true)", (actor.firm_id,))
                connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor.actor_id,))
                yield connection
        except (CommonMaterialUploadPersistenceBlocked, PermissionError, VersionConflict, IdempotencyConflict):
            raise
        except Exception:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material upload store is unavailable"
            ) from None


def _event(
    connection: Any,
    *,
    operation: CommonMaterialUploadOperation,
    event_type: str,
    occurred_at: datetime,
    failure_code: CommonMaterialUploadFailureCode | None = None,
    matter_version: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO web_common_material_upload_events (
            event_id, upload_id, material_object_id, firm_id, matter_id,
            actor_id, event_type, attempt_id, source_reference_hash,
            failure_code, matter_version, occurred_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            str(uuid4()), operation.upload_id, operation.material_object_id,
            operation.firm_id, operation.matter_id, operation.actor_id,
            event_type, operation.attempt_id, operation.source_reference_hash,
            None if failure_code is None else failure_code.value,
            matter_version, occurred_at,
        ),
    )


def _invalidate_image_evidence_outputs(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    now: datetime,
) -> None:
    """A newly admitted native image invalidates every prior locked evidence view."""

    reason = "新增图片证据原件后，旧证据清单与派生文件已失效。"
    connection.execute(
        """
        UPDATE evidence_derivative_runs run
           SET status = 'STALE', lease_id = NULL, lease_expires_at = NULL,
               related_derivative_id = NULL, annotated_derivative_id = NULL,
               stale_at = %s, stale_reason = %s, updated_at = %s
         WHERE run.matter_id = %s AND run.firm_id = %s
           AND run.status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
           AND EXISTS (
               SELECT 1 FROM evidence_manifests manifest
                WHERE manifest.manifest_id = run.manifest_id
                  AND manifest.matter_id = run.matter_id
                  AND manifest.firm_id = run.firm_id
                  AND manifest.status = 'LOCKED'
           )
        """,
        (now, reason, now, matter_id, firm_id),
    )
    connection.execute(
        """
        UPDATE evidence_derivative_artifacts derivative
           SET status = 'STALE', updated_at = %s, invalidated_at = %s,
               invalidation_reason = %s
         WHERE derivative.matter_id = %s AND derivative.firm_id = %s
           AND derivative.status IN ('CANDIDATE', 'VERIFIED')
           AND EXISTS (
               SELECT 1 FROM evidence_manifests manifest
                WHERE manifest.manifest_id = derivative.manifest_id
                  AND manifest.matter_id = derivative.matter_id
                  AND manifest.firm_id = derivative.firm_id
                  AND manifest.status = 'LOCKED'
           )
        """,
        (now, now, reason, matter_id, firm_id),
    )
    connection.execute(
        """
        UPDATE evidence_manifests
           SET status = 'INVALIDATED', invalidated_at = %s
         WHERE matter_id = %s AND firm_id = %s AND status = 'LOCKED'
        """,
        (now, matter_id, firm_id),
    )


def _operation(row: object) -> CommonMaterialUploadOperation:
    if not isinstance(row, Mapping):
        raise CommonMaterialUploadPersistenceBlocked("common material upload row is unavailable")
    try:
        flags = row.get("review_flags") or []
        if not isinstance(flags, list) or not all(isinstance(value, str) for value in flags):
            raise ValueError("review flags")
        admitted_format = row.get("admitted_format")
        route = row.get("route")
        failure = row.get("failure_code")
        agent_status = row.get("agent_status")
        return CommonMaterialUploadOperation(
            upload_id=_uuid(row["upload_id"], "row upload"),
            material_object_id=_uuid(row["material_object_id"], "row material object"),
            firm_id=_uuid(row["firm_id"], "row firm"),
            matter_id=_uuid(row["matter_id"], "row matter"),
            actor_id=_uuid(row["actor_id"], "row actor"),
            session_id=_uuid(row["session_id"], "row session"),
            expected_matter_version=_positive(row["expected_matter_version"], "row expected version"),
            display_name=_display_name(row["display_name"]),
            declared_byte_size=_byte_size(row["declared_byte_size"], "row declared size"),
            declared_media_type=_declared_media_type(row["declared_media_type"]),
            reserve_idempotency_key=_idempotency_key(row["reserve_idempotency_key"]),
            reserve_request_hash=_sha(row["reserve_request_hash"], "row reserve hash"),
            status=CommonMaterialUploadStatus(str(row["status"])),
            created_at=_aware(row["created_at"], "row creation time"),
            expires_at=_aware(row["expires_at"], "row expiry time"),
            content_idempotency_key=(
                None if row.get("content_idempotency_key") is None
                else _idempotency_key(row.get("content_idempotency_key"))
            ),
            attempt_id=_optional_uuid(row.get("attempt_id"), "row attempt"),
            attempt_count=int(row.get("attempt_count") or 0),
            claimed_at=_optional_time(row.get("claimed_at"), "row claim time"),
            admitted_format=None if admitted_format is None else CommonMaterialFormat(str(admitted_format)),
            canonical_kind=None if row.get("canonical_kind") is None else str(row.get("canonical_kind")),
            admitted_media_type=None if row.get("admitted_media_type") is None else str(row.get("admitted_media_type")),
            route=None if route is None else CommonMaterialRoute(str(route)),
            admitted_byte_size=_optional_size(row.get("admitted_byte_size"), "row admitted size"),
            admitted_content_sha256=_optional_sha(row.get("admitted_content_sha256"), "row content hash"),
            admitted_inspection_hash=_optional_sha(row.get("admitted_inspection_hash"), "row inspection hash"),
            scanner_name=None if row.get("scanner_name") is None else str(row.get("scanner_name")),
            scanner_definitions_version=(
                None if row.get("scanner_definitions_version") is None
                else str(row.get("scanner_definitions_version"))
            ),
            review_flags=tuple(flags),
            source_object_key=None if row.get("source_object_key") is None else str(row.get("source_object_key")),
            source_object_version_id=(
                None if row.get("source_object_version_id") is None
                else str(row.get("source_object_version_id"))
            ),
            source_reference_hash=_optional_sha(row.get("source_reference_hash"), "row source reference hash"),
            object_stored_at=_optional_time(row.get("object_stored_at"), "row object time"),
            result_matter_version=(
                None if row.get("result_matter_version") is None
                else _positive(row.get("result_matter_version"), "row result version")
            ),
            agent_status=(
                None
                if agent_status is None
                else CommonMaterialAgentStatus(str(agent_status))
            ),
            agent_source_ref=(
                None if row.get("agent_source_ref") is None else str(row.get("agent_source_ref"))
            ),
            audit_event_id=_optional_uuid(row.get("audit_event_id"), "row audit event"),
            outbox_id=_optional_uuid(row.get("outbox_id"), "row outbox"),
            completed_at=_optional_time(row.get("completed_at"), "row completion time"),
            failure_code=None if failure is None else CommonMaterialUploadFailureCode(str(failure)),
            terminal_at=_optional_time(row.get("terminal_at"), "row terminal time"),
        )
    except (KeyError, TypeError, ValueError, CommonMaterialUploadPersistenceBlocked) as error:
        raise CommonMaterialUploadPersistenceBlocked("common material upload row is malformed") from error


def _validate_identity(identity: object, *, now: datetime) -> Actor:
    if not isinstance(identity, ServerIdentityContext) or identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
        raise CommonMaterialUploadPersistenceBlocked("common material upload requires OIDC MFA")
    identity.validate(now=_aware(now, "identity check time"))
    actor = identity.actor
    if not isinstance(actor, Actor) or Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection(_ROLES):
        raise PermissionError("actor lacks a permitted common material upload role")
    _uuid(actor.actor_id, "actor")
    _uuid(actor.firm_id, "firm")
    _uuid(identity.session_id, "session")
    return actor


def _owned(operation: object, *, identity: ServerIdentityContext) -> None:
    if not isinstance(operation, CommonMaterialUploadOperation):
        raise CommonMaterialUploadPersistenceBlocked("common material operation is invalid")
    if (
        operation.firm_id != identity.actor.firm_id
        or operation.actor_id != identity.actor.actor_id
        or operation.session_id != identity.session_id
    ):
        raise CommonMaterialUploadPersistenceBlocked("common material operation ownership differs")


def _validate_admitted(admitted: object, *, operation: CommonMaterialUploadOperation) -> None:
    if not isinstance(admitted, AdmittedCommonMaterial):
        raise CommonMaterialUploadPersistenceBlocked("common material admission is invalid")
    admitted.validate()
    if (
        admitted.material_object_id != operation.material_object_id
        or admitted.display_name != operation.display_name
        or admitted.byte_size != operation.declared_byte_size
    ):
        raise CommonMaterialUploadPersistenceBlocked("common material admission differs from reservation")


def _validate_stored(
    stored: object,
    *,
    admitted: AdmittedCommonMaterial,
    operation: CommonMaterialUploadOperation,
) -> None:
    if not isinstance(stored, StoredCommonMaterialOriginal):
        raise CommonMaterialUploadPersistenceBlocked("common material object receipt is invalid")
    if (
        stored.material_object_id != operation.material_object_id
        or stored.content_sha256 != admitted.content_sha256
        or stored.byte_size != admitted.byte_size
        or stored.media_type != admitted.media_type
        or stored.admitted_format is not admitted.admitted_format
        or stored.route is not admitted.route
        or stored.inspection_hash != admitted.inspection_hash
    ):
        raise CommonMaterialUploadPersistenceBlocked("common material object differs from admission")
    expected_key = common_material_object_key(
        firm_id=operation.firm_id,
        matter_id=operation.matter_id,
        material_object_id=operation.material_object_id,
        content_sha256=admitted.content_sha256,
        admitted_format=admitted.admitted_format,
    )
    if stored.object_key != expected_key:
        raise CommonMaterialUploadPersistenceBlocked("common material object is outside its tenant/matter scope")


def _validate_existing_recovery_metadata(
    operation: CommonMaterialUploadOperation,
    *,
    admitted: AdmittedCommonMaterial,
    object_key: str,
    reference_hash: str,
) -> None:
    """Prevent an uncertain DB response from replacing a prior object hand-off."""

    if operation.admitted_format is None:
        if operation.source_object_key is not None or operation.source_reference_hash is not None:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material recovery row contains partial object metadata"
            )
        return
    expected = (
        admitted.admitted_format,
        admitted.canonical_kind.value,
        admitted.media_type,
        admitted.route,
        admitted.byte_size,
        admitted.content_sha256,
        admitted.inspection_hash,
        admitted.scanner_name,
        admitted.scanner_definitions_version,
        admitted.review_flags,
        object_key,
        reference_hash,
    )
    actual = (
        operation.admitted_format,
        operation.canonical_kind,
        operation.admitted_media_type,
        operation.route,
        operation.admitted_byte_size,
        operation.admitted_content_sha256,
        operation.admitted_inspection_hash,
        operation.scanner_name,
        operation.scanner_definitions_version,
        operation.review_flags,
        operation.source_object_key,
        operation.source_reference_hash,
    )
    for existing, candidate in zip(actual, expected, strict=True):
        if existing is not None and existing != candidate:
            raise CommonMaterialUploadPersistenceBlocked(
                "common material recovery metadata differs from the persisted hand-off"
            )


def _display_name(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value.encode("utf-8")) <= 255:
        raise CommonMaterialUploadPersistenceBlocked("common material display name is invalid")
    if "/" in value or "\\" in value or any(ord(character) < 32 for character in value):
        raise CommonMaterialUploadPersistenceBlocked("common material display name is invalid")
    suffix = Path(value).suffix.casefold()
    if suffix not in {".docx", ".xlsx", ".pptx", ".rtf", ".txt", ".csv", ".html", ".htm", ".eml", ".jpg", ".jpeg", ".jpe", ".png"}:
        raise CommonMaterialUploadPersistenceBlocked("common material format is not admitted by 0041")
    return value


def _declared_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise CommonMaterialUploadPersistenceBlocked("common material declared media type is invalid")
    normalized = value.split(";", 1)[0].strip().casefold()
    if not 3 <= len(normalized) <= 160 or any(ord(character) < 32 for character in normalized):
        raise CommonMaterialUploadPersistenceBlocked("common material declared media type is invalid")
    return normalized


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY.fullmatch(value) is None:
        raise CommonMaterialUploadPersistenceBlocked("common material idempotency key is invalid")
    return value


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise CommonMaterialUploadPersistenceBlocked(f"common material {label} is invalid") from error


def _optional_uuid(value: object, label: str) -> str | None:
    return None if value is None else _uuid(value, label)


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise CommonMaterialUploadPersistenceBlocked(f"common material {label} is invalid")
    return value


def _byte_size(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 100 * 1024 * 1024:
        raise CommonMaterialUploadPersistenceBlocked(f"common material {label} is invalid")
    return value


def _optional_size(value: object, label: str) -> int | None:
    return None if value is None else _byte_size(value, label)


def _sha(value: object, label: str) -> str:
    normalized = str(value)
    if _SHA.fullmatch(normalized) is None:
        raise CommonMaterialUploadPersistenceBlocked(f"common material {label} is invalid")
    return normalized


def _optional_sha(value: object, label: str) -> str | None:
    return None if value is None else _sha(value, label)


def _aware(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CommonMaterialUploadPersistenceBlocked(f"common material {label} is invalid")
    return value.astimezone(timezone.utc)


def _optional_time(value: object, label: str) -> datetime | None:
    return None if value is None else _aware(value, label)


def _canonical_hash(payload: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = (
    "CommonMaterialUploadPersistenceBlocked",
    "PostgresCommonMaterialUploadStore",
)
