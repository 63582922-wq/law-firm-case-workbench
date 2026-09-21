"""Atomic PostgreSQL projection for server-owned case-Agent planning inputs.

This adapter deliberately does not compose the existing per-ledger stores.
Every object used by one planning run is read through one tenant-scoped,
``REPEATABLE READ``, read-only transaction.  The exact case-ledger snapshot
captured when the run was created is checked at the opening fence and rebuilt
at the closing fence before any projection is returned.

Only metadata needed to build opaque planning references leaves this module.
Document bytes, case prose, labels, URLs and private storage locators are never
included in :class:`AuthoritativeCasePlanningProjection`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_planner import (
    PlanningInputStatus,
    PlanningSignalCategory,
    ReextractionPlanningObligation,
)
from .case_agent_lawyer_decisions import (
    GovernedLawyerPlanningDecision,
    LawyerPlanningDecisionBlocked,
    LawyerPlanningDecisionCode,
)
from .case_agent_planning_snapshot import (
    ActiveDynamicWorkPlanProjection,
    AuthoritativeCasePlanningProjection,
    AuthoritativePlanningObject,
    CasePlanningProjectionBlocked,
    ConfirmedPostureProjection,
    GovernedLawyerPlanningSignal,
    PlanningProjectionObjectType,
    ProjectionSectionState,
    object_version_code,
    planning_object_ref_id,
)
from .case_agent_supervisor import AgentSupervisorBlocked, CaseSnapshotRef
from .case_ledger_postgres import _group_ids, _payload_hash
from .models import Actor, Role


CASE_LEDGER_SNAPSHOT_SCHEMA_VERSION = "case-ledger-snapshot-v1"


@dataclass(frozen=True)
class _LedgerRead:
    snapshot: CaseSnapshotRef
    facts: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    responses: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    transactions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _ProjectionCapabilities:
    materials: bool
    posture: bool
    work_plan: bool
    legal: bool
    procedure: bool
    ledger_exception_review: bool
    ledger_exception_followups: bool


class PostgresCasePlanningProjectionRepository:
    """Build one authoritative planning projection in one PostgreSQL snapshot.

    ``dsn`` must identify the same PostgreSQL cluster used to create the
    ``AgentRunState.snapshot``.  The connection role must be subject to the
    repository's forced RLS policies; this adapter additionally authorizes the
    supplied principal as the matter's active, dedicated ``SYSTEM_WORKER``.
    """

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn.strip()

    def read_atomic_projection(
        self,
        *,
        firm_id: str,
        matter_id: str,
        actor: Actor,
        expected_case_snapshot: CaseSnapshotRef,
    ) -> AuthoritativeCasePlanningProjection:
        _validate_request(
            firm_id=firm_id,
            matter_id=matter_id,
            actor=actor,
            expected_case_snapshot=expected_case_snapshot,
        )
        with _RepeatableReadPlanningTransaction(self._dsn, firm_id) as connection:
            return read_authoritative_projection_in_transaction(
                connection,
                firm_id=firm_id,
                matter_id=matter_id,
                actor=actor,
                expected_case_snapshot=expected_case_snapshot,
            )


def read_current_case_snapshot_in_transaction(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    actor: Actor,
) -> CaseSnapshotRef:
    """Rebuild the current authoritative ledger fence under a caller's lock.

    Snapshot-refresh consumption already owns both the Agent run row and the
    matter-version lock.  Reusing the exact ledger projection here avoids a
    second transaction and, more importantly, avoids inventing a hash from
    the refresh request or browser payload.
    """

    _uuid(firm_id, "firm_id")
    _uuid(matter_id, "matter_id")
    if not isinstance(actor, Actor):
        raise CasePlanningProjectionBlocked("planning worker identity is invalid")
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "actor firm_id")
    if actor.firm_id != firm_id:
        raise PermissionError("planning worker belongs to another firm")
    if actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError(
            "planning snapshot refresh requires a dedicated SYSTEM_WORKER identity"
        )
    _authorize_system_worker(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        actor_id=actor.actor_id,
    )
    snapshot = _read_case_ledger(
        connection, firm_id=firm_id, matter_id=matter_id
    ).snapshot
    try:
        snapshot.validate()
    except AgentSupervisorBlocked as error:
        raise CasePlanningProjectionBlocked(
            "current case ledger snapshot is invalid"
        ) from error
    return snapshot


def read_authoritative_projection_in_transaction(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    actor: Actor,
    expected_case_snapshot: CaseSnapshotRef,
) -> AuthoritativeCasePlanningProjection:
    """Read the exact projection inside an existing short server transaction.

    This seam lets a promotion command validate and append its 0030 candidate
    under the same matter lock.  It remains server-only and repeats the same
    dedicated ``SYSTEM_WORKER`` authorization as the standalone repository.
    """

    _validate_request(
        firm_id=firm_id,
        matter_id=matter_id,
        actor=actor,
        expected_case_snapshot=expected_case_snapshot,
    )
    _authorize_system_worker(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        actor_id=actor.actor_id,
    )
    capabilities = _read_capabilities(connection)
    opening = _read_case_ledger(connection, firm_id=firm_id, matter_id=matter_id)
    _require_expected_snapshot(opening.snapshot, expected_case_snapshot, "opening")

    objects, ledger_lawyer_signals = _ledger_planning_objects(opening)
    if capabilities.materials:
        objects.extend(
            _read_material_objects(connection, firm_id=firm_id, matter_id=matter_id)
        )
    objects.extend(
        _read_evidence_page_objects(
            connection,
            firm_id=firm_id,
            matter_id=matter_id,
            matter_version=opening.snapshot.matter_version,
        )
    )

    posture_state, posture, posture_object = _read_posture(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=capabilities.posture,
    )
    if posture_object is not None:
        objects.append(posture_object)

    work_plan_state, active_work_plan, work_plan_objects = _read_work_plan(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        matter_version=opening.snapshot.matter_version,
        configured=capabilities.work_plan,
    )
    objects.extend(work_plan_objects)

    legal_state, legal_objects = _read_legal_sources(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=capabilities.legal,
    )
    objects.extend(legal_objects)

    procedure_state, procedure_objects = _read_procedural_events(
        connection,
        firm_id=firm_id,
        matter_id=matter_id,
        configured=capabilities.procedure,
    )
    objects.extend(procedure_objects)
    lawyer_signals = (
        *ledger_lawyer_signals,
        *_read_current_agent_lawyer_signals(
            connection,
            firm_id=firm_id,
            matter_id=matter_id,
            authorized_refs=frozenset(item.ref_id for item in objects),
        ),
        *(
            _read_ledger_exception_decision_signals(
                connection,
                firm_id=firm_id,
                matter_id=matter_id,
                authorized_refs=frozenset(item.ref_id for item in objects),
            )
            if capabilities.ledger_exception_followups
            else ()
        ),
    )
    # These objects describe review decisions, not facts or raw page contents.
    # Original signals keep their page references in the full projection.
    from .case_agent_review_obligations import bind_review_obligation, REVIEW_OBLIGATION_CODES
    for signal in lawyer_signals:
        if signal.code in REVIEW_OBLIGATION_CODES:
            obligation = bind_review_obligation(signal=signal,
                authorized_refs=frozenset(item.ref_id for item in objects))
            objects.append(AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.REVIEW_OBLIGATION,
                object_id=obligation.obligation_id, object_version=obligation.object_version,
                content_hash=obligation.content_hash, status=obligation.status))
    reextraction_obligations = (
        _read_active_reextraction_planning_obligations(
            connection,
            firm_id=firm_id,
            matter_id=matter_id,
            authorized_refs=frozenset(item.ref_id for item in objects),
        )
        if capabilities.ledger_exception_followups
        else ()
    )

    if capabilities.ledger_exception_followups:
        from dataclasses import replace
        from .case_agent_transaction_candidates import read_transaction_candidates, transaction_candidate_planning_object
        objects.extend(transaction_candidate_planning_object(candidate) for candidate in
            read_transaction_candidates(connection, firm_id=firm_id, matter_id=matter_id))
        from .case_agent_transaction_candidates import read_fact_candidates, fact_candidate_planning_object
        objects.extend(fact_candidate_planning_object(candidate) for candidate in
            read_fact_candidates(connection, firm_id=firm_id, matter_id=matter_id))
        from .case_agent_material_coverage import read_material_extraction_coverage
        coverage = read_material_extraction_coverage(connection, firm_id=firm_id, matter_id=matter_id)
        covered_pages = {page for item in coverage for page in item.extracted_page_ids}
        objects = [replace(item, extraction_complete=item.object_id in covered_pages)
            if item.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE else item for item in objects]
    closing = _read_case_ledger(connection, firm_id=firm_id, matter_id=matter_id)
    _require_expected_snapshot(closing.snapshot, expected_case_snapshot, "closing")
    if closing.snapshot != opening.snapshot:
        raise CasePlanningProjectionBlocked(
            "case ledger changed between planning projection fences"
        )

    return AuthoritativeCasePlanningProjection.build(
        firm_id=firm_id,
        matter_id=matter_id,
        opening_case_snapshot=opening.snapshot,
        closing_case_snapshot=closing.snapshot,
        objects=objects,
        posture_state=posture_state,
        posture=posture,
        work_plan_state=work_plan_state,
        active_work_plan=active_work_plan,
        legal_state=legal_state,
        procedure_state=procedure_state,
        lawyer_signals=lawyer_signals,
        reextraction_obligations=reextraction_obligations,
    )


class _RepeatableReadPlanningTransaction:
    def __init__(self, dsn: str, firm_id: str) -> None:
        self._dsn = dsn
        self._firm_id = firm_id
        self._context: Any = None
        self._connection: psycopg.Connection | None = None

    def __enter__(self) -> psycopg.Connection:
        self._context = psycopg.connect(self._dsn, row_factory=dict_row)
        self._connection = self._context.__enter__()
        self._connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        self._connection.execute(
            "SELECT set_config('app.firm_id', %s, true)", (self._firm_id,)
        )
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None:
        return self._context.__exit__(exc_type, exc, traceback)


def _validate_request(
    *,
    firm_id: str,
    matter_id: str,
    actor: Actor,
    expected_case_snapshot: CaseSnapshotRef,
) -> None:
    _uuid(firm_id, "firm_id")
    _uuid(matter_id, "matter_id")
    if not isinstance(actor, Actor):
        raise CasePlanningProjectionBlocked("planning worker identity is invalid")
    _uuid(actor.actor_id, "actor_id")
    _uuid(actor.firm_id, "actor firm_id")
    if actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError(
            "planning projection requires a dedicated SYSTEM_WORKER identity"
        )
    if actor.firm_id != firm_id:
        raise PermissionError("planning worker belongs to another firm")
    if not isinstance(expected_case_snapshot, CaseSnapshotRef):
        raise CasePlanningProjectionBlocked("expected case snapshot is invalid")
    try:
        expected_case_snapshot.validate()
    except AgentSupervisorBlocked as error:
        raise CasePlanningProjectionBlocked("expected case snapshot is invalid") from error
    if expected_case_snapshot.matter_id != matter_id:
        raise CasePlanningProjectionBlocked("expected case snapshot belongs to another matter")
    if expected_case_snapshot.schema_version != CASE_LEDGER_SNAPSHOT_SCHEMA_VERSION:
        raise CasePlanningProjectionBlocked(
            "planning projection requires the authoritative case-ledger snapshot schema"
        )


def _authorize_system_worker(
    connection: Any, *, firm_id: str, matter_id: str, actor_id: str
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM matters matter
        JOIN matter_actor_roles role
          ON role.matter_id = matter.matter_id AND role.firm_id = matter.firm_id
        JOIN users principal
          ON principal.user_id = role.user_id AND principal.firm_id = role.firm_id
        WHERE matter.matter_id = %s AND matter.firm_id = %s
          AND role.user_id = %s AND role.role = 'SYSTEM_WORKER'
          AND role.revoked_at IS NULL AND principal.status = 'ACTIVE'
        LIMIT 1
        """,
        (matter_id, firm_id, actor_id),
    ).fetchone()
    if row is None:
        raise PermissionError(
            "planning actor is not an active SYSTEM_WORKER on this matter"
        )


