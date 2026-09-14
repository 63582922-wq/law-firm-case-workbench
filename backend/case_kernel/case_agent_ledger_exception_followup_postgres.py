"""PostgreSQL port for the governed 0049 exception follow-up lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_agent_ledger_exception_followup import (
    LedgerExceptionControlHealth,
    LedgerExceptionFollowup,
    LedgerExceptionFollowupAction,
    LedgerExceptionFollowupBlocked,
    LedgerExceptionFollowupKind,
    LedgerExceptionFollowupSnapshot,
    LedgerExceptionFollowupState,
    ManagedEvidenceSourceCandidate,
    ManagedEvidenceSourceRef,
    ManagedEvidenceSourceType,
    control_transfer_request_hash,
    followup_request_hash,
    reextraction_set_satisfaction_request_hash,
    reextraction_task_binding_request_hash,
    validate_followup_action,
    validate_idempotency_key,
)
from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    _authorize_matter_read,
    _require_positive_version,
    _require_roles,
    _validate_command_identity,
    _validate_uuid,
)
from .errors import IdempotencyConflict, VersionConflict
from .models import Actor, Role


_READ_ROLES = frozenset(
    {
        Role.ASSISTANT,
        Role.COLLABORATING_LAWYER,
        Role.LEAD_LAWYER,
        Role.REVIEWER,
    }
)
_LEAD_ROLES = frozenset({Role.LEAD_LAWYER})
_WORKER_ROLES = frozenset({Role.SYSTEM_WORKER})
_VERSION_CONFLICT_SQLSTATE = "P4091"
_INTENT_CONFLICT_SQLSTATE = "P4092"


@dataclass(frozen=True)
class ReextractionTaskBindingReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    task_binding_id: str


@dataclass(frozen=True)
class ReextractionSetReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    audit_event_id: str
    graph_id: str
    followup_count: int


@dataclass(frozen=True)
class ExceptionControlTransferReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    audit_event_id: str
    control_assignment_id: str
    control_health: LedgerExceptionControlHealth


@dataclass(frozen=True)
class ExceptionControlRecoveryIntentReceipt:
    """Server-only durable fence written before a recovery run can exist."""

    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    recovery_intent_id: str
    recovery_state: str
    transfer_idempotency_key: str
    replacement_run_id: str
    run_exists: bool


@dataclass(frozen=True)
class ExceptionControlState:
    """Server-only current control cursor; never serialize to the browser."""

    matter_id: str
    control_assignment_id: str
    control_run_id: str
    control_health: LedgerExceptionControlHealth
    head_sequence: int


@dataclass(frozen=True)
class ActiveLedgerExceptionFollowupPage:
    """One bounded ACTIVE page plus the complete matter-level count."""

    total_count: int
    offset: int
    followups: tuple[LedgerExceptionFollowupSnapshot, ...]


@dataclass(frozen=True)
class ManagedEvidenceSourceCandidatePage:
    """One bounded page of sources eligible for a managed evidence request."""

    total_count: int
    offset: int
    sources: tuple[ManagedEvidenceSourceCandidate, ...]


@dataclass(frozen=True)
class FollowupEvidencePageIdPage:
    """Opaque evidence-page identities for one follow-up."""

    total_count: int
    offset: int
    evidence_page_ids: tuple[str, ...]


class PostgresCaseLedgerExceptionFollowupStore:
    """Read active heads and call only the SECURITY DEFINER command ports."""

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip() or dsn != dsn.strip():
            raise ValueError("case-Agent exception follow-up PostgreSQL DSN is required")
        self._dsn = dsn

    def list_active_followups(
        self,
        *,
        matter_id: str,
        actor: Actor,
        offset: int,
        limit: int,
    ) -> ActiveLedgerExceptionFollowupPage:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_page_request(offset=offset, limit=limit, maximum_limit=50)
        _require_roles(actor, _READ_ROLES | _WORKER_ROLES)
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES | _WORKER_ROLES,
            )
            rows = connection.execute(
                """
                SELECT followup.followup_id,
                       followup.origin_exception_decision_id,
                       followup.origin_exception_group_id,
                       followup.origin_extraction_batch_id,
                       followup.followup_kind,
                       followup.created_matter_version,
                       followup.created_at,
                       head.current_state, head.head_sequence,
                       decision.reason_code, decision.reason_note,
                       exception_group.candidate_count,
                       exception_group.canonical_reason_codes,
                       (pg_catalog.array_agg(
                           DISTINCT page.evidence_page_id
                           ORDER BY page.evidence_page_id
                       ))[1:50] AS evidence_page_ids,
                       pg_catalog.count(DISTINCT page.evidence_page_id)
                            AS evidence_page_count,
                       pg_catalog.count(*) OVER () AS active_total_count,
                       request.evidence_request_id,
                       request.acceptance_criteria,
                       control_head.current_state AS control_health,
                       CASE
                           WHEN followup.followup_kind <> 'REEXTRACTION'
                           THEN NULL
                           WHEN control_head.current_state =
                                'RECOVERY_REQUIRED'
                           THEN 'RECOVERY_REQUIRED'
                           WHEN binding_head.current_task_binding_id IS NULL
                           THEN 'WAITING_FOR_PLAN'
                           WHEN binding.expected_matter_version <>
                                    matter.version
                             OR binding.run_id <>
                                    control_assignment.control_run_id
                             OR binding.graph_id IS DISTINCT FROM
                                    agent_run.current_graph_id
                           THEN 'WAITING_FOR_REPLAN'
                           WHEN task_head.status IN (
                                'PENDING', 'READY', 'WAITING_APPROVAL',
                                'RETRYABLE'
                           ) THEN 'QUEUED'
                           WHEN task_head.status IN ('RUNNING', 'UNKNOWN')
                           THEN 'RUNNING'
                           WHEN task_head.status = 'SUCCEEDED'
                           THEN 'VERIFYING'
                           ELSE 'BLOCKED'
                       END AS automation_status
                  FROM case_agent_ledger_exception_followups followup
                  JOIN case_agent_ledger_exception_followup_heads head
                    ON head.followup_id = followup.followup_id
                   AND head.firm_id = followup.firm_id
                   AND head.matter_id = followup.matter_id
                  JOIN case_agent_ledger_exception_group_decisions decision
                    ON decision.exception_decision_id =
                        followup.origin_exception_decision_id
                   AND decision.firm_id = followup.firm_id
                   AND decision.matter_id = followup.matter_id
                  JOIN matters matter
                    ON matter.matter_id = followup.matter_id
                   AND matter.firm_id = followup.firm_id
                  JOIN case_agent_ledger_exception_control_heads control_head
                    ON control_head.firm_id = followup.firm_id
                   AND control_head.matter_id = followup.matter_id
                  JOIN case_agent_ledger_exception_control_assignments
                        control_assignment
                    ON control_assignment.control_assignment_id =
                            control_head.current_control_assignment_id
                   AND control_assignment.firm_id = control_head.firm_id
                   AND control_assignment.matter_id = control_head.matter_id
                   AND control_assignment.state_after =
                            control_head.current_state
                   AND control_assignment.assignment_sequence =
                            control_head.head_sequence
                  JOIN case_agent_ledger_exception_groups exception_group
                    ON exception_group.exception_group_id =
                        followup.origin_exception_group_id
                   AND exception_group.extraction_batch_id =
                        followup.origin_extraction_batch_id
                   AND exception_group.firm_id = followup.firm_id
                   AND exception_group.matter_id = followup.matter_id
                  JOIN case_agent_ledger_exception_group_members member
                    ON member.exception_group_id =
                        followup.origin_exception_group_id
                   AND member.extraction_batch_id =
                        followup.origin_extraction_batch_id
                   AND member.firm_id = followup.firm_id
                   AND member.matter_id = followup.matter_id
                  JOIN case_agent_ledger_extraction_candidate_pages page
                    ON page.extraction_candidate_id =
                        member.extraction_candidate_id
                   AND page.firm_id = member.firm_id
                   AND page.matter_id = member.matter_id
                  LEFT JOIN case_agent_ledger_exception_managed_evidence_requests request
                    ON request.followup_id = followup.followup_id
                   AND request.firm_id = followup.firm_id
                   AND request.matter_id = followup.matter_id
                  LEFT JOIN case_agent_ledger_exception_reextraction_task_binding_heads
                        binding_head
                    ON binding_head.followup_id = followup.followup_id
                   AND binding_head.firm_id = followup.firm_id
                   AND binding_head.matter_id = followup.matter_id
                  LEFT JOIN case_agent_ledger_exception_reextraction_task_bindings binding
                    ON binding.task_binding_id =
                        binding_head.current_task_binding_id
                   AND binding.followup_id = binding_head.followup_id
                   AND binding.firm_id = binding_head.firm_id
                   AND binding.matter_id = binding_head.matter_id
                  LEFT JOIN case_agent_runs agent_run
                    ON agent_run.run_id = control_assignment.control_run_id
                   AND agent_run.firm_id = followup.firm_id
                   AND agent_run.matter_id = followup.matter_id
                  LEFT JOIN case_agent_task_heads task_head
                    ON task_head.graph_id = binding.graph_id
                   AND task_head.task_id = binding.task_id
                   AND task_head.run_id = binding.run_id
                   AND task_head.firm_id = binding.firm_id
                   AND task_head.matter_id = binding.matter_id
                 WHERE followup.firm_id = %s
                   AND followup.matter_id = %s
                   AND head.current_state = 'ACTIVE'
                 GROUP BY followup.followup_id,
                       followup.origin_exception_decision_id,
                       followup.origin_exception_group_id,
                       followup.origin_extraction_batch_id,
                       followup.followup_kind,
                       followup.created_matter_version,
                       followup.created_at,
                       head.current_state, head.head_sequence,
                       decision.reason_code, decision.reason_note,
                       exception_group.candidate_count,
                       exception_group.canonical_reason_codes,
                       request.evidence_request_id,
                       request.acceptance_criteria,
                       control_head.current_state,
                       control_assignment.control_run_id,
                       matter.version,
                       binding_head.current_task_binding_id,
                       binding.expected_matter_version,
                       binding.run_id, binding.graph_id,
                       agent_run.current_graph_id,
                       task_head.status
                 ORDER BY followup.created_at, followup.followup_id
                 LIMIT %s OFFSET %s
                """,
                (actor.firm_id, matter_id, limit, offset),
            ).fetchall()
            if not rows:
                if offset != 0:
                    raise LedgerExceptionFollowupBlocked(
                        "active follow-up page is outside the current result set"
                    )
                return ActiveLedgerExceptionFollowupPage(
                    total_count=0,
                    offset=0,
                    followups=(),
                )
            total_count = int(rows[0]["active_total_count"])
            if total_count < offset + len(rows) or any(
                int(row["active_total_count"]) != total_count for row in rows
            ):
                raise LedgerExceptionFollowupBlocked(
                    "active follow-up page count is inconsistent"
                )
            return ActiveLedgerExceptionFollowupPage(
                total_count=total_count,
                offset=offset,
                followups=tuple(
                    _project_followup_snapshot(dict(row)) for row in rows
                ),
            )

    def list_eligible_managed_evidence_sources(
        self,
        *,
        matter_id: str,
        actor: Actor,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> ManagedEvidenceSourceCandidatePage:
        """Return opaque, current sources eligible for one managed request."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("followup_id", followup_id)
        _validate_page_request(offset=offset, limit=limit, maximum_limit=50)
        _require_roles(actor, _READ_ROLES)
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            followup = _read_followup_for_command(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                followup_id=followup_id,
            )
            if (
                followup.kind is not LedgerExceptionFollowupKind.MORE_EVIDENCE
                or followup.state is not LedgerExceptionFollowupState.ACTIVE
                or followup.managed_evidence_request_id is None
            ):
                raise LedgerExceptionFollowupBlocked(
                    "managed evidence source selection requires an active request"
                )
            rows = connection.execute(
                """
                WITH managed_request AS (
                    SELECT request.evidence_request_id, request.created_at
                      FROM case_agent_ledger_exception_managed_evidence_requests request
                     WHERE request.evidence_request_id = %s
                       AND request.followup_id = %s
                       AND request.firm_id = %s
                       AND request.matter_id = %s
                ), eligible AS (
                    SELECT 'EVIDENCE_FILE'::text AS object_type,
                           original.evidence_file_id AS object_id,
                           pg_catalog.left(COALESCE(NULLIF(pg_catalog.btrim(
                               pg_catalog.regexp_replace(
                                   original.original_label,
                                   '[[:cntrl:]]', ' ', 'g'
                               )
                           ), ''), '未命名PDF材料'), 500) AS display_label,
                           original.created_at
                      FROM evidence_original_files original
                      CROSS JOIN managed_request request
                     WHERE original.firm_id = %s
                       AND original.matter_id = %s
                       AND original.created_at > request.created_at
                       AND NOT EXISTS (
                            SELECT 1
                              FROM evidence_original_files successor
                             WHERE successor.supersedes_file_id =
                                    original.evidence_file_id
                               AND successor.firm_id = original.firm_id
                               AND successor.matter_id = original.matter_id
                       )
                    UNION ALL
                    SELECT 'MATERIAL_OBJECT'::text,
                           material.material_object_id,
                           material.original_display_name,
                           material.created_at
                      FROM case_material_objects material
                      CROSS JOIN managed_request request
                     WHERE material.firm_id = %s
                       AND material.matter_id = %s
                       AND material.created_at > request.created_at
                )
                SELECT eligible.object_type, eligible.object_id,
                       eligible.display_label, eligible.created_at,
                       pg_catalog.count(*) OVER () AS eligible_total_count
                  FROM eligible
                 WHERE NOT EXISTS (
                    SELECT 1
                      FROM case_agent_ledger_exception_evidence_source_bindings binding
                     WHERE binding.evidence_request_id = %s
                       AND binding.followup_id = %s
                       AND binding.firm_id = %s
                       AND binding.matter_id = %s
                       AND binding.source_type = eligible.object_type
                       AND binding.source_object_id = eligible.object_id
                 )
                 ORDER BY eligible.created_at, eligible.object_type,
                          eligible.object_id
                 LIMIT %s OFFSET %s
                """,
                (
                    followup.managed_evidence_request_id,
                    followup_id,
                    actor.firm_id,
                    matter_id,
                    actor.firm_id,
                    matter_id,
                    actor.firm_id,
                    matter_id,
                    followup.managed_evidence_request_id,
                    followup_id,
                    actor.firm_id,
                    matter_id,
                    limit,
                    offset,
                ),
            ).fetchall()
            if not rows:
                if offset != 0:
                    raise LedgerExceptionFollowupBlocked(
                        "managed evidence source page is outside the current result set"
                    )
                return ManagedEvidenceSourceCandidatePage(
                    total_count=0,
                    offset=0,
                    sources=(),
                )
            total_count = int(rows[0]["eligible_total_count"])
            if total_count < offset + len(rows) or any(
                int(row["eligible_total_count"]) != total_count for row in rows
            ):
                raise LedgerExceptionFollowupBlocked(
                    "managed evidence source page count is inconsistent"
                )
            return ManagedEvidenceSourceCandidatePage(
                total_count=total_count,
                offset=offset,
                sources=tuple(
                    _project_managed_evidence_source_candidate(dict(row))
                    for row in rows
                ),
            )

    def get_followup(
        self,
        *,
        matter_id: str,
        actor: Actor,
        followup_id: str,
    ) -> LedgerExceptionFollowup:
        """Read one exact head without scanning or materializing its matter."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("followup_id", followup_id)
        _require_roles(actor, _READ_ROLES)
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            return _read_followup_for_command(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                followup_id=followup_id,
            )

    def list_followup_evidence_page_ids(
        self,
        *,
        matter_id: str,
        actor: Actor,
        followup_id: str,
        offset: int,
        limit: int,
    ) -> FollowupEvidencePageIdPage:
        """Page the complete immutable source-page set for one ACTIVE head."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("followup_id", followup_id)
        _validate_page_request(offset=offset, limit=limit, maximum_limit=50)
        _require_roles(actor, _READ_ROLES)
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES,
            )
            followup = _read_followup_for_command(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                followup_id=followup_id,
            )
            if followup.state is not LedgerExceptionFollowupState.ACTIVE:
                raise LedgerExceptionFollowupBlocked(
                    "evidence pages require an active follow-up"
                )
            rows = connection.execute(
                """
                WITH source_pages AS (
                    SELECT DISTINCT page.evidence_page_id
                      FROM case_agent_ledger_exception_followups followup
                      JOIN case_agent_ledger_exception_group_members member
                        ON member.exception_group_id =
                            followup.origin_exception_group_id
                       AND member.extraction_batch_id =
                            followup.origin_extraction_batch_id
                       AND member.firm_id = followup.firm_id
                       AND member.matter_id = followup.matter_id
                      JOIN case_agent_ledger_extraction_candidate_pages page
                        ON page.extraction_candidate_id =
                            member.extraction_candidate_id
                       AND page.firm_id = member.firm_id
                       AND page.matter_id = member.matter_id
                     WHERE followup.followup_id = %s
                       AND followup.firm_id = %s
                       AND followup.matter_id = %s
                )
                SELECT evidence_page_id,
                       pg_catalog.count(*) OVER () AS source_page_total_count
                  FROM source_pages
                 ORDER BY evidence_page_id
                 LIMIT %s OFFSET %s
                """,
                (followup_id, actor.firm_id, matter_id, limit, offset),
            ).fetchall()
            if not rows:
                if offset != 0:
                    raise LedgerExceptionFollowupBlocked(
                        "follow-up evidence page is outside the current result set"
                    )
                raise LedgerExceptionFollowupBlocked(
                    "active follow-up has no evidence source pages"
                )
            total_count = int(rows[0]["source_page_total_count"])
            page_ids = tuple(str(row["evidence_page_id"]) for row in rows)
            if total_count < offset + len(page_ids):
                raise LedgerExceptionFollowupBlocked(
                    "follow-up evidence page count is inconsistent"
                )
            for page_id in page_ids:
                _validate_uuid("evidence_page_id", page_id)
            return FollowupEvidencePageIdPage(
                total_count=total_count,
                offset=offset,
                evidence_page_ids=page_ids,
            )

    def read_current_control_state(
        self,
        *,
        matter_id: str,
        actor: Actor,
    ) -> ExceptionControlState | None:
        """Read the internal matter control head after tenant authorization."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, _READ_ROLES | _WORKER_ROLES)
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_READ_ROLES | _WORKER_ROLES,
            )
            row = connection.execute(
                """
                SELECT head.current_control_assignment_id,
                       head.current_state, head.head_sequence,
                       assignment.control_run_id
                FROM case_agent_ledger_exception_control_heads head
                JOIN case_agent_ledger_exception_control_assignments assignment
                  ON assignment.control_assignment_id =
                        head.current_control_assignment_id
                 AND assignment.firm_id = head.firm_id
                 AND assignment.matter_id = head.matter_id
                 AND assignment.state_after = head.current_state
                 AND assignment.assignment_sequence = head.head_sequence
                WHERE head.firm_id = %s AND head.matter_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM case_agent_ledger_exception_followup_heads followup_head
                      WHERE followup_head.firm_id = head.firm_id
                        AND followup_head.matter_id = head.matter_id
                        AND followup_head.current_state = 'ACTIVE'
                  )
                """,
                (actor.firm_id, matter_id),
            ).fetchone()
            if row is None:
                return None
            try:
                assignment_id = str(row["current_control_assignment_id"])
                control_run_id = str(row["control_run_id"])
                _validate_uuid("control_assignment_id", assignment_id)
                _validate_uuid("control_run_id", control_run_id)
                sequence = int(row["head_sequence"])
                if sequence < 1:
                    raise ValueError("head sequence")
                return ExceptionControlState(
                    matter_id=matter_id,
                    control_assignment_id=assignment_id,
                    control_run_id=control_run_id,
                    control_health=LedgerExceptionControlHealth(
                        str(row["current_state"])
                    ),
                    head_sequence=sequence,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise LedgerExceptionFollowupBlocked(
                    "exception control head projection is invalid"
                ) from error

    def resolve_followup(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        expected_version: int,
        idempotency_key: str,
        followup_id: str,
        action: LedgerExceptionFollowupAction,
        reason_note: str | None,
        managed_evidence_sources: Sequence[ManagedEvidenceSourceRef] = (),
    ) -> CaseLedgerCommandReceipt:
        _validate_command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _require_roles(actor, _LEAD_ROLES)
        _validate_uuid("server_session_id", server_session_id)
        _validate_uuid("followup_id", followup_id)
        if not isinstance(action, LedgerExceptionFollowupAction):
            raise LedgerExceptionFollowupBlocked(
                "lawyer follow-up action is invalid"
            )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=_LEAD_ROLES,
            )
            current = _read_followup_for_command(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                followup_id=followup_id,
            )
            managed_evidence_request_id = (
                current.managed_evidence_request_id
                if action is LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE
                else None
            )
            normalized_note = validate_followup_action(
                kind=current.kind,
                action=action,
                managed_evidence_request_id=managed_evidence_request_id,
                managed_evidence_sources=managed_evidence_sources,
                reextraction_batch_id=None,
                reason_note=reason_note,
            )
            request_hash = followup_request_hash(
                matter_id=matter_id,
                expected_version=expected_version,
                followup_id=followup_id,
                action=action,
                managed_evidence_request_id=managed_evidence_request_id,
                managed_evidence_sources=managed_evidence_sources,
                reason_note=normalized_note,
            )
            source_payload = [
                {
                    "object_type": source.object_type.value,
                    "object_id": source.object_id,
                }
                for source in sorted(
                    managed_evidence_sources,
                    key=lambda item: item.canonical_ref,
                )
            ]
            try:
                result = connection.execute(
                    """
                    SELECT public.resolve_case_agent_ledger_exception_followup_from_web_session(
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        server_session_id,
                        matter_id,
                        followup_id,
                        expected_version,
                        idempotency_key,
                        action.value,
                        managed_evidence_request_id,
                        Jsonb(source_payload),
                        normalized_note,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                _raise_known_conflict(error, "follow-up")
            return _parse_mutating_receipt(
                result,
                command_name="RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP",
                matter_id=matter_id,
                followup_id=followup_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    def transfer_control_to_recovery_run(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        replacement_run_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ExceptionControlTransferReceipt:
        """Transfer the matter-level control head to a server-created run.

        ``replacement_run_id`` is an application-internal value returned by
        the governed run-creation service.  It is never accepted from a
        browser payload; PostgreSQL independently requires the current lead
        to own a new same-snapshot CREATED/PLANNING run.
        """

        _validate_command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _require_roles(actor, _LEAD_ROLES)
        _validate_uuid("server_session_id", server_session_id)
        _validate_uuid("replacement_run_id", replacement_run_id)
        request_hash = control_transfer_request_hash(
            matter_id=matter_id,
            expected_version=expected_version,
            replacement_run_id=replacement_run_id,
        )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            try:
                result = connection.execute(
                    """
                    SELECT public.transfer_case_agent_ledger_exception_control_from_web_session(
                        %s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        server_session_id,
                        matter_id,
                        replacement_run_id,
                        expected_version,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                _raise_known_conflict(error, "exception control transfer")
            return _parse_control_transfer_receipt(
                result,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    def prepare_control_recovery(
        self,
        *,
        matter_id: str,
        actor: Actor,
        server_session_id: str,
        replacement_run_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ExceptionControlRecoveryIntentReceipt:
        """Persist the non-runnable replacement identity before RUN_CREATED.

        The same server-derived run id and transfer idempotency key are used
        by the subsequent transfer.  PostgreSQL binds them to the current
        RECOVERY_REQUIRED assignment, so a process crash can leave only a
        fenced PENDING intent, never a claimable orphan run.
        """

        _validate_command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _require_roles(actor, _LEAD_ROLES)
        _validate_uuid("server_session_id", server_session_id)
        _validate_uuid("replacement_run_id", replacement_run_id)
        request_hash = control_transfer_request_hash(
            matter_id=matter_id,
            expected_version=expected_version,
            replacement_run_id=replacement_run_id,
        )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            try:
                result = connection.execute(
                    """
                    SELECT public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(
                        %s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        server_session_id,
                        matter_id,
                        replacement_run_id,
                        expected_version,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                _raise_known_conflict(error, "exception control recovery intent")
            return _parse_control_recovery_intent_receipt(
                result,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    def bind_reextraction_task(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        followup_id: str,
        run_id: str,
        graph_id: str,
        task_id: str,
    ) -> ReextractionTaskBindingReceipt:
        _validate_command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _require_dedicated_worker(actor)
        for label, value in (
            ("followup_id", followup_id),
            ("run_id", run_id),
            ("graph_id", graph_id),
            ("task_id", task_id),
        ):
            _validate_uuid(label, value)
        request_hash = reextraction_task_binding_request_hash(
            matter_id=matter_id,
            expected_version=expected_version,
            followup_id=followup_id,
            run_id=run_id,
            graph_id=graph_id,
            task_id=task_id,
        )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            try:
                result = connection.execute(
                    """
                    SELECT public.bind_case_agent_ledger_exception_reextraction_task_from_worker(
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        actor.actor_id,
                        actor.firm_id,
                        matter_id,
                        followup_id,
                        run_id,
                        graph_id,
                        task_id,
                        expected_version,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                _raise_known_conflict(error, "re-extraction task binding")
            return _parse_task_binding_receipt(
                result,
                matter_id=matter_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )

    def satisfy_reextraction_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        graph_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> ReextractionSetReceipt:
        _validate_command(
            matter_id=matter_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        _require_dedicated_worker(actor)
        _validate_uuid("run_id", run_id)
        _validate_uuid("graph_id", graph_id)
        request_hash = reextraction_set_satisfaction_request_hash(
            matter_id=matter_id,
            expected_version=expected_version,
            run_id=run_id,
            graph_id=graph_id,
        )
        with _TenantTransaction(self._dsn, actor.firm_id) as connection:
            try:
                result = connection.execute(
                    """
                    SELECT public.satisfy_case_agent_ledger_reextraction_set_from_worker(
                        %s,%s,%s,%s,%s,%s,%s,%s
                    ) AS receipt
                    """,
                    (
                        actor.actor_id,
                        actor.firm_id,
                        matter_id,
                        run_id,
                        graph_id,
                        expected_version,
                        idempotency_key,
                        request_hash,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                _raise_known_conflict(error, "re-extraction set satisfaction")
            return _parse_reextraction_set_receipt(
                result,
                matter_id=matter_id,
                graph_id=graph_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )


def preflight_case_agent_ledger_exception_followup_schema(
    *,
    dsn: str,
    firm_id: str,
) -> None:
    """Fail closed unless every 0049 authority boundary is installed."""

    _validate_uuid("firm_id", firm_id)
    required_tables = {
        "case_agent_ledger_exception_followups",
        "case_agent_ledger_exception_control_assignments",
        "case_agent_ledger_exception_control_heads",
        "case_agent_ledger_exception_managed_evidence_requests",
        "case_agent_ledger_exception_evidence_source_bindings",
        "case_agent_ledger_exception_followup_events",
        "case_agent_ledger_exception_followup_heads",
        "case_agent_ledger_exception_reextraction_task_bindings",
        "case_agent_ledger_exception_reextraction_task_binding_heads",
        "case_agent_ledger_exception_reextraction_bindings",
        "case_agent_ledger_exception_duplicate_dispositions",
        "case_agent_ledger_exception_duplicate_heads",
        "case_agent_ledger_exception_recovery_intents",
        "case_agent_ledger_exception_recovery_intent_heads",
        "case_agent_ledger_exception_recovery_quarantines",
    }
    # tgtype values are PostgreSQL's ROW(1) + BEFORE(2) +
    # INSERT(4)/DELETE(8)/UPDATE(16) bitmask.  Binding names to their exact
    # relation/function/timing prevents an enabled same-name decoy trigger
    # from satisfying startup readiness.
    required_triggers = {
        "case_agent_ledger_exception_decision_initializes_lifecycle": (
            "case_agent_ledger_exception_group_decisions",
            "initialize_case_agent_ledger_exception_lifecycle_trigger",
            5,
            False,
        ),
        "case_agent_exception_followup_enqueues_snapshot_refresh": (
            "outbox_events",
            "enqueue_case_agent_snapshot_refresh_from_exception_followup",
            5,
            "(new.event_type = ANY (ARRAY['CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED'::text, 'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'::text, 'CASE_LEDGER_EXCEPTION_MORE_EVIDENCE_CONFIRMED'::text, 'CASE_LEDGER_EXCEPTION_DEFER_RESUMED'::text, 'CASE_LEDGER_EXCEPTION_FOLLOWUP_WITHDRAWN'::text, 'CASE_LEDGER_EXCEPTION_FOLLOWUP_SUPERSEDED'::text]))",
        ),
        "case_agent_work_plan_active_exception_followup_block": (
            "case_agent_work_plan_promotions",
            "block_active_exception_followup_work_plan_promotion",
            7,
            False,
        ),
        "case_work_plan_active_exception_followup_activation_block": (
            "case_work_plans",
            "block_active_exception_followup_work_plan_activation",
            23,
            False,
        ),
        "case_agent_ledger_exception_followup_heads_guard": (
            "case_agent_ledger_exception_followup_heads",
            "guard_case_agent_ledger_exception_followup_head",
            27,
            False,
        ),
        "case_agent_ledger_exception_followups_append_only": (
            "case_agent_ledger_exception_followups",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_evidence_requests_append_only": (
            "case_agent_ledger_exception_managed_evidence_requests",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_followup_events_append_only": (
            "case_agent_ledger_exception_followup_events",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_reextraction_bindings_append_only": (
            "case_agent_ledger_exception_reextraction_bindings",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_reextraction_tasks_append_only": (
            "case_agent_ledger_exception_reextraction_task_bindings",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_reextraction_task_heads_guard": (
            "case_agent_ledger_exception_reextraction_task_binding_heads",
            "guard_case_agent_ledger_exception_reextraction_task_binding_hea",
            27,
            False,
        ),
        "case_agent_ledger_exception_evidence_sources_append_only": (
            "case_agent_ledger_exception_evidence_source_bindings",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_duplicate_dispositions_append_only": (
            "case_agent_ledger_exception_duplicate_dispositions",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_duplicate_heads_guard": (
            "case_agent_ledger_exception_duplicate_heads",
            "guard_case_agent_ledger_exception_duplicate_head",
            27,
            False,
        ),
        "case_agent_exception_control_run_refresh_materializes": (
            "outbox_events",
            "enqueue_case_agent_exception_control_run_refresh",
            5,
            "(new.event_type = 'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED'::text)",
        ),
        "zz_case_agent_exception_control_run_refresh_fanout": (
            "outbox_events",
            "enqueue_case_agent_exception_control_run_refresh_outbox",
            5,
            False,
        ),
        "case_agent_active_exception_control_run_cancel_block": (
            "case_agent_events",
            "block_active_exception_control_run_cancellation",
            7,
            "(new.event_type = 'RUN_CANCELLED'::text)",
        ),
        "case_agent_ledger_exception_control_assignments_append_only": (
            "case_agent_ledger_exception_control_assignments",
            "prohibit_case_agent_ledger_exception_lifecycle_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_control_heads_guard": (
            "case_agent_ledger_exception_control_heads",
            "guard_case_agent_ledger_exception_control_head",
            27,
            False,
        ),
        "case_agent_ledger_exception_control_failure_requires_recovery": (
            "case_agent_events",
            "mark_case_agent_ledger_exception_control_recovery_required",
            5,
            "(new.event_type = 'VERIFICATION_FAILED'::text)",
        ),
        "case_agent_snapshot_refresh_requests_guard": (
            "case_agent_snapshot_refresh_requests",
            "guard_case_agent_snapshot_refresh_request",
            27,
            False,
        ),
        "case_agent_ledger_exception_recovery_intents_append_only": (
            "case_agent_ledger_exception_recovery_intents",
            "prohibit_case_agent_ledger_exception_recovery_intent_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_recovery_quarantines_append_only": (
            "case_agent_ledger_exception_recovery_quarantines",
            "prohibit_case_agent_ledger_exception_recovery_intent_mutation",
            27,
            False,
        ),
        "case_agent_ledger_exception_recovery_intent_heads_guard": (
            "case_agent_ledger_exception_recovery_intent_heads",
            "guard_case_agent_ledger_exception_recovery_intent_head",
            27,
            False,
        ),
        "case_agent_ledger_exception_recovery_goal_bind": (
            "case_agent_ledger_exception_recovery_intents",
            "bind_case_agent_ledger_exception_recovery_goal",
            7,
            False,
        ),
        "case_agent_runs_recovery_goal_binding_guard": (
            "case_agent_runs",
            "guard_case_agent_recovery_run_goal_binding",
            23,
            False,
        ),
    }
    required_procedures = {
        "public.resolve_case_agent_ledger_exception_followup_from_web_session(uuid,uuid,uuid,integer,text,text,uuid,jsonb,text,text)",
        "public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(uuid,uuid,uuid,integer,text,text)",
        "public.transfer_case_agent_ledger_exception_control_from_web_session(uuid,uuid,uuid,integer,text,text)",
        "public.bind_case_agent_ledger_exception_reextraction_task_from_worker(uuid,uuid,uuid,uuid,uuid,uuid,uuid,integer,text,text)",
        "public.satisfy_case_agent_ledger_reextraction_set_from_worker(uuid,uuid,uuid,uuid,uuid,integer,text,text)",
    }
    denied_procedures = {
        "public.satisfy_case_agent_ledger_reextraction_followup_from_worker(uuid,uuid,uuid,uuid,uuid,integer,text,text)",
    }
    internal_definers = {
        "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
        "public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()",
        "public.initialize_case_agent_ledger_exception_lifecycle(uuid,uuid,uuid)",
        "public.initialize_case_agent_ledger_exception_lifecycle_trigger()",
        "public.enqueue_case_agent_exception_control_run_refresh_outbox()",
        "public.enqueue_case_agent_exception_control_run_refresh()",
        "public.block_active_exception_control_run_cancellation()",
        "public.mark_case_agent_ledger_exception_control_recovery_required()",
        "public.wake_case_agent_run()",
        "public.case_agent_ledger_exception_recovery_goal_hash(uuid,uuid)",
        "public.bind_case_agent_ledger_exception_recovery_goal()",
        "public.guard_case_agent_recovery_run_goal_binding()",
        "public.case_agent_prepare_recovery_v2_inner(uuid,uuid,uuid,integer,text,text)",
        "public.case_agent_transfer_recovery_v2_inner(uuid,uuid,uuid,integer,text,text)",
    }
    internal_definer_search_paths = {
        signature: (
            "search_path=pg_catalog, public, pg_temp"
            if signature in {
                "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
                "public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()",
            }
            else "search_path=pg_catalog"
        )
        for signature in internal_definers
    }
    recovery_contract_functions = {
        "public.guard_case_agent_ledger_exception_recovery_intent_head()":
            "lawcase.case-agent-control-recovery.contract-v2",
        "public.wake_case_agent_run()":
            "lawcase.case-agent-control-recovery.contract-v2",
        "public.prepare_case_agent_ledger_exception_control_recovery_from_web_session(uuid,uuid,uuid,integer,text,text)":
            "lawcase.case-agent-control-recovery.contract-v3",
        "public.transfer_case_agent_ledger_exception_control_from_web_session(uuid,uuid,uuid,integer,text,text)":
            "lawcase.case-agent-control-recovery.contract-v3",
        "public.bind_case_agent_ledger_exception_recovery_goal()":
            "lawcase.case-agent-control-recovery.contract-v3",
        "public.guard_case_agent_recovery_run_goal_binding()":
            "lawcase.case-agent-control-recovery.contract-v3",
    }
    recovery_contract_columns = {
        "case_agent_ledger_exception_recovery_intents": {
            "recovery_intent_id": ("uuid", "NO"),
            "firm_id": ("uuid", "NO"),
            "matter_id": ("uuid", "NO"),
            "source_control_assignment_id": ("uuid", "NO"),
            "replacement_run_id": ("uuid", "NO"),
            "actor_id": ("uuid", "NO"),
            "prepared_web_session_id": ("uuid", "NO"),
            "expected_matter_version": ("int4", "NO"),
            "idempotency_key": ("text", "NO"),
            "request_hash": ("bpchar", "NO"),
            "prepared_at": ("timestamptz", "NO"),
            "recovery_goal_id": ("uuid", "NO"),
            "recovery_goal_hash": ("bpchar", "NO"),
        },
        "case_agent_ledger_exception_recovery_intent_heads": {
            "recovery_intent_id": ("uuid", "NO"),
            "firm_id": ("uuid", "NO"),
            "matter_id": ("uuid", "NO"),
            "current_outcome": ("text", "NO"),
            "transfer_control_assignment_id": ("uuid", "YES"),
            "outcome_reason_code": ("text", "YES"),
            "outcome_actor_id": ("uuid", "YES"),
            "outcome_web_session_id": ("uuid", "YES"),
            "outcome_at": ("timestamptz", "YES"),
            "outcome_version": ("int4", "NO"),
            "updated_at": ("timestamptz", "NO"),
        },
        "case_agent_ledger_exception_recovery_quarantines": {
            "run_id": ("uuid", "NO"),
            "firm_id": ("uuid", "NO"),
            "matter_id": ("uuid", "NO"),
            "source_control_assignment_id": ("uuid", "NO"),
            "reason_code": ("text", "NO"),
            "quarantined_at": ("timestamptz", "NO"),
        },
    }
    # 0049 can call trigger/helper code installed by 0046 and 0047 from a
    # pg_catalog-only SECURITY DEFINER.  Fixing historical migration text does
    # not repair an already-upgraded database, so 0049 alters and attests every
    # reachable helper explicitly.
    required_hardened_procedures = {
        "public.enqueue_case_agent_snapshot_refresh_from_ledger_confirmation()",
        "public.wake_case_agent_run_for_snapshot_refresh()",
        "public.guard_case_agent_snapshot_refresh_request()",
        "public.validate_case_agent_ledger_exception_decision()",
        "public.audit_case_agent_ledger_exception_decision()",
        "public.case_agent_ledger_extraction_target_matches_candidate(uuid,uuid,uuid,uuid,text,uuid)",
        "public.enforce_case_agent_ledger_extraction_promotion_target()",
        "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
        "public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()",
        "public.materialize_case_agent_ledger_exception_groups(uuid,uuid,uuid)",
        "public.validate_case_agent_ledger_exception_group_integrity(uuid,uuid,uuid)",
        "public.enforce_case_agent_ledger_exception_group_integrity()",
        "public.group_case_agent_ledger_exceptions_after_staging()",
        "public.case_agent_ledger_extraction_batch_review_status(uuid,uuid,uuid)",
        "public.case_agent_ledger_extraction_run_staging_complete(uuid,uuid,uuid)",
        "public.case_agent_ledger_extraction_current_review_version(uuid,uuid,uuid)",
        "public.case_agent_ledger_extraction_run_review_resolved(uuid,uuid,uuid)",
        "public.unblock_case_agent_snapshot_refresh_after_exception_decision()",
        "public.normalize_case_agent_snapshot_refresh_review_gate()",
        "public.enqueue_case_agent_snapshot_refresh_from_exception_only_run()",
        "public.block_unresolved_ledger_review_work_plan_promotion()",
        "public.block_unresolved_ledger_review_work_plan_activation()",
    }
    owner_read_tables = {
        "web_sessions",
        "users",
        "matters",
        "matter_actor_roles",
        "command_idempotency",
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_candidates",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_extraction_promotions",
        "case_agent_ledger_extraction_batch_confirmations",
        "case_agent_runs",
        "case_agent_goals",
        "case_agent_task_graphs",
        "case_agent_tasks",
        "case_agent_verification_attempts",
        "case_agent_verification_receipts",
        "case_agent_work_plan_promotions",
        "audit_events",
        "evidence_pages",
        "evidence_original_files",
        "case_facts",
        "case_transactions",
        "case_agent_ledger_exception_groups",
        "case_agent_ledger_exception_group_members",
        "case_agent_ledger_exception_group_decisions",
        "case_agent_snapshot_refresh_requests",
        "case_agent_run_inbox",
        "case_material_objects",
        "case_agent_task_heads",
        "case_agent_events",
        "outbox_events",
        "case_agent_ledger_exception_recovery_intents",
        "case_agent_ledger_exception_recovery_intent_heads",
        "case_agent_ledger_exception_recovery_quarantines",
    }
    restricted_runtime_tables = {
        "case_agent_ledger_exception_control_assignments",
        "case_agent_ledger_exception_followup_events",
        "case_agent_ledger_exception_recovery_intents",
        "case_agent_ledger_exception_recovery_intent_heads",
        "case_agent_ledger_exception_recovery_quarantines",
    }
    runtime_full_read_tables = (required_tables - restricted_runtime_tables) | {
        "users",
        "matters",
        "matter_actor_roles",
        "case_agent_ledger_extraction_batches",
        "case_agent_ledger_extraction_candidate_pages",
        "case_agent_ledger_exception_groups",
        "case_agent_ledger_exception_group_members",
        "case_agent_ledger_exception_group_decisions",
        "case_agent_runs",
        "case_agent_task_heads",
        "evidence_original_files",
        "case_material_objects",
    }
    # These are the exact columns referenced by direct Python Web/Worker SQL.
    # Session UUIDs stay behind the owner-bound definer commands and are never
    # part of an application projection.
    runtime_column_read_contract = {
        "lawcase_web_application": {
            "case_agent_ledger_exception_control_assignments": {
                "control_assignment_id",
                "firm_id",
                "matter_id",
                "assignment_sequence",
                "control_run_id",
                "state_after",
            },
        },
        "lawcase_agent_worker": {
            "case_agent_ledger_exception_control_assignments": {
                "control_assignment_id",
                "firm_id",
                "matter_id",
                "assignment_sequence",
                "control_run_id",
                "state_after",
            },
            "case_agent_ledger_exception_followup_events": {
                "followup_event_id",
                "followup_id",
                "event_sequence",
                "firm_id",
                "matter_id",
                "subject_hash",
                "state_after",
                "expected_matter_version",
                "event_hash",
            },
            "case_agent_ledger_exception_recovery_intents": {
                "recovery_intent_id",
                "firm_id",
                "matter_id",
                "replacement_run_id",
                "actor_id",
                "recovery_goal_id",
                "recovery_goal_hash",
            },
            "case_agent_ledger_exception_recovery_intent_heads": {
                "recovery_intent_id",
                "firm_id",
                "matter_id",
                "current_outcome",
                "transfer_control_assignment_id",
            },
            "case_agent_ledger_exception_recovery_quarantines": {
                "run_id",
                "firm_id",
                "matter_id",
            },
        },
    }
    sensitive_session_columns = {
        "case_agent_ledger_exception_control_assignments": "web_session_id",
        "case_agent_ledger_exception_followup_events": "web_session_id",
        "case_agent_ledger_exception_recovery_intents":
            "prepared_web_session_id",
        "case_agent_ledger_exception_recovery_intent_heads":
            "outcome_web_session_id",
    }
    owner_insert_tables = required_tables | {
        "audit_events",
        "outbox_events",
        "command_idempotency",
        "case_agent_snapshot_refresh_requests",
    }
    owner_update_tables = {
        "case_agent_ledger_exception_control_heads",
        "case_agent_ledger_exception_followup_heads",
        "case_agent_ledger_exception_reextraction_task_binding_heads",
        "case_agent_ledger_exception_duplicate_heads",
        "case_agent_snapshot_refresh_requests",
        "case_agent_run_inbox",
        "case_agent_ledger_exception_recovery_intent_heads",
    }
    try:
        with _TenantTransaction(dsn, firm_id) as connection:
            owner_role = connection.execute(
                """
                SELECT NOT owner.rolcanlogin AS no_login,
                       NOT owner.rolinherit AS no_inherit,
                       NOT owner.rolsuper AS not_super,
                       NOT owner.rolbypassrls AS no_bypass_rls,
                       NOT pg_catalog.pg_has_role(
                           'lawcase_web_application', owner.rolname, 'MEMBER'
                       ) AS web_not_member,
                       NOT pg_catalog.pg_has_role(
                           'lawcase_agent_worker', owner.rolname, 'MEMBER'
                       ) AS worker_not_member,
                       NOT pg_catalog.has_schema_privilege(
                           'lawcase_web_application', 'public', 'CREATE'
                       ) AS web_cannot_shadow,
                       NOT pg_catalog.has_schema_privilege(
                           'lawcase_agent_worker', 'public', 'CREATE'
                       ) AS worker_cannot_shadow,
                       NOT pg_catalog.has_schema_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public', 'CREATE'
                       ) AS owner_cannot_shadow,
                       NOT EXISTS (
                           SELECT 1
                             FROM pg_catalog.pg_namespace namespace,
                                  LATERAL pg_catalog.aclexplode(
                                      COALESCE(
                                          namespace.nspacl,
                                          pg_catalog.acldefault(
                                              'n'::"char", namespace.nspowner
                                          )
                                      )
                                  ) acl
                            WHERE namespace.nspname = 'public'
                              AND acl.grantee = 0
                              AND acl.privilege_type = 'CREATE'
                       ) AS public_cannot_shadow
                  FROM pg_catalog.pg_roles owner
                 WHERE owner.rolname = 'lawcase_ledger_confirmation_owner'
                """
            ).fetchone()
            if owner_role is None or any(
                owner_role[key] is not True
                for key in (
                    "no_login",
                    "no_inherit",
                    "not_super",
                    "no_bypass_rls",
                    "web_not_member",
                    "worker_not_member",
                    "web_cannot_shadow",
                    "worker_cannot_shadow",
                    "owner_cannot_shadow",
                    "public_cannot_shadow",
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 owner role is not an isolated NOBYPASSRLS authority"
                )
            table_rows = connection.execute(
                """
                SELECT relation.relname, relation.relrowsecurity,
                       relation.relforcerowsecurity,
                       owner.rolname AS owner_name
                  FROM pg_catalog.pg_class relation
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_catalog.pg_roles owner
                    ON owner.oid = relation.relowner
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                """,
                (list(required_tables),),
            ).fetchall()
            table_state = {
                str(row["relname"]): (
                    bool(row["relrowsecurity"]),
                    bool(row["relforcerowsecurity"]),
                    str(row["owner_name"]),
                )
                for row in table_rows
            }
            if set(table_state) != required_tables or any(
                state != (True, True, "lawcase_ledger_confirmation_owner")
                for state in table_state.values()
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 follow-up schema requires all FORCE RLS tables"
                )
            recovery_column_rows = connection.execute(
                """
                SELECT relation.relname AS table_name,
                       attribute.attname AS column_name,
                       data_type.typname AS udt_name,
                       CASE WHEN attribute.attnotnull THEN 'NO' ELSE 'YES' END
                           AS is_nullable
                  FROM pg_catalog.pg_class relation
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_catalog.pg_attribute attribute
                    ON attribute.attrelid = relation.oid
                   AND attribute.attnum > 0
                   AND NOT attribute.attisdropped
                  JOIN pg_catalog.pg_type data_type
                    ON data_type.oid = attribute.atttypid
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                """,
                (list(recovery_contract_columns),),
            ).fetchall()
            observed_recovery_columns: dict[
                str, dict[str, tuple[str, str]]
            ] = {}
            for row in recovery_column_rows:
                observed_recovery_columns.setdefault(
                    str(row["table_name"]), {}
                )[str(row["column_name"])] = (
                    str(row["udt_name"]), str(row["is_nullable"])
                )
            if any(
                observed_recovery_columns.get(table_name) != columns
                for table_name, columns in recovery_contract_columns.items()
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0050 recovery outcome columns do not match the exact contract"
                )
            recovery_constraint_row = connection.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint constraint_row
                         WHERE constraint_row.conrelid =
                            'public.case_agent_ledger_exception_recovery_intent_heads'::regclass
                           AND constraint_row.contype = 'c'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%ABANDONED%%'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%outcome_reason_code%%'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%outcome_web_session_id%%'
                    ) AS head_terminal_contract,
                    NOT EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint constraint_row
                         WHERE constraint_row.conrelid =
                            'public.case_agent_ledger_exception_recovery_intents'::regclass
                           AND constraint_row.contype = 'u'
                           AND constraint_row.conkey = ARRAY[(
                                SELECT attribute.attnum
                                  FROM pg_catalog.pg_attribute attribute
                                 WHERE attribute.attrelid =
                                    constraint_row.conrelid
                                   AND attribute.attname =
                                    'source_control_assignment_id'
                           )]::smallint[]
                    ) AS source_history_allows_terminal_replacement,
                    EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_index index_row
                          JOIN pg_catalog.pg_class index_relation
                            ON index_relation.oid = index_row.indexrelid
                         WHERE index_relation.relname =
                            'case_agent_ledger_exception_one_pending_recovery'
                           AND index_relation.relnamespace =
                                'public'::regnamespace
                           AND index_row.indrelid =
                                'public.case_agent_ledger_exception_recovery_intent_heads'::regclass
                           AND index_row.indisunique
                           AND index_row.indnkeyatts = 2
                           AND index_row.indkey[0] = (
                                SELECT attribute.attnum
                                  FROM pg_catalog.pg_attribute attribute
                                 WHERE attribute.attrelid = index_row.indrelid
                                   AND attribute.attname = 'firm_id'
                           )
                           AND index_row.indkey[1] = (
                                SELECT attribute.attnum
                                  FROM pg_catalog.pg_attribute attribute
                                 WHERE attribute.attrelid = index_row.indrelid
                                   AND attribute.attname = 'matter_id'
                           )
                           AND pg_catalog.pg_get_expr(
                                index_row.indpred, index_row.indrelid, true
                           ) LIKE '%%current_outcome = ''PENDING''%%'
                    ) AS one_pending_contract,
                    EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint constraint_row
                         WHERE constraint_row.conrelid =
                            'public.case_agent_ledger_exception_recovery_quarantines'::regclass
                           AND constraint_row.contype = 'c'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%LEGACY_PRE_INTENT_RECOVERY_RUN%%'
                    ) AS legacy_quarantine_contract,
                    EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint constraint_row
                         WHERE constraint_row.conrelid =
                            'public.case_agent_ledger_exception_recovery_intents'::regclass
                           AND constraint_row.contype = 'c'
                           AND constraint_row.conname =
                            'case_agent_ledger_exception_recovery_goal_hash_exact'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%recovery_goal_hash%%'
                           AND pg_catalog.pg_get_constraintdef(
                                constraint_row.oid, true
                           ) LIKE '%%recovery_goal_id%%'
                    ) AS recovery_goal_hash_contract
                """
            ).fetchone()
            if recovery_constraint_row is None or any(
                recovery_constraint_row[key] is not True
                for key in (
                    "head_terminal_contract",
                    "source_history_allows_terminal_replacement",
                    "one_pending_contract",
                    "legacy_quarantine_contract",
                    "recovery_goal_hash_contract",
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0050 recovery outcome constraints do not match the exact contract"
                )
            trigger_rows = connection.execute(
                """
                SELECT trigger.tgname, trigger.tgenabled,
                       relation.relname AS relation_name,
                       procedure.proname AS function_name,
                       procedure_namespace.nspname AS function_schema,
                       trigger.tgtype,
                       trigger.tgqual IS NOT NULL AS has_when,
                       pg_catalog.pg_get_triggerdef(
                           trigger.oid, true
                       ) AS trigger_definition
                  FROM pg_catalog.pg_trigger trigger
                  JOIN pg_catalog.pg_class relation
                    ON relation.oid = trigger.tgrelid
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid = trigger.tgfoid
                  JOIN pg_catalog.pg_namespace procedure_namespace
                    ON procedure_namespace.oid = procedure.pronamespace
                 WHERE NOT trigger.tgisinternal
                   AND namespace.nspname = 'public'
                   AND trigger.tgname = ANY(%s)
                """,
                (list(required_triggers),),
            ).fetchall()
            trigger_state = {str(row["tgname"]): row for row in trigger_rows}
            trigger_contract_drift = set(trigger_state) != set(required_triggers)
            if not trigger_contract_drift:
                for name, contract in required_triggers.items():
                    row = trigger_state[name]
                    definition = str(row["trigger_definition"])
                    actual_when = None
                    if " WHEN " in definition:
                        actual_when = definition.split(" WHEN ", 1)[1].split(
                            " EXECUTE FUNCTION ", 1
                        )[0]
                    expected_when = contract[3] if contract[3] is not False else None
                    if (
                        str(row["tgenabled"]) not in {"O", "A"}
                        or str(row["relation_name"]) != contract[0]
                        or str(row["function_schema"]) != "public"
                        or str(row["function_name"]) != contract[1]
                        or int(row["tgtype"]) != contract[2]
                        or bool(row["has_when"]) is not (expected_when is not None)
                        or actual_when != expected_when
                    ):
                        trigger_contract_drift = True
                        break
            if trigger_contract_drift:
                raise LedgerExceptionFollowupBlocked(
                    "0049 follow-up schema requires every lifecycle trigger"
                )
            recovery_contract_rows = connection.execute(
                """
                SELECT required.signature,
                       procedure.oid IS NOT NULL AS installed,
                       pg_catalog.strpos(
                           pg_catalog.pg_get_functiondef(procedure.oid),
                           marker.contract_marker
                       ) > 0 AS exact_contract
                  FROM pg_catalog.unnest(%s::text[]) WITH ORDINALITY
                       required(signature, position)
                  JOIN pg_catalog.unnest(%s::text[]) WITH ORDINALITY
                       marker(contract_marker, position)
                    USING (position)
                  LEFT JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                """,
                (
                    list(recovery_contract_functions),
                    list(recovery_contract_functions.values()),
                ),
            ).fetchall()
            if (
                {str(row["signature"]) for row in recovery_contract_rows}
                != set(recovery_contract_functions)
                or any(
                    row["installed"] is not True
                    or row["exact_contract"] is not True
                    for row in recovery_contract_rows
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0052 recovery functions do not match their exact contract"
                )
            hardened_rows = connection.execute(
                """
                SELECT required.signature,
                       procedure.oid IS NOT NULL AS installed,
                       namespace.nspname AS function_schema,
                       procedure.proconfig
                  FROM pg_catalog.unnest(%s::text[]) required(signature)
                  LEFT JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                  LEFT JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = procedure.pronamespace
                """,
                (list(required_hardened_procedures),),
            ).fetchall()
            if (
                {str(row["signature"]) for row in hardened_rows}
                != required_hardened_procedures
                or any(
                    row["installed"] is not True
                    or str(row["function_schema"]) != "public"
                    or tuple(row["proconfig"] or ())
                    != ("search_path=pg_catalog, public, pg_temp",)
                    for row in hardened_rows
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 trusted trigger helpers require an exact search path"
                )
            staging_trigger_definer = connection.execute(
                """
                SELECT procedure.oid IS NOT NULL AS installed,
                       procedure.prosecdef,
                       owner.rolname AS owner,
                       NOT owner.rolcanlogin AS owner_no_login,
                       NOT owner.rolinherit AS owner_no_inherit,
                       NOT owner.rolsuper AS owner_not_super,
                       NOT owner.rolbypassrls AS owner_no_bypass_rls,
                       procedure.proconfig,
                       NOT pg_catalog.has_function_privilege(
                           'lawcase_web_application', procedure.oid, 'EXECUTE'
                       ) AS web_denied,
                       NOT pg_catalog.has_function_privilege(
                           'lawcase_agent_worker', procedure.oid, 'EXECUTE'
                       ) AS worker_denied,
                       NOT pg_catalog.has_function_privilege(
                           'lawcase_agent_verifier', procedure.oid, 'EXECUTE'
                       ) AS verifier_denied,
                       NOT EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 procedure.proacl,
                                 pg_catalog.acldefault(
                                     'f'::"char", procedure.proowner
                                 )
                             )) function_acl
                            WHERE function_acl.grantee = 0
                              AND function_acl.privilege_type = 'EXECUTE'
                       ) AS public_denied,
                       EXISTS (
                           SELECT 1
                             FROM pg_catalog.pg_trigger trigger
                             JOIN pg_catalog.pg_class relation
                               ON relation.oid = trigger.tgrelid
                            WHERE trigger.tgfoid = procedure.oid
                              AND trigger.tgname =
                                  'case_agent_ledger_exception_groups_after_staging'
                              AND relation.relname =
                                  'case_agent_ledger_extraction_staging_events'
                              AND trigger.tgenabled = 'O'
                              AND NOT trigger.tgisinternal
                       ) AS staging_trigger_bound
                  FROM pg_catalog.pg_proc procedure
                  JOIN pg_catalog.pg_roles owner
                    ON owner.oid = procedure.proowner
                 WHERE procedure.oid = pg_catalog.to_regprocedure(
                     'public.group_case_agent_ledger_exceptions_after_staging()'
                 )
                """
            ).fetchone()
            if staging_trigger_definer is None or any(
                staging_trigger_definer[key] is not True
                for key in (
                    "installed",
                    "prosecdef",
                    "owner_no_login",
                    "owner_no_inherit",
                    "owner_not_super",
                    "owner_no_bypass_rls",
                    "web_denied",
                    "worker_denied",
                    "verifier_denied",
                    "public_denied",
                    "staging_trigger_bound",
                )
            ) or (
                str(staging_trigger_definer["owner"]) != "lawcase_schema_owner"
                or tuple(staging_trigger_definer["proconfig"] or ())
                != ("search_path=pg_catalog, public, pg_temp",)
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0062 staging trigger authority is unavailable or over-broad"
                )
            procedure_rows = connection.execute(
                """
                SELECT required.signature,
                       procedure.oid IS NOT NULL AS installed,
                       procedure.prosecdef,
                       pg_catalog.pg_get_userbyid(procedure.proowner) AS owner,
                       procedure.proconfig
                  FROM pg_catalog.unnest(%s::text[]) required(signature)
                  LEFT JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                """,
                (list(required_procedures),),
            ).fetchall()
            if {
                str(row["signature"])
                for row in procedure_rows
                if row["installed"]
                and row["prosecdef"] is True
                and str(row["owner"]) == "lawcase_ledger_confirmation_owner"
                and tuple(row["proconfig"] or ())
                    == ("search_path=pg_catalog",)
            } != required_procedures:
                raise LedgerExceptionFollowupBlocked(
                    "0049 follow-up schema requires every owner-bound definer command"
                )
            internal_rows = connection.execute(
                """
                SELECT required.signature,
                       procedure.oid IS NOT NULL AS installed,
                       procedure.prosecdef,
                       pg_catalog.pg_get_userbyid(procedure.proowner) AS owner,
                       procedure.proconfig,
                       pg_catalog.has_function_privilege(
                           'lawcase_web_application', procedure.oid,
                           'EXECUTE'
                       ) AS web_execute,
                       pg_catalog.has_function_privilege(
                           'lawcase_agent_worker', procedure.oid,
                           'EXECUTE'
                       ) AS worker_execute,
                       EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 procedure.proacl,
                                 pg_catalog.acldefault(
                                     'f'::"char", procedure.proowner
                                 )
                             )) function_acl
                            WHERE function_acl.grantee = 0
                              AND function_acl.privilege_type = 'EXECUTE'
                       ) AS public_execute
                  FROM pg_catalog.unnest(%s::text[]) required(signature)
                  LEFT JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                """,
                (list(internal_definers),),
            ).fetchall()
            if (
                {str(row["signature"]) for row in internal_rows}
                != internal_definers
                or any(
                    row["installed"] is not True
                    or row["prosecdef"] is not True
                    or str(row["owner"])
                        != "lawcase_ledger_confirmation_owner"
                    or tuple(row["proconfig"] or ())
                        != (
                            internal_definer_search_paths[
                                str(row["signature"])
                            ],
                        )
                    or row["web_execute"] is not False
                    or row["worker_execute"] is not False
                    or row["public_execute"] is not False
                    for row in internal_rows
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 internal definers are not isolated from application roles"
                )
            execute_rows = connection.execute(
                """
                SELECT required.signature,
                       pg_catalog.has_function_privilege(
                           CASE
                               WHEN required.signature LIKE
                                   '%%_from_web_session(%%'
                               THEN 'lawcase_web_application'
                               ELSE 'lawcase_agent_worker'
                           END,
                           required.signature,
                           'EXECUTE'
                       ) AS intended_execute,
                       pg_catalog.has_function_privilege(
                           CASE
                               WHEN required.signature LIKE
                                   '%%_from_web_session(%%'
                               THEN 'lawcase_agent_worker'
                               ELSE 'lawcase_web_application'
                           END,
                           required.signature,
                           'EXECUTE'
                       ) AS opposite_execute,
                       EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 procedure.proacl,
                                 pg_catalog.acldefault(
                                     'f'::"char", procedure.proowner
                                 )
                             )) function_acl
                            WHERE function_acl.grantee = 0
                              AND function_acl.privilege_type = 'EXECUTE'
                       ) AS public_execute
                  FROM pg_catalog.unnest(%s::text[]) required(signature)
                  JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                """,
                (list(required_procedures),),
            ).fetchall()
            if (
                {str(row["signature"]) for row in execute_rows}
                != required_procedures
                or any(
                    row["intended_execute"] is not True
                    or row["opposite_execute"] is not False
                    or row["public_execute"] is not False
                    for row in execute_rows
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 command execution grants are not least privilege"
                )
            denied_rows = connection.execute(
                """
                SELECT required.signature,
                       procedure.oid IS NOT NULL AS installed,
                       COALESCE(pg_catalog.has_function_privilege(
                           'lawcase_web_application', procedure.oid,
                           'EXECUTE'
                       ), false) AS web_execute,
                       COALESCE(pg_catalog.has_function_privilege(
                           'lawcase_agent_worker', procedure.oid,
                           'EXECUTE'
                       ), false) AS worker_execute,
                       COALESCE(EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 procedure.proacl,
                                 pg_catalog.acldefault(
                                     'f'::"char", procedure.proowner
                                 )
                             )) function_acl
                            WHERE function_acl.grantee = 0
                              AND function_acl.privilege_type = 'EXECUTE'
                       ), false) AS public_execute
                  FROM pg_catalog.unnest(%s::text[]) required(signature)
                  LEFT JOIN pg_catalog.pg_proc procedure
                    ON procedure.oid =
                        pg_catalog.to_regprocedure(required.signature)
                """,
                (list(denied_procedures),),
            ).fetchall()
            if (
                {str(row["signature"]) for row in denied_rows}
                != denied_procedures
                or any(
                    row["installed"] is True
                    and (
                        row["web_execute"] is True
                        or row["worker_execute"] is True
                        or row["public_execute"] is True
                    )
                    for row in denied_rows
                )
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 legacy singular satisfaction is still executable"
                )
            dml_rows = connection.execute(
                """
                SELECT role_name, object_name,
                       pg_catalog.has_table_privilege(
                           role_name,
                           'public.' || object_name,
                           'INSERT'
                       ) OR pg_catalog.has_table_privilege(
                           role_name,
                           'public.' || object_name,
                           'UPDATE'
                       ) OR pg_catalog.has_table_privilege(
                           role_name,
                           'public.' || object_name,
                           'DELETE'
                       ) OR pg_catalog.has_table_privilege(
                           role_name,
                           'public.' || object_name,
                           'TRUNCATE'
                       ) AS can_mutate
                  FROM pg_catalog.unnest(
                      ARRAY['lawcase_web_application', 'lawcase_agent_worker']
                  ) AS roles(role_name)
                  CROSS JOIN pg_catalog.unnest(%s::text[])
                      AS objects(object_name)
                """,
                (list(required_tables),),
            ).fetchall()
            if len(dml_rows) != 2 * len(required_tables) or any(
                row["can_mutate"] is True for row in dml_rows
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 application roles retain a direct lifecycle mutation grant"
                )
            runtime_read_rows = connection.execute(
                """
                SELECT role_name, object_name,
                       pg_catalog.has_table_privilege(
                           role_name,
                           'public.' || object_name,
                           'SELECT'
                       ) AS runtime_can_select
                  FROM pg_catalog.unnest(
                      ARRAY['lawcase_web_application', 'lawcase_agent_worker']
                  ) AS roles(role_name)
                  CROSS JOIN pg_catalog.unnest(%s::text[])
                      AS objects(object_name)
                """,
                (list(runtime_full_read_tables),),
            ).fetchall()
            if len(runtime_read_rows) != 2 * len(runtime_full_read_tables) or any(
                row["runtime_can_select"] is not True
                for row in runtime_read_rows
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 runtime roles lack an exact tenant-scoped read grant"
                )
            runtime_column_rows = connection.execute(
                """
                SELECT role_name, relation.relname AS object_name,
                       attribute.attname AS column_name,
                       pg_catalog.has_table_privilege(
                           role_name, relation.oid, 'SELECT'
                       ) AS table_select,
                       pg_catalog.has_column_privilege(
                           role_name, relation.oid, attribute.attnum, 'SELECT'
                       ) AS column_select
                  FROM pg_catalog.unnest(
                      ARRAY['lawcase_web_application', 'lawcase_agent_worker']
                  ) AS roles(role_name)
                  CROSS JOIN pg_catalog.pg_class relation
                  JOIN pg_catalog.pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN pg_catalog.pg_attribute attribute
                    ON attribute.attrelid = relation.oid
                   AND attribute.attnum > 0
                   AND NOT attribute.attisdropped
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = ANY(%s)
                """,
                (list(restricted_runtime_tables),),
            ).fetchall()
            expected_role_tables = {
                (role_name, table_name)
                for role_name in runtime_column_read_contract
                for table_name in restricted_runtime_tables
            }
            observed_role_tables: set[tuple[str, str]] = set()
            observed_table_columns: dict[str, set[str]] = {}
            observed_select_columns: dict[tuple[str, str], set[str]] = {}
            has_broad_select = False
            leaked_session_column = False
            for row in runtime_column_rows:
                role_name = str(row["role_name"])
                table_name = str(row["object_name"])
                column_name = str(row["column_name"])
                role_table = (role_name, table_name)
                observed_role_tables.add(role_table)
                observed_table_columns.setdefault(table_name, set()).add(
                    column_name
                )
                if row["table_select"] is True:
                    has_broad_select = True
                if row["column_select"] is True:
                    observed_select_columns.setdefault(role_table, set()).add(
                        column_name
                    )
                    if sensitive_session_columns.get(table_name) == column_name:
                        leaked_session_column = True
            if leaked_session_column:
                raise LedgerExceptionFollowupBlocked(
                    "0049 application roles can read a server session identifier"
                )
            if has_broad_select:
                raise LedgerExceptionFollowupBlocked(
                    "0049 application roles retain a broad lifecycle read grant"
                )
            if observed_role_tables != expected_role_tables or any(
                sensitive_column
                not in observed_table_columns.get(table_name, set())
                for table_name, sensitive_column
                in sensitive_session_columns.items()
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 lifecycle session-column contract is incomplete"
                )
            if any(
                observed_select_columns.get((role_name, table_name), set())
                != runtime_column_read_contract.get(role_name, {}).get(
                    table_name, set()
                )
                for role_name, table_name in expected_role_tables
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 runtime column projection grants are not exact"
                )
            privilege_rows = connection.execute(
                """
                SELECT object_name,
                       pg_catalog.has_table_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.' || object_name,
                           'SELECT'
                       ) AS can_select
                  FROM pg_catalog.unnest(%s::text[]) AS objects(object_name)
                """,
                (list(owner_read_tables),),
            ).fetchall()
            if {str(row["object_name"]) for row in privilege_rows if row["can_select"]} != (
                owner_read_tables
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 owner lacks an effective source-lineage read privilege"
                )
            owner_write_rows = connection.execute(
                """
                SELECT object_name,
                       pg_catalog.has_table_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.' || object_name,
                           privilege_name
                       ) AS allowed,
                       privilege_name
                  FROM (
                      SELECT object_name, 'INSERT'::text AS privilege_name
                        FROM pg_catalog.unnest(%s::text[])
                            AS inserts(object_name)
                      UNION ALL
                      SELECT object_name, 'UPDATE'::text AS privilege_name
                        FROM pg_catalog.unnest(%s::text[])
                            AS updates(object_name)
                  ) required
                """,
                (list(owner_insert_tables), list(owner_update_tables)),
            ).fetchall()
            required_writes = {
                (object_name, "INSERT")
                for object_name in owner_insert_tables
            } | {
                (object_name, "UPDATE")
                for object_name in owner_update_tables
            }
            if {
                (str(row["object_name"]), str(row["privilege_name"]))
                for row in owner_write_rows
                if row["allowed"] is True
            } != required_writes:
                raise LedgerExceptionFollowupBlocked(
                    "0049 owner lacks an effective lifecycle write privilege"
                )
            matter_update = connection.execute(
                """
                SELECT pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.matters', 'version', 'UPDATE'
                       ) AS version_update,
                       pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.matters', 'updated_at', 'UPDATE'
                       ) AS updated_at_update,
                       pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.web_sessions', 'session_id', 'UPDATE'
                       ) AS session_lock,
                       pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.users', 'user_id', 'UPDATE'
                       ) AS user_lock,
                       pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.matter_actor_roles', 'user_id', 'UPDATE'
                       ) AS role_lock,
                       pg_catalog.has_column_privilege(
                           'lawcase_ledger_confirmation_owner',
                           'public.case_agent_runs', 'run_id', 'UPDATE'
                       ) AS run_lock
                """
            ).fetchone()
            if (
                matter_update is None
                or matter_update["version_update"] is not True
                or matter_update["updated_at_update"] is not True
                or matter_update["session_lock"] is not True
                or matter_update["user_lock"] is not True
                or matter_update["role_lock"] is not True
                or matter_update["run_lock"] is not True
            ):
                raise LedgerExceptionFollowupBlocked(
                    "0049 owner lacks authoritative version or row-lock entitlements"
                )
            if connection.execute(
                """
                SELECT pg_catalog.has_function_privilege(
                    'lawcase_ledger_confirmation_owner',
                    'public.case_agent_ledger_extraction_run_staging_complete(uuid,uuid,uuid)',
                    'EXECUTE'
                ) AS can_verify_staging
                """
            ).fetchone()["can_verify_staging"] is not True:
                raise LedgerExceptionFollowupBlocked(
                    "0049 owner cannot verify complete extraction staging"
                )
    except LedgerExceptionFollowupBlocked:
        raise
    except (psycopg.Error, OSError) as error:
        raise LedgerExceptionFollowupBlocked(
            "0049 exception follow-up PostgreSQL preflight failed"
        ) from error


def _read_followup_for_command(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    followup_id: str,
) -> LedgerExceptionFollowup:
    row = connection.execute(
        """
        SELECT followup.followup_id,
               followup.origin_exception_decision_id,
               followup.matter_id, followup.followup_kind,
               followup.subject_hash, head.current_state,
               head.head_sequence, request.evidence_request_id
          FROM case_agent_ledger_exception_followups followup
          JOIN case_agent_ledger_exception_followup_heads head
            ON head.followup_id = followup.followup_id
           AND head.firm_id = followup.firm_id
           AND head.matter_id = followup.matter_id
          LEFT JOIN case_agent_ledger_exception_managed_evidence_requests request
            ON request.followup_id = followup.followup_id
           AND request.firm_id = followup.firm_id
           AND request.matter_id = followup.matter_id
         WHERE followup.followup_id = %s
           AND followup.firm_id = %s
           AND followup.matter_id = %s
        """,
        (followup_id, firm_id, matter_id),
    ).fetchone()
    if row is None:
        raise LedgerExceptionFollowupBlocked(
            "exception follow-up does not exist in this matter"
        )
    return _project_followup(dict(row))


def _project_followup(row: dict[str, Any]) -> LedgerExceptionFollowup:
    try:
        followup = LedgerExceptionFollowup(
            followup_id=str(row["followup_id"]),
            origin_exception_decision_id=str(
                row["origin_exception_decision_id"]
            ),
            matter_id=str(row["matter_id"]),
            kind=LedgerExceptionFollowupKind(str(row["followup_kind"])),
            state=LedgerExceptionFollowupState(str(row["current_state"])),
            subject_hash=str(row["subject_hash"]),
            head_sequence=int(row["head_sequence"]),
            managed_evidence_request_id=(
                str(row["evidence_request_id"])
                if row.get("evidence_request_id") is not None
                else None
            ),
        )
        followup.validate()
        return followup
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LedgerExceptionFollowupBlocked):
            raise
        raise LedgerExceptionFollowupBlocked(
            "exception follow-up projection is invalid"
        ) from error


def _project_followup_snapshot(
    row: dict[str, Any],
) -> LedgerExceptionFollowupSnapshot:
    try:
        snapshot = LedgerExceptionFollowupSnapshot(
            followup_id=str(row["followup_id"]),
            kind=LedgerExceptionFollowupKind(str(row["followup_kind"])),
            state=LedgerExceptionFollowupState(str(row["current_state"])),
            head_sequence=int(row["head_sequence"]),
            origin_exception_decision_id=str(
                row["origin_exception_decision_id"]
            ),
            origin_exception_group_id=str(row["origin_exception_group_id"]),
            origin_extraction_batch_id=str(
                row["origin_extraction_batch_id"]
            ),
            created_matter_version=int(row["created_matter_version"]),
            created_at=row["created_at"],
            reason_code=str(row["reason_code"]),
            reason_note=(
                str(row["reason_note"])
                if row.get("reason_note") is not None
                else None
            ),
            candidate_count=int(row["candidate_count"]),
            canonical_reason_codes=tuple(
                str(code) for code in row["canonical_reason_codes"]
            ),
            evidence_page_ids=tuple(
                str(page_id) for page_id in row["evidence_page_ids"]
            ),
            control_health=LedgerExceptionControlHealth(
                str(row["control_health"])
            ),
            managed_evidence_request_id=(
                str(row["evidence_request_id"])
                if row.get("evidence_request_id") is not None
                else None
            ),
            acceptance_criteria=(
                dict(row["acceptance_criteria"])
                if row.get("acceptance_criteria") is not None
                else None
            ),
            automation_status=(
                str(row["automation_status"])
                if row.get("automation_status") is not None
                else None
            ),
            evidence_page_count=int(row.get("evidence_page_count", 0)) or None,
        )
        snapshot.validate()
        return snapshot
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LedgerExceptionFollowupBlocked):
            raise
        raise LedgerExceptionFollowupBlocked(
            "exception follow-up snapshot is invalid"
        ) from error


def _project_managed_evidence_source_candidate(
    row: dict[str, Any],
) -> ManagedEvidenceSourceCandidate:
    try:
        candidate = ManagedEvidenceSourceCandidate(
            object_type=ManagedEvidenceSourceType(str(row["object_type"])),
            object_id=str(row["object_id"]),
            display_label=str(row["display_label"]),
            created_at=row["created_at"],
        )
        candidate.validate()
        return candidate
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, LedgerExceptionFollowupBlocked):
            raise
        raise LedgerExceptionFollowupBlocked(
            "managed evidence source projection is invalid"
        ) from error


def _validate_page_request(*, offset: int, limit: int, maximum_limit: int) -> None:
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= maximum_limit
    ):
        raise LedgerExceptionFollowupBlocked("projection page request is invalid")


def _parse_mutating_receipt(
    row: Any,
    *,
    command_name: str,
    matter_id: str,
    followup_id: str,
    expected_version: int,
    idempotency_key: str,
) -> CaseLedgerCommandReceipt:
    receipt = _receipt_dict(row)
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "audit_event_id",
        "object_type",
        "object_id",
    }
    if (
        set(receipt) != required
        or receipt.get("command_name") != command_name
        or receipt.get("idempotency_key") != idempotency_key
        or str(receipt.get("matter_id")) != matter_id
        or int(receipt.get("matter_version", -1)) != expected_version + 1
        or receipt.get("object_type") != "CASE_LEDGER_EXCEPTION_FOLLOWUP"
        or str(receipt.get("object_id")) != followup_id
    ):
        raise LedgerExceptionFollowupBlocked(
            "exception follow-up command receipt differs"
        )
    audit_event_id = str(receipt["audit_event_id"])
    _validate_uuid("audit_event_id", audit_event_id)
    return CaseLedgerCommandReceipt(
        command_name=command_name,
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=expected_version + 1,
        audit_event_id=audit_event_id,
        object_type="CASE_LEDGER_EXCEPTION_FOLLOWUP",
        object_id=followup_id,
    )


def _parse_task_binding_receipt(
    row: Any,
    *,
    matter_id: str,
    expected_version: int,
    idempotency_key: str,
) -> ReextractionTaskBindingReceipt:
    receipt = _receipt_dict(row)
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "object_type",
        "object_id",
    }
    task_binding_id = str(receipt.get("object_id", ""))
    if (
        set(receipt) != required
        or receipt.get("command_name") !=
            "BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK"
        or receipt.get("idempotency_key") != idempotency_key
        or str(receipt.get("matter_id")) != matter_id
        or int(receipt.get("matter_version", -1)) != expected_version
        or receipt.get("object_type") !=
            "CASE_LEDGER_EXCEPTION_REEXTRACTION_TASK_BINDING"
    ):
        raise LedgerExceptionFollowupBlocked(
            "re-extraction task binding receipt differs"
        )
    _validate_uuid("task_binding_id", task_binding_id)
    return ReextractionTaskBindingReceipt(
        command_name="BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK",
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=expected_version,
        task_binding_id=task_binding_id,
    )


def _parse_control_transfer_receipt(
    row: Any,
    *,
    matter_id: str,
    expected_version: int,
    idempotency_key: str,
) -> ExceptionControlTransferReceipt:
    receipt = _receipt_dict(row)
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "audit_event_id",
        "object_type",
        "object_id",
        "control_health",
    }
    assignment_id = str(receipt.get("object_id", ""))
    audit_event_id = str(receipt.get("audit_event_id", ""))
    if (
        set(receipt) != required
        or receipt.get("command_name") !=
            "TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL"
        or receipt.get("idempotency_key") != idempotency_key
        or str(receipt.get("matter_id")) != matter_id
        or type(receipt.get("matter_version")) is not int
        or receipt.get("matter_version") != expected_version
        or receipt.get("object_type") !=
            "CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT"
        or receipt.get("control_health") != "HEALTHY"
    ):
        raise LedgerExceptionFollowupBlocked(
            "exception control transfer receipt differs"
        )
    _validate_uuid("control_assignment_id", assignment_id)
    _validate_uuid("audit_event_id", audit_event_id)
    return ExceptionControlTransferReceipt(
        command_name="TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL",
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=expected_version,
        audit_event_id=audit_event_id,
        control_assignment_id=assignment_id,
        control_health=LedgerExceptionControlHealth.HEALTHY,
    )


def _parse_control_recovery_intent_receipt(
    row: Any,
    *,
    matter_id: str,
    expected_version: int,
    idempotency_key: str,
) -> ExceptionControlRecoveryIntentReceipt:
    receipt = _receipt_dict(row)
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "object_type",
        "object_id",
        "recovery_state",
        "transfer_idempotency_key",
        "replacement_run_id",
        "run_exists",
    }
    recovery_intent_id = str(receipt.get("object_id", ""))
    recovery_state = str(receipt.get("recovery_state", ""))
    transfer_idempotency_key = str(
        receipt.get("transfer_idempotency_key", "")
    )
    replacement_run_id = str(receipt.get("replacement_run_id", ""))
    run_exists = receipt.get("run_exists")
    if (
        set(receipt) != required
        or receipt.get("command_name")
            != "PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY"
        or receipt.get("idempotency_key") != idempotency_key
        or str(receipt.get("matter_id")) != matter_id
        or type(receipt.get("matter_version")) is not int
        or receipt.get("matter_version") != expected_version
        or receipt.get("object_type")
            != "CASE_LEDGER_EXCEPTION_RECOVERY_INTENT"
        or recovery_state not in {"PENDING", "TRANSFERRED", "ABANDONED"}
        or type(run_exists) is not bool
    ):
        raise LedgerExceptionFollowupBlocked(
            "exception control recovery intent receipt differs"
        )
    _validate_uuid("recovery_intent_id", recovery_intent_id)
    _validate_uuid("replacement_run_id", replacement_run_id)
    validate_idempotency_key(transfer_idempotency_key)
    return ExceptionControlRecoveryIntentReceipt(
        command_name="PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY",
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=expected_version,
        recovery_intent_id=recovery_intent_id,
        recovery_state=recovery_state,
        transfer_idempotency_key=transfer_idempotency_key,
        replacement_run_id=replacement_run_id,
        run_exists=run_exists,
    )


def _parse_reextraction_set_receipt(
    row: Any,
    *,
    matter_id: str,
    graph_id: str,
    expected_version: int,
    idempotency_key: str,
) -> ReextractionSetReceipt:
    receipt = _receipt_dict(row)
    required = {
        "command_name",
        "idempotency_key",
        "matter_id",
        "matter_version",
        "audit_event_id",
        "object_type",
        "object_id",
        "followup_count",
    }
    try:
        matter_version = int(receipt.get("matter_version", -1))
        followup_count = int(receipt.get("followup_count", -1))
    except (TypeError, ValueError) as error:
        raise LedgerExceptionFollowupBlocked(
            "re-extraction set receipt differs"
        ) from error
    audit_event_id = str(receipt.get("audit_event_id", ""))
    if (
        set(receipt) != required
        or receipt.get("command_name") !=
            "SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET"
        or receipt.get("idempotency_key") != idempotency_key
        or str(receipt.get("matter_id")) != matter_id
        or matter_version != expected_version + 1
        or receipt.get("object_type") !=
            "CASE_LEDGER_EXCEPTION_REEXTRACTION_SET"
        or str(receipt.get("object_id")) != graph_id
        or followup_count < 1
    ):
        raise LedgerExceptionFollowupBlocked(
            "re-extraction set receipt differs"
        )
    _validate_uuid("audit_event_id", audit_event_id)
    _validate_uuid("graph_id", graph_id)
    return ReextractionSetReceipt(
        command_name="SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET",
        idempotency_key=idempotency_key,
        matter_id=matter_id,
        matter_version=matter_version,
        audit_event_id=audit_event_id,
        graph_id=graph_id,
        followup_count=followup_count,
    )


def _receipt_dict(row: Any) -> dict[str, Any]:
    if row is None or not isinstance(row.get("receipt"), dict):
        raise LedgerExceptionFollowupBlocked(
            "exception follow-up command returned no receipt"
        )
    return dict(row["receipt"])


def _raise_known_conflict(error: psycopg.Error, label: str) -> None:
    if error.sqlstate == _VERSION_CONFLICT_SQLSTATE:
        raise VersionConflict(f"{label} matter version changed") from error
    if error.sqlstate == _INTENT_CONFLICT_SQLSTATE:
        raise IdempotencyConflict(f"{label} already has a different intent") from error
    raise error


def _validate_command(
    *,
    matter_id: str,
    actor: Actor,
    expected_version: int,
    idempotency_key: str,
) -> None:
    _validate_command_identity(
        matter_id=matter_id,
        actor=actor,
        idempotency_key=idempotency_key,
    )
    _require_positive_version(expected_version)
    validate_idempotency_key(idempotency_key)


def _validate_read_identity(*, matter_id: str, actor: Actor) -> None:
    _validate_uuid("matter_id", matter_id)
    _validate_uuid("firm_id", actor.firm_id)
    _validate_uuid("actor_id", actor.actor_id)


def _require_dedicated_worker(actor: Actor) -> None:
    if actor.roles != _WORKER_ROLES:
        raise PermissionError(
            "exception follow-up automation requires a dedicated SYSTEM_WORKER"
        )


class _TenantTransaction:
    def __init__(self, dsn: str, firm_id: str) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._context: Any = None

    def __enter__(self):
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        connection = self._context.__enter__()
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)",
            (self._firm_id,),
        )
        return connection

    def __exit__(self, exc_type, exc, traceback):
        return self._context.__exit__(exc_type, exc, traceback)


__all__ = (
    "ExceptionControlState",
    "ExceptionControlTransferReceipt",
    "PostgresCaseLedgerExceptionFollowupStore",
    "ReextractionSetReceipt",
    "ReextractionTaskBindingReceipt",
    "preflight_case_agent_ledger_exception_followup_schema",
)
