"""Guarded PostgreSQL commands for immutable evidence-page manifests.

The adapter shares the same matter version, idempotency, database membership,
audit, and outbox boundary as the fact/transaction ledger. It never deletes or
updates registered source files/pages. Formal page decisions, annotations, and
duplicate resolutions invalidate locked manifests, derivative artifacts, and
the current submission bundle in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Iterator
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _read_projection_version,
    _require_cursor_version,
    _require_expected_projection_version,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
    _canonical_cursor_uuid,
    _parse_utc_cursor_timestamp,
    _utc_cursor_timestamp,
)
from .artifact_access import VerifiedDerivativeLocator
from .evidence_manifest import DuplicateResolution, PageDisposition, ReviewStatus
from .models import Actor, Role
from .original_page_access import OriginalPageLocator
from .local_case_folder import (
    FolderManifest,
    KNOWN_FILE_KINDS,
    OriginalFileRecord,
    compare_folder_manifests,
    folder_manifest_hash,
)
from .stable_pagination import (
    StablePageCursor,
    StablePaginationBlocked,
    decode_page_cursor,
    encode_page_cursor,
    validate_page_limit,
)


@dataclass(frozen=True)
class PersistentEvidenceSnapshot:
    matter_id: str
    version: int
    snapshot_hash: str
    original_files: tuple[dict[str, Any], ...]
    pages: tuple[dict[str, Any], ...]
    duplicate_groups: tuple[dict[str, Any], ...]
    locked_manifest: dict[str, Any] | None
    derivatives: tuple[dict[str, Any], ...]
    derivative_runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PersistentEvidenceReviewSummary:
    matter_id: str
    version: int
    summary_hash: str
    manifest_readiness_hash: str
    total_pages: int
    unresolved_page_count: int
    pending_decision_count: int
    unresolved_duplicate_count: int
    original_files: tuple[dict[str, Any], ...]
    duplicate_groups: tuple[dict[str, Any], ...]
    locked_manifest: dict[str, Any] | None
    derivatives: tuple[dict[str, Any], ...]
    derivative_runs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PersistentEvidencePageListPage:
    matter_id: str
    matter_version: int
    total_count: int
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class PersistentLocalFolderIntakeSummary:
    matter_id: str
    matter_version: int
    summary_hash: str
    approved_scan: dict[str, Any] | None
    candidate_scan: dict[str, Any] | None


@dataclass(frozen=True)
class PersistentLocalFolderFileListPage:
    matter_id: str
    matter_version: int
    scan_id: str
    total_count: int
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class DerivativeRunLease:
    run_id: str
    lease_id: str
    matter_id: str
    manifest_id: str
    manifest_content_hash: str
    attempt_count: int
    lease_expires_at: datetime
    matter_version: int


class PostgresEvidenceManifestStore:
    """UUID-only evidence Manifest repository for PostgreSQL 16+."""

    _CANDIDATE_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.SYSTEM_WORKER}
    )
    _HUMAN_CANDIDATE_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER}
    )
    _DECISION_ROLES = frozenset({Role.LEAD_LAWYER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER, Role.SYSTEM_WORKER}
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def register_original_file(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        original_label: str,
        original_file_sha256: str,
        byte_size: int,
        media_type: str,
        page_count: int,
        source_scan_fingerprint: str,
        supersedes_file_id: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(original_label, "original_label")
        _require_text(media_type, "media_type")
        _validate_sha256("original_file_sha256", original_file_sha256)
        _validate_sha256("source_scan_fingerprint", source_scan_fingerprint)
        if byte_size < 1 or page_count < 1:
            raise CaseLedgerPersistenceBlocked("original byte size and page count must be positive")
        if supersedes_file_id is not None:
            _validate_uuid("supersedes_file_id", supersedes_file_id)
        command_name = "REGISTER_EVIDENCE_ORIGINAL"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "original_label": original_label.strip(),
            "original_file_sha256": original_file_sha256,
            "byte_size": byte_size,
            "media_type": media_type.strip(),
            "page_count": page_count,
            "source_scan_fingerprint": source_scan_fingerprint,
            "supersedes_file_id": supersedes_file_id,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._CANDIDATE_ROLES,
            )
            if prior is not None:
                return prior
            if supersedes_file_id is not None:
                superseded = connection.execute(
                    """
                    SELECT 1 FROM evidence_original_files
                    WHERE evidence_file_id = %s AND matter_id = %s AND firm_id = %s
                    """,
                    (supersedes_file_id, matter_id, actor.firm_id),
                ).fetchone()
                if superseded is None:
                    raise CaseLedgerPersistenceBlocked("superseded original does not belong to this matter")
            evidence_file_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_original_files (
                    evidence_file_id, firm_id, matter_id, original_label,
                    original_file_sha256, byte_size, media_type, page_count,
                    source_scan_fingerprint, supersedes_file_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    evidence_file_id,
                    actor.firm_id,
                    matter_id,
                    original_label.strip(),
                    original_file_sha256,
                    byte_size,
                    media_type.strip(),
                    page_count,
                    source_scan_fingerprint,
                    supersedes_file_id,
                ),
            )
            for page_number in range(1, page_count + 1):
                connection.execute(
                    """
                    INSERT INTO evidence_pages (
                        evidence_page_id, firm_id, matter_id, evidence_file_id, page_number
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (str(uuid4()), actor.firm_id, matter_id, evidence_file_id, page_number),
                )
            _invalidate_current_evidence_outputs(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
                reason="新增或替换原始证据后，旧证据清单已失效。",
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_ORIGINAL_REGISTERED",
                object_type="EVIDENCE_ORIGINAL",
                object_id=evidence_file_id,
                audit_payload={
                    "evidence_file_id": evidence_file_id,
                    "original_file_sha256": original_file_sha256,
                    "page_count": page_count,
                    "supersedes_file_id": supersedes_file_id,
                },
                stale_submission=True,
            )

    def create_page_decision_candidate(
        self,
        *,
        matter_id: str,
        evidence_page_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        disposition: PageDisposition,
        reason: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("evidence_page_id", evidence_page_id)
        _require_roles(actor, self._HUMAN_CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(reason, "page decision reason")
        command_name = "CREATE_EVIDENCE_PAGE_DECISION_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "evidence_page_id": evidence_page_id,
            "expected_version": expected_version,
            "disposition": disposition.value,
            "reason": reason.strip(),
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._HUMAN_CANDIDATE_ROLES,
            )
            if prior is not None:
                return prior
            _require_page(connection, evidence_page_id=evidence_page_id, matter_id=matter_id, firm_id=actor.firm_id)
            connection.execute(
                """
                UPDATE evidence_page_decisions
                SET status = 'INVALIDATED', approval_hash = NULL, approved_by = NULL, updated_at = now()
                WHERE evidence_page_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'CANDIDATE'
                """,
                (evidence_page_id, matter_id, actor.firm_id),
            )
            decision_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_page_decisions (
                    decision_id, firm_id, matter_id, evidence_page_id,
                    disposition, reason, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'CANDIDATE')
                """,
                (decision_id, actor.firm_id, matter_id, evidence_page_id, disposition.value, reason.strip()),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_PAGE_DECISION_CANDIDATE_CREATED",
                object_type="EVIDENCE_PAGE_DECISION",
                object_id=decision_id,
                audit_payload={"decision_id": decision_id, "evidence_page_id": evidence_page_id, "disposition": disposition.value},
                stale_submission=False,
            )

    def approve_page_decision(
        self,
        *,
        matter_id: str,
        decision_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("decision_id", decision_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("page decision approval_hash", approval_hash)
        command_name = "APPROVE_EVIDENCE_PAGE_DECISION"
        payload = {
            "matter_id": matter_id,
            "decision_id": decision_id,
            "expected_version": expected_version,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            row = connection.execute(
                """
                SELECT evidence_page_id, disposition, status
                FROM evidence_page_decisions
                WHERE decision_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (decision_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(decision_id)
            if row["status"] != ReviewStatus.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active page decision candidate can be approved")
            connection.execute(
                """
                UPDATE evidence_page_decisions
                SET status = 'INVALIDATED', approval_hash = NULL, approved_by = NULL, updated_at = now()
                WHERE evidence_page_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'APPROVED'
                """,
                (row["evidence_page_id"], matter_id, actor.firm_id),
            )
            updated = connection.execute(
                """
                UPDATE evidence_page_decisions
                SET status = 'APPROVED', approval_hash = %s, approved_by = %s, updated_at = now()
                WHERE decision_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (approval_hash, actor.actor_id, decision_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("page decision changed before approval")
            _invalidate_current_evidence_outputs(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
                reason="页级纳入或排除决定发生变化，旧证据清单已失效。",
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_PAGE_DECISION_APPROVED",
                object_type="EVIDENCE_PAGE_DECISION",
                object_id=decision_id,
                audit_payload={
                    "decision_id": decision_id,
                    "evidence_page_id": str(row["evidence_page_id"]),
                    "disposition": row["disposition"],
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def create_annotation_candidate(
        self,
        *,
        matter_id: str,
        evidence_page_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        label: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("evidence_page_id", evidence_page_id)
        _require_roles(actor, self._HUMAN_CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _require_text(label, "annotation label")
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise CaseLedgerPersistenceBlocked("annotation rectangle must use normalized page coordinates")
        command_name = "CREATE_EVIDENCE_ANNOTATION_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "evidence_page_id": evidence_page_id,
            "expected_version": expected_version,
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "label": label.strip(),
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._HUMAN_CANDIDATE_ROLES,
            )
            if prior is not None:
                return prior
            _require_page(connection, evidence_page_id=evidence_page_id, matter_id=matter_id, firm_id=actor.firm_id)
            annotation_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_page_annotations (
                    annotation_id, firm_id, matter_id, evidence_page_id, purpose,
                    x0, y0, x1, y1, label, status
                ) VALUES (%s, %s, %s, %s, 'HIGHLIGHT_RELEVANT_REGION', %s, %s, %s, %s, %s, 'CANDIDATE')
                """,
                (annotation_id, actor.firm_id, matter_id, evidence_page_id, x0, y0, x1, y1, label.strip()),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_ANNOTATION_CANDIDATE_CREATED",
                object_type="EVIDENCE_ANNOTATION",
                object_id=annotation_id,
                audit_payload={"annotation_id": annotation_id, "evidence_page_id": evidence_page_id},
                stale_submission=False,
            )

    def approve_annotation(
        self,
        *,
        matter_id: str,
        annotation_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("annotation_id", annotation_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("annotation approval_hash", approval_hash)
        command_name = "APPROVE_EVIDENCE_ANNOTATION"
        payload = {
            "matter_id": matter_id,
            "annotation_id": annotation_id,
            "expected_version": expected_version,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            row = connection.execute(
                """
                SELECT evidence_page_id, status
                FROM evidence_page_annotations
                WHERE annotation_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (annotation_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(annotation_id)
            if row["status"] != ReviewStatus.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active annotation candidate can be approved")
            updated = connection.execute(
                """
                UPDATE evidence_page_annotations
                SET status = 'APPROVED', approval_hash = %s, approved_by = %s, updated_at = now()
                WHERE annotation_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (approval_hash, actor.actor_id, annotation_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("annotation changed before approval")
            _invalidate_current_evidence_outputs(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
                reason="红框坐标或标注发生变化，旧证据清单已失效。",
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_ANNOTATION_APPROVED",
                object_type="EVIDENCE_ANNOTATION",
                object_id=annotation_id,
                audit_payload={
                    "annotation_id": annotation_id,
                    "evidence_page_id": str(row["evidence_page_id"]),
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def create_duplicate_group_candidate(
        self,
        *,
        matter_id: str,
        evidence_page_ids: tuple[str, ...],
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        normalized_page_ids = _validate_uuid_set("evidence_page_ids", evidence_page_ids, minimum=2)
        _require_roles(actor, self._HUMAN_CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        command_name = "CREATE_EVIDENCE_DUPLICATE_GROUP_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "evidence_page_ids": normalized_page_ids,
            "expected_version": expected_version,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._HUMAN_CANDIDATE_ROLES,
            )
            if prior is not None:
                return prior
            page_rows = connection.execute(
                """
                SELECT evidence_page_id
                FROM evidence_pages
                WHERE evidence_page_id = ANY(%s) AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (list(normalized_page_ids), matter_id, actor.firm_id),
            ).fetchall()
            if len(page_rows) != len(normalized_page_ids):
                raise CaseLedgerPersistenceBlocked("one or more duplicate pages do not belong to this matter")
            active = connection.execute(
                """
                SELECT member.evidence_page_id
                FROM evidence_page_duplicate_members member
                JOIN evidence_page_duplicate_groups duplicate_group
                  ON duplicate_group.duplicate_group_id = member.duplicate_group_id
                 AND duplicate_group.firm_id = member.firm_id
                 AND duplicate_group.matter_id = member.matter_id
                WHERE member.evidence_page_id = ANY(%s)
                  AND member.matter_id = %s AND member.firm_id = %s
                  AND duplicate_group.status <> 'INVALIDATED'
                LIMIT 1
                """,
                (list(normalized_page_ids), matter_id, actor.firm_id),
            ).fetchone()
            if active is not None:
                raise CaseLedgerPersistenceBlocked("a page already belongs to an active duplicate group")
            duplicate_group_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_page_duplicate_groups (
                    duplicate_group_id, firm_id, matter_id, status
                ) VALUES (%s, %s, %s, 'CANDIDATE')
                """,
                (duplicate_group_id, actor.firm_id, matter_id),
            )
            for evidence_page_id in normalized_page_ids:
                connection.execute(
                    """
                    INSERT INTO evidence_page_duplicate_members (
                        duplicate_group_id, evidence_page_id, firm_id, matter_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (duplicate_group_id, evidence_page_id, actor.firm_id, matter_id),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DUPLICATE_GROUP_CANDIDATE_CREATED",
                object_type="EVIDENCE_DUPLICATE_GROUP",
                object_id=duplicate_group_id,
                audit_payload={"duplicate_group_id": duplicate_group_id, "evidence_page_ids": normalized_page_ids},
                stale_submission=False,
            )

    def resolve_duplicate_group(
        self,
        *,
        matter_id: str,
        duplicate_group_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        same_source_page: bool,
        canonical_page_id: str | None,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("duplicate_group_id", duplicate_group_id)
        if canonical_page_id is not None:
            _validate_uuid("canonical_page_id", canonical_page_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("duplicate resolution approval_hash", approval_hash)
        if same_source_page and canonical_page_id is None:
            raise CaseLedgerPersistenceBlocked("same-source duplicate pages require a canonical page")
        if not same_source_page and canonical_page_id is not None:
            raise CaseLedgerPersistenceBlocked("distinct pages cannot select a canonical page")
        command_name = "RESOLVE_EVIDENCE_DUPLICATE_GROUP"
        payload = {
            "matter_id": matter_id,
            "duplicate_group_id": duplicate_group_id,
            "expected_version": expected_version,
            "same_source_page": same_source_page,
            "canonical_page_id": canonical_page_id,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            group = connection.execute(
                """
                SELECT status
                FROM evidence_page_duplicate_groups
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (duplicate_group_id, matter_id, actor.firm_id),
            ).fetchone()
            if group is None:
                raise KeyError(duplicate_group_id)
            if group["status"] != DuplicateResolution.CANDIDATE.value:
                raise CaseLedgerPersistenceBlocked("only an active duplicate page candidate can be resolved")
            members = connection.execute(
                """
                SELECT evidence_page_id
                FROM evidence_page_duplicate_members
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY evidence_page_id ASC
                """,
                (duplicate_group_id, matter_id, actor.firm_id),
            ).fetchall()
            member_ids = {str(row["evidence_page_id"]) for row in members}
            if len(member_ids) < 2:
                raise CaseLedgerPersistenceBlocked("a duplicate group must retain at least two source pages")
            if same_source_page and canonical_page_id not in member_ids:
                raise CaseLedgerPersistenceBlocked("canonical page must be a member of the duplicate group")
            status = DuplicateResolution.SAME_SOURCE_PAGE if same_source_page else DuplicateResolution.DISTINCT_PAGES
            updated = connection.execute(
                """
                UPDATE evidence_page_duplicate_groups
                SET status = %s, canonical_page_id = %s, approval_hash = %s,
                    approved_by = %s, updated_at = now()
                WHERE duplicate_group_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (
                    status.value,
                    canonical_page_id,
                    approval_hash,
                    actor.actor_id,
                    duplicate_group_id,
                    matter_id,
                    actor.firm_id,
                ),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("duplicate page group changed before resolution")
            _invalidate_current_evidence_outputs(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
                reason="重复页结论发生变化，旧证据清单已失效。",
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DUPLICATE_GROUP_RESOLVED",
                object_type="EVIDENCE_DUPLICATE_GROUP",
                object_id=duplicate_group_id,
                audit_payload={
                    "duplicate_group_id": duplicate_group_id,
                    "status": status.value,
                    "canonical_page_id": canonical_page_id,
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def lock_manifest(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        approval_hash: str,
        readiness_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("manifest approval_hash", approval_hash)
        _validate_sha256("manifest readiness_hash", readiness_hash)
        command_name = "LOCK_EVIDENCE_MANIFEST"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "approval_hash": approval_hash,
            "readiness_hash": readiness_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            current = connection.execute(
                """
                SELECT manifest_id
                FROM evidence_manifests
                WHERE matter_id = %s AND firm_id = %s AND status = 'LOCKED'
                FOR UPDATE
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if current is not None:
                raise CaseLedgerPersistenceBlocked("this matter already has a current locked evidence manifest")
            pages = connection.execute(
                """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       source.original_label, source.original_file_sha256,
                       decision.decision_id, decision.disposition,
                       pending.decision_id AS pending_decision_id
                FROM evidence_pages page
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                LEFT JOIN evidence_page_decisions decision
                  ON decision.evidence_page_id = page.evidence_page_id
                 AND decision.firm_id = page.firm_id AND decision.matter_id = page.matter_id
                 AND decision.status = 'APPROVED'
                LEFT JOIN LATERAL (
                    SELECT decision_id
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'CANDIDATE'
                    ORDER BY updated_at DESC, decision_id DESC
                    LIMIT 1
                ) pending ON TRUE
                WHERE page.matter_id = %s AND page.firm_id = %s
                ORDER BY source.created_at ASC, source.evidence_file_id ASC, page.page_number ASC
                FOR SHARE OF page, source
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            if not pages:
                raise CaseLedgerPersistenceBlocked("an evidence manifest requires at least one original page")
            missing = [row for row in pages if row["decision_id"] is None]
            if missing:
                raise CaseLedgerPersistenceBlocked(
                    f"all source pages require an approved disposition; unresolved pages: {len(missing)}"
                )
            if any(row["pending_decision_id"] is not None for row in pages):
                raise CaseLedgerPersistenceBlocked(
                    "pending page disposition candidates must be resolved before manifest lock"
                )
            duplicate_groups = connection.execute(
                """
                SELECT duplicate_group_id, status, canonical_page_id
                FROM evidence_page_duplicate_groups
                WHERE matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                ORDER BY duplicate_group_id ASC
                FOR SHARE
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            if any(row["status"] == DuplicateResolution.CANDIDATE.value for row in duplicate_groups):
                raise CaseLedgerPersistenceBlocked("all duplicate page candidates must be resolved before manifest lock")
            member_rows = connection.execute(
                """
                SELECT duplicate_group_id, evidence_page_id
                FROM evidence_page_duplicate_members
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY duplicate_group_id ASC, evidence_page_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            members = _group_members(member_rows)
            decision_by_page = {str(row["evidence_page_id"]): row["disposition"] for row in pages}
            for group in duplicate_groups:
                if group["status"] != DuplicateResolution.SAME_SOURCE_PAGE.value:
                    continue
                group_id = str(group["duplicate_group_id"])
                included = sorted(
                    page_id
                    for page_id in members.get(group_id, ())
                    if decision_by_page.get(page_id) == PageDisposition.INCLUDE.value
                )
                canonical = str(group["canonical_page_id"])
                if included != [canonical]:
                    raise CaseLedgerPersistenceBlocked(
                        "same-source duplicates must include only the approved canonical page"
                    )
            annotation_rows = connection.execute(
                """
                SELECT annotation_id, evidence_page_id, purpose, x0, y0, x1, y1, label, approval_hash
                FROM evidence_page_annotations
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                ORDER BY evidence_page_id ASC, annotation_id ASC
                FOR SHARE
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            annotation_map = _group_annotations(annotation_rows)
            current_readiness_hash = _manifest_readiness_hash(
                matter_id=matter_id,
                matter_version=expected_version,
                page_rows=pages,
                duplicate_group_rows=duplicate_groups,
                duplicate_members=members,
                annotation_map=annotation_map,
            )
            if current_readiness_hash != readiness_hash:
                raise CaseLedgerPersistenceBlocked(
                    "evidence manifest readiness changed before lock; refresh the evidence review"
                )
            manifest_id = str(uuid4())
            derivative_sequence = 0
            entries: list[dict[str, Any]] = []
            for page in pages:
                disposition = page["disposition"]
                sequence = None
                if disposition == PageDisposition.INCLUDE.value:
                    derivative_sequence += 1
                    sequence = derivative_sequence
                page_id = str(page["evidence_page_id"])
                entries.append(
                    {
                        "evidence_page_id": page_id,
                        "evidence_file_id": str(page["evidence_file_id"]),
                        "page_number": page["page_number"],
                        "original_file_sha256": page["original_file_sha256"],
                        "decision_id": str(page["decision_id"]),
                        "disposition": disposition,
                        "derivative_sequence": sequence,
                        "annotation_ids": tuple(
                            str(item["annotation_id"]) for item in annotation_map.get(page_id, ())
                        ),
                    }
                )
            content_hash = _payload_hash(
                {
                    "matter_id": matter_id,
                    "ledger_version": expected_version,
                    "entries": entries,
                    "duplicate_groups": tuple(
                        {
                            "duplicate_group_id": str(row["duplicate_group_id"]),
                            "status": row["status"],
                            "canonical_page_id": str(row["canonical_page_id"]) if row["canonical_page_id"] else None,
                            "members": members.get(str(row["duplicate_group_id"]), ()),
                        }
                        for row in duplicate_groups
                    ),
                }
            )
            included_pages = sum(entry["disposition"] == PageDisposition.INCLUDE.value for entry in entries)
            connection.execute(
                """
                INSERT INTO evidence_manifests (
                    manifest_id, firm_id, matter_id, ledger_version, status, content_hash,
                    total_pages, included_pages, excluded_pages, approval_hash, approved_by
                ) VALUES (%s, %s, %s, %s, 'LOCKED', %s, %s, %s, %s, %s, %s)
                """,
                (
                    manifest_id,
                    actor.firm_id,
                    matter_id,
                    expected_version,
                    content_hash,
                    len(entries),
                    included_pages,
                    len(entries) - included_pages,
                    approval_hash,
                    actor.actor_id,
                ),
            )
            for entry in entries:
                connection.execute(
                    """
                    INSERT INTO evidence_manifest_pages (
                        manifest_id, evidence_page_id, decision_id, firm_id, matter_id,
                        disposition, derivative_sequence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        manifest_id,
                        entry["evidence_page_id"],
                        entry["decision_id"],
                        actor.firm_id,
                        matter_id,
                        entry["disposition"],
                        entry["derivative_sequence"],
                    ),
                )
                for annotation_id in entry["annotation_ids"]:
                    connection.execute(
                        """
                        INSERT INTO evidence_manifest_page_annotations (
                            manifest_id, evidence_page_id, annotation_id, firm_id, matter_id
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (manifest_id, entry["evidence_page_id"], annotation_id, actor.firm_id, matter_id),
                    )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_MANIFEST_LOCKED",
                object_type="EVIDENCE_MANIFEST",
                object_id=manifest_id,
                audit_payload={
                    "manifest_id": manifest_id,
                    "content_hash": content_hash,
                    "total_pages": len(entries),
                    "included_pages": included_pages,
                    "excluded_pages": len(entries) - included_pages,
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def register_derivative_candidate(
        self,
        *,
        matter_id: str,
        manifest_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        manifest_content_hash: str,
        artifact_type: str,
        storage_object_key: str,
        artifact_sha256: str,
        page_count: int,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("manifest_id", manifest_id)
        _require_roles(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_version(expected_version)
        _validate_sha256("manifest_content_hash", manifest_content_hash)
        _validate_sha256("artifact_sha256", artifact_sha256)
        if artifact_type not in {"RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"}:
            raise CaseLedgerPersistenceBlocked("unsupported evidence derivative artifact type")
        if not re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca", storage_object_key):
            raise CaseLedgerPersistenceBlocked("derivative storage key is not a managed content-addressed object")
        expected_object_key = f"{artifact_sha256[:2]}/{artifact_sha256[2:4]}/{artifact_sha256}.lca"
        if storage_object_key != expected_object_key:
            raise CaseLedgerPersistenceBlocked("derivative storage key must match the artifact SHA-256")
        if page_count < 1:
            raise CaseLedgerPersistenceBlocked("derivative page count must be positive")
        command_name = "REGISTER_EVIDENCE_DERIVATIVE_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "manifest_id": manifest_id,
            "expected_version": expected_version,
            "manifest_content_hash": manifest_content_hash,
            "artifact_type": artifact_type,
            "storage_object_key": storage_object_key,
            "artifact_sha256": artifact_sha256,
            "page_count": page_count,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=frozenset({Role.SYSTEM_WORKER}),
            )
            if prior is not None:
                return prior
            manifest = connection.execute(
                """
                SELECT content_hash, included_pages, status
                FROM evidence_manifests
                WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
                FOR SHARE
                """,
                (manifest_id, matter_id, actor.firm_id),
            ).fetchone()
            if manifest is None:
                raise KeyError(manifest_id)
            if manifest["status"] != "LOCKED" or manifest["content_hash"] != manifest_content_hash:
                raise CaseLedgerPersistenceBlocked("derivative source Manifest is no longer current or hash-bound")
            if manifest["included_pages"] != page_count:
                raise CaseLedgerPersistenceBlocked("derivative page count differs from the locked Manifest")
            derivative_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_derivative_artifacts (
                    derivative_id, firm_id, matter_id, manifest_id, artifact_type,
                    storage_object_key, artifact_sha256, page_count, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'CANDIDATE')
                """,
                (
                    derivative_id,
                    actor.firm_id,
                    matter_id,
                    manifest_id,
                    artifact_type,
                    storage_object_key,
                    artifact_sha256,
                    page_count,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_CANDIDATE_REGISTERED",
                object_type="EVIDENCE_DERIVATIVE",
                object_id=derivative_id,
                audit_payload={
                    "derivative_id": derivative_id,
                    "manifest_id": manifest_id,
                    "manifest_content_hash": manifest_content_hash,
                    "artifact_type": artifact_type,
                    "artifact_sha256": artifact_sha256,
                    "page_count": page_count,
                },
                stale_submission=False,
            )

    def verify_derivative(
        self,
        *,
        matter_id: str,
        derivative_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        verification_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("derivative_id", derivative_id)
        verification_roles = frozenset({Role.SYSTEM_WORKER, Role.LEAD_LAWYER})
        _require_roles(actor, verification_roles)
        _require_positive_version(expected_version)
        _validate_sha256("derivative verification_hash", verification_hash)
        command_name = "VERIFY_EVIDENCE_DERIVATIVE"
        payload = {
            "matter_id": matter_id,
            "derivative_id": derivative_id,
            "expected_version": expected_version,
            "verification_hash": verification_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=verification_roles,
            )
            if prior is not None:
                return prior
            derivative = connection.execute(
                """
                SELECT derivative.status, derivative.manifest_id, derivative.artifact_type,
                       derivative.artifact_sha256, manifest.status AS manifest_status
                FROM evidence_derivative_artifacts derivative
                JOIN evidence_manifests manifest
                  ON manifest.manifest_id = derivative.manifest_id
                 AND manifest.matter_id = derivative.matter_id
                 AND manifest.firm_id = derivative.firm_id
                WHERE derivative.derivative_id = %s
                  AND derivative.matter_id = %s AND derivative.firm_id = %s
                FOR UPDATE OF derivative
                """,
                (derivative_id, matter_id, actor.firm_id),
            ).fetchone()
            if derivative is None:
                raise KeyError(derivative_id)
            if derivative["status"] != "CANDIDATE" or derivative["manifest_status"] != "LOCKED":
                raise CaseLedgerPersistenceBlocked("only a candidate from the current locked Manifest can be verified")
            updated = connection.execute(
                """
                UPDATE evidence_derivative_artifacts
                SET status = 'VERIFIED', verification_hash = %s, verified_by = %s,
                    verified_at = now(), updated_at = now()
                WHERE derivative_id = %s AND matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (verification_hash, actor.actor_id, derivative_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("derivative changed before verification")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_VERIFIED",
                object_type="EVIDENCE_DERIVATIVE",
                object_id=derivative_id,
                audit_payload={
                    "derivative_id": derivative_id,
                    "manifest_id": str(derivative["manifest_id"]),
                    "artifact_type": derivative["artifact_type"],
                    "artifact_sha256": derivative["artifact_sha256"],
                    "verification_hash": verification_hash,
                },
                stale_submission=False,
            )

    def enqueue_derivative_run(
        self,
        *,
        matter_id: str,
        manifest_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        manifest_content_hash: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("manifest_id", manifest_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("manifest_content_hash", manifest_content_hash)
        _validate_sha256("approval_hash", approval_hash)
        command_name = "ENQUEUE_EVIDENCE_DERIVATIVE_RUN"
        payload = {
            "matter_id": matter_id,
            "manifest_id": manifest_id,
            "expected_version": expected_version,
            "manifest_content_hash": manifest_content_hash,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            manifest = connection.execute(
                """
                SELECT status, content_hash
                FROM evidence_manifests
                WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (manifest_id, matter_id, actor.firm_id),
            ).fetchone()
            if manifest is None:
                raise KeyError(manifest_id)
            if manifest["status"] != "LOCKED" or manifest["content_hash"] != manifest_content_hash:
                raise CaseLedgerPersistenceBlocked("derivative run requires the current hash-bound locked Manifest")
            existing = connection.execute(
                """
                SELECT status FROM evidence_derivative_runs
                WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
                  AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED')
                LIMIT 1
                """,
                (manifest_id, matter_id, actor.firm_id),
            ).fetchone()
            if existing is not None:
                raise CaseLedgerPersistenceBlocked("the locked Manifest already has an active or successful derivative run")
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO evidence_derivative_runs (
                    run_id, firm_id, matter_id, manifest_id, manifest_content_hash,
                    input_matter_version, status, created_by, approval_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED', %s, %s)
                """,
                (
                    run_id,
                    actor.firm_id,
                    matter_id,
                    manifest_id,
                    manifest_content_hash,
                    expected_version,
                    actor.actor_id,
                    approval_hash,
                ),
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_RUN_QUEUED",
                object_type="EVIDENCE_DERIVATIVE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "manifest_id": manifest_id,
                    "manifest_content_hash": manifest_content_hash,
                    "approval_hash": approval_hash,
                },
                stale_submission=False,
            )

    def claim_derivative_run(
        self,
        *,
        matter_id: str,
        run_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> DerivativeRunLease:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("run_id", run_id)
        _require_roles(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_version(expected_version)
        if lease_seconds < 30 or lease_seconds > 300:
            raise CaseLedgerPersistenceBlocked("derivative run lease must be between 30 and 300 seconds")
        command_name = "CLAIM_EVIDENCE_DERIVATIVE_RUN"
        lease_id = str(uuid5(NAMESPACE_URL, f"lawcase:{actor.firm_id}:{matter_id}:{run_id}:{idempotency_key}"))
        payload = {
            "matter_id": matter_id,
            "run_id": run_id,
            "expected_version": expected_version,
            "lease_id": lease_id,
            "lease_seconds": lease_seconds,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=frozenset({Role.SYSTEM_WORKER}),
            )
            run = connection.execute(
                """
                SELECT run.status, run.attempt_count, run.lease_id, run.lease_expires_at,
                       run.manifest_id, run.manifest_content_hash,
                       manifest.status AS manifest_status,
                       manifest.content_hash AS current_manifest_hash
                FROM evidence_derivative_runs run
                JOIN evidence_manifests manifest
                  ON manifest.manifest_id = run.manifest_id
                 AND manifest.matter_id = run.matter_id AND manifest.firm_id = run.firm_id
                WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
                FOR UPDATE OF run
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if prior is not None:
                if str(run["lease_id"]) != lease_id or run["status"] != "RUNNING":
                    raise CaseLedgerPersistenceBlocked("the replayed derivative run claim has already progressed")
                return DerivativeRunLease(
                    run_id=run_id,
                    lease_id=lease_id,
                    matter_id=matter_id,
                    manifest_id=str(run["manifest_id"]),
                    manifest_content_hash=run["manifest_content_hash"],
                    attempt_count=run["attempt_count"],
                    lease_expires_at=run["lease_expires_at"],
                    matter_version=prior.matter_version,
                )
            if run["manifest_status"] != "LOCKED" or run["current_manifest_hash"] != run["manifest_content_hash"]:
                raise CaseLedgerPersistenceBlocked("derivative run Manifest is no longer current")
            if run["attempt_count"] >= 3:
                raise CaseLedgerPersistenceBlocked("derivative run has exhausted its recovery attempts")
            claimable = run["status"] == "QUEUED" or (
                run["status"] == "RUNNING"
                and run["lease_expires_at"] is not None
                and connection.execute("SELECT %s <= now() AS expired", (run["lease_expires_at"],)).fetchone()["expired"]
            )
            if not claimable:
                raise CaseLedgerPersistenceBlocked("derivative run is not claimable")
            claimed = connection.execute(
                """
                UPDATE evidence_derivative_runs
                SET status = 'RUNNING', attempt_count = attempt_count + 1,
                    lease_id = %s, lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                RETURNING attempt_count, lease_expires_at, manifest_id, manifest_content_hash
                """,
                (lease_id, lease_seconds, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if claimed is None:
                raise CaseLedgerPersistenceBlocked("derivative run changed before claim")
            receipt = _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_RUN_CLAIMED",
                object_type="EVIDENCE_DERIVATIVE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "manifest_id": str(claimed["manifest_id"]),
                    "attempt_count": claimed["attempt_count"],
                    "lease_id": lease_id,
                },
                stale_submission=False,
            )
            return DerivativeRunLease(
                run_id=run_id,
                lease_id=lease_id,
                matter_id=matter_id,
                manifest_id=str(claimed["manifest_id"]),
                manifest_content_hash=claimed["manifest_content_hash"],
                attempt_count=claimed["attempt_count"],
                lease_expires_at=claimed["lease_expires_at"],
                matter_version=receipt.matter_version,
            )

    def complete_derivative_run(
        self,
        *,
        matter_id: str,
        run_id: str,
        lease_id: str,
        related_derivative_id: str,
        annotated_derivative_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        for label, value in (
            ("run_id", run_id),
            ("lease_id", lease_id),
            ("related_derivative_id", related_derivative_id),
            ("annotated_derivative_id", annotated_derivative_id),
        ):
            _validate_uuid(label, value)
        _require_roles(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_version(expected_version)
        command_name = "COMPLETE_EVIDENCE_DERIVATIVE_RUN"
        payload = {
            "matter_id": matter_id,
            "run_id": run_id,
            "lease_id": lease_id,
            "related_derivative_id": related_derivative_id,
            "annotated_derivative_id": annotated_derivative_id,
            "expected_version": expected_version,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=frozenset({Role.SYSTEM_WORKER}),
            )
            if prior is not None:
                return prior
            run = connection.execute(
                """
                SELECT run.status, run.lease_id, run.lease_expires_at, run.manifest_id,
                       run.manifest_content_hash, manifest.status AS manifest_status,
                       manifest.content_hash AS current_manifest_hash
                FROM evidence_derivative_runs run
                JOIN evidence_manifests manifest
                  ON manifest.manifest_id = run.manifest_id
                 AND manifest.matter_id = run.matter_id AND manifest.firm_id = run.firm_id
                WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
                FOR UPDATE OF run
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if (
                run["status"] != "RUNNING"
                or str(run["lease_id"]) != lease_id
                or run["lease_expires_at"] is None
                or connection.execute("SELECT %s > now() AS active", (run["lease_expires_at"],)).fetchone()["active"] is not True
            ):
                raise CaseLedgerPersistenceBlocked("derivative run lease is missing, expired, or replaced")
            if run["manifest_status"] != "LOCKED" or run["current_manifest_hash"] != run["manifest_content_hash"]:
                raise CaseLedgerPersistenceBlocked("derivative run Manifest is no longer current")
            artifacts = connection.execute(
                """
                SELECT derivative_id, artifact_type, status, manifest_id
                FROM evidence_derivative_artifacts
                WHERE derivative_id = ANY(%s) AND matter_id = %s AND firm_id = %s
                """,
                ([related_derivative_id, annotated_derivative_id], matter_id, actor.firm_id),
            ).fetchall()
            artifact_map = {row["artifact_type"]: row for row in artifacts if row["status"] == "VERIFIED"}
            expected = {
                "RELATED_PAGES_PDF": related_derivative_id,
                "ANNOTATED_RELATED_PAGES_PDF": annotated_derivative_id,
            }
            if set(artifact_map) != set(expected) or any(
                str(artifact_map[artifact_type]["derivative_id"]) != derivative_id
                or str(artifact_map[artifact_type]["manifest_id"]) != str(run["manifest_id"])
                for artifact_type, derivative_id in expected.items()
            ):
                raise CaseLedgerPersistenceBlocked("derivative run outputs are not both verified and Manifest-bound")
            updated = connection.execute(
                """
                UPDATE evidence_derivative_runs
                SET status = 'SUCCEEDED', lease_id = NULL, lease_expires_at = NULL,
                    related_derivative_id = %s, annotated_derivative_id = %s,
                    completed_at = now(), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s AND status = 'RUNNING'
                """,
                (related_derivative_id, annotated_derivative_id, run_id, matter_id, actor.firm_id),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("derivative run changed before completion")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_RUN_SUCCEEDED",
                object_type="EVIDENCE_DERIVATIVE_RUN",
                object_id=run_id,
                audit_payload={
                    "run_id": run_id,
                    "manifest_id": str(run["manifest_id"]),
                    "related_derivative_id": related_derivative_id,
                    "annotated_derivative_id": annotated_derivative_id,
                },
                stale_submission=False,
            )

    def renew_derivative_run_lease(
        self,
        *,
        matter_id: str,
        run_id: str,
        lease_id: str,
        actor: Actor,
        lease_seconds: int = 120,
    ) -> datetime:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("run_id", run_id)
        _validate_uuid("lease_id", lease_id)
        worker_roles = frozenset({Role.SYSTEM_WORKER})
        _require_roles(actor, worker_roles)
        if lease_seconds < 30 or lease_seconds > 300:
            raise CaseLedgerPersistenceBlocked("derivative run lease must be between 30 and 300 seconds")
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=worker_roles,
            )
            renewed = connection.execute(
                """
                UPDATE evidence_derivative_runs
                SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND lease_id = %s AND lease_expires_at > now()
                RETURNING lease_expires_at
                """,
                (lease_seconds, run_id, matter_id, actor.firm_id, lease_id),
            ).fetchone()
            if renewed is None:
                raise CaseLedgerPersistenceBlocked("derivative run lease is missing, expired, or replaced")
            return renewed["lease_expires_at"]

    def fail_derivative_run(
        self,
        *,
        matter_id: str,
        run_id: str,
        lease_id: str,
        failure_code: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("run_id", run_id)
        _validate_uuid("lease_id", lease_id)
        _require_roles(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_version(expected_version)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", failure_code):
            raise CaseLedgerPersistenceBlocked("derivative run failure_code is invalid")
        command_name = "FAIL_EVIDENCE_DERIVATIVE_RUN"
        payload = {
            "matter_id": matter_id,
            "run_id": run_id,
            "lease_id": lease_id,
            "failure_code": failure_code,
            "expected_version": expected_version,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=frozenset({Role.SYSTEM_WORKER}),
            )
            if prior is not None:
                return prior
            updated = connection.execute(
                """
                UPDATE evidence_derivative_runs
                SET status = 'FAILED', lease_id = NULL, lease_expires_at = NULL,
                    failure_code = %s, completed_at = now(), updated_at = now()
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND lease_id = %s AND lease_expires_at > now()
                """,
                (failure_code, run_id, matter_id, actor.firm_id, lease_id),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("derivative run lease is missing, expired, or replaced")
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="EVIDENCE_DERIVATIVE_RUN_FAILED",
                object_type="EVIDENCE_DERIVATIVE_RUN",
                object_id=run_id,
                audit_payload={"run_id": run_id, "failure_code": failure_code},
                stale_submission=False,
            )

    def create_local_folder_scan_candidate(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        manifest: FolderManifest,
    ) -> CaseLedgerCommandReceipt:
        """Persist an authorized local inventory without persisting its absolute root."""

        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._HUMAN_CANDIDATE_ROLES)
        _require_positive_version(expected_version)
        _validate_folder_manifest(manifest)
        command_name = "CREATE_LOCAL_FOLDER_SCAN_CANDIDATE"
        payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "scan_id": manifest.scan_id,
            "root_fingerprint": manifest.root_fingerprint,
            "manifest_hash": manifest.manifest_hash,
            "total_files": manifest.total_files,
            "total_bytes": manifest.total_bytes,
            "skipped_symlinks": manifest.skipped_symlinks,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._HUMAN_CANDIDATE_ROLES,
            )
            if prior is not None:
                return prior
            approved = connection.execute(
                """
                SELECT scan_id, root_fingerprint, manifest_hash, total_files, total_bytes,
                       skipped_symlinks, scanned_at
                FROM local_folder_scans
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                FOR SHARE
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            previous_manifest = None
            if approved is not None:
                approved_files = connection.execute(
                    """
                    SELECT relative_path, byte_size, file_sha256, detected_kind
                    FROM local_folder_scan_files
                    WHERE scan_id = %s AND matter_id = %s AND firm_id = %s AND present = true
                    ORDER BY sort_sequence ASC
                    """,
                    (approved["scan_id"], matter_id, actor.firm_id),
                ).fetchall()
                previous_manifest = FolderManifest(
                    scan_id=str(approved["scan_id"]),
                    root_fingerprint=approved["root_fingerprint"],
                    manifest_hash=approved["manifest_hash"],
                    scanned_at=approved["scanned_at"],
                    total_files=approved["total_files"],
                    total_bytes=approved["total_bytes"],
                    skipped_symlinks=approved["skipped_symlinks"],
                    originals=tuple(
                        OriginalFileRecord(
                            relative_path=row["relative_path"],
                            byte_size=row["byte_size"],
                            sha256=row["file_sha256"],
                            detected_kind=row["detected_kind"],
                        )
                        for row in approved_files
                    ),
                )
            comparison = compare_folder_manifests(manifest, previous_manifest)
            connection.execute(
                """
                UPDATE local_folder_scans
                SET status = 'INVALIDATED', invalidated_at = now()
                WHERE matter_id = %s AND firm_id = %s AND status = 'CANDIDATE'
                """,
                (matter_id, actor.firm_id),
            )
            connection.execute(
                """
                INSERT INTO local_folder_scans (
                    scan_id, firm_id, matter_id, root_fingerprint, manifest_hash,
                    base_scan_id, status, total_files, total_bytes, skipped_symlinks,
                    new_count, modified_count, moved_count, missing_count,
                    unchanged_count, duplicate_content_count, created_by, scanned_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'CANDIDATE', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    manifest.scan_id,
                    actor.firm_id,
                    matter_id,
                    manifest.root_fingerprint,
                    manifest.manifest_hash,
                    comparison.base_scan_id,
                    manifest.total_files,
                    manifest.total_bytes,
                    manifest.skipped_symlinks,
                    comparison.new_count,
                    comparison.modified_count,
                    comparison.moved_count,
                    comparison.missing_count,
                    comparison.unchanged_count,
                    comparison.duplicate_content_count,
                    actor.actor_id,
                    manifest.scanned_at,
                ),
            )
            for sequence, item in enumerate(comparison.files, start=1):
                connection.execute(
                    """
                    INSERT INTO local_folder_scan_files (
                        scan_id, firm_id, matter_id, relative_path, previous_relative_path,
                        byte_size, file_sha256, detected_kind, change_kind, present,
                        sort_sequence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        manifest.scan_id,
                        actor.firm_id,
                        matter_id,
                        item.relative_path,
                        item.previous_relative_path,
                        item.byte_size,
                        item.sha256,
                        item.detected_kind,
                        item.change_kind,
                        item.present,
                        sequence,
                    ),
                )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="LOCAL_FOLDER_SCAN_CANDIDATE_CREATED",
                object_type="LOCAL_FOLDER_SCAN",
                object_id=manifest.scan_id,
                audit_payload={
                    "scan_id": manifest.scan_id,
                    "manifest_hash": manifest.manifest_hash,
                    "total_files": manifest.total_files,
                    "new_count": comparison.new_count,
                    "modified_count": comparison.modified_count,
                    "moved_count": comparison.moved_count,
                    "missing_count": comparison.missing_count,
                    "duplicate_content_count": comparison.duplicate_content_count,
                },
                stale_submission=False,
            )

    def approve_local_folder_scan(
        self,
        *,
        matter_id: str,
        scan_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        manifest_hash: str,
        approval_hash: str,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _validate_uuid("scan_id", scan_id)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("local folder manifest_hash", manifest_hash)
        _validate_sha256("local folder approval_hash", approval_hash)
        command_name = "APPROVE_LOCAL_FOLDER_SCAN"
        payload = {
            "matter_id": matter_id,
            "scan_id": scan_id,
            "expected_version": expected_version,
            "manifest_hash": manifest_hash,
            "approval_hash": approval_hash,
        }
        payload_hash = _payload_hash(payload)
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin_or_replay(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                allowed_roles=self._DECISION_ROLES,
            )
            if prior is not None:
                return prior
            candidate = connection.execute(
                """
                SELECT scan_id, manifest_hash, status
                FROM local_folder_scans
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchone()
            if candidate is None:
                raise KeyError(scan_id)
            if candidate["status"] != "CANDIDATE" or candidate["manifest_hash"] != manifest_hash:
                raise CaseLedgerPersistenceBlocked("local folder scan changed before approval")
            connection.execute(
                """
                UPDATE local_folder_scans
                SET status = 'INVALIDATED', invalidated_at = now()
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                """,
                (matter_id, actor.firm_id),
            )
            updated = connection.execute(
                """
                UPDATE local_folder_scans
                SET status = 'APPROVED', approved_by = %s, approval_hash = %s,
                    approved_at = now()
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'CANDIDATE' AND manifest_hash = %s
                """,
                (actor.actor_id, approval_hash, scan_id, matter_id, actor.firm_id, manifest_hash),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("local folder scan changed before approval")
            _invalidate_current_evidence_outputs(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
                reason="律师批准了新的案卷文件范围，旧证据清单及派生件已失效。",
            )
            return _finish_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_version,
                command_name=command_name,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                event_type="LOCAL_FOLDER_SCAN_APPROVED",
                object_type="LOCAL_FOLDER_SCAN",
                object_id=scan_id,
                audit_payload={
                    "scan_id": scan_id,
                    "manifest_hash": manifest_hash,
                    "approval_hash": approval_hash,
                },
                stale_submission=True,
            )

    def get_local_folder_intake_summary(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentLocalFolderIntakeSummary:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter_version = _read_projection_version(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
            )
            rows = connection.execute(
                """
                SELECT scan_id, manifest_hash, base_scan_id, status, total_files,
                       total_bytes, skipped_symlinks, new_count, modified_count,
                       moved_count, missing_count, unchanged_count,
                       duplicate_content_count, scanned_at, approved_at
                FROM local_folder_scans
                WHERE matter_id = %s AND firm_id = %s
                  AND status IN ('CANDIDATE', 'APPROVED')
                ORDER BY created_at DESC, scan_id DESC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        approved = next((row for row in rows if row["status"] == "APPROVED"), None)
        candidate = next((row for row in rows if row["status"] == "CANDIDATE"), None)
        payload = {
            "matter_id": matter_id,
            "matter_version": matter_version,
            "approved_scan": _local_folder_scan_summary_payload(approved),
            "candidate_scan": _local_folder_scan_summary_payload(candidate),
        }
        return PersistentLocalFolderIntakeSummary(summary_hash=_payload_hash(payload), **payload)

    def list_local_folder_scan_file_page(
        self,
        *,
        matter_id: str,
        scan_id: str,
        actor: Actor,
        limit: int,
        cursor: str | None,
        expected_version: int | None = None,
    ) -> PersistentLocalFolderFileListPage:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("scan_id", scan_id)
        _require_roles(actor, self._READ_ROLES)
        page_limit = validate_page_limit(limit)
        decoded = (
            decode_page_cursor(cursor, expected_kind="LOCAL_FOLDER_FILES", expected_matter_id=matter_id)
            if cursor is not None
            else None
        )
        after_sequence = _local_folder_file_cursor_value(decoded, scan_id=scan_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter_version = _read_projection_version(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
            )
            _require_expected_projection_version(expected_version, matter_version)
            _require_cursor_version(decoded, matter_version)
            scan = connection.execute(
                """
                SELECT scan_id FROM local_folder_scans
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                  AND status IN ('CANDIDATE', 'APPROVED')
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchone()
            if scan is None:
                raise KeyError(scan_id)
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS total_count FROM local_folder_scan_files
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (scan_id, matter_id, actor.firm_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT relative_path, previous_relative_path, byte_size, file_sha256,
                       detected_kind, change_kind, present, sort_sequence
                FROM local_folder_scan_files
                WHERE scan_id = %s AND matter_id = %s AND firm_id = %s
                  AND sort_sequence > %s
                ORDER BY sort_sequence ASC
                LIMIT %s
                """,
                (scan_id, matter_id, actor.firm_id, after_sequence, page_limit + 1),
            ).fetchall()
        visible = rows[:page_limit]
        has_more = len(rows) > page_limit
        next_cursor = None
        if has_more and visible:
            next_cursor = encode_page_cursor(
                kind="LOCAL_FOLDER_FILES",
                matter_id=matter_id,
                matter_version=matter_version,
                sort_values=(scan_id, str(visible[-1]["sort_sequence"])),
            )
        return PersistentLocalFolderFileListPage(
            matter_id=matter_id,
            matter_version=matter_version,
            scan_id=scan_id,
            total_count=int(count_row["total_count"] if count_row else 0),
            items=tuple(_local_folder_file_payload(row) for row in visible),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def list_evidence_page(
        self,
        *,
        matter_id: str,
        actor: Actor,
        limit: int,
        cursor: str | None,
        expected_version: int | None = None,
    ) -> PersistentEvidencePageListPage:
        """Return one minimal, version-bound batch of source pages and annotations."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        page_limit = validate_page_limit(limit)
        decoded = (
            decode_page_cursor(
                cursor,
                expected_kind="EVIDENCE_PAGES",
                expected_matter_id=matter_id,
            )
            if cursor is not None
            else None
        )
        after_created_at, after_file_id, after_page_number, after_page_id = _evidence_page_cursor_values(
            decoded
        )
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter_version = _read_projection_version(
                connection,
                matter_id=matter_id,
                firm_id=actor.firm_id,
            )
            _require_expected_projection_version(expected_version, matter_version)
            _require_cursor_version(decoded, matter_version)
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS total_count
                FROM evidence_pages
                WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            base_select = """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       source.original_label, source.created_at AS source_created_at,
                       approved.decision_id, approved.disposition, approved.reason,
                       approved.status, approved.approval_hash, approved.approved_by,
                       pending.decision_id AS pending_decision_id,
                       pending.disposition AS pending_disposition,
                       pending.reason AS pending_reason,
                       pending.status AS pending_status
                FROM evidence_pages page
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                LEFT JOIN LATERAL (
                    SELECT decision_id, disposition, reason, status, approval_hash, approved_by
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'APPROVED'
                    ORDER BY updated_at DESC, decision_id DESC
                    LIMIT 1
                ) approved ON TRUE
                LEFT JOIN LATERAL (
                    SELECT decision_id, disposition, reason, status
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'CANDIDATE'
                    ORDER BY updated_at DESC, decision_id DESC
                    LIMIT 1
                ) pending ON TRUE
            """
            if after_created_at is None:
                rows = connection.execute(
                    base_select
                    + """
                    WHERE page.matter_id = %s AND page.firm_id = %s
                    ORDER BY source.created_at ASC, page.evidence_file_id ASC,
                             page.page_number ASC, page.evidence_page_id ASC
                    LIMIT %s
                    """,
                    (matter_id, actor.firm_id, page_limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    base_select
                    + """
                    WHERE page.matter_id = %s AND page.firm_id = %s
                      AND (source.created_at, page.evidence_file_id,
                           page.page_number, page.evidence_page_id) > (%s, %s, %s, %s)
                    ORDER BY source.created_at ASC, page.evidence_file_id ASC,
                             page.page_number ASC, page.evidence_page_id ASC
                    LIMIT %s
                    """,
                    (
                        matter_id,
                        actor.firm_id,
                        after_created_at,
                        after_file_id,
                        after_page_number,
                        after_page_id,
                        page_limit + 1,
                    ),
                ).fetchall()
            visible = rows[:page_limit]
            visible_ids = [str(row["evidence_page_id"]) for row in visible]
            annotation_rows = (
                connection.execute(
                    """
                    SELECT annotation_id, evidence_page_id, purpose, x0, y0, x1, y1,
                           label, status, approval_hash, approved_by
                    FROM evidence_page_annotations
                    WHERE matter_id = %s AND firm_id = %s
                      AND evidence_page_id = ANY(%s::uuid[])
                      AND status <> 'INVALIDATED'
                    ORDER BY evidence_page_id ASC, annotation_id ASC
                    """,
                    (matter_id, actor.firm_id, visible_ids),
                ).fetchall()
                if visible_ids
                else []
            )
        annotation_map = _group_annotations(annotation_rows)
        has_more = len(rows) > page_limit
        next_cursor = None
        if has_more and visible:
            tail = visible[-1]
            next_cursor = encode_page_cursor(
                kind="EVIDENCE_PAGES",
                matter_id=matter_id,
                matter_version=matter_version,
                sort_values=(
                    _utc_cursor_timestamp(tail["source_created_at"]),
                    str(tail["evidence_file_id"]),
                    str(tail["page_number"]),
                    str(tail["evidence_page_id"]),
                ),
            )
        return PersistentEvidencePageListPage(
            matter_id=matter_id,
            matter_version=matter_version,
            total_count=int(count_row["total_count"] if count_row else 0),
            items=tuple(_evidence_page_payload(row, annotation_map) for row in visible),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_evidence_review_summary(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> PersistentEvidenceReviewSummary:
        """Return evidence workflow state without transferring every source page."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter = connection.execute(
                """
                SELECT matter_id, version FROM matters
                WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            originals = connection.execute(
                """
                SELECT evidence_file_id, original_label, original_file_sha256, byte_size,
                       media_type, page_count, source_scan_fingerprint, supersedes_file_id, created_at
                FROM evidence_original_files
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY created_at ASC, evidence_file_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            readiness_pages = connection.execute(
                """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       approved.decision_id, approved.disposition,
                       pending.decision_id AS pending_decision_id
                FROM evidence_pages page
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                LEFT JOIN LATERAL (
                    SELECT decision_id, disposition
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'APPROVED'
                    ORDER BY updated_at DESC, decision_id DESC LIMIT 1
                ) approved ON TRUE
                LEFT JOIN LATERAL (
                    SELECT decision_id
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'CANDIDATE'
                    ORDER BY updated_at DESC, decision_id DESC LIMIT 1
                ) pending ON TRUE
                WHERE page.matter_id = %s AND page.firm_id = %s
                ORDER BY source.created_at ASC, page.evidence_file_id ASC,
                         page.page_number ASC, page.evidence_page_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            approved_annotations = connection.execute(
                """
                SELECT annotation_id, evidence_page_id
                FROM evidence_page_annotations
                WHERE matter_id = %s AND firm_id = %s AND status = 'APPROVED'
                ORDER BY evidence_page_id ASC, annotation_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            annotation_map = _group_annotations(approved_annotations)
            group_rows = connection.execute(
                """
                SELECT duplicate_group_id, status, canonical_page_id, approval_hash, approved_by
                FROM evidence_page_duplicate_groups
                WHERE matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                ORDER BY duplicate_group_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            member_rows = connection.execute(
                """
                SELECT member.duplicate_group_id, member.evidence_page_id,
                       page.evidence_file_id, page.page_number, source.original_label
                FROM evidence_page_duplicate_members member
                JOIN evidence_pages page
                  ON page.evidence_page_id = member.evidence_page_id
                 AND page.firm_id = member.firm_id AND page.matter_id = member.matter_id
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                WHERE member.matter_id = %s AND member.firm_id = %s
                ORDER BY member.duplicate_group_id ASC, member.evidence_page_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            member_map = _group_members(member_rows)
            member_details = _group_member_details(member_rows)
            manifest = connection.execute(
                """
                SELECT manifest_id, ledger_version, status, content_hash, total_pages,
                       included_pages, excluded_pages, approval_hash, approved_by
                FROM evidence_manifests
                WHERE matter_id = %s AND firm_id = %s AND status = 'LOCKED'
                ORDER BY created_at DESC LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            derivative_rows = connection.execute(
                """
                SELECT derivative_id, manifest_id, artifact_type, artifact_sha256,
                       page_count, status, verification_hash, verified_by, verified_at
                FROM evidence_derivative_artifacts
                WHERE matter_id = %s AND firm_id = %s AND status IN ('CANDIDATE', 'VERIFIED')
                ORDER BY created_at ASC, derivative_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            derivative_run_rows = connection.execute(
                """
                SELECT run_id, manifest_id, manifest_content_hash, input_matter_version,
                       status, attempt_count, failure_code, related_derivative_id,
                       annotated_derivative_id, created_by, created_at, updated_at, completed_at
                FROM evidence_derivative_runs
                WHERE matter_id = %s AND firm_id = %s
                  AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
                ORDER BY created_at DESC, run_id DESC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        version = matter["version"]
        readiness_hash = _manifest_readiness_hash(
            matter_id=matter_id,
            matter_version=version,
            page_rows=readiness_pages,
            duplicate_group_rows=group_rows,
            duplicate_members=member_map,
            annotation_map=annotation_map,
        )
        original_payload = tuple(_evidence_original_payload(row) for row in originals)
        groups = tuple(
            {
                "duplicate_group_id": str(row["duplicate_group_id"]),
                "status": row["status"],
                "canonical_page_id": str(row["canonical_page_id"]) if row["canonical_page_id"] else None,
                "approval_hash": row["approval_hash"],
                "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
                "members": member_details.get(str(row["duplicate_group_id"]), ()),
            }
            for row in group_rows
        )
        manifest_payload = _evidence_manifest_summary_payload(manifest)
        derivatives = tuple(_evidence_derivative_payload(row) for row in derivative_rows)
        derivative_runs = tuple(_evidence_derivative_run_payload(row) for row in derivative_run_rows)
        payload = {
            "matter_id": matter_id,
            "version": version,
            "manifest_readiness_hash": readiness_hash,
            "total_pages": len(readiness_pages),
            "unresolved_page_count": sum(row["decision_id"] is None for row in readiness_pages),
            "pending_decision_count": sum(row["pending_decision_id"] is not None for row in readiness_pages),
            "unresolved_duplicate_count": sum(
                row["status"] == DuplicateResolution.CANDIDATE.value for row in group_rows
            ),
            "original_files": original_payload,
            "duplicate_groups": groups,
            "locked_manifest": manifest_payload,
            "derivatives": derivatives,
            "derivative_runs": derivative_runs,
        }
        return PersistentEvidenceReviewSummary(summary_hash=_payload_hash(payload), **payload)

    def get_evidence_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentEvidenceSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            matter = connection.execute(
                """
                SELECT matter_id, version FROM matters
                WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            originals = connection.execute(
                """
                SELECT evidence_file_id, original_label, original_file_sha256, byte_size,
                       media_type, page_count, source_scan_fingerprint, supersedes_file_id, created_at
                FROM evidence_original_files
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY created_at ASC, evidence_file_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            page_rows = connection.execute(
                """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       page.rendered_page_sha256,
                       approved.decision_id, approved.disposition, approved.reason,
                       approved.status, approved.approval_hash, approved.approved_by,
                       pending.decision_id AS pending_decision_id,
                       pending.disposition AS pending_disposition,
                       pending.reason AS pending_reason,
                       pending.status AS pending_status
                FROM evidence_pages page
                LEFT JOIN LATERAL (
                    SELECT decision_id, disposition, reason, status, approval_hash, approved_by
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'APPROVED'
                    ORDER BY updated_at DESC, decision_id DESC
                    LIMIT 1
                ) approved ON TRUE
                LEFT JOIN LATERAL (
                    SELECT decision_id, disposition, reason, status
                    FROM evidence_page_decisions
                    WHERE evidence_page_id = page.evidence_page_id
                      AND firm_id = page.firm_id AND matter_id = page.matter_id
                      AND status = 'CANDIDATE'
                    ORDER BY updated_at DESC, decision_id DESC
                    LIMIT 1
                ) pending ON TRUE
                WHERE page.matter_id = %s AND page.firm_id = %s
                ORDER BY page.evidence_file_id ASC, page.page_number ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            annotations = connection.execute(
                """
                SELECT annotation_id, evidence_page_id, purpose, x0, y0, x1, y1,
                       label, status, approval_hash, approved_by
                FROM evidence_page_annotations
                WHERE matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                ORDER BY evidence_page_id ASC, annotation_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            annotation_map = _group_annotations(annotations)
            group_rows = connection.execute(
                """
                SELECT duplicate_group_id, status, canonical_page_id, approval_hash, approved_by
                FROM evidence_page_duplicate_groups
                WHERE matter_id = %s AND firm_id = %s AND status <> 'INVALIDATED'
                ORDER BY duplicate_group_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            member_rows = connection.execute(
                """
                SELECT duplicate_group_id, evidence_page_id
                FROM evidence_page_duplicate_members
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY duplicate_group_id ASC, evidence_page_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            member_map = _group_members(member_rows)
            manifest = connection.execute(
                """
                SELECT manifest_id, ledger_version, status, content_hash, total_pages,
                       included_pages, excluded_pages, approval_hash, approved_by, created_at
                FROM evidence_manifests
                WHERE matter_id = %s AND firm_id = %s AND status = 'LOCKED'
                ORDER BY created_at DESC LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            manifest_payload = None
            if manifest is not None:
                manifest_entries = connection.execute(
                    """
                    SELECT evidence_page_id, decision_id, disposition, derivative_sequence
                    FROM evidence_manifest_pages
                    WHERE manifest_id = %s AND matter_id = %s AND firm_id = %s
                    ORDER BY evidence_page_id ASC
                    """,
                    (manifest["manifest_id"], matter_id, actor.firm_id),
                ).fetchall()
                manifest_payload = {
                    "manifest_id": str(manifest["manifest_id"]),
                    "ledger_version": manifest["ledger_version"],
                    "status": manifest["status"],
                    "content_hash": manifest["content_hash"],
                    "total_pages": manifest["total_pages"],
                    "included_pages": manifest["included_pages"],
                    "excluded_pages": manifest["excluded_pages"],
                    "approval_hash": manifest["approval_hash"],
                    "approved_by": str(manifest["approved_by"]),
                    "entries": tuple(
                        {
                            "evidence_page_id": str(row["evidence_page_id"]),
                            "decision_id": str(row["decision_id"]),
                            "disposition": row["disposition"],
                            "derivative_sequence": row["derivative_sequence"],
                        }
                        for row in manifest_entries
                    ),
                }
            derivative_rows = connection.execute(
                """
                SELECT derivative_id, manifest_id, artifact_type, artifact_sha256,
                       page_count, status, verification_hash, verified_by, verified_at
                FROM evidence_derivative_artifacts
                WHERE matter_id = %s AND firm_id = %s AND status IN ('CANDIDATE', 'VERIFIED')
                ORDER BY created_at ASC, derivative_id ASC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
            derivative_run_rows = connection.execute(
                """
                SELECT run_id, manifest_id, manifest_content_hash, input_matter_version,
                       status, attempt_count, failure_code, related_derivative_id,
                       annotated_derivative_id, created_by, created_at, updated_at, completed_at
                FROM evidence_derivative_runs
                WHERE matter_id = %s AND firm_id = %s
                  AND status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')
                ORDER BY created_at DESC, run_id DESC
                """,
                (matter_id, actor.firm_id),
            ).fetchall()
        original_payload = tuple(
            {
                **{key: row[key] for key in ("original_label", "original_file_sha256", "byte_size", "media_type", "page_count", "source_scan_fingerprint")},
                "evidence_file_id": str(row["evidence_file_id"]),
                "supersedes_file_id": str(row["supersedes_file_id"]) if row["supersedes_file_id"] else None,
                "created_at": row["created_at"].isoformat(),
            }
            for row in originals
        )
        pages = tuple(
            {
                "evidence_page_id": str(row["evidence_page_id"]),
                "evidence_file_id": str(row["evidence_file_id"]),
                "page_number": row["page_number"],
                "rendered_page_sha256": row["rendered_page_sha256"],
                "decision": (
                    {
                        "decision_id": str(row["decision_id"]),
                        "disposition": row["disposition"],
                        "reason": row["reason"],
                        "status": row["status"],
                        "approval_hash": row["approval_hash"],
                        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
                    }
                    if row["decision_id"]
                    else None
                ),
                "pending_decision": (
                    {
                        "decision_id": str(row["pending_decision_id"]),
                        "disposition": row["pending_disposition"],
                        "reason": row["pending_reason"],
                        "status": row["pending_status"],
                        "approval_hash": None,
                        "approved_by": None,
                    }
                    if row["pending_decision_id"]
                    else None
                ),
                "annotations": tuple(_annotation_payload(item) for item in annotation_map.get(str(row["evidence_page_id"]), ())),
            }
            for row in page_rows
        )
        groups = tuple(
            {
                "duplicate_group_id": str(row["duplicate_group_id"]),
                "status": row["status"],
                "canonical_page_id": str(row["canonical_page_id"]) if row["canonical_page_id"] else None,
                "approval_hash": row["approval_hash"],
                "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
                "evidence_page_ids": member_map.get(str(row["duplicate_group_id"]), ()),
            }
            for row in group_rows
        )
        derivatives = tuple(
            {
                "derivative_id": str(row["derivative_id"]),
                "manifest_id": str(row["manifest_id"]),
                "artifact_type": row["artifact_type"],
                "artifact_sha256": row["artifact_sha256"],
                "page_count": row["page_count"],
                "status": row["status"],
                "verification_hash": row["verification_hash"],
                "verified_by": str(row["verified_by"]) if row["verified_by"] else None,
                "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
            }
            for row in derivative_rows
        )
        derivative_runs = tuple(
            {
                "run_id": str(row["run_id"]),
                "manifest_id": str(row["manifest_id"]),
                "manifest_content_hash": row["manifest_content_hash"],
                "input_matter_version": row["input_matter_version"],
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "failure_code": row["failure_code"],
                "related_derivative_id": str(row["related_derivative_id"]) if row["related_derivative_id"] else None,
                "annotated_derivative_id": str(row["annotated_derivative_id"]) if row["annotated_derivative_id"] else None,
                "created_by": str(row["created_by"]),
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            }
            for row in derivative_run_rows
        )
        snapshot_payload = {
            "matter_id": matter_id,
            "version": matter["version"],
            "original_files": original_payload,
            "pages": pages,
            "duplicate_groups": groups,
            "locked_manifest": manifest_payload,
            "derivatives": derivatives,
            "derivative_runs": derivative_runs,
        }
        return PersistentEvidenceSnapshot(snapshot_hash=_payload_hash(snapshot_payload), **snapshot_payload)

    def get_verified_derivative_locator(
        self,
        *,
        matter_id: str,
        derivative_id: str,
        actor: Actor,
    ) -> VerifiedDerivativeLocator:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("derivative_id", derivative_id)
        human_read_roles = self._READ_ROLES.difference({Role.SYSTEM_WORKER})
        _require_roles(actor, human_read_roles)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=human_read_roles,
            )
            row = connection.execute(
                """
                SELECT derivative.derivative_id, derivative.manifest_id,
                       derivative.artifact_type, derivative.storage_object_key,
                       derivative.artifact_sha256, derivative.page_count,
                       derivative.status
                FROM evidence_derivative_artifacts derivative
                JOIN evidence_manifests manifest
                  ON manifest.manifest_id = derivative.manifest_id
                 AND manifest.matter_id = derivative.matter_id
                 AND manifest.firm_id = derivative.firm_id
                WHERE derivative.derivative_id = %s
                  AND derivative.matter_id = %s
                  AND derivative.firm_id = %s
                  AND derivative.status = 'VERIFIED'
                  AND manifest.status = 'LOCKED'
                """,
                (derivative_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            raise KeyError(derivative_id)
        return VerifiedDerivativeLocator(
            firm_id=actor.firm_id,
            matter_id=matter_id,
            derivative_id=str(row["derivative_id"]),
            manifest_id=str(row["manifest_id"]),
            artifact_type=row["artifact_type"],
            object_key=row["storage_object_key"],
            artifact_sha256=row["artifact_sha256"],
            page_count=row["page_count"],
            status=row["status"],
        )

    def get_original_page_locator(
        self,
        *,
        matter_id: str,
        evidence_page_id: str,
        actor: Actor,
    ) -> OriginalPageLocator:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("evidence_page_id", evidence_page_id)
        human_read_roles = self._READ_ROLES.difference({Role.SYSTEM_WORKER})
        _require_roles(actor, human_read_roles)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=human_read_roles,
            )
            row = connection.execute(
                """
                SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
                       source.original_label, source.original_file_sha256,
                       source.byte_size, source.media_type, source.page_count
                FROM evidence_pages page
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                WHERE page.evidence_page_id = %s
                  AND page.matter_id = %s AND page.firm_id = %s
                """,
                (evidence_page_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            raise KeyError(evidence_page_id)
        return OriginalPageLocator(
            firm_id=actor.firm_id,
            matter_id=matter_id,
            evidence_page_id=str(row["evidence_page_id"]),
            evidence_file_id=str(row["evidence_file_id"]),
            original_label=row["original_label"],
            original_file_sha256=row["original_file_sha256"],
            byte_size=row["byte_size"],
            media_type=row["media_type"],
            page_count=row["page_count"],
            page_number=row["page_number"],
        )

    def _begin_or_replay(
        self,
        connection: psycopg.Connection,
        *,
        actor: Actor,
        matter_id: str,
        expected_version: int,
        command_name: str,
        idempotency_key: str,
        payload_hash: str,
        allowed_roles: frozenset[Role],
    ) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
        )
        prior = _prior_receipt(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if prior is not None:
            return prior
        _authorize_and_lock_matter(
            connection,
            actor=actor,
            matter_id=matter_id,
            expected_version=expected_version,
            allowed_roles=allowed_roles,
        )
        return None

    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        return _TenantTransaction(self._dsn, firm_id, read_only=False)

    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        return _TenantTransaction(self._dsn, firm_id, read_only=True)


class _TenantTransaction:
    def __init__(self, dsn: str, firm_id: str, *, read_only: bool) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._read_only = read_only
        self._context: Any = None
        self._connection: psycopg.Connection | None = None

    def __enter__(self) -> psycopg.Connection:
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        self._connection = self._context.__enter__()
        if self._read_only:
            self._connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self._connection.execute("SELECT set_config('app.firm_id', %s, true)", (self._firm_id,))
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None:
        return self._context.__exit__(exc_type, exc, traceback)


def _validate_uuid_set(label: str, values: tuple[str, ...], *, minimum: int) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if len(normalized) < minimum:
        raise CaseLedgerPersistenceBlocked(f"{label} requires at least {minimum} distinct UUID values")
    for value in normalized:
        _validate_uuid(label, value)
    return normalized


def _require_page(
    connection: psycopg.Connection,
    *,
    evidence_page_id: str,
    matter_id: str,
    firm_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM evidence_pages
        WHERE evidence_page_id = %s AND matter_id = %s AND firm_id = %s
        """,
        (evidence_page_id, matter_id, firm_id),
    ).fetchone()
    if row is None:
        raise KeyError(evidence_page_id)


def _invalidate_current_evidence_outputs(
    connection: psycopg.Connection,
    *,
    matter_id: str,
    firm_id: str,
    reason: str,
) -> None:
    connection.execute(
        """
        UPDATE evidence_derivative_runs run
        SET status = 'STALE', lease_id = NULL, lease_expires_at = NULL,
            related_derivative_id = NULL, annotated_derivative_id = NULL,
            stale_at = now(), stale_reason = %s, updated_at = now()
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
        (reason, matter_id, firm_id),
    )
    connection.execute(
        """
        UPDATE evidence_derivative_artifacts derivative
        SET status = 'STALE', updated_at = now(), invalidated_at = now(), invalidation_reason = %s
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
        (reason, matter_id, firm_id),
    )
    connection.execute(
        """
        UPDATE evidence_manifests
        SET status = 'INVALIDATED', invalidated_at = now()
        WHERE matter_id = %s AND firm_id = %s AND status = 'LOCKED'
        """,
        (matter_id, firm_id),
    )


def _group_members(rows: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["duplicate_group_id"]), []).append(str(row["evidence_page_id"]))
    return {key: tuple(values) for key, values in grouped.items()}


def _group_member_details(rows: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["duplicate_group_id"]), []).append(
            {
                "evidence_page_id": str(row["evidence_page_id"]),
                "evidence_file_id": str(row["evidence_file_id"]),
                "page_number": int(row["page_number"]),
                "original_label": row["original_label"],
            }
        )
    return {key: tuple(values) for key, values in grouped.items()}


def _validate_folder_manifest(manifest: FolderManifest) -> None:
    _validate_uuid("folder manifest scan_id", manifest.scan_id)
    _validate_sha256("folder manifest root_fingerprint", manifest.root_fingerprint)
    _validate_sha256("folder manifest manifest_hash", manifest.manifest_hash)
    if manifest.scanned_at.tzinfo is None:
        raise CaseLedgerPersistenceBlocked("folder manifest scanned_at must be timezone-aware")
    if manifest.total_files != len(manifest.originals) or manifest.total_files > 10_000:
        raise CaseLedgerPersistenceBlocked("folder manifest file count is invalid")
    if manifest.total_bytes != sum(item.byte_size for item in manifest.originals):
        raise CaseLedgerPersistenceBlocked("folder manifest byte total is invalid")
    if manifest.total_bytes > 10 * 1024 * 1024 * 1024 or manifest.skipped_symlinks < 0:
        raise CaseLedgerPersistenceBlocked("folder manifest safety totals are invalid")
    paths = [item.relative_path for item in manifest.originals]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise CaseLedgerPersistenceBlocked("folder manifest paths must be unique and sorted")
    for item in manifest.originals:
        if (
            not item.relative_path
            or item.relative_path.startswith("/")
            or ".." in item.relative_path.split("/")
            or item.byte_size < 0
            or item.detected_kind not in set(KNOWN_FILE_KINDS.values()).union({"OTHER"})
        ):
            raise CaseLedgerPersistenceBlocked("folder manifest contains an invalid file entry")
        _validate_sha256("folder manifest file sha256", item.sha256)
    if folder_manifest_hash(manifest.root_fingerprint, manifest.originals) != manifest.manifest_hash:
        raise CaseLedgerPersistenceBlocked("folder manifest hash does not match its file inventory")


def _local_folder_scan_summary_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "scan_id": str(row["scan_id"]),
        "manifest_hash": row["manifest_hash"],
        "base_scan_id": str(row["base_scan_id"]) if row["base_scan_id"] else None,
        "status": row["status"],
        "total_files": row["total_files"],
        "total_bytes": row["total_bytes"],
        "skipped_symlinks": row["skipped_symlinks"],
        "new_count": row["new_count"],
        "modified_count": row["modified_count"],
        "moved_count": row["moved_count"],
        "missing_count": row["missing_count"],
        "unchanged_count": row["unchanged_count"],
        "duplicate_content_count": row["duplicate_content_count"],
        "scanned_at": row["scanned_at"].isoformat(),
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
    }


def _local_folder_file_cursor_value(
    cursor: StablePageCursor | None,
    *,
    scan_id: str,
) -> int:
    if cursor is None:
        return 0
    if len(cursor.sort_values) != 2 or cursor.sort_values[0] != scan_id:
        raise StablePaginationBlocked("local folder file cursor scope is invalid")
    sequence_text = cursor.sort_values[1]
    if not sequence_text.isdigit() or sequence_text.startswith("0"):
        raise StablePaginationBlocked("local folder file cursor sequence is invalid")
    return int(sequence_text)


def _local_folder_file_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "relative_path": row["relative_path"],
        "previous_relative_path": row["previous_relative_path"],
        "byte_size": row["byte_size"],
        "file_sha256": row["file_sha256"],
        "detected_kind": row["detected_kind"],
        "change_kind": row["change_kind"],
        "present": row["present"],
    }


def _group_annotations(rows: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["evidence_page_id"]), []).append(row)
    return {key: tuple(values) for key, values in grouped.items()}


def _evidence_page_cursor_values(
    cursor: StablePageCursor | None,
) -> tuple[datetime | None, str | None, int | None, str | None]:
    if cursor is None:
        return None, None, None, None
    if len(cursor.sort_values) != 4:
        raise StablePaginationBlocked("evidence page cursor sort key is invalid")
    created_at_text, file_id_text, page_number_text, page_id_text = cursor.sort_values
    if not page_number_text.isdigit() or page_number_text.startswith("0"):
        raise StablePaginationBlocked("evidence page cursor page number is invalid")
    page_number = int(page_number_text)
    if page_number < 1:
        raise StablePaginationBlocked("evidence page cursor page number is invalid")
    return (
        _parse_utc_cursor_timestamp(created_at_text),
        _canonical_cursor_uuid(file_id_text),
        page_number,
        _canonical_cursor_uuid(page_id_text),
    )


def _manifest_readiness_hash(
    *,
    matter_id: str,
    matter_version: int,
    page_rows: list[dict[str, Any]],
    duplicate_group_rows: list[dict[str, Any]],
    duplicate_members: dict[str, tuple[str, ...]],
    annotation_map: dict[str, tuple[dict[str, Any], ...]],
) -> str:
    return _payload_hash(
        {
            "matter_id": matter_id,
            "matter_version": matter_version,
            "pages": tuple(
                {
                    "evidence_page_id": str(row["evidence_page_id"]),
                    "evidence_file_id": str(row["evidence_file_id"]),
                    "page_number": int(row["page_number"]),
                    "decision_id": str(row["decision_id"]) if row.get("decision_id") else None,
                    "disposition": row.get("disposition"),
                    "pending_decision_id": (
                        str(row["pending_decision_id"]) if row.get("pending_decision_id") else None
                    ),
                    "approved_annotation_ids": tuple(
                        str(annotation["annotation_id"])
                        for annotation in annotation_map.get(str(row["evidence_page_id"]), ())
                    ),
                }
                for row in page_rows
            ),
            "duplicate_groups": tuple(
                {
                    "duplicate_group_id": str(row["duplicate_group_id"]),
                    "status": row["status"],
                    "canonical_page_id": (
                        str(row["canonical_page_id"]) if row.get("canonical_page_id") else None
                    ),
                    "members": duplicate_members.get(str(row["duplicate_group_id"]), ()),
                }
                for row in duplicate_group_rows
            ),
        }
    )


def _evidence_page_payload(
    row: dict[str, Any],
    annotation_map: dict[str, tuple[dict[str, Any], ...]],
) -> dict[str, Any]:
    page_id = str(row["evidence_page_id"])
    return {
        "evidence_page_id": page_id,
        "evidence_file_id": str(row["evidence_file_id"]),
        "original_label": row["original_label"],
        "page_number": int(row["page_number"]),
        "decision": (
            {
                "decision_id": str(row["decision_id"]),
                "disposition": row["disposition"],
                "reason": row["reason"],
                "status": row["status"],
                "approval_hash": row["approval_hash"],
                "approved_by": str(row["approved_by"]) if row.get("approved_by") else None,
            }
            if row.get("decision_id")
            else None
        ),
        "pending_decision": (
            {
                "decision_id": str(row["pending_decision_id"]),
                "disposition": row["pending_disposition"],
                "reason": row["pending_reason"],
                "status": row["pending_status"],
                "approval_hash": None,
                "approved_by": None,
            }
            if row.get("pending_decision_id")
            else None
        ),
        "annotations": tuple(
            _annotation_payload(item) for item in annotation_map.get(page_id, ())
        ),
    }


def _evidence_original_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: row[key]
            for key in (
                "original_label",
                "original_file_sha256",
                "byte_size",
                "media_type",
                "page_count",
                "source_scan_fingerprint",
            )
        },
        "evidence_file_id": str(row["evidence_file_id"]),
        "supersedes_file_id": str(row["supersedes_file_id"]) if row["supersedes_file_id"] else None,
        "created_at": row["created_at"].isoformat(),
    }


def _evidence_manifest_summary_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "manifest_id": str(row["manifest_id"]),
        "ledger_version": row["ledger_version"],
        "status": row["status"],
        "content_hash": row["content_hash"],
        "total_pages": row["total_pages"],
        "included_pages": row["included_pages"],
        "excluded_pages": row["excluded_pages"],
        "approval_hash": row["approval_hash"],
        "approved_by": str(row["approved_by"]),
    }


def _evidence_derivative_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "derivative_id": str(row["derivative_id"]),
        "manifest_id": str(row["manifest_id"]),
        "artifact_type": row["artifact_type"],
        "artifact_sha256": row["artifact_sha256"],
        "page_count": row["page_count"],
        "status": row["status"],
        "verification_hash": row["verification_hash"],
        "verified_by": str(row["verified_by"]) if row["verified_by"] else None,
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
    }


def _evidence_derivative_run_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "manifest_id": str(row["manifest_id"]),
        "manifest_content_hash": row["manifest_content_hash"],
        "input_matter_version": row["input_matter_version"],
        "status": row["status"],
        "attempt_count": row["attempt_count"],
        "failure_code": row["failure_code"],
        "related_derivative_id": (
            str(row["related_derivative_id"]) if row["related_derivative_id"] else None
        ),
        "annotated_derivative_id": (
            str(row["annotated_derivative_id"]) if row["annotated_derivative_id"] else None
        ),
        "created_by": str(row["created_by"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }


def _annotation_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "annotation_id": str(row["annotation_id"]),
        "purpose": row["purpose"],
        "x0": str(row["x0"]),
        "y0": str(row["y0"]),
        "x1": str(row["x1"]),
        "y1": str(row["y1"]),
        "label": row["label"],
        "status": row.get("status", ReviewStatus.APPROVED.value),
        "approval_hash": row["approval_hash"],
        "approved_by": str(row["approved_by"]) if row.get("approved_by") else None,
    }