def _read_capabilities(connection: Any) -> _ProjectionCapabilities:
    row = connection.execute(
        """
        SELECT
          to_regclass('public.matters') IS NOT NULL AS has_matters,
          to_regclass('public.case_facts') IS NOT NULL AS has_case_facts,
          to_regclass('public.case_claims') IS NOT NULL AS has_case_claims,
          to_regclass('public.case_transactions') IS NOT NULL AS has_case_transactions,
          to_regclass('public.evidence_original_files') IS NOT NULL AS has_evidence_files,
          to_regclass('public.evidence_pages') IS NOT NULL AS has_evidence_pages,
          to_regclass('public.case_agent_material_objects') IS NOT NULL
            AND to_regclass('public.case_agent_material_object_tombstones') IS NOT NULL
            AS has_materials,
          to_regclass('public.case_posture_profiles') IS NOT NULL
            AND to_regclass('public.case_posture_profile_heads') IS NOT NULL
            AND to_regclass('public.case_posture_profile_events') IS NOT NULL
            AS has_posture,
          to_regclass('public.case_work_plans') IS NOT NULL
            AND to_regclass('public.case_work_plan_items') IS NOT NULL
            AND to_regclass('public.case_work_plan_heads') IS NOT NULL
            AS has_work_plan,
          to_regclass('public.case_legal_bundles') IS NOT NULL
            AND to_regclass('public.case_legal_bundle_segments') IS NOT NULL
            AND to_regclass('public.official_legal_source_snapshots') IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'public'
                AND table_name = 'official_legal_source_snapshots'
                AND column_name = 'license_review_hash'
            ) AS has_legal,
          to_regclass('public.case_legal_events') IS NOT NULL AS has_procedure,
          to_regclass('public.case_agent_lawyer_decision_signals') IS NOT NULL
            AS has_lawyer_signals,
          (
            to_regclass('public.case_agent_ledger_exception_groups') IS NOT NULL
            OR to_regclass('public.case_agent_ledger_exception_group_members') IS NOT NULL
            OR to_regclass('public.case_agent_ledger_exception_group_decisions') IS NOT NULL
            OR to_regclass('public.case_agent_ledger_exception_decision_events') IS NOT NULL
          ) AS has_any_ledger_exception_review,
          (
            to_regclass('public.case_agent_ledger_exception_groups') IS NOT NULL
            AND to_regclass('public.case_agent_ledger_exception_group_members') IS NOT NULL
            AND to_regclass('public.case_agent_ledger_exception_group_decisions') IS NOT NULL
            AND to_regclass('public.case_agent_ledger_exception_decision_events') IS NOT NULL
          ) AS has_ledger_exception_review,
          (
            to_regclass('public.case_agent_ledger_exception_followups') IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_control_assignments'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_control_heads'
            ) IS NOT NULL
            OR to_regclass('public.case_agent_ledger_exception_followup_events') IS NOT NULL
            OR to_regclass('public.case_agent_ledger_exception_followup_heads') IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_managed_evidence_requests'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_evidence_source_bindings'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_reextraction_task_bindings'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_reextraction_task_binding_heads'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_reextraction_bindings'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_duplicate_dispositions'
            ) IS NOT NULL
            OR to_regclass(
                'public.case_agent_ledger_exception_duplicate_heads'
            ) IS NOT NULL
          ) AS has_any_ledger_exception_followups,
          (
            to_regclass('public.case_agent_ledger_exception_followups') IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_control_assignments'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_control_heads'
            ) IS NOT NULL
            AND to_regclass('public.case_agent_ledger_exception_followup_events') IS NOT NULL
            AND to_regclass('public.case_agent_ledger_exception_followup_heads') IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_managed_evidence_requests'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_evidence_source_bindings'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_reextraction_task_bindings'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_reextraction_task_binding_heads'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_reextraction_bindings'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_duplicate_dispositions'
            ) IS NOT NULL
            AND to_regclass(
                'public.case_agent_ledger_exception_duplicate_heads'
            ) IS NOT NULL
          ) AS has_ledger_exception_followups
        """
    ).fetchone()
    if row is None or not all(
        bool(row[key])
        for key in (
            "has_matters",
            "has_case_facts",
            "has_case_claims",
            "has_case_transactions",
            "has_evidence_files",
            "has_evidence_pages",
        )
    ):
        raise CasePlanningProjectionBlocked(
            "core case-ledger or evidence migrations are not installed"
        )
    if not bool(row["has_lawyer_signals"]):
        raise CasePlanningProjectionBlocked(
            "governed lawyer decision signal migration 0035 is not installed"
        )
    if bool(row["has_any_ledger_exception_review"]) != bool(
        row["has_ledger_exception_review"]
    ):
        raise CasePlanningProjectionBlocked(
            "ledger exception review migration 0047 is only partially installed"
        )
    if bool(row["has_any_ledger_exception_followups"]) != bool(
        row["has_ledger_exception_followups"]
    ):
        raise CasePlanningProjectionBlocked(
            "ledger exception follow-up migration 0049 is only partially installed"
        )
    if bool(row["has_ledger_exception_review"]) and not bool(
        row["has_ledger_exception_followups"]
    ):
        raise CasePlanningProjectionBlocked(
            "ledger exception review migration 0047 requires the complete 0049 "
            "follow-up lifecycle"
        )
    if bool(row["has_ledger_exception_followups"]) and not bool(
        row["has_ledger_exception_review"]
    ):
        raise CasePlanningProjectionBlocked(
            "ledger exception follow-up migration 0049 requires the complete 0047 "
            "review ledger"
        )
    return _ProjectionCapabilities(
        materials=bool(row["has_materials"]),
        posture=bool(row["has_posture"]),
        work_plan=bool(row["has_work_plan"]),
        legal=bool(row["has_legal"]),
        procedure=bool(row["has_procedure"]),
        ledger_exception_review=bool(row["has_ledger_exception_review"]),
        ledger_exception_followups=bool(row["has_ledger_exception_followups"]),
    )


