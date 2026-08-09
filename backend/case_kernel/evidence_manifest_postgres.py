"""Guarded PostgreSQL commands for immutable evidence-page manifests.

The adapter shares the same matter version, idempotency, database membership,
audit, and outbox boundary as the fact/transaction ledger. It never deletes or
updates registered source files/pages. Formal page decisions, annotations, and
duplicate resolutions invalidate locked manifests, derivative artifacts, and
the current submission bundle in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterator
from uuid import uuid4

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
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .artifact_access import VerifiedDerivativeLocator
from .evidence_manifest import DuplicateResolution, PageDisposition, ReviewStatus
from .models import Actor, Role


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
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._DECISION_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("manifest approval_hash", approval_hash)
        command_name = "LOCK_EVIDENCE_MANIFEST"
        payload = {
            "matter_id": matter_id,
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
                       decision.decision_id, decision.disposition
                FROM evidence_pages page
                JOIN evidence_original_files source
                  ON source.evidence_file_id = page.evidence_file_id
                 AND source.firm_id = page.firm_id AND source.matter_id = page.matter_id
                LEFT JOIN evidence_page_decisions decision
                  ON decision.evidence_page_id = page.evidence_page_id
                 AND decision.firm_id = page.firm_id AND decision.matter_id = page.matter_id
                 AND decision.status = 'APPROVED'
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
                       decision.decision_id, decision.disposition, decision.reason,
                       decision.approval_hash, decision.approved_by
                FROM evidence_pages page
                LEFT JOIN evidence_page_decisions decision
                  ON decision.evidence_page_id = page.evidence_page_id
                 AND decision.firm_id = page.firm_id AND decision.matter_id = page.matter_id
                 AND decision.status = 'APPROVED'
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
                        "approval_hash": row["approval_hash"],
                        "approved_by": str(row["approved_by"]),
                    }
                    if row["decision_id"]
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
        snapshot_payload = {
            "matter_id": matter_id,
            "version": matter["version"],
            "original_files": original_payload,
            "pages": pages,
            "duplicate_groups": groups,
            "locked_manifest": manifest_payload,
            "derivatives": derivatives,
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


def _group_annotations(rows: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["evidence_page_id"]), []).append(row)
    return {key: tuple(values) for key, values in grouped.items()}


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