def _read_case_ledger(connection: Any, *, firm_id: str, matter_id: str) -> _LedgerRead:
    matter = connection.execute(
        """
        SELECT matter_id, title, stage, version
        FROM matters
        WHERE matter_id = %s AND firm_id = %s
        """,
        (matter_id, firm_id),
    ).fetchone()
    if matter is None:
        raise KeyError(matter_id)
    fact_rows = _rows(
        connection.execute(
            """
            SELECT fact_id, original_text, origin, status,
                   jsonb_array_length(evidence_links) AS evidence_count,
                   decision_hash, decided_by, evidence_links,
                   to_jsonb(case_facts)->>'correction_candidate_id' AS correction_candidate_id
            FROM case_facts
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, fact_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    claim_rows = _rows(
        connection.execute(
            """
            SELECT claim_id, original_claim_text, claimed_amount, currency, status,
                   jsonb_array_length(evidence_links) AS evidence_count,
                   confirmation_hash, confirmed_by
            FROM case_claims
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, claim_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    response_rows = _rows(
        connection.execute(
            """
            SELECT claim_response_id, claim_id, position, partial_amount, currency,
                   approval_hash, approved_by
            FROM case_claim_responses
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, claim_response_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    response_fact_rows = _rows(
        connection.execute(
            """
            SELECT claim_response_id, fact_id
            FROM case_claim_response_facts
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY claim_response_id ASC, fact_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    issue_rows = _rows(
        connection.execute(
            """
            SELECT issue_id, question, status, approval_hash, approved_by
            FROM case_dispute_issues
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, issue_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    issue_claim_rows = _rows(
        connection.execute(
            """
            SELECT issue_id, claim_id
            FROM case_dispute_issue_claims
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY issue_id ASC, claim_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    issue_fact_rows = _rows(
        connection.execute(
            """
            SELECT issue_id, fact_id
            FROM case_dispute_issue_facts
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY issue_id ASC, fact_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    transaction_rows = _rows(
        connection.execute(
            """
            SELECT transaction_id, local_date, date_precision, amount, currency,
                   direction, payer_label, payee_label, channel, transaction_reference,
                   status, jsonb_array_length(evidence_links) AS evidence_count,
                   confirmation_hash, confirmed_by
            FROM case_transactions
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY local_date ASC NULLS LAST, created_at ASC, transaction_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    classification_rows = _rows(
        connection.execute(
            """
            SELECT classification_id, transaction_id, origin, nature, same_day_sequence,
                   status, jsonb_array_length(evidence_links) AS evidence_count,
                   approval_hash, approved_by
            FROM case_payment_classifications
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, classification_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    allocation_rows = _rows(
        connection.execute(
            """
            SELECT classification_id, obligation_id, amount, currency
            FROM case_payment_allocations
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY classification_id ASC, obligation_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    group_rows = _rows(
        connection.execute(
            """
            SELECT duplicate_group_id, status, canonical_transaction_id,
                   approval_hash, approved_by
            FROM case_transaction_duplicate_groups
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY created_at ASC, duplicate_group_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )
    member_rows = _rows(
        connection.execute(
            """
            SELECT duplicate_group_id, transaction_id
            FROM case_transaction_duplicate_members
            WHERE matter_id = %s AND firm_id = %s
            ORDER BY duplicate_group_id ASC, transaction_id ASC
            """,
            (matter_id, firm_id),
        ).fetchall()
    )

    response_facts = _group_ids(response_fact_rows, "claim_response_id", "fact_id")
    responses_by_claim = {
        str(row["claim_id"]): {
            "claim_response_id": str(row["claim_response_id"]),
            "position": row["position"],
            "partial_amount": row["partial_amount"],
            "currency": row["currency"],
            "confirmed_fact_ids": response_facts.get(str(row["claim_response_id"]), ()),
            "approval_hash": row["approval_hash"],
            "approved_by": str(row["approved_by"]),
        }
        for row in response_rows
    }
    issue_claims = _group_ids(issue_claim_rows, "issue_id", "claim_id")
    issue_facts = _group_ids(issue_fact_rows, "issue_id", "fact_id")
    allocation_map: dict[str, list[dict[str, Any]]] = {}
    for row in allocation_rows:
        allocation_map.setdefault(str(row["classification_id"]), []).append(
            {
                "obligation_id": row["obligation_id"],
                "amount": row["amount"],
                "currency": row["currency"],
            }
        )
    member_map = _group_ids(member_rows, "duplicate_group_id", "transaction_id")
    facts = tuple(
        {
            "fact_id": str(row["fact_id"]),
            "original_text": row["original_text"],
            "origin": row["origin"],
            "status": row["status"],
            "evidence_count": row["evidence_count"],
            "decision_hash": row["decision_hash"],
            "decided_by": str(row["decided_by"]) if row["decided_by"] else None,
            "evidence_links": row.get("evidence_links", []),
            "correction_candidate_id": row.get("correction_candidate_id"),
        }
        for row in fact_rows
    )
    claims = tuple(
        {
            "claim_id": str(row["claim_id"]),
            "original_claim_text": row["original_claim_text"],
            "claimed_amount": row["claimed_amount"],
            "currency": row["currency"],
            "status": row["status"],
            "evidence_count": row["evidence_count"],
            "confirmation_hash": row["confirmation_hash"],
            "confirmed_by": str(row["confirmed_by"]) if row["confirmed_by"] else None,
            "response": responses_by_claim.get(str(row["claim_id"])),
        }
        for row in claim_rows
    )
    issues = tuple(
        {
            "issue_id": str(row["issue_id"]),
            "question": row["question"],
            "status": row["status"],
            "claim_ids": issue_claims.get(str(row["issue_id"]), ()),
            "confirmed_fact_ids": issue_facts.get(str(row["issue_id"]), ()),
            "approval_hash": row["approval_hash"],
            "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        }
        for row in issue_rows
    )
    transactions = tuple(
        {
            "transaction_id": str(row["transaction_id"]),
            "local_date": row["local_date"],
            "date_precision": row["date_precision"],
            "amount": row["amount"],
            "currency": row["currency"],
            "direction": row["direction"],
            "payer_label": row["payer_label"],
            "payee_label": row["payee_label"],
            "channel": row["channel"],
            "transaction_reference": row["transaction_reference"],
            "status": row["status"],
            "evidence_count": row["evidence_count"],
            "confirmation_hash": row["confirmation_hash"],
            "confirmed_by": str(row["confirmed_by"]) if row["confirmed_by"] else None,
        }
        for row in transaction_rows
    )
    classifications = tuple(
        {
            "classification_id": str(row["classification_id"]),
            "transaction_id": str(row["transaction_id"]),
            "origin": row["origin"],
            "nature": row["nature"],
            "same_day_sequence": row["same_day_sequence"],
            "status": row["status"],
            "evidence_count": row["evidence_count"],
            "approval_hash": row["approval_hash"],
            "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
            "allocations": tuple(allocation_map.get(str(row["classification_id"]), ())),
        }
        for row in classification_rows
    )
    groups = tuple(
        {
            "duplicate_group_id": str(row["duplicate_group_id"]),
            "status": row["status"],
            "canonical_transaction_id": (
                str(row["canonical_transaction_id"])
                if row["canonical_transaction_id"]
                else None
            ),
            "transaction_ids": member_map.get(str(row["duplicate_group_id"]), ()),
            "approval_hash": row["approval_hash"],
            "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        }
        for row in group_rows
    )
    payload = {
        "matter_id": str(matter["matter_id"]),
        "title": matter["title"],
        "stage": matter["stage"],
        "version": matter["version"],
        "facts": facts,
        "claims": claims,
        "issues": issues,
        "transactions": transactions,
        "payment_classifications": classifications,
        "duplicate_groups": groups,
    }
    snapshot = CaseSnapshotRef(
        matter_id=str(matter["matter_id"]),
        matter_version=int(matter["version"]),
        snapshot_hash=_payload_hash(payload),
        schema_version=CASE_LEDGER_SNAPSHOT_SCHEMA_VERSION,
    )
    return _LedgerRead(
        snapshot=snapshot,
        facts=facts,
        claims=claims,
        responses=tuple(
            {
                **row,
                "claim_response_id": str(row["claim_response_id"]),
                "claim_id": str(row["claim_id"]),
                "approved_by": str(row["approved_by"]),
                "confirmed_fact_ids": response_facts.get(
                    str(row["claim_response_id"]), ()
                ),
            }
            for row in response_rows
        ),
        issues=issues,
        transactions=transactions,
    )


def _ledger_planning_objects(
    ledger: _LedgerRead,
) -> tuple[list[AuthoritativePlanningObject], tuple[GovernedLawyerPlanningSignal, ...]]:
    objects: list[AuthoritativePlanningObject] = []
    signals: list[GovernedLawyerPlanningSignal] = []
    version = object_version_code(ledger.snapshot.matter_version)
    responded_claim_ids = {
        str(row["claim_id"])
        for row in ledger.responses
        if row.get("approval_hash") is not None and row.get("approved_by") is not None
    }

    for row in ledger.facts:
        status = str(row["status"])
        if status == "INVALIDATED":
            continue
        fact_id = str(row["fact_id"])
        objects.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.CASE_FACT,
                object_id=fact_id,
                object_version=version,
                content_hash=_payload_hash(
                    {"schema_version": "planning-case-fact-v1", **row}
                ),
                status={
                    "CANDIDATE": PlanningInputStatus.REVIEW_REQUIRED,
                    "CONFIRMED": PlanningInputStatus.CONFIRMED,
                    "DISPUTED": PlanningInputStatus.DISPUTED,
                    "DENIED": PlanningInputStatus.BLOCKED,
                }[status],
            )
        )
        if status in {"DISPUTED", "DENIED"}:
            signals.append(
                GovernedLawyerPlanningSignal(
                    signal_id=fact_id,
                    signal_version=version,
                    decision_hash=str(row["decision_hash"]),
                    category=PlanningSignalCategory.CONFIRMED_FACT,
                    code=(
                        "LAWYER_DISPUTED_FACT"
                        if status == "DISPUTED"
                        else "LAWYER_DENIED_FACT"
                    ),
                    status=(
                        PlanningInputStatus.DISPUTED
                        if status == "DISPUTED"
                        else PlanningInputStatus.BLOCKED
                    ),
                    summary=(
                        "律师已将该事实标记为争议，后续分析不得把它作为无争议事实。"
                        if status == "DISPUTED"
                        else "律师已否认该事实，后续分析不得把它作为已确认事实。"
                    ),
                    source_ref_ids=(
                        planning_object_ref_id(
                            PlanningProjectionObjectType.CASE_FACT, fact_id
                        ),
                    ),
                )
            )

    active_claim_ids: set[str] = set()
    for row in ledger.claims:
        status = str(row["status"])
        if status == "INVALIDATED":
            continue
        claim_id = str(row["claim_id"])
        active_claim_ids.add(claim_id)
        objects.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.CASE_CLAIM,
                object_id=claim_id,
                object_version=version,
                content_hash=_payload_hash(
                    {"schema_version": "planning-case-claim-v1", **row}
                ),
                status=(
                    PlanningInputStatus.CONFIRMED
                    if (
                        status == "CONFIRMED_SCOPE"
                        and claim_id in responded_claim_ids
                    )
                    else PlanningInputStatus.REVIEW_REQUIRED
                ),
            )
        )

    for row in ledger.issues:
        status = str(row["status"])
        if status == "INVALIDATED":
            continue
        objects.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.DISPUTE_ISSUE,
                object_id=str(row["issue_id"]),
                object_version=version,
                content_hash=_payload_hash(
                    {"schema_version": "planning-dispute-issue-v1", **row}
                ),
                status=(
                    PlanningInputStatus.CONFIRMED
                    if status == "CONFIRMED"
                    else PlanningInputStatus.REVIEW_REQUIRED
                ),
            )
        )

    for row in ledger.transactions:
        status = str(row["status"])
        if status == "INVALIDATED":
            continue
        objects.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.CASE_TRANSACTION,
                object_id=str(row["transaction_id"]),
                object_version=version,
                content_hash=_payload_hash(
                    {"schema_version": "planning-case-transaction-v1", **row}
                ),
                status=(
                    PlanningInputStatus.CONFIRMED
                    if status == "CONFIRMED"
                    else PlanningInputStatus.REVIEW_REQUIRED
                ),
            )
        )

    for row in ledger.responses:
        claim_id = str(row["claim_id"])
        if claim_id not in active_claim_ids:
            continue
        position = str(row["position"])
        signal_status = {
            "ADMIT": PlanningInputStatus.CONFIRMED,
            "PARTIALLY_ADMIT": PlanningInputStatus.DISPUTED,
            "DISPUTE": PlanningInputStatus.DISPUTED,
            "OUTSIDE_SCOPE": PlanningInputStatus.BLOCKED,
        }[position]
        signals.append(
            GovernedLawyerPlanningSignal(
                signal_id=str(row["claim_response_id"]),
                signal_version=version,
                decision_hash=str(row["approval_hash"]),
                category=PlanningSignalCategory.CONFIRMED_FACT,
                code={
                    "ADMIT": "LAWYER_ADMITTED_CLAIM_RESPONSE",
                    "PARTIALLY_ADMIT": "LAWYER_PARTIALLY_ADMITTED_CLAIM_RESPONSE",
                    "DISPUTE": "LAWYER_DISPUTED_CLAIM_RESPONSE",
                    "OUTSIDE_SCOPE": "LAWYER_EXCLUDED_CLAIM_FROM_SCOPE",
                }[position],
                status=signal_status,
                summary={
                    "ADMIT": "律师已确认该诉请的答复口径；后续分析仍须遵守证据与法律复核。",
                    "PARTIALLY_ADMIT": "律师仅部分认可该诉请，后续分析不得扩大认可范围。",
                    "DISPUTE": "律师已对该诉请提出争议，后续分析不得视为已认可。",
                    "OUTSIDE_SCOPE": "律师已将该诉请排除在当前答复范围之外。",
                }[position],
                source_ref_ids=(
                    planning_object_ref_id(
                        PlanningProjectionObjectType.CASE_CLAIM, claim_id
                    ),
                ),
            )
        )
    return objects, tuple(signals)


def _read_current_agent_lawyer_signals(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    authorized_refs: frozenset[str],
) -> tuple[GovernedLawyerPlanningSignal, ...]:
    """Read only current, still task-bound corrections from migration 0035.

    A foreign key proves the referenced task once existed.  These joins also
    prove at projection time that the run, graph, task and their immutable
    hashes still agree and belong to this tenant/matter.  The graph need not be
    the mutable run head: recording a correction intentionally marks that graph
    stale before the next planning pass.
    """

    rows = _rows(
        connection.execute(
            """
            SELECT signal.signal_id, signal.signal_version,
                   signal.decision_code, signal.category, signal.signal_status,
                   signal.summary, signal.source_ref_ids, signal.decision_hash,
                   signal.task_input_hash, signal.graph_hash,
                   signal.subject_hash, signal.recorded_event_sequence,
                   signal.recorded_by, signal.decided_at,
                   signal.supersedes_signal_id,
                   signal.run_id, signal.graph_id, signal.task_id,
                   run.matter_id AS run_matter_id,
                   graph.matter_id AS graph_matter_id,
                   task.matter_id AS task_matter_id,
                   graph.graph_hash AS current_graph_hash,
                   task.input_hash AS current_task_input_hash
            FROM case_agent_lawyer_decision_signals signal
            JOIN case_agent_runs run
              ON run.run_id = signal.run_id
             AND run.firm_id = signal.firm_id
             AND run.matter_id = signal.matter_id
            JOIN case_agent_task_graphs graph
              ON graph.graph_id = signal.graph_id
             AND graph.run_id = signal.run_id
             AND graph.firm_id = signal.firm_id
             AND graph.matter_id = signal.matter_id
            JOIN case_agent_tasks task
              ON task.graph_id = signal.graph_id
             AND task.task_id = signal.task_id
             AND task.run_id = signal.run_id
             AND task.firm_id = signal.firm_id
             AND task.matter_id = signal.matter_id
            WHERE signal.firm_id = %s AND signal.matter_id = %s
              AND signal.is_current AND signal.superseded_at IS NULL
            ORDER BY signal.decided_at ASC, signal.signal_id ASC
            """,
            (firm_id, matter_id),
        ).fetchall()
    )
    result: list[GovernedLawyerPlanningSignal] = []
    for row in rows:
        if not (
            str(row["run_matter_id"]) == matter_id
            and str(row["graph_matter_id"]) == matter_id
            and str(row["task_matter_id"]) == matter_id
            and str(row["graph_hash"]) == str(row["current_graph_hash"])
            and str(row["task_input_hash"]) == str(row["current_task_input_hash"])
        ):
            raise CasePlanningProjectionBlocked(
                "current lawyer correction no longer binds its governed run, graph or task"
            )
        raw_refs = row["source_ref_ids"]
        if not isinstance(raw_refs, list) or not all(
            isinstance(value, str) for value in raw_refs
        ):
            raise CasePlanningProjectionBlocked(
                "current lawyer correction has malformed source references"
            )
        refs = tuple(sorted(set(raw_refs)))
        if len(refs) != len(raw_refs) or not set(refs).issubset(authorized_refs):
            raise CasePlanningProjectionBlocked(
                "current lawyer correction cites an unavailable planning input"
            )
        try:
            category = PlanningSignalCategory(str(row["category"]))
            status = PlanningInputStatus(str(row["signal_status"]))
            decision_code = LawyerPlanningDecisionCode(str(row["decision_code"]))
        except ValueError as error:
            raise CasePlanningProjectionBlocked(
                "current lawyer correction has an unsupported policy binding"
            ) from error
        try:
            decision = GovernedLawyerPlanningDecision(
                signal_id=str(row["signal_id"]),
                run_id=str(row["run_id"]),
                firm_id=firm_id,
                matter_id=matter_id,
                graph_id=str(row["graph_id"]),
                task_id=str(row["task_id"]),
                signal_version=int(row["signal_version"]),
                decision_code=decision_code,
                category=category,
                status=status,
                summary=str(row["summary"]),
                source_ref_ids=refs,
                task_input_hash=str(row["task_input_hash"]),
                graph_hash=str(row["graph_hash"]),
                subject_hash=str(row["subject_hash"]),
                decision_hash=str(row["decision_hash"]),
                recorded_event_sequence=int(row["recorded_event_sequence"]),
                recorded_by=str(row["recorded_by"]),
                decided_at=row["decided_at"],
                supersedes_signal_id=(
                    str(row["supersedes_signal_id"])
                    if row["supersedes_signal_id"] is not None
                    else None
                ),
            )
            decision.validate()
        except (LawyerPlanningDecisionBlocked, TypeError, ValueError) as error:
            raise CasePlanningProjectionBlocked(
                "current lawyer correction differs from its governed decision hash"
            ) from error
        result.append(
            GovernedLawyerPlanningSignal(
                signal_id=decision.signal_id,
                signal_version=object_version_code(decision.signal_version),
                decision_hash=decision.decision_hash,
                category=decision.category,
                code=decision.decision_code.value,
                status=decision.status,
                summary=decision.summary,
                source_ref_ids=decision.source_ref_ids,
            )
        )
    return tuple(result)


def _read_ledger_exception_decision_signals(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    authorized_refs: frozenset[str],
) -> tuple[GovernedLawyerPlanningSignal, ...]:
    """Project only current 0049 lifecycle heads with exact source lineage.

    The append-only 0047 decisions remain historical authority, but they are
    not themselves current planning work.  An unresolved route is projected
    only while its 0049 follow-up head is ``ACTIVE``; a duplicate disposition
    is projected only through the subject's current duplicate head.  Closed
    follow-ups and superseded duplicate dispositions therefore remain
    auditable without consuming the bounded planning-signal budget.
    """

    active_followups = _rows(
        connection.execute(
            """
            SELECT 'ACTIVE_FOLLOWUP'::text AS signal_scope,
                   followup.followup_id AS lifecycle_id,
                   followup.followup_kind AS lifecycle_kind,
                   followup.subject_hash,
                   head.head_event_id,
                   head.head_sequence,
                   head_event.event_hash AS current_hash,
                   head_event.expected_matter_version AS signal_matter_version,
                   head_event.followup_id = head.followup_id
                       AND head_event.event_sequence = head.head_sequence
                       AND head_event.subject_hash = head.subject_hash
                       AND head_event.state_after = head.current_state
                       AND head_event.expected_matter_version =
                           followup.created_matter_version
                       AS head_integrity,
                   decision.exception_decision_id,
                   decision.decision_hash AS origin_decision_hash,
                   decision.decision AS origin_decision,
                   decision.reason_code, decision.reason_note,
                   decision.exception_group_id =
                           followup.origin_exception_group_id
                       AND decision.extraction_batch_id =
                           followup.origin_extraction_batch_id
                       AND decision.run_id = followup.origin_run_id
                       AND decision.expected_matter_version =
                           followup.created_matter_version
                       AS origin_integrity,
                   exception_group.candidate_count,
                   validate_case_agent_ledger_exception_group_integrity(
                       exception_group.extraction_batch_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) AS group_integrity,
                   case_agent_ledger_exception_group_subject_hash(
                       exception_group.exception_group_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) = followup.subject_hash AS subject_integrity,
                   array_agg(DISTINCT candidate_page.evidence_page_id::text
                             ORDER BY candidate_page.evidence_page_id::text)
                       AS evidence_page_ids
              FROM case_agent_ledger_exception_followup_heads head
              JOIN case_agent_ledger_exception_followups followup
                ON followup.followup_id = head.followup_id
               AND followup.subject_hash = head.subject_hash
               AND followup.firm_id = head.firm_id
               AND followup.matter_id = head.matter_id
              JOIN case_agent_ledger_exception_followup_events head_event
                ON head_event.followup_event_id = head.head_event_id
               AND head_event.firm_id = head.firm_id
               AND head_event.matter_id = head.matter_id
              JOIN case_agent_ledger_exception_group_decisions decision
                ON decision.exception_decision_id =
                        followup.origin_exception_decision_id
               AND decision.firm_id = followup.firm_id
               AND decision.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_groups exception_group
                ON exception_group.exception_group_id =
                        followup.origin_exception_group_id
               AND exception_group.extraction_batch_id =
                        followup.origin_extraction_batch_id
               AND exception_group.firm_id = followup.firm_id
               AND exception_group.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_group_members member
                ON member.exception_group_id = exception_group.exception_group_id
               AND member.extraction_batch_id =
                        exception_group.extraction_batch_id
               AND member.firm_id = exception_group.firm_id
               AND member.matter_id = exception_group.matter_id
              JOIN case_agent_ledger_extraction_candidate_pages candidate_page
                ON candidate_page.extraction_candidate_id =
                        member.extraction_candidate_id
               AND candidate_page.firm_id = member.firm_id
               AND candidate_page.matter_id = member.matter_id
             WHERE head.firm_id = %s AND head.matter_id = %s
               AND head.current_state = 'ACTIVE'
             GROUP BY followup.followup_id, followup.followup_kind,
                      followup.subject_hash, head.followup_id,
                      head.subject_hash, head.current_state,
                      head.head_event_id, head.head_sequence,
                      head_event.event_hash,
                      head_event.expected_matter_version,
                      head_event.followup_id, head_event.event_sequence,
                      head_event.subject_hash, head_event.state_after,
                      decision.exception_decision_id, decision.decision_hash,
                      decision.exception_group_id,
                      decision.extraction_batch_id, decision.run_id,
                      decision.expected_matter_version,
                      decision.decision, decision.reason_code,
                      decision.reason_note, exception_group.exception_group_id,
                      exception_group.extraction_batch_id,
                      exception_group.firm_id, exception_group.matter_id,
                      exception_group.candidate_count
             ORDER BY followup.followup_id
            """,
            (firm_id, matter_id),
        ).fetchall()
    )
    current_duplicates = _rows(
        connection.execute(
            """
            SELECT 'CURRENT_DUPLICATE'::text AS signal_scope,
                   disposition.duplicate_disposition_id AS lifecycle_id,
                   'DUPLICATE'::text AS lifecycle_kind,
                   head.subject_hash,
                   NULL::uuid AS head_event_id,
                   NULL::integer AS head_sequence,
                   disposition.decision_hash AS current_hash,
                   disposition.decided_matter_version AS signal_matter_version,
                   TRUE AS head_integrity,
                   decision.exception_decision_id,
                   decision.decision_hash AS origin_decision_hash,
                   decision.decision AS origin_decision,
                   decision.reason_code, decision.reason_note,
                   decision.exception_group_id =
                           disposition.origin_exception_group_id
                       AND decision.extraction_batch_id =
                           disposition.origin_extraction_batch_id
                       AND decision.expected_matter_version =
                           disposition.decided_matter_version
                       AS origin_integrity,
                   exception_group.candidate_count,
                   validate_case_agent_ledger_exception_group_integrity(
                       exception_group.extraction_batch_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) AS group_integrity,
                   case_agent_ledger_exception_group_subject_hash(
                       exception_group.exception_group_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) = head.subject_hash AS subject_integrity,
                   array_agg(DISTINCT candidate_page.evidence_page_id::text
                             ORDER BY candidate_page.evidence_page_id::text)
                       AS evidence_page_ids
              FROM case_agent_ledger_exception_duplicate_heads head
              JOIN case_agent_ledger_exception_duplicate_dispositions disposition
                ON disposition.duplicate_disposition_id =
                        head.current_disposition_id
               AND disposition.subject_hash = head.subject_hash
               AND disposition.firm_id = head.firm_id
               AND disposition.matter_id = head.matter_id
              JOIN case_agent_ledger_exception_group_decisions decision
                ON decision.exception_decision_id =
                        disposition.origin_exception_decision_id
               AND decision.firm_id = disposition.firm_id
               AND decision.matter_id = disposition.matter_id
              JOIN case_agent_ledger_exception_groups exception_group
                ON exception_group.exception_group_id =
                        disposition.origin_exception_group_id
               AND exception_group.extraction_batch_id =
                        disposition.origin_extraction_batch_id
               AND exception_group.firm_id = disposition.firm_id
               AND exception_group.matter_id = disposition.matter_id
              JOIN case_agent_ledger_exception_group_members member
                ON member.exception_group_id = exception_group.exception_group_id
               AND member.extraction_batch_id =
                        exception_group.extraction_batch_id
               AND member.firm_id = exception_group.firm_id
               AND member.matter_id = exception_group.matter_id
              JOIN case_agent_ledger_extraction_candidate_pages candidate_page
                ON candidate_page.extraction_candidate_id =
                        member.extraction_candidate_id
               AND candidate_page.firm_id = member.firm_id
               AND candidate_page.matter_id = member.matter_id
             WHERE head.firm_id = %s AND head.matter_id = %s
             GROUP BY disposition.duplicate_disposition_id,
                      disposition.decision_hash,
                      disposition.decided_matter_version, head.subject_hash,
                      decision.exception_decision_id, decision.decision_hash,
                      decision.exception_group_id,
                      decision.extraction_batch_id,
                      decision.expected_matter_version,
                      decision.decision, decision.reason_code,
                      decision.reason_note, exception_group.exception_group_id,
                      exception_group.extraction_batch_id,
                      exception_group.firm_id, exception_group.matter_id,
                      exception_group.candidate_count
             ORDER BY head.subject_hash, disposition.duplicate_disposition_id
            """,
            (firm_id, matter_id),
        ).fetchall()
    )
    rows = (*active_followups, *current_duplicates)
    result: list[GovernedLawyerPlanningSignal] = []
    active_policy = {
        "REEXTRACTION": (
            "REQUEST_REEXTRACTION",
            PlanningSignalCategory.WORK_PLAN,
            PlanningInputStatus.OPEN,
            "LAWYER_REQUESTED_LEDGER_REEXTRACTION",
            "律师要求对所引原页重新提取；新规划应安排可恢复的重提取任务，不得沿用原候选。",
        ),
        "MORE_EVIDENCE": (
            "REQUEST_MORE_EVIDENCE",
            PlanningSignalCategory.WORK_PLAN,
            PlanningInputStatus.OPEN,
            "LAWYER_REQUESTED_MORE_LEDGER_EVIDENCE",
            "律师认为现有材料不足；新规划应明确补证缺口，不得自行补全事实。",
        ),
        "DEFERRED_REVIEW": (
            "DEFER_WITH_REASON",
            PlanningSignalCategory.WORK_PLAN,
            PlanningInputStatus.BLOCKED,
            "LAWYER_DEFERRED_LEDGER_EXCEPTION",
            "律师已暂缓处理该组候选；解除暂缓前，新规划不得将其作为已确认台账内容。",
        ),
    }
    duplicate_policy = (
        "REJECT_AS_DUPLICATE",
        PlanningSignalCategory.CONFIRMED_FACT,
        PlanningInputStatus.DISPUTED,
        "LAWYER_REJECTED_EXTRACTION_DUPLICATE",
        "律师已将该组材料提取候选判定为重复；新规划不得将其重复写入案件事实或收付款台账。",
    )
    current_subjects: set[str] = set()
    for row in rows:
        if (
            row["head_integrity"] is not True
            or row["origin_integrity"] is not True
            or row["group_integrity"] is not True
            or row["subject_integrity"] is not True
        ):
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle head no longer binds its exact group subject"
            )
        scope = str(row["signal_scope"])
        lifecycle_kind = str(row["lifecycle_kind"])
        origin_decision = str(row["origin_decision"])
        if scope == "ACTIVE_FOLLOWUP":
            if lifecycle_kind not in active_policy:
                raise CasePlanningProjectionBlocked(
                    "active ledger exception follow-up kind is unsupported"
                )
            expected_decision, category, status, code, base_summary = active_policy[
                lifecycle_kind
            ]
        elif scope == "CURRENT_DUPLICATE":
            expected_decision, category, status, code, base_summary = duplicate_policy
            if lifecycle_kind != "DUPLICATE":
                raise CasePlanningProjectionBlocked(
                    "current duplicate disposition kind is invalid"
                )
        else:
            raise CasePlanningProjectionBlocked(
                "ledger exception lifecycle signal scope is unsupported"
            )
        if origin_decision != expected_decision:
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle head differs from its origin route"
            )
        lifecycle_id = str(row["lifecycle_id"])
        _uuid(lifecycle_id, "ledger exception lifecycle_id")
        if scope == "ACTIVE_FOLLOWUP":
            _uuid(str(row["head_event_id"]), "ledger exception head_event_id")
            try:
                head_sequence = int(row["head_sequence"])
            except (TypeError, ValueError) as error:
                raise CasePlanningProjectionBlocked(
                    "active ledger exception head sequence is invalid"
                ) from error
            if head_sequence < 1:
                raise CasePlanningProjectionBlocked(
                    "active ledger exception head sequence is invalid"
                )
        else:
            head_sequence = None
        subject_hash = str(row["subject_hash"])
        current_hash = str(row["current_hash"])
        origin_decision_hash = str(row["origin_decision_hash"])
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in (subject_hash, current_hash, origin_decision_hash)
        ):
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle hash is invalid"
            )
        if scope == "CURRENT_DUPLICATE" and current_hash != origin_decision_hash:
            raise CasePlanningProjectionBlocked(
                "current duplicate disposition differs from its origin decision hash"
            )
        if subject_hash in current_subjects:
            raise CasePlanningProjectionBlocked(
                "ledger exception subject has more than one current lifecycle head"
            )
        current_subjects.add(subject_hash)
        origin_exception_decision_id = str(row["exception_decision_id"])
        _uuid(
            origin_exception_decision_id,
            "ledger exception origin_exception_decision_id",
        )
        try:
            signal_matter_version = int(row["signal_matter_version"])
        except (TypeError, ValueError) as error:
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle version is invalid"
            ) from error
        if signal_matter_version < 1:
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle version is invalid"
            )
        raw_page_ids = row["evidence_page_ids"]
        if not isinstance(raw_page_ids, list) or not raw_page_ids:
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle head has no authoritative source page"
            )
        page_ids = tuple(str(page_id) for page_id in raw_page_ids)
        for page_id in page_ids:
            _uuid(page_id, "ledger exception evidence_page_id")
        if page_ids != tuple(sorted(set(page_ids))):
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle pages are not canonical"
            )
        refs = tuple(f"evidence-page:{page_id}" for page_id in page_ids)
        if not set(refs).issubset(authorized_refs):
            raise CasePlanningProjectionBlocked(
                "current ledger exception lifecycle head cites an unavailable evidence page"
            )
        note = row["reason_note"]
        summary = base_summary
        if lifecycle_kind == "DEFERRED_REVIEW":
            if not isinstance(note, str) or not note.strip() or len(note) > 500:
                raise CasePlanningProjectionBlocked(
                    "active deferred ledger exception has no bounded lawyer reason"
                )
            summary = f"{base_summary}律师原因：{note.strip()}"
        if len(summary) > 1_000:
            raise CasePlanningProjectionBlocked(
                "ledger exception planning summary is too long"
            )
        shards = tuple(
            refs[index : index + 100] for index in range(0, len(refs), 100)
        )
        lifecycle_namespace = UUID(lifecycle_id)
        for shard_index, source_refs in enumerate(shards, start=1):
            shard_id = str(
                uuid5(
                    lifecycle_namespace,
                    f"ledger-exception-current-signal-v2:{scope}:{shard_index}",
                )
            )
            shard_hash = _payload_hash(
                {
                    "schema_version": "ledger-exception-current-planning-signal-v2",
                    "signal_scope": scope,
                    "lifecycle_id": lifecycle_id,
                    "lifecycle_kind": lifecycle_kind,
                    "subject_hash": subject_hash,
                    "current_hash": current_hash,
                    "origin_exception_decision_id": origin_exception_decision_id,
                    "origin_decision_hash": origin_decision_hash,
                    "origin_decision": origin_decision,
                    "reason_code": str(row["reason_code"]),
                    "head_sequence": head_sequence,
                    "shard_index": shard_index,
                    "shard_count": len(shards),
                    "source_ref_ids": source_refs,
                }
            )
            result.append(
                GovernedLawyerPlanningSignal(
                    signal_id=shard_id,
                    signal_version=object_version_code(signal_matter_version),
                    decision_hash=shard_hash,
                    category=category,
                    code=code,
                    status=status,
                    summary=(
                        summary
                        if len(shards) == 1
                        else f"{summary}来源分片 {shard_index}/{len(shards)}。"
                    ),
                    source_ref_ids=source_refs,
                )
            )
    if len(result) > 100:
        raise CasePlanningProjectionBlocked(
            "active ledger exception follow-ups and current duplicate dispositions "
            "require more than 100 governed source shards"
        )
    return tuple(result)


def _read_active_reextraction_planning_obligations(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    authorized_refs: frozenset[str],
) -> tuple[ReextractionPlanningObligation, ...]:
    """Return one server-only obligation per exact ACTIVE source cohort.

    This query intentionally does not reuse the 100-reference planning-signal
    shards.  A re-extraction task must cover the complete authoritative page
    set, and two current follow-ups over that same set must share one execution
    while retaining both lifecycle identities for later binding.
    """

    rows = _rows(
        connection.execute(
            """
            SELECT followup.followup_id,
                   control_assignment.control_run_id,
                   control_head.current_state AS control_state,
                   followup.subject_hash, followup.created_matter_version,
                   head.head_event_id, head.head_sequence,
                   head_event.event_hash AS current_event_hash,
                   head_event.followup_id = head.followup_id
                       AND head_event.event_sequence = head.head_sequence
                       AND head_event.subject_hash = head.subject_hash
                       AND head_event.state_after = head.current_state
                       AND head_event.expected_matter_version =
                           followup.created_matter_version
                       AS head_integrity,
                   decision.exception_decision_id,
                   decision.decision_hash AS origin_decision_hash,
                   decision.exception_group_id =
                           followup.origin_exception_group_id
                       AND decision.extraction_batch_id =
                           followup.origin_extraction_batch_id
                       AND decision.run_id = followup.origin_run_id
                       AND decision.expected_matter_version =
                           followup.created_matter_version
                       AND decision.decision = 'REQUEST_REEXTRACTION'
                       AS origin_integrity,
                   validate_case_agent_ledger_exception_group_integrity(
                       exception_group.extraction_batch_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) AS group_integrity,
                   case_agent_ledger_exception_group_subject_hash(
                       exception_group.exception_group_id,
                       exception_group.firm_id,
                       exception_group.matter_id
                   ) = followup.subject_hash AS subject_integrity,
                   array_agg(
                       DISTINCT candidate_page.evidence_page_id::text
                       ORDER BY candidate_page.evidence_page_id::text
                   ) AS evidence_page_ids
              FROM case_agent_ledger_exception_followups followup
              JOIN case_agent_ledger_exception_followup_heads head
                ON head.followup_id = followup.followup_id
               AND head.subject_hash = followup.subject_hash
               AND head.firm_id = followup.firm_id
               AND head.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_followup_events head_event
                ON head_event.followup_event_id = head.head_event_id
               AND head_event.firm_id = head.firm_id
               AND head_event.matter_id = head.matter_id
              JOIN case_agent_ledger_exception_control_heads control_head
                ON control_head.firm_id = followup.firm_id
               AND control_head.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_control_assignments
                   control_assignment
                ON control_assignment.control_assignment_id =
                       control_head.current_control_assignment_id
               AND control_assignment.firm_id = control_head.firm_id
               AND control_assignment.matter_id = control_head.matter_id
               AND control_assignment.state_after = control_head.current_state
               AND control_assignment.assignment_sequence =
                       control_head.head_sequence
              JOIN case_agent_ledger_exception_group_decisions decision
                ON decision.exception_decision_id =
                       followup.origin_exception_decision_id
               AND decision.firm_id = followup.firm_id
               AND decision.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_groups exception_group
                ON exception_group.exception_group_id =
                       followup.origin_exception_group_id
               AND exception_group.extraction_batch_id =
                       followup.origin_extraction_batch_id
               AND exception_group.firm_id = followup.firm_id
               AND exception_group.matter_id = followup.matter_id
              JOIN case_agent_ledger_exception_group_members member
                ON member.exception_group_id = exception_group.exception_group_id
               AND member.extraction_batch_id =
                       exception_group.extraction_batch_id
               AND member.firm_id = exception_group.firm_id
               AND member.matter_id = exception_group.matter_id
              JOIN case_agent_ledger_extraction_candidate_pages candidate_page
                ON candidate_page.extraction_candidate_id =
                       member.extraction_candidate_id
               AND candidate_page.firm_id = member.firm_id
               AND candidate_page.matter_id = member.matter_id
             WHERE followup.firm_id = %s AND followup.matter_id = %s
               AND followup.followup_kind = 'REEXTRACTION'
               AND head.current_state = 'ACTIVE'
             GROUP BY followup.followup_id,
                      control_assignment.control_run_id,
                      control_head.current_state,
                      followup.subject_hash, followup.created_matter_version,
                      head.followup_id, head.subject_hash, head.current_state,
                      head.head_event_id, head.head_sequence,
                      head_event.followup_id, head_event.event_sequence,
                      head_event.subject_hash, head_event.state_after,
                      head_event.expected_matter_version,
                      head_event.event_hash,
                      decision.exception_decision_id, decision.decision_hash,
                      decision.exception_group_id,
                      decision.extraction_batch_id, decision.run_id,
                      decision.expected_matter_version, decision.decision,
                      exception_group.exception_group_id,
                      exception_group.extraction_batch_id,
                      exception_group.firm_id, exception_group.matter_id
             ORDER BY followup.control_run_id, followup.followup_id
            """,
            (firm_id, matter_id),
        ).fetchall()
    )
    cohorts: dict[
        tuple[str, tuple[str, ...]], list[dict[str, object]]
    ] = {}
    seen_followups: set[str] = set()
    control_runs: set[str] = set()
    for row in rows:
        if row["control_state"] == "RECOVERY_REQUIRED":
            raise CasePlanningProjectionBlocked(
                "REEXTRACTION_CONTROL_RECOVERY_REQUIRED"
            )
        if row["control_state"] != "HEALTHY":
            raise CasePlanningProjectionBlocked(
                "active re-extraction control state is invalid"
            )
        if any(
            row[name] is not True
            for name in (
                "head_integrity",
                "origin_integrity",
                "group_integrity",
                "subject_integrity",
            )
        ):
            raise CasePlanningProjectionBlocked(
                "active re-extraction obligation differs from its lifecycle authority"
            )
        followup_id = str(row["followup_id"])
        control_run_id = str(row["control_run_id"])
        head_event_id = str(row["head_event_id"])
        origin_decision_id = str(row["exception_decision_id"])
        for value, label in (
            (followup_id, "re-extraction followup_id"),
            (control_run_id, "re-extraction control_run_id"),
            (head_event_id, "re-extraction head_event_id"),
            (origin_decision_id, "re-extraction origin decision_id"),
        ):
            _uuid(value, label)
        if followup_id in seen_followups:
            raise CasePlanningProjectionBlocked(
                "active re-extraction follow-up is duplicated"
            )
        seen_followups.add(followup_id)
        control_runs.add(control_run_id)
        try:
            head_sequence = int(row["head_sequence"])
            created_matter_version = int(row["created_matter_version"])
        except (TypeError, ValueError) as error:
            raise CasePlanningProjectionBlocked(
                "active re-extraction lifecycle version is invalid"
            ) from error
        if head_sequence < 1 or created_matter_version < 1:
            raise CasePlanningProjectionBlocked(
                "active re-extraction lifecycle version is invalid"
            )
        hashes = {
            "subject_hash": str(row["subject_hash"]),
            "current_event_hash": str(row["current_event_hash"]),
            "origin_decision_hash": str(row["origin_decision_hash"]),
        }
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in hashes.values()
        ):
            raise CasePlanningProjectionBlocked(
                "active re-extraction lifecycle hash is invalid"
            )
        raw_page_ids = row["evidence_page_ids"]
        if not isinstance(raw_page_ids, list) or not raw_page_ids:
            raise CasePlanningProjectionBlocked(
                "active re-extraction obligation has no authoritative source page"
            )
        page_ids = tuple(str(value) for value in raw_page_ids)
        for page_id in page_ids:
            _uuid(page_id, "re-extraction evidence_page_id")
        if page_ids != tuple(sorted(set(page_ids))):
            raise CasePlanningProjectionBlocked(
                "active re-extraction source pages are not canonical"
            )
        source_refs = tuple(f"evidence-page:{page_id}" for page_id in page_ids)
        if not set(source_refs).issubset(authorized_refs):
            raise CasePlanningProjectionBlocked(
                "active re-extraction obligation cites an unavailable evidence page"
            )
        cohorts.setdefault((control_run_id, source_refs), []).append(
            {
                "followup_id": followup_id,
                "subject_hash": hashes["subject_hash"],
                "head_event_id": head_event_id,
                "head_sequence": head_sequence,
                "current_event_hash": hashes["current_event_hash"],
                "origin_decision_id": origin_decision_id,
                "origin_decision_hash": hashes["origin_decision_hash"],
                "created_matter_version": created_matter_version,
            }
        )
    if len(control_runs) > 1:
        raise CasePlanningProjectionBlocked(
            "active re-extraction obligations span multiple control runs"
        )

    obligations: list[ReextractionPlanningObligation] = []
    for (control_run_id, source_refs), lifecycle_rows in sorted(
        cohorts.items(), key=lambda item: item[0]
    ):
        canonical_rows = tuple(
            sorted(lifecycle_rows, key=lambda item: str(item["followup_id"]))
        )
        obligations.append(
            ReextractionPlanningObligation.build(
                control_run_id=control_run_id,
                followup_ids=tuple(
                    str(item["followup_id"]) for item in canonical_rows
                ),
                source_ref_ids=source_refs,
                lifecycle_hash=_payload_hash(
                    {
                        "schema_version": (
                            "case-ledger-active-reextraction-obligation-v1"
                        ),
                        "control_run_id": control_run_id,
                        "source_ref_ids": source_refs,
                        "followups": canonical_rows,
                    }
                ),
            )
        )
    return tuple(sorted(obligations, key=lambda item: item.obligation_id))


def _read_material_objects(
    connection: Any, *, firm_id: str, matter_id: str
) -> tuple[AuthoritativePlanningObject, ...]:
    rows = connection.execute(
        """
        SELECT material.material_object_id, material.content_sha256
        FROM case_agent_material_objects material
        WHERE material.firm_id = %s AND material.matter_id = %s
          AND NOT EXISTS (
            SELECT 1 FROM case_agent_material_object_tombstones tombstone
            WHERE tombstone.material_object_id = material.material_object_id
              AND tombstone.firm_id = material.firm_id
              AND tombstone.matter_id = material.matter_id
          )
        ORDER BY material.created_at ASC, material.material_object_id ASC
        """,
        (firm_id, matter_id),
    ).fetchall()
    return tuple(
        AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.MATERIAL_OBJECT,
            object_id=str(row["material_object_id"]),
            object_version="v1",
            content_hash=str(row["content_sha256"]),
            status=PlanningInputStatus.AVAILABLE,
        )
        for row in rows
    )


def _read_evidence_page_objects(
    connection: Any, *, firm_id: str, matter_id: str, matter_version: int
) -> tuple[AuthoritativePlanningObject, ...]:
    rows = connection.execute(
        """
        SELECT page.evidence_page_id, page.evidence_file_id, page.page_number,
               page.rendered_page_sha256, original.original_file_sha256,
               original.media_type,
               decision.decision_id, decision.disposition,
               decision.status AS decision_status, decision.approval_hash
        FROM evidence_pages page
        JOIN evidence_original_files original
          ON original.evidence_file_id = page.evidence_file_id
         AND original.firm_id = page.firm_id
         AND original.matter_id = page.matter_id
        LEFT JOIN LATERAL (
          SELECT item.decision_id, item.disposition, item.status, item.approval_hash
          FROM evidence_page_decisions item
          WHERE item.evidence_page_id = page.evidence_page_id
            AND item.firm_id = page.firm_id AND item.matter_id = page.matter_id
            AND item.status <> 'INVALIDATED'
          ORDER BY CASE WHEN item.status = 'APPROVED' THEN 0 ELSE 1 END,
                   item.created_at DESC, item.decision_id DESC
          LIMIT 1
        ) decision ON true
        WHERE page.firm_id = %s AND page.matter_id = %s
        ORDER BY original.created_at ASC, page.page_number ASC, page.evidence_page_id ASC
        """,
        (firm_id, matter_id),
    ).fetchall()
    result: list[AuthoritativePlanningObject] = []
    for raw in rows:
        row = dict(raw)
        result.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.EVIDENCE_PAGE,
                object_id=str(row["evidence_page_id"]),
                object_version=object_version_code(matter_version),
                content_hash=_payload_hash(
                    {"schema_version": "planning-evidence-page-v1", **row}
                ),
                status=(
                    PlanningInputStatus.CONFIRMED
                    if row["decision_status"] == "APPROVED"
                    else PlanningInputStatus.REVIEW_REQUIRED
                ),
                source_media_type=str(row["media_type"]),
            )
        )
    return tuple(result)


def _read_posture(
    connection: Any, *, firm_id: str, matter_id: str, configured: bool
) -> tuple[
    ProjectionSectionState,
    ConfirmedPostureProjection | None,
    AuthoritativePlanningObject | None,
]:
    if not configured:
        return ProjectionSectionState.NOT_CONFIGURED, None, None
    row = connection.execute(
        """
        SELECT profile.profile_id, profile.profile_version, profile.profile_hash,
               profile.case_type_code, profile.procedure_stage,
               profile.represented_position, profile.authority_scope_code,
               profile.engagement_state, latest.effective_status
        FROM case_posture_profile_heads head
        JOIN case_posture_profiles profile
          ON profile.profile_id = head.current_profile_id
         AND profile.firm_id = head.firm_id AND profile.matter_id = head.matter_id
        JOIN LATERAL (
          SELECT event.effective_status
          FROM case_posture_profile_events event
          WHERE event.profile_id = profile.profile_id
            AND event.firm_id = profile.firm_id AND event.matter_id = profile.matter_id
          ORDER BY event.event_sequence DESC LIMIT 1
        ) latest ON true
        WHERE head.firm_id = %s AND head.matter_id = %s
        """,
        (firm_id, matter_id),
    ).fetchone()
    if row is None or row["effective_status"] != "CURRENT":
        return ProjectionSectionState.EMPTY, None, None
    version = object_version_code(int(row["profile_version"]))
    posture = ConfirmedPostureProjection(
        profile_id=str(row["profile_id"]),
        profile_version=version,
        profile_hash=str(row["profile_hash"]),
        effective_status="CURRENT",
        case_type_code=str(row["case_type_code"]),
        procedure_stage=str(row["procedure_stage"]),
        represented_position=str(row["represented_position"]),
        authority_scope_code=str(row["authority_scope_code"]),
        engagement_state=str(row["engagement_state"]),
    )
    return (
        ProjectionSectionState.AVAILABLE,
        posture,
        AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.POSTURE_PROFILE,
            object_id=posture.profile_id,
            object_version=posture.profile_version,
            content_hash=posture.profile_hash,
            status=PlanningInputStatus.CONFIRMED,
        ),
    )


def _read_work_plan(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    matter_version: int,
    configured: bool,
) -> tuple[
    ProjectionSectionState,
    ActiveDynamicWorkPlanProjection | None,
    tuple[AuthoritativePlanningObject, ...],
]:
    if not configured:
        return ProjectionSectionState.NOT_CONFIGURED, None, ()
    plan = connection.execute(
        """
        SELECT plan.plan_id, plan.plan_version, plan.status, plan.plan_hash,
               plan.activated_matter_version
        FROM case_work_plan_heads head
        JOIN case_work_plans plan
          ON plan.plan_id = head.current_plan_id
         AND plan.firm_id = head.firm_id AND plan.matter_id = head.matter_id
        WHERE head.firm_id = %s AND head.matter_id = %s
        """,
        (firm_id, matter_id),
    ).fetchone()
    if plan is None or plan["status"] != "ACTIVE":
        return ProjectionSectionState.EMPTY, None, ()
    if int(plan["activated_matter_version"] or 0) != matter_version:
        # A plan activated against an older case ledger is no longer an
        # authoritative planning input.  Treat it as absent so a new ordinary
        # Agent run can propose its replacement; blocking here turns the stale
        # plan into a permanent catch-22.  Active-plan execution remains safe:
        # its server-bound compiler requires the exact ACTIVE_DYNAMIC_WORK_PLAN
        # signal and fails closed when this section is EMPTY.
        return ProjectionSectionState.EMPTY, None, ()
    items = _rows(
        connection.execute(
            """
            SELECT item_id, sequence, item_kind, readiness, title, purpose, rationale,
                   risk_if_omitted, confidence, review_gate,
                   delivery_target, deliverable_kind, required_for_delivery,
                   is_primary_document
            FROM case_work_plan_items
            WHERE plan_id = %s AND firm_id = %s AND matter_id = %s
            ORDER BY sequence ASC, item_id ASC
            """,
            (plan["plan_id"], firm_id, matter_id),
        ).fetchall()
    )
    if not items:
        raise CasePlanningProjectionBlocked("active work plan has no items")
    if len(items) > 500:
        raise CasePlanningProjectionBlocked("active work plan exceeds the planning input limit")
    plan_version = object_version_code(int(plan["plan_version"]))
    plan_hash = str(plan["plan_hash"])
    objects = tuple(
        AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.WORK_PLAN_ITEM,
            object_id=str(row["item_id"]),
            object_version=plan_version,
            content_hash=planning_work_plan_item_content_hash(
                plan_id=str(plan["plan_id"]),
                plan_hash=plan_hash,
                row=row,
            ),
            status=(
                PlanningInputStatus.CONFIRMED
                if row["readiness"] == "ACTIONABLE"
                else PlanningInputStatus.OPEN
            ),
        )
        for row in items
    )
    readiness = [str(row["readiness"]) for row in items]
    projection = ActiveDynamicWorkPlanProjection(
        plan_id=str(plan["plan_id"]),
        plan_version=plan_version,
        plan_hash=plan_hash,
        bound_matter_version=matter_version,
        item_ids=tuple(str(row["item_id"]) for row in items),
        actionable_count=readiness.count("ACTIONABLE"),
        needs_information_count=readiness.count("NEEDS_INFORMATION"),
        needs_research_count=readiness.count("NEEDS_RESEARCH"),
    )
    return ProjectionSectionState.AVAILABLE, projection, objects


def planning_work_plan_item_content_hash(
    *, plan_id: str, plan_hash: str, row: Any
) -> str:
    """Hash the exact public metadata used for one WORK_PLAN_ITEM input.

    Both the atomic planning projection and the active-plan execution command
    use this helper, so a second run cannot be bound to a browser-supplied or
    differently serialized item hash.
    """

    fields = (
        "item_id",
        "sequence",
        "item_kind",
        "readiness",
        "title",
        "purpose",
        "rationale",
        "risk_if_omitted",
        "confidence",
        "review_gate",
        "delivery_target",
        "deliverable_kind",
        "required_for_delivery",
        "is_primary_document",
    )
    data = {field: row[field] for field in fields}
    data["item_id"] = str(data["item_id"])
    return _payload_hash(
        {
            "schema_version": "planning-work-plan-item-v1",
            "plan_id": str(plan_id),
            "plan_hash": str(plan_hash),
            **data,
        }
    )


def _read_legal_sources(
    connection: Any, *, firm_id: str, matter_id: str, configured: bool
) -> tuple[ProjectionSectionState, tuple[AuthoritativePlanningObject, ...]]:
    if not configured:
        return ProjectionSectionState.NOT_CONFIGURED, ()
    bundle = connection.execute(
        """
        SELECT bundle_id
        FROM case_legal_bundles
        WHERE firm_id = %s AND matter_id = %s AND status = 'APPROVED'
        """,
        (firm_id, matter_id),
    ).fetchone()
    if bundle is None:
        return ProjectionSectionState.EMPTY, ()
    references = _rows(
        connection.execute(
            """
            SELECT source_snapshot_id AS snapshot_id, source_sha256 AS expected_hash
            FROM case_legal_bundle_segments
            WHERE bundle_id = %s AND firm_id = %s AND matter_id = %s
            UNION ALL
            SELECT parameter_source_snapshot_id AS snapshot_id,
                   parameter_source_sha256 AS expected_hash
            FROM case_legal_bundle_segments
            WHERE bundle_id = %s AND firm_id = %s AND matter_id = %s
              AND parameter_source_snapshot_id IS NOT NULL
            """,
            (bundle["bundle_id"], firm_id, matter_id, bundle["bundle_id"], firm_id, matter_id),
        ).fetchall()
    )
    if not references:
        return ProjectionSectionState.EMPTY, ()
    expected: dict[str, str] = {}
    for row in references:
        snapshot_id = str(row["snapshot_id"])
        expected_hash = str(row["expected_hash"])
        prior = expected.setdefault(snapshot_id, expected_hash)
        if prior != expected_hash:
            raise CasePlanningProjectionBlocked(
                "approved legal bundle has conflicting source hashes"
            )
    sources = _rows(
        connection.execute(
            """
            SELECT snapshot_id, content_sha256
            FROM official_legal_source_snapshots
            WHERE firm_id = %s AND snapshot_id = ANY(%s::uuid[])
              AND verification_status = 'VERIFIED' AND license_status = 'ACTIVE'
              AND license_review_hash IS NOT NULL
            ORDER BY snapshot_id ASC
            """,
            (firm_id, list(expected)),
        ).fetchall()
    )
    actual = {str(row["snapshot_id"]): str(row["content_sha256"]) for row in sources}
    if actual != expected:
        raise CasePlanningProjectionBlocked(
            "approved legal bundle cites an unavailable, changed or unlicensed source"
        )
    rule_rows = _rows(
        connection.execute(
            """
            SELECT DISTINCT segment.rule_version_id,
                   segment.rule_version AS segment_rule_version,
                   rule.rule_version AS approved_rule_version,
                   rule.status AS approved_rule_status,
                   rule.approval_hash
            FROM case_legal_bundle_segments segment
            LEFT JOIN legal_rule_versions rule
              ON rule.rule_version_id = segment.rule_version_id
             AND rule.firm_id = segment.firm_id
            WHERE segment.bundle_id = %s AND segment.firm_id = %s
              AND segment.matter_id = %s
            ORDER BY segment.rule_version_id ASC
            """,
            (bundle["bundle_id"], firm_id, matter_id),
        ).fetchall()
    )
    if not rule_rows:
        raise CasePlanningProjectionBlocked(
            "approved legal bundle has no bound approved rule versions"
        )
    rules: list[AuthoritativePlanningObject] = []
    for row in rule_rows:
        rule_id = str(row["rule_version_id"])
        rule_version = row["segment_rule_version"]
        if (
            not isinstance(rule_version, str)
            or row["approved_rule_version"] != rule_version
            or row["approved_rule_status"] != "APPROVED"
            or not isinstance(row["approval_hash"], str)
        ):
            raise CasePlanningProjectionBlocked(
                "approved legal bundle cites a changed or unapproved rule version"
            )
        rules.append(
            AuthoritativePlanningObject(
                object_type=PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                object_id=rule_id,
                # ``rule_version_id`` already identifies one immutable rule
                # version.  Planning-object versions must be stable codes,
                # while a lawyer-maintained rule label is allowed to begin
                # with a date or other non-code text.  Keep the projection
                # envelope at v1 and re-read the exact legal label plus its
                # approval hash before document disclosure.
                object_version="v1",
                content_hash=str(row["approval_hash"]),
                status=PlanningInputStatus.LOCKED,
            )
        )
    objects = tuple(
        AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            object_id=snapshot_id,
            object_version="v1",
            content_hash=content_hash,
            status=PlanningInputStatus.LOCKED,
        )
        for snapshot_id, content_hash in sorted(actual.items())
    )
    return ProjectionSectionState.AVAILABLE, (*objects, *rules)


def _read_procedural_events(
    connection: Any, *, firm_id: str, matter_id: str, configured: bool
) -> tuple[ProjectionSectionState, tuple[AuthoritativePlanningObject, ...]]:
    if not configured:
        return ProjectionSectionState.NOT_CONFIGURED, ()
    rows = _rows(
        connection.execute(
            """
            SELECT legal_event_id, event_kind, local_date, approval_hash
            FROM case_legal_events
            WHERE firm_id = %s AND matter_id = %s AND status = 'APPROVED'
              AND event_kind IN ('CLAIM_FILED', 'CASE_ACCEPTED', 'JUDGMENT')
            ORDER BY local_date ASC, legal_event_id ASC
            """,
            (firm_id, matter_id),
        ).fetchall()
    )
    if not rows:
        return ProjectionSectionState.EMPTY, ()
    objects = tuple(
        AuthoritativePlanningObject(
            object_type=PlanningProjectionObjectType.PROCEDURAL_EVENT,
            object_id=str(row["legal_event_id"]),
            object_version="v1",
            content_hash=_payload_hash(
                {"schema_version": "planning-procedural-event-v1", **row}
            ),
            status=PlanningInputStatus.CONFIRMED,
        )
        for row in rows
    )
    return ProjectionSectionState.AVAILABLE, objects


def _require_expected_snapshot(
    actual: CaseSnapshotRef, expected: CaseSnapshotRef, fence: str
) -> None:
    if actual != expected:
        raise CasePlanningProjectionBlocked(
            f"{fence} case snapshot differs from AgentRunState.snapshot"
        )


def _rows(values: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(value) for value in values)


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise CasePlanningProjectionBlocked(f"{label} must be a UUID") from error


__all__ = [
    "PostgresCasePlanningProjectionRepository",
    "read_authoritative_projection_in_transaction",
    "read_current_case_snapshot_in_transaction",
]
