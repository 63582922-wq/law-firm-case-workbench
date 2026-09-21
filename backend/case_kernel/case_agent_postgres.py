"""PostgreSQL event store and durable dispatcher for the case Agent supervisor.

`case_agent_supervisor` is the sole workflow authority.  This adapter persists
its immutable events, replays them through the reducer, and maintains only
operational projections needed to lease work efficiently.  A database row can
therefore never invent a legal or execution transition that the reducer would
reject.
"""

from __future__ import annotations

import json

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Iterable, Iterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_agent_supervisor import (
    AdapterExecutionMode,
    ActivePlanDeliverableRef,
    ActivePlanExecutionRef,
    AgentAutonomyLevel,
    AgentDeliverableFormat,
    AgentDeliverableKind,
    AgentEventType,
    AgentGoal,
    AgentRiskLevel,
    AgentRunState,
    AgentRunStatus,
    AgentSupervisorBlocked,
    AgentSupervisorEvent,
    AgentTaskGraph,
    AgentTaskSpec,
    AgentTaskStatus,
    ApprovalPayload,
    ApprovalRecord,
    ArtifactReceipt,
    CaseSnapshotRef,
    ExternalSubmissionState,
    NetworkPolicy,
    PlanningFailurePayload,
    PlanningBudgetReviewPayload,
    SupplementaryMaterialStageReviewPayload,
    CaseAnalysisStageReviewPayload,
    PlanningMaterialScopeReviewPayload,
    ResultStatus,
    RetryMode,
    RunCompletedPayload,
    RunCreatedPayload,
    RunFinalReviewApproval,
    LawyerPlanCorrectionPayload,
    RunResourceBudget,
    SkillBinding,
    SnapshotChangedPayload,
    SupervisorCommand,
    SupervisorCommandKind,
    TaskCapabilityContract,
    TaskGraphPayload,
    TaskResourceBudget,
    TaskResultPayload,
    TaskResultReceipt,
    TaskStartedPayload,
    VerificationPayload,
    decide_next_commands,
    reduce_agent_event,
    replay_agent_events,
)
from .case_ledger_postgres import (
    CaseLedgerPersistenceBlocked,
    IdempotencyConflict,
    VersionConflict,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _payload_hash,
    _require_positive_version,
    _require_roles,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
    _validate_uuid,
)
from .models import Actor, Role
from .skill_registry import ApprovalGate, CapabilityScope
from .case_agent_verifier import RunVerificationReceipt, VerificationOutcome
from .case_agent_planning_snapshot_postgres import (
    planning_work_plan_item_content_hash,
)
from .case_agent_planner import CasePlanProposal, case_plan_proposal_payload
from .controlled_defence_case_agent_planner import CONTROLLED_DEFENCE_PLANNER_ID


@dataclass(frozen=True)
class AgentControlCommandReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    run_id: str
    event_version: int
    event_id: str
    status: str


@dataclass(frozen=True)
class FinalReviewCompletionIntent:
    """Immutable proof for one exact browser final-review command.

    This is intentionally reconstructed from the idempotency row, command
    audit and immutable event log.  It therefore remains valid if the run head
    later advances (for example, after a snapshot refresh) and cannot be
    confused with a completion submitted under another idempotency key.
    """

    receipt: AgentControlCommandReceipt
    reviewed_artifact_count: int


@dataclass(frozen=True)
class PreparedActivePlanExecution:
    execution: ActivePlanExecutionRef
    matter_version: int
    existing_run_id: str | None = None


_SAFE_LOCAL_EXECUTION_REISSUE_ERRORS = (
    "LOCAL_ADAPTER_FAILED",
    "DOCUMENT_RENDERER_REJECTED",
    "DOCUMENT_RENDERER_RESULT_UNKNOWN",
)


def _active_plan_execution_can_be_reissued(row: Any) -> bool:
    """Allow a new run only after an auditable, side-effect-free local stop."""

    return bool(
        row is not None
        and str(row["run_status"])
        in {
            AgentRunStatus.WAITING_INPUT.value,
            AgentRunStatus.WAITING_APPROVAL.value,
        }
        and not bool(row["is_stale"])
        and not bool(row["is_cancelled"])
        and bool(row["has_safe_local_failure"])
        and bool(row["has_only_local_results"])
    )


def _assert_new_case_agent_run_allowed(
    latest: Any, *, expected_matter_version: int, prior_state: AgentRunState | None = None
) -> None:
    """Prevent parallel/duplicate paid runs while preserving a new-case round.

    A verified result may remain awaiting lawyer review after the lawyer adds
    a confirmed claim or dispute issue.  That old result stays immutable, but
    a distinct run may analyse the newer matter snapshot.  Every other live or
    human-blocked run must be resolved, paused/cancelled, or completed first.
    """

    if latest is None:
        return
    status = str(latest["status"])
    snapshot_matter_version = int(latest["snapshot_matter_version"])
    if status in {
        AgentRunStatus.COMPLETED.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.STALE.value,
    }:
        return
    if (status == AgentRunStatus.WAITING_INPUT.value
            and prior_state is not None
            and prior_state.status is AgentRunStatus.WAITING_INPUT
            and prior_state.tasks
            and all(task.status in {AgentTaskStatus.SUCCEEDED, AgentTaskStatus.FAILED}
                    and task.receipts
                    and task.active_attempt_id == task.receipts[-1].attempt_id
                    and len(task.receipts) == task.attempt_count
                    and all(receipt.status in {ResultStatus.SUCCEEDED, ResultStatus.FAILED}
                            and receipt.external_submission_state is not ExternalSubmissionState.UNKNOWN
                            for receipt in task.receipts)
                    for task in prior_state.tasks)
            and any(task.status is AgentTaskStatus.FAILED
                    and task.receipts[-1].error_code == "LAWYER_ANALYSIS_OUTPUT_REJECTED"
                    for task in prior_state.tasks)):
        # A new explicit command may follow a known rejected response. Never
        # retry its attempt or change the old immutable failure history.
        return
    if (
        status == AgentRunStatus.READY_FOR_REVIEW.value
        and snapshot_matter_version < expected_matter_version
    ):
        return
    if status == AgentRunStatus.READY_FOR_REVIEW.value:
        raise AgentSupervisorBlocked(
            "the current Agent result still awaits lawyer review for this case snapshot"
        )
    raise AgentSupervisorBlocked(
        "another Agent run is still active or awaiting a governed intervention"
    )


@dataclass(frozen=True)
class AppliedCaseSnapshotRefresh:
    """One atomic 0046 request -> CASE_SNAPSHOT_CHANGED transition."""

    refresh_request_id: str
    run_id: str
    matter_id: str
    target_matter_version: int
    event_version: int
    snapshot_hash: str


@dataclass(frozen=True)
class ClaimedAgentTask:
    run_id: str
    task_id: str
    attempt_id: str
    event_version: int
    lease_owner: str
    lease_expires_at: datetime
    task: AgentTaskSpec
    attempt_version: int = 1
    reconciliation: bool = False
    external_request_id: str | None = None


@dataclass(frozen=True)
class ReapedAgentAttempt:
    run_id: str
    task_id: str
    attempt_id: str
    event_version: int
    requires_reconciliation: bool


@dataclass(frozen=True)
class ReapedAgentPlanningAttempt:
    run_id: str
    planning_attempt_id: str
    event_version: int
    requires_reconciliation: bool


@dataclass(frozen=True)
class ClaimedAgentPlanningAttempt:
    """One durable planning lease.

    ``external_request_id`` is an opaque attempt correlation value.  Only a
    provider-backed planner may use it to cross the external boundary; the
    governed local defendant-response planner records its outcome without
    creating an outbound request.
    """

    run_id: str
    planning_attempt_id: str
    external_request_id: str
    event_version: int
    matter_version: int
    planning_kind: str
    planning_hash: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    attempt_version: int
    status: str = "CLAIMED"
    request_hash: str | None = None
    external_ledger_status: str | None = None
    external_ledger_version: int | None = None
    recovered_proposal: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentPlanningOutcome:
    run_id: str
    planning_attempt_id: str
    status: str
    event_version: int
    structured_proposal: dict[str, Any] | None
    output_hash: str | None
    error_code: str | None


@dataclass(frozen=True)
class AgentWorkerHeartbeatRecord:
    firm_id: str
    worker_id: str
    actor_id: str
    planner_id: str
    adapter_catalog_hash: str
    verifier_actor_id: str
    verifier_id: str
    verifier_version: str
    verifier_policy_hash: str
    observed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class PersistentAgentRunProjection:
    state: AgentRunState
    next_commands: tuple[SupervisorCommand, ...]
    checkpoint_verified: bool


@dataclass(frozen=True)
class PersistentAgentRunRecord:
    projection: PersistentAgentRunProjection
    created_at: datetime
    updated_at: datetime
    # This is a browser-safe lineage classification, not a client-controlled
    # comparison of aggregate version numbers.  Registering a verified plan
    # candidate intentionally advances the matter aggregate, while leaving
    # the analysed facts, evidence, claims and rules unchanged.
    input_snapshot_status: str = "CURRENT"


def _classify_run_input_snapshot_status(
    *,
    current_matter_version: int,
    snapshot_matter_version: int,
    run_is_stale: bool,
    current_plan_lineage: str | None,
) -> str:
    """Classify whether an Agent's analysed inputs remain usable.

    Matter ``version`` is an aggregate CAS clock, not a synonym for factual
    input change.  A verified plan candidate and its subsequent activation
    deliberately advance that clock.  Only a lineage row proved by the server
    may suppress the normal stale-input result.
    """

    if (
        not isinstance(current_matter_version, int)
        or not isinstance(snapshot_matter_version, int)
        or current_matter_version < 1
        or snapshot_matter_version < 1
        or current_matter_version < snapshot_matter_version
    ):
        raise CaseLedgerPersistenceBlocked("Agent input snapshot versions are invalid")
    if type(run_is_stale) is not bool:
        raise CaseLedgerPersistenceBlocked("Agent input snapshot stale flag is invalid")
    if current_plan_lineage not in {
        None,
        "PLAN_CANDIDATE_REGISTERED",
        "PLAN_ACTIVE",
    }:
        raise CaseLedgerPersistenceBlocked("Agent input snapshot lineage is invalid")
    if run_is_stale:
        return "INPUTS_CHANGED"
    if current_matter_version == snapshot_matter_version:
        return "CURRENT"
    if current_plan_lineage is not None:
        return current_plan_lineage
    return "INPUTS_CHANGED"


class PostgresCaseAgentStore:
    """Durable control-plane adapter; it contains no independent state machine."""

    _HUMAN_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )
    _APPROVER_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _WORKER_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = _HUMAN_ROLES | frozenset({Role.SYSTEM_WORKER})

    _WORKER_EVENTS = frozenset(
        {
            AgentEventType.PLANNING_STARTED,
            AgentEventType.PLANNING_FAILED,
            AgentEventType.PLANNING_RESULT_UNKNOWN,
            AgentEventType.TASK_GRAPH_ACCEPTED,
            AgentEventType.TASK_STARTED,
            AgentEventType.TASK_RESULT_RECORDED,
            AgentEventType.CASE_SNAPSHOT_CHANGED,
            AgentEventType.VERIFICATION_STARTED,
            AgentEventType.VERIFICATION_PASSED,
            AgentEventType.VERIFICATION_FAILED,
        }
    )
    _HUMAN_EVENTS = frozenset(
        {
            AgentEventType.RUN_PAUSED,
            AgentEventType.RUN_RESUMED,
            AgentEventType.RUN_CANCELLED,
        }
    )
    _APPROVAL_EVENTS = frozenset(
        {AgentEventType.APPROVAL_GRANTED, AgentEventType.RUN_COMPLETED}
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def create_run(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_matter_version: int,
        idempotency_key: str,
        event: AgentSupervisorEvent,
    ) -> AgentControlCommandReceipt:
        """Create a lawyer-owned goal and the first authoritative run event."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_human(actor, self._HUMAN_ROLES)
        _require_positive_version(expected_matter_version)
        if (
            event.event_type is not AgentEventType.RUN_CREATED
            or event.sequence != 1
            or event.matter_id != matter_id
            or event.firm_id != actor.firm_id
            or event.actor_id != actor.actor_id
            or not isinstance(event.payload, RunCreatedPayload)
        ):
            raise AgentSupervisorBlocked("run creation requires the exact first lawyer event")
        state = reduce_agent_event(None, event)
        if state.snapshot.matter_version != expected_matter_version:
            raise AgentSupervisorBlocked("run snapshot must bind the current matter version")
        command_name = "CREATE_CASE_AGENT_RUN"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_matter_version": expected_matter_version,
                "goal": _payload_json(event.payload),
                "run_id": event.run_id,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            replay = self._prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_matter_version,
                allowed_roles=self._HUMAN_ROLES,
            )
            latest = connection.execute(
                """
                SELECT run_id, status, snapshot_matter_version
                FROM case_agent_runs
                WHERE matter_id = %s AND firm_id = %s
                  AND is_cancelled = false
                ORDER BY updated_at DESC, run_id DESC
                LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            prior_state = None
            if latest is not None and str(latest["status"]) == "WAITING_INPUT":
                prior_state = replay_agent_events(self._load_events(
                    connection, actor=actor, matter_id=matter_id,
                    run_id=str(latest["run_id"])))
            _assert_new_case_agent_run_allowed(
                latest, expected_matter_version=expected_matter_version,
                prior_state=prior_state,
            )
            projection = _state_json(state)
            projection_hash = _payload_hash(projection)
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective, success_criteria,
                    constraints, requested_by, goal_hash,
                    requested_deliverables, active_plan_execution
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    state.goal.goal_id,
                    actor.firm_id,
                    matter_id,
                    state.goal.objective,
                    Jsonb(list(state.goal.success_criteria)),
                    Jsonb(list(state.goal.constraints)),
                    actor.actor_id,
                    state.goal.goal_hash,
                    Jsonb(
                        [item.value for item in state.goal.requested_deliverables]
                    ),
                    Jsonb(_active_plan_execution_json(state.goal.active_plan_execution))
                    if state.goal.active_plan_execution is not None
                    else None,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status, current_event_version,
                    snapshot_matter_version, snapshot_schema_version, snapshot_hash,
                    run_budget, projection_hash, paused_from, is_stale, is_cancelled,
                    verification_hash, failure_code, created_by
                ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
                          NULL, false, false, NULL, NULL, %s)
                """,
                (
                    state.run_id,
                    actor.firm_id,
                    matter_id,
                    state.goal.goal_id,
                    state.status.value,
                    state.snapshot.matter_version,
                    state.snapshot.schema_version,
                    state.snapshot.snapshot_hash,
                    Jsonb(asdict(state.budget)),
                    projection_hash,
                    actor.actor_id,
                ),
            )
            self._insert_event(connection, event)
            self._insert_checkpoint(connection, state, projection, projection_hash)
            return self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=state.status,
            )

    def prepare_active_plan_execution(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_matter_version: int,
    ) -> PreparedActivePlanExecution:
        """Read the exact ACTIVE-plan deliverables for a server-built goal."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_positive_version(expected_matter_version)
        _require_roles(actor, frozenset({Role.LEAD_LAWYER}))
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=frozenset({Role.LEAD_LAWYER}),
            )
            execution = self._active_plan_execution_ref_in_transaction(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_matter_version=expected_matter_version,
                lock_rows=False,
            )
            existing = connection.execute(
                """
                SELECT execution.execution_id, execution.run_id,
                       execution.execution_attempt,
                       run.status AS run_status, run.is_stale, run.is_cancelled,
                       (public.case_agent_local_plan_reissue_allowed(
                           execution.run_id, execution.firm_id, execution.matter_id
                       ) OR EXISTS (
                           SELECT 1
                           FROM case_agent_task_receipts receipt
                           WHERE receipt.run_id = execution.run_id
                             AND receipt.firm_id = execution.firm_id
                             AND receipt.matter_id = execution.matter_id
                             AND receipt.result_status = 'FAILED'
                             AND receipt.external_submission_state =
                                 'NOT_APPLICABLE'
                             AND receipt.error_code = ANY(%s)
                       )) AS has_safe_local_failure,
                       NOT EXISTS (
                           SELECT 1
                           FROM case_agent_task_receipts receipt
                           WHERE receipt.run_id = execution.run_id
                             AND receipt.firm_id = execution.firm_id
                             AND receipt.matter_id = execution.matter_id
                             AND (
                                 receipt.external_submission_state <>
                                     'NOT_APPLICABLE'
                                 OR receipt.external_calls <> 0
                                 OR receipt.result_status = 'UNKNOWN'
                             )
                       ) AS has_only_local_results
                FROM case_agent_active_plan_execution_runs execution
                JOIN case_agent_runs run
                  ON run.run_id = execution.run_id
                 AND run.firm_id = execution.firm_id
                 AND run.matter_id = execution.matter_id
                WHERE execution.plan_id = %s
                  AND execution.matter_id = %s
                  AND execution.firm_id = %s
                ORDER BY execution.execution_attempt DESC
                LIMIT 1
                """,
                (
                    list(_SAFE_LOCAL_EXECUTION_REISSUE_ERRORS),
                    execution.plan_id,
                    matter_id,
                    actor.firm_id,
                ),
            ).fetchone()
        return PreparedActivePlanExecution(
            execution=execution,
            matter_version=expected_matter_version,
            existing_run_id=(
                None
                if existing is None
                or _active_plan_execution_can_be_reissued(existing)
                else str(existing["run_id"])
            ),
        )

    def active_plan_execution_intent(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
    ) -> tuple[str, int] | None:
        """Return the immutable plan/version binding for one execution run.

        The aggregate run snapshot may advance after a governed replan.  Unknown
        write reconciliation must therefore use the immutable 0053 execution
        row plus the plan's activation version, not the run's current snapshot.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _validate_uuid("run_id", run_id)
        _require_roles(actor, frozenset({Role.LEAD_LAWYER}))
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=frozenset({Role.LEAD_LAWYER}),
            )
            row = connection.execute(
                """
                SELECT execution.plan_id, execution.activated_matter_version
                FROM case_agent_active_plan_execution_runs execution
                JOIN case_work_plans plan
                  ON plan.plan_id = execution.plan_id
                 AND plan.firm_id = execution.firm_id
                 AND plan.matter_id = execution.matter_id
                WHERE execution.run_id = %s
                  AND execution.matter_id = %s
                  AND execution.firm_id = %s
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            return None
        activated_version = row["activated_matter_version"]
        if type(activated_version) is not int or activated_version < 1:
            raise CaseLedgerPersistenceBlocked(
                "已激活计划执行记录缺少不可变的激活版本"
            )
        return str(row["plan_id"]), activated_version

    def final_review_completion_intent(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
    ) -> FinalReviewCompletionIntent | None:
        """Read the exact durable receipt for one final-review request.

        The current run projection is deliberately not consulted.  A later
        event may legitimately advance that projection, whereas the original
        command idempotency row, audit and RUN_COMPLETED event are immutable.
        """

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _validate_uuid("run_id", run_id)
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        command_name = f"APPEND_CASE_AGENT_{AgentEventType.RUN_COMPLETED.value}"
        expected_event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"final-review-event:{idempotency_key}",
            )
        )
        expected_approval_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"final-review:{idempotency_key}",
            )
        )
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._APPROVER_ROLES,
            )
            row = connection.execute(
                """
                SELECT command.request_hash,
                       command.response_json,
                       audit.request_hash AS audit_request_hash,
                       audit.input_event_version,
                       audit.output_event_version,
                       audit.event_id AS audit_event_id,
                       event.event_sequence,
                       event.event_type,
                       event.actor_id,
                       event.payload,
                       to_jsonb(approval) AS approval_projection
                FROM command_idempotency command
                JOIN case_agent_command_audits audit
                  ON audit.firm_id = command.firm_id
                 AND audit.matter_id = command.matter_id
                 AND audit.actor_id = command.actor_id
                 AND audit.command_name = command.command_name
                 AND audit.request_hash = command.request_hash
                 AND audit.run_id = %s
                 AND audit.event_id = %s
                 AND audit.input_event_version = %s
                 AND audit.output_event_version = %s
                JOIN case_agent_events event
                  ON event.event_id = audit.event_id
                 AND event.run_id = audit.run_id
                 AND event.firm_id = audit.firm_id
                 AND event.matter_id = audit.matter_id
                LEFT JOIN case_agent_approvals approval
                  ON approval.run_id = event.run_id AND approval.firm_id = event.firm_id
                 AND approval.matter_id = event.matter_id AND approval.event_sequence = event.event_sequence
                 AND approval.approval_id::text = event.payload->'final_review'->>'approval_id'
                 AND approval.approval_kind = 'FINAL_REVIEW'
                WHERE command.firm_id = %s
                  AND command.matter_id = %s
                  AND command.actor_id = %s
                 AND command.command_name = %s
                 AND command.idempotency_key = %s
                  AND event.event_sequence = %s
                """,
                (
                    run_id,
                    expected_event_id,
                    expected_event_version,
                    expected_event_version + 1,
                    actor.firm_id,
                    matter_id,
                    actor.actor_id,
                    command_name,
                    idempotency_key,
                    expected_event_version + 1,
                ),
            ).fetchone()
            if row is None:
                return None
            response_json = row["response_json"]
            _exact_keys(
                response_json,
                {
                    "command_name",
                    "idempotency_key",
                    "matter_id",
                    "run_id",
                    "event_version",
                    "event_id",
                    "status",
                },
                "final-review command receipt",
            )
            receipt = AgentControlCommandReceipt(**response_json)
            completed_event_version = expected_event_version + 1
            if (
                row["request_hash"] != row["audit_request_hash"]
                or int(row["input_event_version"]) != expected_event_version
                or int(row["output_event_version"]) != completed_event_version
                or str(row["audit_event_id"]) != expected_event_id
                or int(row["event_sequence"]) != completed_event_version
                or str(row["event_type"]) != AgentEventType.RUN_COMPLETED.value
                or str(row["actor_id"]) != actor.actor_id
                or receipt.command_name != command_name
                or receipt.idempotency_key != idempotency_key
                or receipt.matter_id != matter_id
                or receipt.run_id != run_id
                or receipt.event_version != completed_event_version
                or receipt.event_id != expected_event_id
                or receipt.status != AgentRunStatus.COMPLETED.value
            ):
                raise CaseLedgerPersistenceBlocked(
                    "final-review command receipt differs from its immutable audit"
                )
            events = self._load_events(
                connection, actor=actor, matter_id=matter_id, run_id=run_id
            )

        reviewed_events = tuple(
            event for event in events if event.sequence <= expected_event_version
        )
        if (
            len(reviewed_events) != expected_event_version
            or tuple(event.sequence for event in reviewed_events)
            != tuple(range(1, expected_event_version + 1))
        ):
            raise CaseLedgerPersistenceBlocked(
                "final-review predecessor event history is incomplete"
            )
        reviewed_state = replay_agent_events(reviewed_events)
        completed_event = next(
            (event for event in events if event.sequence == completed_event_version),
            None,
        )
        if (
            completed_event is None
            or completed_event.event_id != expected_event_id
            or completed_event.event_type is not AgentEventType.RUN_COMPLETED
            or completed_event.actor_id != actor.actor_id
            or not isinstance(completed_event.payload, RunCompletedPayload)
            or completed_event.payload.final_review.approval_id
            != expected_approval_id
            or completed_event.payload.final_review.approved_by != actor.actor_id
        ):
            raise CaseLedgerPersistenceBlocked(
                "final-review event differs from the exact browser intent"
            )
        # The reducer revalidates graph, verification and artifact-manifest
        # bindings against the exact reviewed predecessor.
        expected_request_hash = _payload_hash({
            "matter_id": matter_id,
            "expected_event_version": expected_event_version,
            "run_id": run_id,
            "event_type": AgentEventType.RUN_COMPLETED.value,
            "payload": _payload_json(completed_event.payload),
        })
        if row["request_hash"] != expected_request_hash:
            raise CaseLedgerPersistenceBlocked("final-review request hash differs from its immutable event payload")
        approval = completed_event.payload.final_review
        expected_projection = {
            "approval_id": approval.approval_id, "run_id": run_id,
            "firm_id": actor.firm_id, "matter_id": matter_id,
            "graph_id": reviewed_state.graph.graph_id if reviewed_state.graph else None,
            "approval_kind": "FINAL_REVIEW", "task_id": None, "task_input_hash": None, "gate": None,
            "graph_hash": approval.graph_hash, "verification_hash": approval.verification_hash,
            "artifact_manifest_hash": approval.artifact_manifest_hash,
            "approved_by": approval.approved_by, "approval_hash": approval.approval_hash,
            "event_sequence": completed_event_version,
        }
        projection = row.get("approval_projection")
        if not isinstance(projection, dict) or any(
            key not in projection or projection[key] != value for key, value in expected_projection.items()
        ):
            raise CaseLedgerPersistenceBlocked("final-review approval projection differs from its immutable event")
        completed_state = reduce_agent_event(reviewed_state, completed_event)
        if (
            reviewed_state.status is not AgentRunStatus.READY_FOR_REVIEW
            or completed_state.status is not AgentRunStatus.COMPLETED
            or completed_state.event_version != completed_event_version
        ):
            raise CaseLedgerPersistenceBlocked(
                "final-review event does not complete its reviewed predecessor"
            )
        return FinalReviewCompletionIntent(
            receipt=receipt,
            reviewed_artifact_count=len(reviewed_state.artifacts),
        )

    def create_active_plan_execution_run(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_matter_version: int,
        idempotency_key: str,
        event: AgentSupervisorEvent,
    ) -> str:
        """Atomically bind one ACTIVE plan to at most one server-derived run."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_roles(actor, frozenset({Role.LEAD_LAWYER}))
        _require_positive_version(expected_matter_version)
        if (
            event.event_type is not AgentEventType.RUN_CREATED
            or event.sequence != 1
            or event.matter_id != matter_id
            or event.firm_id != actor.firm_id
            or event.actor_id != actor.actor_id
            or not isinstance(event.payload, RunCreatedPayload)
            or event.payload.goal.active_plan_execution is None
        ):
            raise AgentSupervisorBlocked(
                "active-plan execution requires the exact first server-owned event"
            )
        state = reduce_agent_event(None, event)
        if state.snapshot.matter_version != expected_matter_version:
            raise AgentSupervisorBlocked(
                "active-plan execution snapshot must bind the current matter version"
            )
        command_name = "EXECUTE_ACTIVE_CASE_WORK_PLAN"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_matter_version": expected_matter_version,
                "goal": _payload_json(event.payload),
                "run_id": event.run_id,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            replay = self._prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay.run_id
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=expected_matter_version,
                allowed_roles=frozenset({Role.LEAD_LAWYER}),
            )
            execution = self._active_plan_execution_ref_in_transaction(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_matter_version=expected_matter_version,
                lock_rows=True,
            )
            existing = connection.execute(
                """
                SELECT execution.execution_id, execution.run_id,
                       execution.execution_attempt,
                       run.status AS run_status, run.is_stale, run.is_cancelled,
                       (public.case_agent_local_plan_reissue_allowed(
                           execution.run_id, execution.firm_id, execution.matter_id
                       ) OR EXISTS (
                           SELECT 1
                           FROM case_agent_task_receipts receipt
                           WHERE receipt.run_id = execution.run_id
                             AND receipt.firm_id = execution.firm_id
                             AND receipt.matter_id = execution.matter_id
                             AND receipt.result_status = 'FAILED'
                             AND receipt.external_submission_state =
                                 'NOT_APPLICABLE'
                             AND receipt.error_code = ANY(%s)
                       )) AS has_safe_local_failure,
                       NOT EXISTS (
                           SELECT 1
                           FROM case_agent_task_receipts receipt
                           WHERE receipt.run_id = execution.run_id
                             AND receipt.firm_id = execution.firm_id
                             AND receipt.matter_id = execution.matter_id
                             AND (
                                 receipt.external_submission_state <>
                                     'NOT_APPLICABLE'
                                 OR receipt.external_calls <> 0
                                 OR receipt.result_status = 'UNKNOWN'
                             )
                       ) AS has_only_local_results
                FROM case_agent_active_plan_execution_runs execution
                JOIN case_agent_runs run
                  ON run.run_id = execution.run_id
                 AND run.firm_id = execution.firm_id
                 AND run.matter_id = execution.matter_id
                WHERE execution.plan_id = %s
                  AND execution.matter_id = %s
                  AND execution.firm_id = %s
                -- The matter row is already locked above, which serializes
                -- all execution intents for this matter.  This provenance
                -- table is append-only; a second row lock would add no safety
                -- and incorrectly require UPDATE privilege on immutable data.
                ORDER BY execution.execution_attempt DESC
                LIMIT 1
                """,
                (
                    list(_SAFE_LOCAL_EXECUTION_REISSUE_ERRORS),
                    execution.plan_id,
                    matter_id,
                    actor.firm_id,
                ),
            ).fetchone()
            if existing is not None and not _active_plan_execution_can_be_reissued(
                existing
            ):
                return str(existing["run_id"])
            execution_attempt = (
                1 if existing is None else int(existing["execution_attempt"]) + 1
            )
            supersedes_execution_id = (
                None if existing is None else str(existing["execution_id"])
            )
            retry_reason_code = (
                None if existing is None else "SAFE_LOCAL_FAILURE_REISSUE"
            )
            if (
                state.goal.active_plan_execution != execution
                or state.goal.requested_deliverables
                != tuple(item.deliverable_kind for item in execution.items)
                or event.run_id == execution.source_run_id
            ):
                raise AgentSupervisorBlocked(
                    "active-plan execution goal differs from the current server plan"
                )
            projection = _state_json(state)
            projection_hash = _payload_hash(projection)
            connection.execute(
                """
                INSERT INTO case_agent_goals (
                    goal_id, firm_id, matter_id, objective, success_criteria,
                    constraints, requested_by, goal_hash,
                    requested_deliverables, active_plan_execution
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    state.goal.goal_id,
                    actor.firm_id,
                    matter_id,
                    state.goal.objective,
                    Jsonb(list(state.goal.success_criteria)),
                    Jsonb(list(state.goal.constraints)),
                    actor.actor_id,
                    state.goal.goal_hash,
                    Jsonb([item.value for item in state.goal.requested_deliverables]),
                    Jsonb(_active_plan_execution_json(execution)),
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_runs (
                    run_id, firm_id, matter_id, goal_id, status, current_event_version,
                    snapshot_matter_version, snapshot_schema_version, snapshot_hash,
                    run_budget, projection_hash, paused_from, is_stale, is_cancelled,
                    verification_hash, failure_code, created_by
                ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s,
                          NULL, false, false, NULL, NULL, %s)
                """,
                (
                    state.run_id,
                    actor.firm_id,
                    matter_id,
                    state.goal.goal_id,
                    state.status.value,
                    state.snapshot.matter_version,
                    state.snapshot.schema_version,
                    state.snapshot.snapshot_hash,
                    Jsonb(asdict(state.budget)),
                    projection_hash,
                    actor.actor_id,
                ),
            )
            self._insert_event(connection, event)
            self._insert_checkpoint(connection, state, projection, projection_hash)
            connection.execute(
                """
                INSERT INTO case_agent_active_plan_execution_runs (
                    execution_id, firm_id, matter_id, plan_id, plan_hash,
                    activated_matter_version, source_run_id, run_id, requested_by,
                    execution_attempt, supersedes_execution_id, retry_reason_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(
                        uuid5(
                            UUID(execution.plan_id),
                            (
                                "active-plan-execution-v1"
                                if execution_attempt == 1
                                else "active-plan-execution-safe-local-reissue-v1:"
                                f"{execution_attempt}:{supersedes_execution_id}"
                            ),
                        )
                    ),
                    actor.firm_id,
                    matter_id,
                    execution.plan_id,
                    execution.plan_hash,
                    expected_matter_version,
                    execution.source_run_id,
                    state.run_id,
                    actor.actor_id,
                    execution_attempt,
                    supersedes_execution_id,
                    retry_reason_code,
                ),
            )
            self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=state.status,
            )
            return state.run_id

    @staticmethod
    def _active_plan_execution_ref_in_transaction(
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        expected_matter_version: int,
        lock_rows: bool,
    ) -> ActivePlanExecutionRef:
        lock_clause = "FOR SHARE OF matter, source_run" if lock_rows else ""
        plan = connection.execute(
            f"""
            SELECT plan.plan_id, plan.plan_hash, plan.status,
                   plan.activated_matter_version, matter.version AS matter_version,
                   promotion.run_id AS source_run_id,
                   source_run.status AS source_run_status,
                   source_run.is_stale AS source_run_is_stale,
                   source_run.is_cancelled AS source_run_is_cancelled,
                   goal.requested_deliverables,
                   goal.active_plan_execution AS source_active_plan_execution
            FROM case_work_plan_heads head
            JOIN case_work_plans plan
              ON plan.plan_id = head.current_plan_id
             AND plan.firm_id = head.firm_id AND plan.matter_id = head.matter_id
            JOIN matters matter
              ON matter.matter_id = plan.matter_id AND matter.firm_id = plan.firm_id
            JOIN case_agent_work_plan_promotions promotion
              ON promotion.plan_id = plan.plan_id
             AND promotion.firm_id = plan.firm_id
             AND promotion.matter_id = plan.matter_id
            JOIN case_agent_runs source_run
              ON source_run.run_id = promotion.run_id
             AND source_run.firm_id = promotion.firm_id
             AND source_run.matter_id = promotion.matter_id
            JOIN case_agent_goals goal
              ON goal.goal_id = source_run.goal_id
             AND goal.firm_id = source_run.firm_id
             AND goal.matter_id = source_run.matter_id
            WHERE head.matter_id = %s AND head.firm_id = %s
            {lock_clause}
            """,
            (matter_id, actor.firm_id),
        ).fetchone()
        if (
            plan is None
            or str(plan["status"]) != "ACTIVE"
            or int(plan["matter_version"]) != expected_matter_version
            or int(plan["activated_matter_version"] or 0) != expected_matter_version
            or str(plan["source_run_status"]) not in {"READY_FOR_REVIEW", "COMPLETED"}
            or bool(plan["source_run_is_stale"])
            or bool(plan["source_run_is_cancelled"])
            or plan["source_active_plan_execution"] is not None
        ):
            raise CaseLedgerPersistenceBlocked(
                "当前没有可由第二次运行执行的已激活 Agent 计划"
            )
        raw_requested = plan["requested_deliverables"]
        if not isinstance(raw_requested, list) or not raw_requested:
            raise CaseLedgerPersistenceBlocked(
                "已激活计划没有结构化的可复核成果意图"
            )
        try:
            requested = tuple(AgentDeliverableKind(item) for item in raw_requested)
        except (TypeError, ValueError) as error:
            raise CaseLedgerPersistenceBlocked(
                "已激活计划包含当前版本不支持的成果类型"
            ) from error
        if tuple(sorted(set(requested), key=lambda item: item.value)) != requested:
            raise CaseLedgerPersistenceBlocked(
                "已激活计划成果意图不是唯一的服务器目录集合"
            )
        rows = connection.execute(
            """
            SELECT item_id, sequence, item_kind, readiness, title, purpose, rationale,
                   risk_if_omitted, confidence, review_gate, delivery_target,
                   deliverable_kind, required_for_delivery, is_primary_document
            FROM case_work_plan_items
            WHERE plan_id = %s AND matter_id = %s AND firm_id = %s
              AND deliverable_kind IS NOT NULL
            ORDER BY deliverable_kind ASC, item_id ASC
            """,
            (plan["plan_id"], matter_id, actor.firm_id),
        ).fetchall()
        actual = tuple(str(row["deliverable_kind"]) for row in rows)
        if actual != tuple(item.value for item in requested):
            raise CaseLedgerPersistenceBlocked(
                "已激活计划的文书项与原结构化成果意图不一致"
            )
        if any(
            str(row["item_kind"]) != "DOCUMENT_CANDIDATE"
            or str(row["delivery_target"]) != "INTERNAL_WORK_PRODUCT"
            for row in rows
        ):
            raise CaseLedgerPersistenceBlocked(
                "已激活计划包含非内部审阅的文书成果"
            )
        actionable_rows = tuple(
            row for row in rows if str(row["readiness"]) == "ACTIONABLE"
        )
        if not actionable_rows:
            raise CaseLedgerPersistenceBlocked(
                "已激活计划没有具备可执行来源的内部审阅成果"
            )
        if any(
            bool(row["required_for_delivery"])
            and str(row["readiness"]) != "ACTIONABLE"
            for row in rows
        ):
            raise CaseLedgerPersistenceBlocked(
                "已激活计划仍有必须交付的成果缺少可执行来源"
            )
        plan_id = str(plan["plan_id"])
        plan_hash = str(plan["plan_hash"])
        format_by_kind = {
            AgentDeliverableKind.CASE_REVIEW_MEMO: AgentDeliverableFormat.DOCX,
            AgentDeliverableKind.DEFENCE_STATEMENT: AgentDeliverableFormat.DOCX,
            AgentDeliverableKind.EVIDENCE_CATALOGUE: AgentDeliverableFormat.XLSX,
            AgentDeliverableKind.PAYMENT_LEDGER: AgentDeliverableFormat.XLSX,
            AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST: AgentDeliverableFormat.DOCX,
        }
        items = tuple(
            ActivePlanDeliverableRef(
                item_id=str(row["item_id"]),
                item_hash=planning_work_plan_item_content_hash(
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    row=row,
                ),
                deliverable_kind=AgentDeliverableKind(
                    str(row["deliverable_kind"])
                ),
                output_format=format_by_kind[
                    AgentDeliverableKind(str(row["deliverable_kind"]))
                ],
            )
            for row in actionable_rows
        )
        result = ActivePlanExecutionRef(
            plan_id=plan_id,
            plan_hash=plan_hash,
            source_run_id=str(plan["source_run_id"]),
            items=items,
        )
        result.validate()
        return result

    def append_event(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_event_version: int,
        idempotency_key: str,
        event: AgentSupervisorEvent,
    ) -> AgentControlCommandReceipt:
        """Append only after replaying and reducing the exact event in one lock."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_positive_version(expected_event_version)
        allowed_roles = self._roles_for_event(event.event_type)
        self._validate_actor_class(actor, allowed_roles)
        if (
            event.matter_id != matter_id
            or event.firm_id != actor.firm_id
            or event.actor_id != actor.actor_id
            or event.sequence != expected_event_version + 1
        ):
            raise AgentSupervisorBlocked("event envelope differs from the expected actor or run version")
        command_name = f"APPEND_CASE_AGENT_{event.event_type.value}"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_event_version": expected_event_version,
                "run_id": event.run_id,
                "event_type": event.event_type.value,
                "payload": _payload_json(event.payload),
            }
        )
        with self._transaction(actor.firm_id) as connection:
            return self._append_event_locked(
                connection,
                matter_id=matter_id,
                actor=actor,
                expected_event_version=expected_event_version,
                idempotency_key=idempotency_key,
                command_name=command_name,
                request_hash=request_hash,
                event=event,
                allowed_roles=allowed_roles,
            )

    def review_retained_planning_budget(
        self, *, matter_id: str, actor: Actor, expected_event_version: int,
        idempotency_key: str, event: AgentSupervisorEvent, compiler: object,
        planning_snapshot: object,
    ) -> AgentControlCommandReceipt:
        """Server-only reviewed recovery; database event permission remains a deployment gate."""
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        if (event.event_type is not AgentEventType.PLANNING_BUDGET_REVIEWED
                or event.matter_id != matter_id or event.firm_id != actor.firm_id
                or event.actor_id != actor.actor_id or event.sequence != expected_event_version + 1):
            raise AgentSupervisorBlocked("budget review envelope differs from the expected actor or version")
        command_name = "REVIEW_RETAINED_PLANNING_BUDGET"
        request_hash = _payload_hash({"matter_id": matter_id, "run_id": event.run_id,
            "expected_event_version": expected_event_version, "event_type": event.event_type.value,
            "payload": _payload_json(event.payload)})
        with self._transaction(actor.firm_id) as connection:
            return self._append_event_locked(connection, matter_id=matter_id, actor=actor,
                expected_event_version=expected_event_version, idempotency_key=idempotency_key,
                command_name=command_name, request_hash=request_hash, event=event,
                allowed_roles=self._APPROVER_ROLES, budget_review_compilation=(compiler, planning_snapshot))

    def review_supplementary_material_stage(
        self, *, matter_id: str, actor: Actor, expected_event_version: int,
        idempotency_key: str, event: AgentSupervisorEvent,
    ) -> AgentControlCommandReceipt:
        """Append a reviewed same-run continuation; never grant a new run budget silently."""
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        if (event.event_type is not AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED
                or event.matter_id != matter_id or event.firm_id != actor.firm_id
                or event.actor_id != actor.actor_id or event.sequence != expected_event_version + 1):
            raise AgentSupervisorBlocked("supplementary material review envelope differs")
        request_hash = _payload_hash({"matter_id": matter_id, "run_id": event.run_id,
            "expected_event_version": expected_event_version, "event_type": event.event_type.value,
            "payload": _payload_json(event.payload)})
        with self._transaction(actor.firm_id) as connection:
            return self._append_event_locked(connection, matter_id=matter_id, actor=actor,
                expected_event_version=expected_event_version, idempotency_key=idempotency_key,
                command_name="REVIEW_SUPPLEMENTARY_MATERIAL_STAGE", request_hash=request_hash,
                event=event, allowed_roles=self._APPROVER_ROLES)

    def review_case_analysis_stage(self, *, matter_id: str, actor: Actor,
        expected_event_version: int, idempotency_key: str, event: AgentSupervisorEvent,
    ) -> AgentControlCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        if (event.event_type not in {AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED, AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED,
                                    AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED}
                or event.matter_id != matter_id or event.firm_id != actor.firm_id
                or event.actor_id != actor.actor_id or event.sequence != expected_event_version + 1):
            raise AgentSupervisorBlocked("analysis stage envelope differs")
        request_hash = _payload_hash({"matter_id": matter_id, "run_id": event.run_id,
            "expected_event_version": expected_event_version, "event_type": event.event_type.value,
            "payload": _payload_json(event.payload)})
        with self._transaction(actor.firm_id) as connection:
            return self._append_event_locked(connection, matter_id=matter_id, actor=actor,
                expected_event_version=expected_event_version, idempotency_key=idempotency_key,
                command_name="REVIEW_CASE_ANALYSIS_STAGE", request_hash=request_hash,
                event=event, allowed_roles=self._APPROVER_ROLES)

    def current_case_analysis_stage_inputs(
        self, *, matter_id: str, actor: Actor, run_id: str,
    ) -> tuple[AgentRunState, tuple[tuple[str, str], ...]]:
        """Read one exact verified extraction result and its still-current candidates.

        The Web boundary must never supply candidate IDs, hashes, or a source
        range.  Reading both the replayed run and the governed candidate
        bindings under the same firm transaction gives the caller a stable
        preflight; :meth:`review_case_analysis_stage` repeats the binding
        check while appending, so a concurrent material change still fails
        closed rather than broadening the analysis scope.
        """
        _validate_uuid("matter_id", matter_id)
        _validate_uuid("run_id", run_id)
        with self._read_transaction(actor.firm_id) as connection:
            # This is a preflight read.  ``_replay_locked`` intentionally uses
            # ``FOR UPDATE`` for append paths, which PostgreSQL rejects in the
            # least-privilege read-only transaction used here.  Authorization
            # plus an immutable event replay is sufficient for this snapshot;
            # the later append repeats all bindings under its write lock.
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            state = replay_agent_events(
                self._load_events(
                    connection,
                    actor=actor,
                    matter_id=matter_id,
                    run_id=run_id,
                )
            )
            from .case_agent_analysis_stage import current_analysis_candidate_bindings
            bindings = current_analysis_candidate_bindings(
                connection, firm_id=actor.firm_id, matter_id=matter_id,
            )
        return state, bindings

    def review_retained_material_scope(
        self, *, matter_id: str, actor: Actor, expected_event_version: int,
        idempotency_key: str, event: AgentSupervisorEvent, compiler: object, planning_snapshot: object,
    ) -> AgentControlCommandReceipt:
        """Dedicated append-only writer; SQL migration remains a deployment gate."""
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        if (event.event_type is not AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED
                or event.matter_id != matter_id or event.firm_id != actor.firm_id
                or event.actor_id != actor.actor_id or event.sequence != expected_event_version + 1):
            raise AgentSupervisorBlocked("material scope review envelope differs")
        command_name = "REVIEW_RETAINED_MATERIAL_SCOPE"
        request_hash = _payload_hash({"matter_id": matter_id, "run_id": event.run_id,
            "expected_event_version": expected_event_version, "event_type": event.event_type.value,
            "payload": _payload_json(event.payload)})
        with self._transaction(actor.firm_id) as connection:
            return self._append_event_locked(connection, matter_id=matter_id, actor=actor,
                expected_event_version=expected_event_version, idempotency_key=idempotency_key,
                command_name=command_name, request_hash=request_hash, event=event,
                allowed_roles=self._APPROVER_ROLES, material_scope_compilation=(compiler, planning_snapshot))

    def apply_pending_snapshot_refresh(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
    ) -> AppliedCaseSnapshotRefresh | None:
        """Consume one server-derived 0046 refresh without browser input.

        The request, current ledger snapshot, Agent event, projections,
        checkpoint, audit and APPLIED marker commit together.  A lost response
        can therefore expose either the old PENDING row or the fully applied
        event, never a reason to append the snapshot change twice.
        """

        _validate_uuid("matter_id", matter_id)
        _validate_uuid("run_id", run_id)
        self._require_worker(actor)
        from .case_agent_planning_snapshot_postgres import (
            read_current_case_snapshot_in_transaction,
        )

        with self._transaction(actor.firm_id) as connection:
            # Match the normal append-event lock order: run first, matter
            # second.  The 0042 confirmation owns only the matter lock, so
            # there is no inverse run->matter / matter->run deadlock cycle.
            state = self._replay_locked(connection, actor, matter_id, run_id)
            request = connection.execute(
                """
                SELECT refresh_request_id, source_matter_version,
                       target_matter_version, extraction_batch_id,
                       control_assignment_id
                FROM case_agent_snapshot_refresh_requests
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND request_status = 'PENDING'
                ORDER BY target_matter_version DESC, refresh_request_id
                LIMIT 1
                FOR UPDATE
                """,
                (run_id, actor.firm_id, matter_id),
            ).fetchone()
            if request is None:
                return None

            control_assignment_id = request.get("control_assignment_id")
            if control_assignment_id is None:
                # A normal 0046 refresh is still bound to a verified staged
                # extraction batch.  Repeat its review proof at consumption.
                review = connection.execute(
                    """
                    SELECT case_agent_ledger_extraction_run_review_resolved(
                        %s, %s, %s
                    ) AS review_resolved
                    """,
                    (run_id, actor.firm_id, matter_id),
                ).fetchone()
                if review is None or not bool(review["review_resolved"]):
                    raise CaseLedgerPersistenceBlocked(
                        "snapshot refresh is blocked by open ledger review"
                    )
            else:
                # A control refresh is emitted by a terminal follow-up event.
                # Its append-only current assignment is the exact authority;
                # stale/superseded/recovery-required heads cannot be consumed.
                # The 0065 insert gate keeps the request blocked while any
                # ACTIVE follow-up remains, so a PENDING request must repeat
                # the complementary no-active-follow-up proof here.
                authority = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM case_agent_ledger_exception_control_heads head
                        JOIN case_agent_ledger_exception_control_assignments assignment
                          ON assignment.control_assignment_id =
                                head.current_control_assignment_id
                         AND assignment.firm_id = head.firm_id
                         AND assignment.matter_id = head.matter_id
                         AND assignment.state_after = head.current_state
                         AND assignment.assignment_sequence = head.head_sequence
                        WHERE head.firm_id = %s
                          AND head.matter_id = %s
                          AND head.current_state = 'HEALTHY'
                          AND head.current_control_assignment_id = %s
                          AND assignment.control_run_id = %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM case_agent_ledger_exception_followup_heads followup_head
                              WHERE followup_head.firm_id = head.firm_id
                                AND followup_head.matter_id = head.matter_id
                                AND followup_head.current_state = 'ACTIVE'
                          )
                    ) AS current_authority
                    """,
                    (
                        actor.firm_id,
                        matter_id,
                        control_assignment_id,
                        run_id,
                    ),
                ).fetchone()
                if authority is None or not bool(authority["current_authority"]):
                    raise CaseLedgerPersistenceBlocked(
                        "snapshot refresh control assignment is no longer current"
                    )

            matter = connection.execute(
                """
                SELECT version
                FROM matters
                WHERE matter_id = %s AND firm_id = %s
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            current_matter_version = int(matter["version"])
            target_matter_version = int(request["target_matter_version"])
            if current_matter_version < target_matter_version:
                raise CaseLedgerPersistenceBlocked(
                    "snapshot refresh target is ahead of the authoritative matter"
                )
            _authorize_and_lock_matter(
                connection,
                actor=actor,
                matter_id=matter_id,
                expected_version=current_matter_version,
                allowed_roles=self._WORKER_ROLES,
            )
            snapshot = read_current_case_snapshot_in_transaction(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                actor=actor,
            )
            if (
                snapshot.matter_version != current_matter_version
                or snapshot.matter_version <= state.snapshot.matter_version
            ):
                raise CaseLedgerPersistenceBlocked(
                    "snapshot refresh does not advance the Agent case snapshot"
                )

            refresh_request_id = str(request["refresh_request_id"])
            event = AgentSupervisorEvent(
                event_id=str(
                    uuid5(
                        UUID(refresh_request_id),
                        f"case-snapshot-changed:{snapshot.snapshot_hash}",
                    )
                ),
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=AgentEventType.CASE_SNAPSHOT_CHANGED,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor.actor_id,
                payload=SnapshotChangedPayload(snapshot=snapshot),
            )
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            self._persist_event_details(
                connection,
                event=event,
                previous_state=state,
                next_state=next_state,
            )
            self._update_projection(
                connection, previous_state=state, state=next_state
            )
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            request_hash = _payload_hash(
                {
                    "schema_version": "case-agent-snapshot-refresh-v1",
                    "refresh_request_id": refresh_request_id,
                    "run_id": run_id,
                    "matter_id": matter_id,
                    "target_matter_version": target_matter_version,
                    "applied_snapshot": _payload_json(
                        SnapshotChangedPayload(snapshot=snapshot)
                    ),
                }
            )
            self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="APPLY_CASE_AGENT_SNAPSHOT_REFRESH",
                idempotency_key=f"agent-snapshot-refresh:{refresh_request_id}",
                request_hash=request_hash,
                event=event,
                status=next_state.status,
            )
            applied = connection.execute(
                """
                UPDATE case_agent_snapshot_refresh_requests
                SET request_status = 'APPLIED', applied_event_id = %s,
                    applied_event_sequence = %s, applied_by = %s,
                    applied_at = %s, updated_at = %s
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND request_status = 'PENDING'
                  AND target_matter_version <= %s
                """,
                (
                    event.event_id,
                    event.sequence,
                    actor.actor_id,
                    event.occurred_at,
                    event.occurred_at,
                    run_id,
                    actor.firm_id,
                    matter_id,
                    snapshot.matter_version,
                ),
            )
            if applied.rowcount < 1:
                raise CaseLedgerPersistenceBlocked(
                    "snapshot refresh request changed before event commit"
                )
            return AppliedCaseSnapshotRefresh(
                refresh_request_id=refresh_request_id,
                run_id=run_id,
                matter_id=matter_id,
                target_matter_version=target_matter_version,
                event_version=event.sequence,
                snapshot_hash=snapshot.snapshot_hash,
            )

    def record_lawyer_plan_correction(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_event_version: int,
        idempotency_key: str,
        event: AgentSupervisorEvent,
        decision: "GovernedLawyerPlanningDecision",
    ) -> AgentControlCommandReceipt:
        """Atomically persist a governed signal and its stale/re-plan event."""

        from .case_agent_lawyer_decisions import GovernedLawyerPlanningDecision

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        _require_positive_version(expected_event_version)
        self._require_human(actor, self._APPROVER_ROLES)
        if not isinstance(decision, GovernedLawyerPlanningDecision):
            raise ValueError("governed lawyer planning decision is required")
        decision.validate()
        if (
            event.event_type is not AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED
            or not isinstance(event.payload, LawyerPlanCorrectionPayload)
            or event.matter_id != matter_id
            or event.firm_id != actor.firm_id
            or event.actor_id != actor.actor_id
            or event.sequence != expected_event_version + 1
            or decision.matter_id != matter_id
            or decision.firm_id != actor.firm_id
            or decision.run_id != event.run_id
            or decision.recorded_by != actor.actor_id
            or decision.recorded_event_sequence != event.sequence
            or event.payload.signal_id != decision.signal_id
            or event.payload.task_id != decision.task_id
            or event.payload.decision_hash != decision.decision_hash
            or event.payload.subject_hash != decision.subject_hash
            or event.payload.decision_code != decision.decision_code.value
        ):
            raise AgentSupervisorBlocked(
                "lawyer correction event differs from its governed decision"
            )
        command_name = "RECORD_CASE_AGENT_LAWYER_PLAN_CORRECTION"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "expected_event_version": expected_event_version,
                "run_id": event.run_id,
                "decision_hash": decision.decision_hash,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            replay = self._prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            state = self._replay_locked(connection, actor, matter_id, event.run_id)
            if state.event_version != expected_event_version or state.graph is None:
                raise VersionConflict("Agent run changed before lawyer correction persistence")
            current_task = next(
                (item.spec for item in state.tasks if item.spec.task_id == decision.task_id),
                None,
            )
            if (
                current_task is None
                or state.graph.graph_id != decision.graph_id
                or state.graph.graph_hash != decision.graph_hash
                or current_task.input_hash != decision.task_input_hash
                or tuple(sorted(current_task.input_refs)) != decision.source_ref_ids
            ):
                raise AgentSupervisorBlocked(
                    "lawyer correction does not bind the current compiled task"
                )
            self._authorize_current_snapshot(
                connection,
                actor=actor,
                matter_id=matter_id,
                state=state,
                allowed_roles=self._APPROVER_ROLES,
            )
            prior = connection.execute(
                """
                SELECT signal_id, signal_version
                FROM case_agent_lawyer_decision_signals
                WHERE firm_id = %s AND matter_id = %s AND subject_hash = %s
                  AND is_current
                FOR UPDATE
                """,
                (actor.firm_id, matter_id, decision.subject_hash),
            ).fetchone()
            expected_signal_version = 1 if prior is None else int(prior["signal_version"]) + 1
            expected_supersedes = None if prior is None else str(prior["signal_id"])
            if (
                decision.signal_version != expected_signal_version
                or decision.supersedes_signal_id != expected_supersedes
            ):
                raise VersionConflict("lawyer correction subject changed before persistence")
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            self._persist_event_details(
                connection, event=event, previous_state=state, next_state=next_state
            )
            if prior is not None:
                connection.execute(
                    """
                    UPDATE case_agent_lawyer_decision_signals
                    SET is_current = false, superseded_at = %s
                    WHERE signal_id = %s AND firm_id = %s AND matter_id = %s
                      AND is_current
                    """,
                    (event.occurred_at, prior["signal_id"], actor.firm_id, matter_id),
                )
            inserted = connection.execute(
                """
                INSERT INTO case_agent_lawyer_decision_signals (
                    signal_id, run_id, graph_id, task_id, firm_id, matter_id,
                    signal_version, decision_code, category, signal_status,
                    summary, source_ref_ids, task_input_hash, graph_hash,
                    subject_hash, decision_hash, recorded_event_sequence,
                    recorded_by, decided_at, supersedes_signal_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    decision.signal_id,
                    decision.run_id,
                    decision.graph_id,
                    decision.task_id,
                    decision.firm_id,
                    decision.matter_id,
                    decision.signal_version,
                    decision.decision_code.value,
                    decision.category.value,
                    decision.status.value,
                    decision.summary,
                    Jsonb(list(decision.source_ref_ids)),
                    decision.task_input_hash,
                    decision.graph_hash,
                    decision.subject_hash,
                    decision.decision_hash,
                    decision.recorded_event_sequence,
                    decision.recorded_by,
                    decision.decided_at,
                    decision.supersedes_signal_id,
                ),
            )
            if inserted.rowcount != 1:
                raise CaseLedgerPersistenceBlocked("lawyer correction was not persisted")
            self._update_projection(connection, previous_state=state, state=next_state)
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            return self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=next_state.status,
            )

    def start_verification(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        execution_actor_id: str,
        verifier_id: str,
        verifier_version: str,
        policy_hash: str,
    ):
        """Persist STARTED under a distinct verifier principal and recover it.

        A process crash after this transaction leaves one immutable attempt.
        Re-entry returns that same attempt and does not manufacture another
        VERIFICATION_STARTED event.
        """

        from .case_agent_worker import DurableVerificationClaim

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _validate_uuid("execution_actor_id", execution_actor_id)
        _validate_sha256("policy_hash", policy_hash)
        if actor.actor_id == execution_actor_id:
            raise PermissionError(
                "the execution worker cannot serve as its own independent verifier"
            )
        if not _safe_runtime_identifier(verifier_id) or not _safe_semver(verifier_version):
            raise AgentSupervisorBlocked("verifier identity is invalid")
        with self._actor_transaction(actor) as connection:
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if (
                state.status is not AgentRunStatus.VERIFYING
                or state.graph is None
                or state.stale
                or state.cancelled
            ):
                raise AgentSupervisorBlocked("Agent run is not ready for verification")
            self._authorize_independent_execution_actor(
                connection,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                execution_actor_id=execution_actor_id,
            )
            existing = connection.execute(
                """
                SELECT verification_attempt_id, graph_hash, snapshot_hash,
                       execution_actor_id, verifier_actor_id, verifier_id,
                       verifier_version, policy_hash
                FROM case_agent_verification_attempts
                WHERE run_id = %s AND graph_hash = %s AND firm_id = %s
                  AND matter_id = %s
                """,
                (run_id, state.graph.graph_hash, actor.firm_id, matter_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["execution_actor_id"]) != execution_actor_id
                    or str(existing["verifier_actor_id"]) != actor.actor_id
                    or existing["verifier_id"] != verifier_id
                    or existing["verifier_version"] != verifier_version
                    or existing["policy_hash"] != policy_hash
                    or existing["snapshot_hash"] != state.snapshot.snapshot_hash
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "current verification attempt has a different principal or policy binding"
                    )
                return DurableVerificationClaim(
                    verification_attempt_id=str(existing["verification_attempt_id"]),
                    run_id=run_id,
                    event_version=state.event_version,
                    graph_hash=state.graph.graph_hash,
                    snapshot_hash=state.snapshot.snapshot_hash,
                    execution_actor_id=execution_actor_id,
                    verifier_actor_id=actor.actor_id,
                    state=state,
                )
            if state.event_version != expected_event_version:
                raise VersionConflict(
                    f"expected Agent event version {expected_event_version}, current version is {state.event_version}"
                )
            self._authorize_verifier_current_snapshot(
                connection,
                actor=actor,
                matter_id=matter_id,
                state=state,
            )
            event = AgentSupervisorEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=AgentEventType.VERIFICATION_STARTED,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor.actor_id,
            )
            next_state = reduce_agent_event(state, event)
            attempt_id = str(
                uuid5(UUID(run_id), f"verification:{state.graph.graph_hash}")
            )
            self._insert_event(connection, event)
            self._persist_event_details(
                connection, event=event, previous_state=state, next_state=next_state
            )
            connection.execute(
                """
                INSERT INTO case_agent_verification_attempts (
                    verification_attempt_id, run_id, graph_id, firm_id,
                    matter_id, execution_actor_id, verifier_actor_id,
                    verifier_id, verifier_version, policy_hash, graph_hash,
                    snapshot_hash, started_event_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    attempt_id, run_id, state.graph.graph_id, actor.firm_id,
                    matter_id, execution_actor_id, actor.actor_id, verifier_id,
                    verifier_version, policy_hash, state.graph.graph_hash,
                    state.snapshot.snapshot_hash, event.sequence,
                ),
            )
            self._update_projection(
                connection, previous_state=state, state=next_state
            )
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            request_hash = _payload_hash(
                {
                    "run_id": run_id,
                    "graph_hash": state.graph.graph_hash,
                    "execution_actor_id": execution_actor_id,
                    "verifier_id": verifier_id,
                    "verifier_version": verifier_version,
                    "policy_hash": policy_hash,
                }
            )
            self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="START_CASE_AGENT_VERIFICATION",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=next_state.status,
            )
            return DurableVerificationClaim(
                verification_attempt_id=attempt_id,
                run_id=run_id,
                event_version=next_state.event_version,
                graph_hash=state.graph.graph_hash,
                snapshot_hash=state.snapshot.snapshot_hash,
                execution_actor_id=execution_actor_id,
                verifier_actor_id=actor.actor_id,
                state=next_state,
            )

    def record_verification_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: object,
        idempotency_key: str,
        receipt: RunVerificationReceipt,
    ) -> int:
        """Atomically append the full receipt and terminal supervisor event."""

        from .case_agent_worker import DurableVerificationClaim

        if not isinstance(claim, DurableVerificationClaim):
            raise TypeError("durable verification claim is required")
        claim.validate()
        receipt.validate()
        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        if (
            actor.actor_id != claim.verifier_actor_id
            or actor.actor_id != receipt.verifier_actor_id
            or claim.execution_actor_id != receipt.execution_actor_id
            or actor.actor_id == receipt.execution_actor_id
            or actor.firm_id != receipt.firm_id
            or matter_id != receipt.matter_id
            or claim.run_id != receipt.run_id
            or claim.verification_attempt_id != receipt.verification_attempt_id
            or claim.graph_hash != receipt.graph_hash
            or claim.snapshot_hash != receipt.snapshot_hash
        ):
            raise PermissionError("verification receipt differs from its principal or claim")
        with self._actor_transaction(actor) as connection:
            state = self._replay_locked(
                connection, actor, matter_id, claim.run_id
            )
            existing = connection.execute(
                """
                SELECT verification_hash, terminal_event_sequence
                FROM case_agent_verification_receipts
                WHERE verification_attempt_id = %s AND firm_id = %s
                  AND matter_id = %s
                """,
                (claim.verification_attempt_id, actor.firm_id, matter_id),
            ).fetchone()
            if existing is not None:
                if existing["verification_hash"] != receipt.verification_hash:
                    raise IdempotencyConflict(
                        "verification attempt already has a different terminal receipt"
                    )
                return int(existing["terminal_event_sequence"])
            if (
                state.event_version != claim.event_version
                or state.status is not AgentRunStatus.VERIFYING
                or state.graph is None
                or state.graph.graph_hash != claim.graph_hash
                or state.snapshot.snapshot_hash != claim.snapshot_hash
            ):
                raise VersionConflict(
                    "Agent run changed before verification outcome was stored"
                )
            try:
                receipt.validate_against_state(state)
            except ValueError as error:
                raise CaseLedgerPersistenceBlocked(
                    "verification receipt differs from the current Agent run"
                ) from error
            attempt = connection.execute(
                """
                SELECT execution_actor_id, verifier_actor_id, verifier_id,
                       verifier_version, policy_hash, graph_hash, snapshot_hash,
                       started_event.occurred_at AS started_at
                FROM case_agent_verification_attempts attempt
                JOIN case_agent_events started_event
                  ON started_event.run_id = attempt.run_id
                 AND started_event.event_sequence = attempt.started_event_sequence
                 AND started_event.firm_id = attempt.firm_id
                 AND started_event.matter_id = attempt.matter_id
                WHERE attempt.verification_attempt_id = %s
                  AND attempt.run_id = %s
                  AND attempt.firm_id = %s AND attempt.matter_id = %s
                """,
                (
                    claim.verification_attempt_id, claim.run_id,
                    actor.firm_id, matter_id,
                ),
            ).fetchone()
            if attempt is None or (
                str(attempt["execution_actor_id"]) != receipt.execution_actor_id
                or str(attempt["verifier_actor_id"]) != receipt.verifier_actor_id
                or attempt["verifier_id"] != receipt.verifier_id
                or attempt["verifier_version"] != receipt.verifier_version
                or attempt["policy_hash"] != receipt.policy_hash
                or attempt["graph_hash"] != receipt.graph_hash
                or attempt["snapshot_hash"] != receipt.snapshot_hash
                or receipt.verified_at < attempt["started_at"]
            ):
                raise CaseLedgerPersistenceBlocked(
                    "verification receipt differs from its durable attempt"
                )
            self._authorize_verifier_current_snapshot(
                connection,
                actor=actor,
                matter_id=matter_id,
                state=state,
            )
            event = AgentSupervisorEvent(
                event_id=str(uuid4()),
                run_id=claim.run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=(
                    AgentEventType.VERIFICATION_PASSED
                    if receipt.outcome is VerificationOutcome.PASSED
                    else AgentEventType.VERIFICATION_FAILED
                ),
                occurred_at=receipt.verified_at,
                actor_id=actor.actor_id,
                payload=VerificationPayload(
                    verification_hash=receipt.verification_hash,
                    error_code=receipt.error_code,
                ),
            )
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            connection.execute(
                """
                INSERT INTO case_agent_verification_receipts (
                    verification_receipt_id, verification_attempt_id, run_id,
                    firm_id, matter_id, outcome, verifier_id, verifier_version,
                    policy_hash, graph_hash, snapshot_hash, task_receipts_hash,
                    artifact_manifest_hash, artifact_lineage, error_code,
                    verification_hash, verifier_actor_id, execution_actor_id,
                    verified_at, terminal_event_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid5(UUID(claim.verification_attempt_id), receipt.verification_hash)),
                    claim.verification_attempt_id, claim.run_id, actor.firm_id,
                    matter_id, receipt.outcome.value, receipt.verifier_id,
                    receipt.verifier_version, receipt.policy_hash,
                    receipt.graph_hash, receipt.snapshot_hash,
                    receipt.task_receipts_hash, receipt.artifact_manifest_hash,
                    Jsonb([asdict(item) for item in receipt.artifact_lineage]),
                    receipt.error_code, receipt.verification_hash,
                    receipt.verifier_actor_id, receipt.execution_actor_id,
                    receipt.verified_at, event.sequence,
                ),
            )
            self._persist_event_details(
                connection, event=event, previous_state=state, next_state=next_state
            )
            self._update_projection(
                connection, previous_state=state, state=next_state
            )
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            request_hash = _payload_hash(
                {
                    "verification_attempt_id": claim.verification_attempt_id,
                    "verification_hash": receipt.verification_hash,
                }
            )
            self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="RECORD_CASE_AGENT_VERIFICATION_OUTCOME",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=next_state.status,
            )
            return event.sequence

    @staticmethod
    def _authorize_independent_execution_actor(
        connection: Any,
        *,
        firm_id: str,
        matter_id: str,
        execution_actor_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM matter_actor_roles role
            JOIN users principal
              ON principal.user_id = role.user_id
             AND principal.firm_id = role.firm_id
            WHERE role.firm_id = %s AND role.matter_id = %s
              AND role.user_id = %s AND role.role = 'SYSTEM_WORKER'
              AND role.revoked_at IS NULL AND principal.status = 'ACTIVE'
            """,
            (firm_id, matter_id, execution_actor_id),
        ).fetchone()
        if row is None:
            raise PermissionError(
                "execution actor is not an active SYSTEM_WORKER on this matter"
            )

    def replay_run(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> AgentRunState:
        """Rebuild from the immutable log, never from mutable task heads."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        _validate_uuid("run_id", run_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            events = self._load_events(
                connection, actor=actor, matter_id=matter_id, run_id=run_id
            )
        return replay_agent_events(events)

    def lawyer_plan_decision_head(
        self, *, matter_id: str, actor: Actor, subject_hash: str
    ) -> tuple[int, str] | None:
        """Read the current governed correction head before building its successor."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_human(actor, self._APPROVER_ROLES)
        _validate_sha256("subject_hash", subject_hash)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._APPROVER_ROLES,
            )
            row = connection.execute(
                """
                SELECT signal_version, signal_id
                FROM case_agent_lawyer_decision_signals
                WHERE firm_id = %s AND matter_id = %s AND subject_hash = %s
                  AND is_current
                """,
                (actor.firm_id, matter_id, subject_hash),
            ).fetchone()
        if row is None:
            return None
        return int(row["signal_version"]), str(row["signal_id"])

    def read_projection(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> PersistentAgentRunProjection:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        _validate_uuid("run_id", run_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            events = self._load_events(
                connection, actor=actor, matter_id=matter_id, run_id=run_id
            )
            row = connection.execute(
                """
                SELECT current_event_version, projection_hash
                FROM case_agent_runs
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
            checkpoint = connection.execute(
                """
                SELECT event_version, projection_hash
                FROM case_agent_checkpoints
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                ORDER BY event_version DESC LIMIT 1
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
        state = replay_agent_events(events)
        state_hash = _payload_hash(_state_json(state))
        if row is None or int(row["current_event_version"]) != state.event_version:
            raise CaseLedgerPersistenceBlocked("Agent run projection version differs from its event log")
        if row["projection_hash"] != state_hash:
            raise CaseLedgerPersistenceBlocked("Agent run projection hash differs from event replay")
        checkpoint_verified = bool(
            checkpoint
            and int(checkpoint["event_version"]) == state.event_version
            and checkpoint["projection_hash"] == state_hash
        )
        return PersistentAgentRunProjection(
            state=state,
            next_commands=decide_next_commands(state),
            checkpoint_verified=checkpoint_verified,
        )

    def material_scope_review(
        self, *, matter_id: str, actor: Actor, run_id: str, expected_event_version: int,
    ) -> PlanningMaterialScopeReviewPayload | None:
        """Read the exact append-only review, never mint approval from goal text."""
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        _validate_uuid("run_id", run_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES)
            events = self._load_events(connection, actor=actor, matter_id=matter_id, run_id=run_id)
            state = replay_agent_events(events)
            if state.event_version != expected_event_version:
                raise VersionConflict("material scope review run changed")
            reviews = [event.payload for event in events
                if event.event_type is AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED]
            if not reviews:
                return None
            if len(reviews) != 1 or not isinstance(reviews[0], PlanningMaterialScopeReviewPayload):
                raise CaseLedgerPersistenceBlocked("material scope review history differs")
            review = reviews[0]
            if (state.stale or state.cancelled or state.snapshot != review.snapshot
                    or state.goal.goal_hash != review.effective_goal_hash
                    or state.goal.material_read_refs != review.material_read_refs
                    or state.budget.max_output_bytes != review.approved_output_bytes):
                raise CaseLedgerPersistenceBlocked("material scope review is no longer current")
            return review

    def current_run_record(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentAgentRunRecord | None:
        """Read the newest non-cancelled run for one authorized matter.

        A cancelled run remains available through ``run_record`` for audit,
        but it must not keep occupying the workbench's single *current* task.
        Otherwise a lawyer who safely ended an indeterminate external request
        has no route to create a distinct replacement goal.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            row = connection.execute(
                """
                SELECT run_id, created_at, updated_at
                FROM case_agent_runs
                WHERE matter_id = %s AND firm_id = %s
                  AND is_cancelled = false
                ORDER BY updated_at DESC, run_id DESC
                LIMIT 1
                """,
                (matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            return None
        return self.run_record(
            matter_id=matter_id,
            actor=actor,
            run_id=str(row["run_id"]),
            known_times=(row["created_at"], row["updated_at"]),
        )

    def run_record(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        known_times: tuple[datetime, datetime] | None = None,
    ) -> PersistentAgentRunRecord:
        """Read one replay-verified run and the server-owned display clock."""

        projection = self.read_projection(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        times = known_times
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            row = connection.execute(
                """
                SELECT run.created_at,
                       run.updated_at,
                       run.snapshot_matter_version,
                       run.is_stale,
                       matter.version AS current_matter_version,
                       CASE
                           WHEN EXISTS (
                               SELECT 1
                               FROM case_agent_work_plan_promotions promotion
                               JOIN case_work_plans plan
                                 ON plan.plan_id = promotion.plan_id
                                AND plan.firm_id = promotion.firm_id
                                AND plan.matter_id = promotion.matter_id
                               JOIN case_work_plan_heads head
                                 ON head.firm_id = plan.firm_id
                                AND head.matter_id = plan.matter_id
                               WHERE promotion.run_id = run.run_id
                                 AND promotion.firm_id = run.firm_id
                                 AND promotion.matter_id = run.matter_id
                                 AND promotion.snapshot_matter_version =
                                     run.snapshot_matter_version
                                 AND plan.status = 'CANDIDATE'
                                 AND plan.plan_version = head.latest_plan_version
                                 AND plan.planned_matter_version =
                                     run.snapshot_matter_version
                                 AND matter.version =
                                     plan.planned_matter_version + 1
                           ) THEN 'PLAN_CANDIDATE_REGISTERED'
                           WHEN EXISTS (
                               SELECT 1
                               FROM case_agent_work_plan_promotions promotion
                               JOIN case_work_plans plan
                                 ON plan.plan_id = promotion.plan_id
                                AND plan.firm_id = promotion.firm_id
                                AND plan.matter_id = promotion.matter_id
                               JOIN case_work_plan_heads head
                                 ON head.firm_id = plan.firm_id
                                AND head.matter_id = plan.matter_id
                               WHERE promotion.run_id = run.run_id
                                 AND promotion.firm_id = run.firm_id
                                 AND promotion.matter_id = run.matter_id
                                 AND promotion.snapshot_matter_version =
                                     run.snapshot_matter_version
                                 AND plan.status = 'ACTIVE'
                                 AND plan.plan_id = head.current_plan_id
                                 AND plan.planned_matter_version =
                                     run.snapshot_matter_version
                                 AND matter.version =
                                     plan.activated_matter_version
                           ) THEN 'PLAN_ACTIVE'
                           ELSE NULL
                       END AS current_plan_lineage
                FROM case_agent_runs run
                JOIN matters matter
                  ON matter.matter_id = run.matter_id
                 AND matter.firm_id = run.firm_id
                WHERE run.run_id = %s
                  AND run.matter_id = %s
                  AND run.firm_id = %s
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if times is None:
            times = (row["created_at"], row["updated_at"])
        input_snapshot_status = _classify_run_input_snapshot_status(
            current_matter_version=int(row["current_matter_version"]),
            snapshot_matter_version=int(row["snapshot_matter_version"]),
            run_is_stale=bool(row["is_stale"]),
            current_plan_lineage=(
                None
                if row["current_plan_lineage"] is None
                else str(row["current_plan_lineage"])
            ),
        )
        created_at, updated_at = times
        if (
            not isinstance(created_at, datetime)
            or not isinstance(updated_at, datetime)
            or created_at.tzinfo is None
            or updated_at.tzinfo is None
            or updated_at < created_at
        ):
            raise CaseLedgerPersistenceBlocked("Agent run clock is invalid")
        return PersistentAgentRunRecord(
            projection=projection,
            created_at=created_at,
            updated_at=updated_at,
            input_snapshot_status=input_snapshot_status,
        )

    def claim_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        planning_hash: str,
        planning_kind: str,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int = 120,
    ) -> ClaimedAgentPlanningAttempt:
        """Lease one reducer-authorized plan/replan before any provider call."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _require_positive_version(expected_event_version)
        _validate_sha256("planning_hash", planning_hash)
        if planning_kind not in {"PLAN", "REPLAN"}:
            raise ValueError("planning_kind must be PLAN or REPLAN")
        if not lease_owner.strip() or len(lease_owner.strip()) > 200:
            raise ValueError("planning lease owner is invalid")
        if not 30 <= lease_seconds <= 900:
            raise ValueError("planning lease must be between 30 and 900 seconds")
        command_name = "CLAIM_CASE_AGENT_PLANNING"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "run_id": run_id,
                "expected_event_version": expected_event_version,
                "planning_hash": planning_hash,
                "planning_kind": planning_kind,
                "lease_owner": lease_owner.strip(),
                "lease_seconds": lease_seconds,
            }
        )
        observed_at = datetime.now(timezone.utc)
        lease_expires_at = observed_at + timedelta(seconds=lease_seconds)
        lease_token = str(uuid4())
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            prior = self._prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                row = connection.execute(
                    """
                    SELECT attempt.planning_attempt_id, attempt.external_request_id,
                           attempt.matter_version, attempt.planning_kind,
                           attempt.planning_hash, attempt.lease_owner,
                           attempt.lease_token,
                           attempt.lease_expires_at, attempt.attempt_version,
                           outcome.request_hash,
                           outcome.planning_external_event_id IS NOT NULL
                               AS has_external_outcome,
                           local.planning_local_event_id IS NOT NULL
                               AS has_local_outcome,
                           COALESCE(
                               outcome.structured_proposal,
                               local.structured_proposal
                           ) AS structured_proposal
                    FROM case_agent_planning_attempts attempt
                    LEFT JOIN LATERAL (
                        SELECT external.planning_external_event_id,
                               external.request_hash,
                               external.structured_proposal
                        FROM case_agent_planning_external_events external
                        WHERE external.planning_attempt_id = attempt.planning_attempt_id
                          AND external.ledger_version IN (2, 3)
                          AND external.status = 'SUCCEEDED'
                        ORDER BY external.ledger_version DESC
                        LIMIT 1
                    ) outcome ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT local_event.planning_local_event_id,
                               local_event.structured_proposal
                        FROM case_agent_planning_local_events local_event
                        WHERE local_event.planning_attempt_id = attempt.planning_attempt_id
                        LIMIT 1
                    ) local ON TRUE
                    WHERE attempt.run_id = %s AND attempt.matter_id = %s
                      AND attempt.firm_id = %s
                      AND attempt.started_event_sequence = %s
                    """,
                    (run_id, matter_id, actor.firm_id, prior.event_version),
                ).fetchone()
                if row is None:
                    raise CaseLedgerPersistenceBlocked("idempotent planning claim lost its attempt")
                if bool(row["has_external_outcome"]) and bool(row["has_local_outcome"]):
                    raise CaseLedgerPersistenceBlocked(
                        "planning attempt has both local and external outcomes"
                    )
                return _planning_claim_from_row(row, run_id=run_id, event_version=prior.event_version)
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if state.event_version != expected_event_version:
                raise VersionConflict(
                    f"expected Agent event version {expected_event_version}, current version is {state.event_version}"
                )
            required_kind = (
                SupervisorCommandKind.REQUEST_REPLAN
                if planning_kind == "REPLAN"
                else SupervisorCommandKind.REQUEST_PLAN
            )
            if not any(command.kind is required_kind for command in decide_next_commands(state)):
                raise CaseLedgerPersistenceBlocked("the reducer did not request this planning operation")
            self._authorize_current_snapshot(
                connection,
                actor=actor,
                matter_id=matter_id,
                state=state,
                allowed_roles=self._WORKER_ROLES,
            )
            event = AgentSupervisorEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=AgentEventType.PLANNING_STARTED,
                occurred_at=observed_at,
                actor_id=actor.actor_id,
                payload=None,
            )
            next_state = reduce_agent_event(state, event)
            planning_attempt_id = str(uuid4())
            external_request_id = str(uuid4())
            self._insert_event(connection, event)
            self._update_projection(connection, previous_state=state, state=next_state)
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            connection.execute(
                """
                INSERT INTO case_agent_planning_attempts (
                    planning_attempt_id, run_id, firm_id, matter_id, planning_kind,
                    status, planning_hash, matter_version, external_request_id,
                    lease_owner, lease_token, lease_expires_at, started_event_sequence
                ) VALUES (%s,%s,%s,%s,%s,'CLAIMED',%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    planning_attempt_id, run_id, actor.firm_id, matter_id,
                    planning_kind, planning_hash, state.snapshot.matter_version,
                    external_request_id, lease_owner.strip(), lease_token,
                    lease_expires_at,
                    event.sequence,
                ),
            )
            receipt = self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=next_state.status,
            )
            return ClaimedAgentPlanningAttempt(
                run_id=run_id,
                planning_attempt_id=planning_attempt_id,
                external_request_id=external_request_id,
                event_version=receipt.event_version,
                matter_version=state.snapshot.matter_version,
                planning_kind=planning_kind,
                planning_hash=planning_hash,
                lease_owner=lease_owner.strip(),
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                attempt_version=1,
            )

    def heartbeat_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        planning_attempt_id: str,
        expected_attempt_version: int,
        lease_owner: str,
        extend_seconds: int = 120,
    ) -> tuple[int, datetime]:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("planning_attempt_id", planning_attempt_id)
        _require_positive_version(expected_attempt_version)
        if not lease_owner.strip() or not 30 <= extend_seconds <= 900:
            raise ValueError("planning heartbeat parameters are invalid")
        expires = datetime.now(timezone.utc) + timedelta(seconds=extend_seconds)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, lease_expires_at = %s,
                    last_heartbeat_at = now(), updated_at = now()
                WHERE planning_attempt_id = %s AND matter_id = %s AND firm_id = %s
                  AND attempt_version = %s AND lease_owner = %s
                  AND status IN ('CLAIMED','SUBMISSION_STARTED','RECONCILING')
                  AND lease_expires_at > now()
                """,
                (
                    expires, planning_attempt_id, matter_id, actor.firm_id,
                    expected_attempt_version, lease_owner.strip(),
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("Agent planning lease changed, expired, or belongs to another worker")
        return expected_attempt_version + 1, expires

    def begin_planning_submission(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        external_request_id: str,
        lease_token: str,
        matter_version: int,
        provider_id: str,
        service_id: str,
        input_hash: str,
        request_hash: str,
    ) -> int:
        """Append the immutable provider boundary before sending a byte.

        This external ledger deliberately has its own two-step version, so a
        lease heartbeat cannot race the DeepSeek guard's outcome version.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        for label, value in (
            ("run_id", run_id),
            ("planning_attempt_id", planning_attempt_id),
            ("external_request_id", external_request_id),
            ("lease_token", lease_token),
        ):
            _validate_uuid(label, value)
        _require_positive_version(matter_version)
        _validate_sha256("input_hash", input_hash)
        _validate_sha256("request_hash", request_hash)
        for label, value in (("provider_id", provider_id), ("service_id", service_id)):
            if not value.strip() or len(value.strip()) > 200:
                raise ValueError(f"{label} is required and bounded")
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            row = connection.execute(
                """
                SELECT status, matter_version, planning_hash, external_request_id,
                       lease_token, lease_expires_at
                FROM case_agent_planning_attempts
                WHERE planning_attempt_id = %s AND run_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None:
                raise KeyError(planning_attempt_id)
            if (
                row["status"] != "CLAIMED"
                or int(row["matter_version"]) != matter_version
                or row["planning_hash"] != input_hash
                or str(row["external_request_id"]) != external_request_id
                or str(row["lease_token"]) != lease_token
                or row["lease_expires_at"] <= datetime.now(timezone.utc)
            ):
                raise CaseLedgerPersistenceBlocked(
                    "planning submission is not bound to one current live claim"
                )
            existing = connection.execute(
                """
                SELECT status, request_hash
                FROM case_agent_planning_external_events
                WHERE planning_attempt_id = %s AND ledger_version = 1
                """,
                (planning_attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["status"] != "SUBMISSION_STARTED"
                    or existing["request_hash"] != request_hash
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "planning boundary differs from its durable request"
                    )
                return 1
            connection.execute(
                """
                INSERT INTO case_agent_planning_external_events (
                    planning_external_event_id, planning_attempt_id, run_id,
                    firm_id, matter_id, ledger_version, status,
                    external_request_id, provider_id, service_id, input_hash,
                    request_hash, recorded_by
                ) VALUES (%s,%s,%s,%s,%s,1,'SUBMISSION_STARTED',%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid4()), planning_attempt_id, run_id, actor.firm_id,
                    matter_id, external_request_id, provider_id.strip(),
                    service_id.strip(), input_hash, request_hash, actor.actor_id,
                ),
            )
        return 1

    def record_planning_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        external_request_id: str,
        lease_token: str,
        matter_version: int,
        expected_external_ledger_version: int,
        request_hash: str,
        status: str,
        output_hash: str | None,
        error_code: str | None,
        structured_proposal: dict[str, Any] | None,
    ) -> AgentPlanningOutcome:
        """Atomically persist one provider outcome and supervisor visibility."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        for label, value in (
            ("run_id", run_id),
            ("planning_attempt_id", planning_attempt_id),
            ("external_request_id", external_request_id),
            ("lease_token", lease_token),
        ):
            _validate_uuid(label, value)
        _require_positive_version(matter_version)
        if expected_external_ledger_version != 1:
            raise VersionConflict("planning external ledger version changed")
        _validate_sha256("request_hash", request_hash)
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION"}:
            raise ValueError("planning outcome status is invalid")
        if status == "SUCCEEDED":
            if output_hash is None or not isinstance(structured_proposal, dict) or error_code is not None:
                raise ValueError("successful planning outcome is incomplete")
            _validate_sha256("output_hash", output_hash)
        else:
            if structured_proposal is not None or not error_code:
                raise ValueError("failed or unknown planning outcome is invalid")
            if status == "UNKNOWN_SUBMISSION" and output_hash is not None:
                raise ValueError("unknown planning outcome cannot assert an output hash")
            if output_hash is not None:
                _validate_sha256("output_hash", output_hash)
        safe_error_code = (
            None if error_code is None else _planner_error_code(error_code)
        )
        observed_at = datetime.now(timezone.utc)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            attempt = connection.execute(
                """
                SELECT status, matter_version, planning_hash, external_request_id,
                       lease_token, lease_expires_at, attempt_version
                FROM case_agent_planning_attempts
                WHERE planning_attempt_id = %s AND run_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(planning_attempt_id)
            started = connection.execute(
                """
                SELECT provider_id, service_id, input_hash, request_hash
                FROM case_agent_planning_external_events
                WHERE planning_attempt_id = %s AND ledger_version = 1
                """,
                (planning_attempt_id,),
            ).fetchone()
            if started is None or started["request_hash"] != request_hash:
                raise CaseLedgerPersistenceBlocked("planning outcome lacks its exact started boundary")
            existing = connection.execute(
                """
                SELECT status, output_hash, error_code, structured_proposal
                FROM case_agent_planning_external_events
                WHERE planning_attempt_id = %s AND ledger_version = 2
                """,
                (planning_attempt_id,),
            ).fetchone()
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if existing is not None:
                if (
                    existing["status"] != status
                    or existing["output_hash"] != output_hash
                    or existing["error_code"] != safe_error_code
                    or existing["structured_proposal"] != structured_proposal
                ):
                    raise CaseLedgerPersistenceBlocked("planning outcome already differs")
                return AgentPlanningOutcome(
                    run_id, planning_attempt_id, status, state.event_version,
                    existing["structured_proposal"], existing["output_hash"],
                    existing["error_code"],
                )
            if (
                attempt["status"] != "CLAIMED"
                or int(attempt["matter_version"]) != matter_version
                or str(attempt["external_request_id"]) != external_request_id
                or str(attempt["lease_token"]) != lease_token
                or attempt["lease_expires_at"] <= observed_at
            ):
                raise CaseLedgerPersistenceBlocked("planning attempt differs from its outcome")
            next_event: AgentSupervisorEvent | None = None
            if status == "FAILED":
                next_event = AgentSupervisorEvent(
                    event_id=str(uuid4()), run_id=run_id, firm_id=actor.firm_id,
                    matter_id=matter_id, sequence=state.event_version + 1,
                    event_type=AgentEventType.PLANNING_FAILED,
                    occurred_at=observed_at, actor_id=actor.actor_id,
                    payload=PlanningFailurePayload(
                        error_code=safe_error_code or "PLANNER_FAILED"
                    ),
                )
            elif status == "UNKNOWN_SUBMISSION":
                next_event = AgentSupervisorEvent(
                    event_id=str(uuid4()), run_id=run_id, firm_id=actor.firm_id,
                    matter_id=matter_id, sequence=state.event_version + 1,
                    event_type=AgentEventType.PLANNING_RESULT_UNKNOWN,
                    occurred_at=observed_at, actor_id=actor.actor_id, payload=None,
                )
            connection.execute(
                """
                INSERT INTO case_agent_planning_external_events (
                    planning_external_event_id, planning_attempt_id, run_id,
                    firm_id, matter_id, ledger_version, status,
                    external_request_id, provider_id, service_id, input_hash,
                    request_hash, output_hash, error_code, structured_proposal,
                    recorded_by
                ) VALUES (%s,%s,%s,%s,%s,2,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid4()), planning_attempt_id, run_id, actor.firm_id,
                    matter_id, status, external_request_id, started["provider_id"],
                    started["service_id"], started["input_hash"], request_hash,
                    output_hash, safe_error_code, Jsonb(structured_proposal)
                    if structured_proposal is not None else None, actor.actor_id,
                ),
            )
            finished_sequence: int | None = None
            if next_event is not None:
                next_state = reduce_agent_event(state, next_event)
                self._insert_event(connection, next_event)
                self._update_projection(connection, previous_state=state, state=next_state)
                projection = _state_json(next_state)
                self._insert_checkpoint(
                    connection, next_state, projection, _payload_hash(projection)
                )
                state = next_state
                finished_sequence = next_event.sequence
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, status = %s,
                    finished_event_sequence = %s, updated_at = now()
                WHERE planning_attempt_id = %s AND attempt_version = %s
                  AND firm_id = %s AND matter_id = %s
                """,
                (
                    status, finished_sequence, planning_attempt_id,
                    int(attempt["attempt_version"]), actor.firm_id, matter_id,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("planning attempt changed before outcome was stored")
            return AgentPlanningOutcome(
                run_id, planning_attempt_id, status, state.event_version,
                structured_proposal, output_hash, safe_error_code,
            )

    def record_local_planning_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        lease_token: str,
        matter_version: int,
        planner_id: str,
        proposal: CasePlanProposal,
    ) -> AgentPlanningOutcome:
        """Persist one server-governed plan without faking a provider exchange.

        A local plan is allowed only for the narrow first-release defendant
        response route.  Its immutable evidence lives in a distinct ledger so
        a later audit can prove that no planning request crossed a provider
        boundary.  The normal compiler still validates the proposal before a
        graph is accepted.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        for label, value in (
            ("run_id", run_id),
            ("planning_attempt_id", planning_attempt_id),
            ("lease_token", lease_token),
        ):
            _validate_uuid(label, value)
        _require_positive_version(matter_version)
        if planner_id != CONTROLLED_DEFENCE_PLANNER_ID:
            raise ValueError("local planner identifier is outside the governed catalogue")
        if not isinstance(proposal, CasePlanProposal):
            raise ValueError("local planning proposal is invalid")
        payload = case_plan_proposal_payload(proposal)
        output_hash = _payload_hash(payload)
        observed_at = datetime.now(timezone.utc)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            attempt = connection.execute(
                """
                SELECT status, matter_version, planning_hash, lease_token,
                       lease_expires_at, attempt_version
                FROM case_agent_planning_attempts
                WHERE planning_attempt_id = %s AND run_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(planning_attempt_id)
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if (
                proposal.goal_hash != state.goal.goal_hash
                or proposal.planning_snapshot_hash != str(attempt["planning_hash"])
                or int(attempt["matter_version"]) != matter_version
            ):
                raise CaseLedgerPersistenceBlocked(
                    "local planning proposal is bound to another goal or snapshot"
                )
            external_exists = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM case_agent_planning_external_events
                    WHERE planning_attempt_id = %s
                ) AS exists
                """,
                (planning_attempt_id,),
            ).fetchone()
            if bool(external_exists["exists"]):
                raise CaseLedgerPersistenceBlocked(
                    "local planning outcome cannot follow a provider boundary"
                )
            existing = connection.execute(
                """
                SELECT planner_id, proposal_hash, structured_proposal
                FROM case_agent_planning_local_events
                WHERE planning_attempt_id = %s
                """,
                (planning_attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["planner_id"]) != planner_id
                    or str(existing["proposal_hash"]) != output_hash
                    or existing["structured_proposal"] != payload
                    or attempt["status"] != "SUCCEEDED"
                ):
                    raise CaseLedgerPersistenceBlocked(
                        "local planning outcome already differs"
                    )
                return AgentPlanningOutcome(
                    run_id,
                    planning_attempt_id,
                    "SUCCEEDED",
                    state.event_version,
                    payload,
                    output_hash,
                    None,
                )
            if (
                attempt["status"] != "CLAIMED"
                or str(attempt["lease_token"]) != lease_token
                or attempt["lease_expires_at"] <= observed_at
            ):
                raise CaseLedgerPersistenceBlocked(
                    "local planning outcome differs from its live claim"
                )
            connection.execute(
                """
                INSERT INTO case_agent_planning_local_events (
                    planning_local_event_id, planning_attempt_id, run_id,
                    firm_id, matter_id, planner_id, planning_hash,
                    proposal_hash, structured_proposal, recorded_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid4()),
                    planning_attempt_id,
                    run_id,
                    actor.firm_id,
                    matter_id,
                    planner_id,
                    str(attempt["planning_hash"]),
                    output_hash,
                    Jsonb(payload),
                    actor.actor_id,
                ),
            )
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, status = 'SUCCEEDED',
                    updated_at = now()
                WHERE planning_attempt_id = %s AND attempt_version = %s
                  AND status = 'CLAIMED' AND firm_id = %s AND matter_id = %s
                """,
                (
                    planning_attempt_id,
                    int(attempt["attempt_version"]),
                    actor.firm_id,
                    matter_id,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("planning attempt changed before local outcome was stored")
        return AgentPlanningOutcome(
            run_id,
            planning_attempt_id,
            "SUCCEEDED",
            state.event_version,
            payload,
            output_hash,
            None,
        )

    def planning_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
    ) -> AgentPlanningOutcome | None:
        """Recover an already stored proposal without calling the provider."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        _validate_uuid("run_id", run_id)
        _validate_uuid("planning_attempt_id", planning_attempt_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._READ_ROLES,
            )
            row = connection.execute(
                """
                SELECT attempt.status,
                       outcome.planning_external_event_id IS NOT NULL
                           AS has_external_outcome,
                       local.planning_local_event_id IS NOT NULL
                           AS has_local_outcome,
                       COALESCE(outcome.output_hash, local.proposal_hash)
                           AS output_hash,
                       outcome.error_code,
                       COALESCE(
                           outcome.structured_proposal,
                           local.structured_proposal
                       ) AS structured_proposal,
                       run.current_event_version
                FROM case_agent_planning_attempts attempt
                JOIN case_agent_runs run
                  ON run.run_id = attempt.run_id AND run.firm_id = attempt.firm_id
                 AND run.matter_id = attempt.matter_id
                LEFT JOIN LATERAL (
                    SELECT external.planning_external_event_id,
                           external.output_hash, external.error_code,
                           external.structured_proposal
                    FROM case_agent_planning_external_events external
                    WHERE external.planning_attempt_id = attempt.planning_attempt_id
                      AND external.ledger_version IN (2, 3)
                    ORDER BY external.ledger_version DESC
                    LIMIT 1
                ) outcome ON TRUE
                LEFT JOIN LATERAL (
                    SELECT local_event.planning_local_event_id,
                           local_event.proposal_hash,
                           local_event.structured_proposal
                    FROM case_agent_planning_local_events local_event
                    WHERE local_event.planning_attempt_id = attempt.planning_attempt_id
                    LIMIT 1
                ) local ON TRUE
                WHERE attempt.planning_attempt_id = %s AND attempt.run_id = %s
                  AND attempt.matter_id = %s AND attempt.firm_id = %s
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None or row["status"] not in {
            "SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION", "RECONCILING"
        }:
            return None
        if bool(row["has_external_outcome"]) and bool(row["has_local_outcome"]):
            raise CaseLedgerPersistenceBlocked(
                "planning attempt has both local and external outcomes"
            )
        return AgentPlanningOutcome(
            run_id, planning_attempt_id, row["status"],
            int(row["current_event_version"]), row["structured_proposal"],
            row["output_hash"], row["error_code"],
        )

    def current_planning_attempt(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> ClaimedAgentPlanningAttempt | None:
        """Return the latest durable planning attempt for restart recovery."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            row = connection.execute(
                """
                SELECT attempt.planning_attempt_id, attempt.external_request_id,
                       attempt.matter_version, attempt.planning_kind,
                       attempt.planning_hash, attempt.lease_owner,
                       attempt.lease_token,
                       attempt.lease_expires_at, attempt.attempt_version,
                       attempt.status, outcome.request_hash,
                       outcome.status AS external_ledger_status,
                       outcome.ledger_version AS external_ledger_version,
                       outcome.planning_external_event_id IS NOT NULL
                           AS has_external_outcome,
                       local.planning_local_event_id IS NOT NULL
                           AS has_local_outcome,
                       COALESCE(
                           outcome.structured_proposal,
                           local.structured_proposal
                       ) AS structured_proposal,
                       run.current_event_version
                FROM case_agent_planning_attempts attempt
                JOIN case_agent_runs run
                  ON run.run_id = attempt.run_id AND run.firm_id = attempt.firm_id
                 AND run.matter_id = attempt.matter_id
                LEFT JOIN LATERAL (
                    SELECT external.planning_external_event_id,
                           external.request_hash, external.status,
                           external.ledger_version, external.structured_proposal
                    FROM case_agent_planning_external_events external
                    WHERE external.planning_attempt_id = attempt.planning_attempt_id
                    ORDER BY external.ledger_version DESC
                    LIMIT 1
                ) outcome ON TRUE
                LEFT JOIN LATERAL (
                    SELECT local_event.planning_local_event_id,
                           local_event.structured_proposal
                    FROM case_agent_planning_local_events local_event
                    WHERE local_event.planning_attempt_id = attempt.planning_attempt_id
                    LIMIT 1
                ) local ON TRUE
                WHERE attempt.run_id = %s AND attempt.matter_id = %s
                  AND attempt.firm_id = %s
                ORDER BY attempt.created_at DESC, attempt.planning_attempt_id DESC
                LIMIT 1
                """,
                (run_id, matter_id, actor.firm_id),
            ).fetchone()
        if row is None:
            return None
        if bool(row["has_external_outcome"]) and bool(row["has_local_outcome"]):
            raise CaseLedgerPersistenceBlocked(
                "planning attempt has both local and external outcomes"
            )
        claim = _planning_claim_from_row(
            row, run_id=run_id, event_version=int(row["current_event_version"])
        )
        return ClaimedAgentPlanningAttempt(
            **{**claim.__dict__, "status": str(row["status"])}
        )

    def fail_planning_before_submission(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        error_code: str,
    ) -> AgentPlanningOutcome:
        """Finish a CLAIMED plan that provably never crossed the network."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _validate_uuid("planning_attempt_id", planning_attempt_id)
        safe_code = _planner_error_code(error_code)
        now = datetime.now(timezone.utc)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            attempt = connection.execute(
                """
                SELECT status, attempt_version,
                       EXISTS (
                           SELECT 1 FROM case_agent_planning_external_events boundary
                           WHERE boundary.planning_attempt_id = attempt.planning_attempt_id
                       ) AS crossed_boundary
                FROM case_agent_planning_attempts attempt
                WHERE planning_attempt_id = %s AND run_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(planning_attempt_id)
            if attempt["status"] != "CLAIMED" or attempt["crossed_boundary"]:
                raise CaseLedgerPersistenceBlocked(
                    "planning cannot be failed as pre-submission after an external boundary"
                )
            state = self._replay_locked(connection, actor, matter_id, run_id)
            event = AgentSupervisorEvent(
                event_id=str(uuid4()), run_id=run_id, firm_id=actor.firm_id,
                matter_id=matter_id, sequence=state.event_version + 1,
                event_type=AgentEventType.PLANNING_FAILED, occurred_at=now,
                actor_id=actor.actor_id,
                payload=PlanningFailurePayload(error_code=safe_code),
            )
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            self._update_projection(connection, previous_state=state, state=next_state)
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, status = 'FAILED',
                    finished_event_sequence = %s, updated_at = now()
                WHERE planning_attempt_id = %s AND attempt_version = %s
                  AND status = 'CLAIMED' AND firm_id = %s AND matter_id = %s
                """,
                (
                    event.sequence, planning_attempt_id,
                    int(attempt["attempt_version"]), actor.firm_id, matter_id,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("planning attempt changed before local failure")
        return AgentPlanningOutcome(
            run_id, planning_attempt_id, "FAILED", event.sequence,
            None, None, safe_code,
        )

    def reap_expired_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
    ) -> ReapedAgentPlanningAttempt | None:
        """Classify one expired planner lease without ever resubmitting it.

        No provider boundary means the attempt is a known local failure.  A
        committed v1 boundary without v2 is conservatively promoted to
        UNKNOWN_SUBMISSION and can only use lookup-only reconciliation.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        observed_at = datetime.now(timezone.utc)
        with self._transaction(actor.firm_id) as connection:
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if state.status is not AgentRunStatus.PLANNING:
                return None
            row = connection.execute(
                """
                SELECT attempt.planning_attempt_id, attempt.attempt_version,
                       attempt.external_request_id, boundary.provider_id,
                       boundary.service_id, boundary.input_hash,
                       boundary.request_hash,
                       outcome.planning_external_event_id AS outcome_id
                FROM case_agent_planning_attempts attempt
                LEFT JOIN case_agent_planning_external_events boundary
                  ON boundary.planning_attempt_id = attempt.planning_attempt_id
                 AND boundary.ledger_version = 1
                LEFT JOIN case_agent_planning_external_events outcome
                  ON outcome.planning_attempt_id = attempt.planning_attempt_id
                 AND outcome.ledger_version = 2
                WHERE attempt.run_id = %s AND attempt.matter_id = %s
                  AND attempt.firm_id = %s AND attempt.status = 'CLAIMED'
                  AND attempt.lease_expires_at <= %s
                ORDER BY attempt.created_at ASC, attempt.planning_attempt_id ASC
                LIMIT 1 FOR UPDATE OF attempt SKIP LOCKED
                """,
                (run_id, matter_id, actor.firm_id, observed_at),
            ).fetchone()
            if row is None:
                return None
            if row["outcome_id"] is not None:
                raise CaseLedgerPersistenceBlocked(
                    "expired planning attempt has an outcome but is not terminal"
                )
            planning_attempt_id = str(row["planning_attempt_id"])
            crossed_boundary = row["request_hash"] is not None
            if crossed_boundary:
                safe_code = "PLANNER_LEASE_EXPIRED_RESULT_UNKNOWN"
                connection.execute(
                    """
                    INSERT INTO case_agent_planning_external_events (
                        planning_external_event_id, planning_attempt_id, run_id,
                        firm_id, matter_id, ledger_version, status,
                        external_request_id, provider_id, service_id, input_hash,
                        request_hash, error_code, recorded_by
                    ) VALUES (%s,%s,%s,%s,%s,2,'UNKNOWN_SUBMISSION',%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        str(uuid4()), planning_attempt_id, run_id, actor.firm_id,
                        matter_id, str(row["external_request_id"]),
                        row["provider_id"], row["service_id"], row["input_hash"],
                        row["request_hash"], safe_code, actor.actor_id,
                    ),
                )
                event_type = AgentEventType.PLANNING_RESULT_UNKNOWN
                payload = None
                next_status = "UNKNOWN_SUBMISSION"
            else:
                safe_code = "PLANNER_LEASE_EXPIRED_BEFORE_SUBMISSION"
                event_type = AgentEventType.PLANNING_FAILED
                payload = PlanningFailurePayload(error_code=safe_code)
                next_status = "FAILED"
            event = AgentSupervisorEvent(
                event_id=str(uuid4()), run_id=run_id, firm_id=actor.firm_id,
                matter_id=matter_id, sequence=state.event_version + 1,
                event_type=event_type, occurred_at=observed_at,
                actor_id=actor.actor_id, payload=payload,
            )
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            self._update_projection(
                connection, previous_state=state, state=next_state
            )
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, status = %s,
                    finished_event_sequence = %s, updated_at = now()
                WHERE planning_attempt_id = %s AND attempt_version = %s
                  AND status = 'CLAIMED' AND lease_expires_at <= %s
                """,
                (
                    next_status, event.sequence, planning_attempt_id,
                    int(row["attempt_version"]), observed_at,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("expired planning lease changed before reap")
            return ReapedAgentPlanningAttempt(
                run_id=run_id,
                planning_attempt_id=planning_attempt_id,
                event_version=next_state.event_version,
                requires_reconciliation=crossed_boundary,
            )

    def claim_planning_reconciliation(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        lease_owner: str,
        lease_seconds: int = 120,
    ) -> ClaimedAgentPlanningAttempt:
        """Lease UNKNOWN planning for provider lookup; never for resubmission."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _validate_uuid("planning_attempt_id", planning_attempt_id)
        if not lease_owner.strip() or not 30 <= lease_seconds <= 900:
            raise ValueError("planning reconciliation lease is invalid")
        observed_at = datetime.now(timezone.utc)
        expires = observed_at + timedelta(seconds=lease_seconds)
        with self._transaction(actor.firm_id) as connection:
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if not any(
                item.kind is SupervisorCommandKind.RECONCILE_PLAN_RESULT
                for item in decide_next_commands(state)
            ):
                raise CaseLedgerPersistenceBlocked("the reducer did not request planning reconciliation")
            row = connection.execute(
                """
                SELECT attempt.planning_attempt_id, attempt.external_request_id,
                       attempt.matter_version, attempt.planning_kind,
                       attempt.planning_hash, attempt.lease_owner,
                       attempt.lease_token,
                       attempt.lease_expires_at, attempt.attempt_version,
                       attempt.status, unknown.request_hash,
                       NULL::jsonb AS structured_proposal
                FROM case_agent_planning_attempts attempt
                JOIN case_agent_planning_external_events unknown
                  ON unknown.planning_attempt_id = attempt.planning_attempt_id
                 AND unknown.ledger_version = 2
                 AND unknown.status = 'UNKNOWN_SUBMISSION'
                WHERE attempt.planning_attempt_id = %s AND attempt.run_id = %s
                  AND attempt.matter_id = %s AND attempt.firm_id = %s
                FOR UPDATE OF attempt
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if row is None or row["status"] not in {"UNKNOWN_SUBMISSION", "RECONCILING"}:
                raise CaseLedgerPersistenceBlocked("unknown planning result is unavailable")
            if row["status"] == "UNKNOWN_SUBMISSION":
                next_lease_token = str(uuid4())
                updated = connection.execute(
                    """
                    UPDATE case_agent_planning_attempts
                    SET attempt_version = attempt_version + 1,
                        status = 'RECONCILING', lease_owner = %s,
                        lease_token = %s, lease_expires_at = %s,
                        last_heartbeat_at = now(),
                        updated_at = now()
                    WHERE planning_attempt_id = %s AND attempt_version = %s
                      AND status = 'UNKNOWN_SUBMISSION'
                    """,
                    (
                        lease_owner.strip(), next_lease_token, expires,
                        planning_attempt_id,
                        int(row["attempt_version"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise VersionConflict("planning reconciliation was claimed elsewhere")
                row = {
                    **row, "status": "RECONCILING",
                    "attempt_version": int(row["attempt_version"]) + 1,
                    "lease_owner": lease_owner.strip(),
                    "lease_token": next_lease_token,
                    "lease_expires_at": expires,
                }
            elif row["lease_expires_at"] <= datetime.now(timezone.utc):
                next_lease_token = str(uuid4())
                updated = connection.execute(
                    """
                    UPDATE case_agent_planning_attempts
                    SET attempt_version = attempt_version + 1,
                        lease_owner = %s, lease_token = %s,
                        lease_expires_at = %s,
                        last_heartbeat_at = now(), updated_at = now()
                    WHERE planning_attempt_id = %s AND attempt_version = %s
                      AND status = 'RECONCILING' AND lease_expires_at <= now()
                    """,
                    (
                        lease_owner.strip(), next_lease_token, expires,
                        planning_attempt_id,
                        int(row["attempt_version"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise VersionConflict(
                        "expired planning reconciliation was claimed elsewhere"
                    )
                row = {
                    **row,
                    "attempt_version": int(row["attempt_version"]) + 1,
                    "lease_owner": lease_owner.strip(),
                    "lease_token": next_lease_token,
                    "lease_expires_at": expires,
                }
            elif row["lease_owner"] != lease_owner.strip():
                raise VersionConflict(
                    "planning reconciliation lease belongs to another worker"
                )
            claim = _planning_claim_from_row(
                row, run_id=run_id, event_version=state.event_version
            )
            return ClaimedAgentPlanningAttempt(
                **{**claim.__dict__, "status": "RECONCILING"}
            )

    def record_planning_reconciliation_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        planning_attempt_id: str,
        external_request_id: str,
        request_hash: str,
        lease_owner: str,
        lease_token: str,
        expected_attempt_version: int,
        status: str,
        output_hash: str | None,
        error_code: str | None,
        structured_proposal: dict[str, Any] | None,
    ) -> AgentPlanningOutcome:
        """Append lookup-only reconciliation; it cannot create a new request."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        for label, value in (
            ("run_id", run_id),
            ("planning_attempt_id", planning_attempt_id),
            ("external_request_id", external_request_id),
            ("lease_token", lease_token),
        ):
            _validate_uuid(label, value)
        _validate_sha256("request_hash", request_hash)
        _require_positive_version(expected_attempt_version)
        if not lease_owner.strip():
            raise ValueError("planning reconciliation lease owner is required")
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("planning reconciliation status is invalid")
        if status == "SUCCEEDED":
            if output_hash is None or error_code is not None or not isinstance(structured_proposal, dict):
                raise ValueError("successful planning reconciliation is incomplete")
            _validate_sha256("output_hash", output_hash)
        elif not error_code or structured_proposal is not None:
            raise ValueError("failed planning reconciliation is invalid")
        elif output_hash is not None:
            _validate_sha256("output_hash", output_hash)
        safe_error_code = (
            None if error_code is None else _planner_error_code(error_code)
        )
        now = datetime.now(timezone.utc)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            attempt = connection.execute(
                """
                SELECT status, attempt_version, lease_owner, lease_token,
                       lease_expires_at
                FROM case_agent_planning_attempts
                WHERE planning_attempt_id = %s AND run_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (planning_attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(planning_attempt_id)
            unknown = connection.execute(
                """
                SELECT provider_id, service_id, input_hash, request_hash,
                       external_request_id
                FROM case_agent_planning_external_events
                WHERE planning_attempt_id = %s AND ledger_version = 2
                  AND status = 'UNKNOWN_SUBMISSION'
                """,
                (planning_attempt_id,),
            ).fetchone()
            if (
                unknown is None
                or str(unknown["external_request_id"]) != external_request_id
                or unknown["request_hash"] != request_hash
            ):
                raise CaseLedgerPersistenceBlocked("planning reconciliation lacks its exact unknown request")
            existing = connection.execute(
                """
                SELECT status, output_hash, error_code, structured_proposal
                FROM case_agent_planning_external_events
                WHERE planning_attempt_id = %s AND ledger_version = 3
                """,
                (planning_attempt_id,),
            ).fetchone()
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if existing is not None:
                if (
                    existing["status"] != status
                    or existing["output_hash"] != output_hash
                    or existing["error_code"] != safe_error_code
                    or existing["structured_proposal"] != structured_proposal
                ):
                    raise CaseLedgerPersistenceBlocked("planning reconciliation already differs")
                return AgentPlanningOutcome(
                    run_id, planning_attempt_id, status, state.event_version,
                    existing["structured_proposal"], existing["output_hash"],
                    existing["error_code"],
                )
            if (
                attempt["status"] != "RECONCILING"
                or int(attempt["attempt_version"]) != expected_attempt_version
                or attempt["lease_owner"] != lease_owner.strip()
                or str(attempt["lease_token"]) != lease_token
                or attempt["lease_expires_at"] <= now
            ):
                raise VersionConflict(
                    "planning reconciliation lease changed, expired, or belongs to another worker"
                )
            connection.execute(
                """
                INSERT INTO case_agent_planning_external_events (
                    planning_external_event_id, planning_attempt_id, run_id,
                    firm_id, matter_id, ledger_version, status,
                    external_request_id, provider_id, service_id, input_hash,
                    request_hash, output_hash, error_code, structured_proposal,
                    recorded_by
                ) VALUES (%s,%s,%s,%s,%s,3,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(uuid4()), planning_attempt_id, run_id, actor.firm_id,
                    matter_id, status, external_request_id, unknown["provider_id"],
                    unknown["service_id"], unknown["input_hash"], request_hash,
                    output_hash, safe_error_code,
                    Jsonb(structured_proposal) if structured_proposal is not None else None,
                    actor.actor_id,
                ),
            )
            finished_sequence: int | None = None
            if status == "FAILED":
                event = AgentSupervisorEvent(
                    event_id=str(uuid4()), run_id=run_id, firm_id=actor.firm_id,
                    matter_id=matter_id, sequence=state.event_version + 1,
                    event_type=AgentEventType.PLANNING_FAILED,
                    occurred_at=now, actor_id=actor.actor_id,
                    payload=PlanningFailurePayload(
                        error_code=safe_error_code or "PLANNER_RECONCILIATION_FAILED"
                    ),
                )
                next_state = reduce_agent_event(state, event)
                self._insert_event(connection, event)
                self._update_projection(connection, previous_state=state, state=next_state)
                projection = _state_json(next_state)
                self._insert_checkpoint(
                    connection, next_state, projection, _payload_hash(projection)
                )
                state = next_state
                finished_sequence = event.sequence
            updated = connection.execute(
                """
                UPDATE case_agent_planning_attempts
                SET attempt_version = attempt_version + 1, status = %s,
                    finished_event_sequence = COALESCE(%s, finished_event_sequence),
                    updated_at = now()
                WHERE planning_attempt_id = %s AND attempt_version = %s
                  AND status = 'RECONCILING' AND lease_owner = %s
                  AND lease_token = %s
                  AND lease_expires_at > now()
                """,
                (
                    status, finished_sequence, planning_attempt_id,
                    expected_attempt_version, lease_owner.strip(), lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("planning reconciliation lease changed")
            return AgentPlanningOutcome(
                run_id, planning_attempt_id, status, state.event_version,
                structured_proposal, output_hash, safe_error_code,
            )

    def claim_next_reconciliation(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int = 120,
    ) -> ClaimedAgentTask:
        """Lease exactly one UNKNOWN task for provider reconciliation only."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _require_positive_version(expected_event_version)
        if not lease_owner.strip() or not 30 <= lease_seconds <= 900:
            raise ValueError("reconciliation lease parameters are invalid")
        observed_at = datetime.now(timezone.utc)
        expires = observed_at + timedelta(seconds=lease_seconds)
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection, actor=actor, matter_id=matter_id,
                command_name="CLAIM_CASE_AGENT_RECONCILIATION",
                idempotency_key=idempotency_key,
            )
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if state.event_version != expected_event_version:
                raise VersionConflict("Agent run changed before reconciliation claim")
            self._authorize_current_snapshot(
                connection, actor=actor, matter_id=matter_id, state=state,
                allowed_roles=self._WORKER_ROLES,
            )
            command = next(
                (
                    item for item in decide_next_commands(state)
                    if item.kind is SupervisorCommandKind.RECONCILE_EXTERNAL_RESULT
                ),
                None,
            )
            if command is None or command.task_id is None or command.attempt_id is None:
                raise CaseLedgerPersistenceBlocked("the reducer has no result to reconcile")
            task = _task_from_state(state, command.task_id)
            row = connection.execute(
                """
                SELECT attempt_version, lease_owner, lease_expires_at,
                       external_request_id, status
                FROM case_agent_task_attempts
                WHERE attempt_id = %s AND run_id = %s AND task_id = %s
                  AND matter_id = %s AND firm_id = %s
                FOR UPDATE
                """,
                (
                    command.attempt_id, run_id, command.task_id,
                    matter_id, actor.firm_id,
                ),
            ).fetchone()
            if row is None or row["status"] not in {"UNKNOWN", "RECONCILING"}:
                raise CaseLedgerPersistenceBlocked("unknown Agent attempt is unavailable")
            if row["status"] == "UNKNOWN":
                updated = connection.execute(
                    """
                    UPDATE case_agent_task_attempts
                    SET attempt_version = attempt_version + 1,
                        status = 'RECONCILING', lease_owner = %s,
                        lease_expires_at = %s, last_heartbeat_at = now(),
                        updated_at = now()
                    WHERE attempt_id = %s AND attempt_version = %s
                      AND status = 'UNKNOWN' AND firm_id = %s AND matter_id = %s
                    """,
                    (
                        lease_owner.strip(), expires, command.attempt_id,
                        int(row["attempt_version"]), actor.firm_id, matter_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise VersionConflict("Agent reconciliation was claimed elsewhere")
                attempt_version = int(row["attempt_version"]) + 1
            elif (
                row["lease_owner"] == lease_owner.strip()
                and row["lease_expires_at"] > observed_at
            ):
                attempt_version = int(row["attempt_version"])
                expires = row["lease_expires_at"]
            elif row["lease_expires_at"] <= observed_at:
                # A reconciliation lease can be lost after the lookup-only
                # provider/object-store check but before the terminal receipt
                # is committed.  Reclaim the same immutable UNKNOWN attempt;
                # the adapter's reconcile path cannot send a second request.
                updated = connection.execute(
                    """
                    UPDATE case_agent_task_attempts
                    SET attempt_version = attempt_version + 1,
                        lease_owner = %s, lease_expires_at = %s,
                        last_heartbeat_at = now(), updated_at = now()
                    WHERE attempt_id = %s AND attempt_version = %s
                      AND status = 'RECONCILING'
                      AND lease_expires_at <= %s
                      AND firm_id = %s AND matter_id = %s
                    """,
                    (
                        lease_owner.strip(), expires, command.attempt_id,
                        int(row["attempt_version"]), observed_at,
                        actor.firm_id, matter_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise VersionConflict(
                        "Agent reconciliation was reclaimed elsewhere"
                    )
                attempt_version = int(row["attempt_version"]) + 1
            else:
                raise VersionConflict("Agent reconciliation lease belongs to another worker")
            if not row["external_request_id"]:
                raise CaseLedgerPersistenceBlocked("unknown Agent result lacks provider request id")
            return ClaimedAgentTask(
                run_id=run_id, task_id=task.task_id,
                attempt_id=command.attempt_id, event_version=state.event_version,
                lease_owner=lease_owner.strip(), lease_expires_at=expires,
                task=task, attempt_version=attempt_version,
                reconciliation=True,
                external_request_id=str(row["external_request_id"]),
            )

    def record_worker_heartbeat(self, heartbeat: object) -> None:
        """Persist only non-sensitive liveness for one firm-scoped worker."""

        required_fields = (
            "firm_id", "worker_id", "actor_id", "planner_id",
            "adapter_catalog_hash", "verifier_actor_id", "verifier_id",
            "verifier_version", "verifier_policy_hash", "observed_at",
            "expires_at",
        )
        if not all(hasattr(heartbeat, field) for field in required_fields):
            raise ValueError("Agent worker heartbeat is invalid")
        for label, value in (
            ("firm_id", heartbeat.firm_id), ("actor_id", heartbeat.actor_id),
            ("verifier_actor_id", heartbeat.verifier_actor_id),
        ):
            _validate_uuid(label, value)
        _validate_sha256("adapter_catalog_hash", heartbeat.adapter_catalog_hash)
        _validate_sha256("verifier_policy_hash", heartbeat.verifier_policy_hash)
        if (
            not heartbeat.worker_id.strip() or len(heartbeat.worker_id.strip()) > 200
            or not heartbeat.planner_id.strip() or len(heartbeat.planner_id.strip()) > 200
            or not _safe_runtime_identifier(heartbeat.verifier_id)
            or not _safe_semver(heartbeat.verifier_version)
            or heartbeat.verifier_actor_id == heartbeat.actor_id
            or heartbeat.observed_at.tzinfo is None
            or heartbeat.expires_at <= heartbeat.observed_at
        ):
            raise ValueError("Agent worker heartbeat fields are invalid")
        actor = Actor(
            actor_id=heartbeat.actor_id,
            firm_id=heartbeat.firm_id,
            roles=frozenset({Role.SYSTEM_WORKER}),
        )
        with self._transaction(heartbeat.firm_id) as connection:
            _require_active_worker_principal(connection, actor)
            _require_active_worker_principal(
                connection,
                Actor(
                    actor_id=heartbeat.verifier_actor_id,
                    firm_id=heartbeat.firm_id,
                    roles=frozenset({Role.SYSTEM_WORKER}),
                ),
            )
            persisted = connection.execute(
                """
                INSERT INTO case_agent_worker_heartbeats (
                    firm_id, worker_id, actor_id, planner_id,
                    adapter_catalog_hash, verifier_actor_id, verifier_id,
                    verifier_version, verifier_policy_hash,
                    observed_at, expires_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (firm_id, worker_id) DO UPDATE
                SET planner_id = EXCLUDED.planner_id,
                    adapter_catalog_hash = EXCLUDED.adapter_catalog_hash,
                    verifier_actor_id = EXCLUDED.verifier_actor_id,
                    verifier_id = EXCLUDED.verifier_id,
                    verifier_version = EXCLUDED.verifier_version,
                    verifier_policy_hash = EXCLUDED.verifier_policy_hash,
                    observed_at = EXCLUDED.observed_at,
                    expires_at = EXCLUDED.expires_at,
                    heartbeat_version = case_agent_worker_heartbeats.heartbeat_version + 1,
                    updated_at = now()
                WHERE case_agent_worker_heartbeats.actor_id = EXCLUDED.actor_id
                  AND case_agent_worker_heartbeats.verifier_actor_id =
                      EXCLUDED.verifier_actor_id
                  AND EXCLUDED.observed_at > case_agent_worker_heartbeats.observed_at
                """,
                (
                    heartbeat.firm_id, heartbeat.worker_id.strip(), heartbeat.actor_id,
                    heartbeat.planner_id.strip(), heartbeat.adapter_catalog_hash,
                    heartbeat.verifier_actor_id, heartbeat.verifier_id,
                    heartbeat.verifier_version, heartbeat.verifier_policy_hash,
                    heartbeat.observed_at, heartbeat.expires_at,
                ),
            )
            if persisted.rowcount != 1:
                raise VersionConflict(
                    "Agent worker heartbeat actor changed or its clock did not advance"
                )

    def latest_worker_heartbeat(
        self, *, firm_id: str, worker_id: str
    ) -> AgentWorkerHeartbeatRecord | None:
        _validate_uuid("firm_id", firm_id)
        if not worker_id.strip() or len(worker_id.strip()) > 200:
            raise ValueError("worker_id is invalid")
        with self._read_transaction(firm_id) as connection:
            row = connection.execute(
                """
                SELECT firm_id, worker_id, actor_id, planner_id,
                       adapter_catalog_hash, verifier_actor_id, verifier_id,
                       verifier_version, verifier_policy_hash,
                       observed_at, expires_at
                FROM case_agent_worker_heartbeats
                WHERE firm_id = %s AND worker_id = %s
                """,
                (firm_id, worker_id.strip()),
            ).fetchone()
        if row is None:
            return None
        return AgentWorkerHeartbeatRecord(
            firm_id=str(row["firm_id"]), worker_id=str(row["worker_id"]),
            actor_id=str(row["actor_id"]), planner_id=str(row["planner_id"]),
            adapter_catalog_hash=str(row["adapter_catalog_hash"]),
            verifier_actor_id=str(row["verifier_actor_id"]),
            verifier_id=str(row["verifier_id"]),
            verifier_version=str(row["verifier_version"]),
            verifier_policy_hash=str(row["verifier_policy_hash"]),
            observed_at=row["observed_at"], expires_at=row["expires_at"],
        )

    def has_fresh_worker(
        self, *, firm_id: str, now: datetime | None = None
    ) -> bool:
        """Return true only for a non-expired heartbeat of an active principal."""

        _validate_uuid("firm_id", firm_id)
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("worker readiness time must be timezone-aware")
        try:
            with self._read_transaction(firm_id) as connection:
                row = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM case_agent_worker_heartbeats heartbeat
                        JOIN users principal
                          ON principal.user_id = heartbeat.actor_id
                         AND principal.firm_id = heartbeat.firm_id
                        JOIN users verifier
                          ON verifier.user_id = heartbeat.verifier_actor_id
                         AND verifier.firm_id = heartbeat.firm_id
                        WHERE heartbeat.firm_id = %s
                          AND heartbeat.observed_at <= %s
                          AND heartbeat.expires_at > %s
                          AND principal.status = 'ACTIVE'
                          AND verifier.status = 'ACTIVE'
                          AND heartbeat.verifier_actor_id <> heartbeat.actor_id
                          AND heartbeat.verifier_id IS NOT NULL
                          AND heartbeat.verifier_version IS NOT NULL
                          AND heartbeat.verifier_policy_hash IS NOT NULL
                    ) AS ready
                    """,
                    (firm_id, observed_at, observed_at),
                ).fetchone()
            return bool(row and row["ready"])
        except Exception:
            return False

    def probe_case_agent_store(self, *, firm_id: str) -> bool:
        """A minimal RLS-bound database liveness probe; no case data is read."""

        _validate_uuid("firm_id", firm_id)
        try:
            with self._read_transaction(firm_id) as connection:
                row = connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and int(row["ok"]) == 1)
        except Exception:
            return False

    def claim_next_dispatchable_task(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int = 120,
    ) -> ClaimedAgentTask:
        """Claim only a deterministic DISPATCH_TASK command from the reducer."""

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _require_positive_version(expected_event_version)
        if not lease_owner.strip() or len(lease_owner.strip()) > 200:
            raise ValueError("lease_owner is required and bounded")
        if not 30 <= lease_seconds <= 900:
            raise ValueError("Agent task lease must be between 30 and 900 seconds")
        command_name = "CLAIM_NEXT_CASE_AGENT_TASK"
        request_hash = _payload_hash(
            {
                "matter_id": matter_id,
                "run_id": run_id,
                "expected_event_version": expected_event_version,
                "lease_owner": lease_owner.strip(),
                "lease_seconds": lease_seconds,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
            )
            prior = self._prior_receipt(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                state = self._replay_locked(connection, actor, matter_id, run_id)
                task = _task_from_state(state, prior.status.split(":", 1)[1])
                attempt = connection.execute(
                    """
                    SELECT attempt_id, attempt_version, lease_owner, lease_expires_at
                    FROM case_agent_task_attempts
                    WHERE run_id = %s AND task_id = %s AND firm_id = %s AND matter_id = %s
                    ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (run_id, task.task_id, actor.firm_id, matter_id),
                ).fetchone()
                if attempt is None:
                    raise CaseLedgerPersistenceBlocked("idempotent Agent claim lost its attempt")
                return ClaimedAgentTask(
                    run_id=run_id,
                    task_id=task.task_id,
                    attempt_id=str(attempt["attempt_id"]),
                    event_version=prior.event_version,
                    lease_owner=attempt["lease_owner"],
                    lease_expires_at=attempt["lease_expires_at"],
                    task=task,
                    attempt_version=int(attempt["attempt_version"]),
                )
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if state.event_version != expected_event_version:
                raise VersionConflict(
                    f"expected Agent event version {expected_event_version}, current version is {state.event_version}"
                )
            self._authorize_current_snapshot(
                connection, actor=actor, matter_id=matter_id, state=state,
                allowed_roles=self._WORKER_ROLES,
            )
            dispatch = next(
                (
                    command
                    for command in decide_next_commands(state)
                    if command.kind is SupervisorCommandKind.DISPATCH_TASK
                ),
                None,
            )
            if dispatch is None or dispatch.task_id is None or dispatch.attempt_id is None:
                raise CaseLedgerPersistenceBlocked("the reducer has no dispatchable Agent task")
            task = _task_from_state(state, dispatch.task_id)
            event = AgentSupervisorEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=AgentEventType.TASK_STARTED,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor.actor_id,
                payload=TaskStartedPayload(
                    task_id=task.task_id,
                    attempt_id=dispatch.attempt_id,
                    graph_hash=dispatch.graph_hash or "",
                    input_hash=task.input_hash,
                ),
            )
            next_state = reduce_agent_event(state, event)
            lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            self._insert_event(connection, event)
            self._persist_event_details(
                connection,
                event=event,
                previous_state=state,
                next_state=next_state,
                command_id=dispatch.command_id,
                lease_owner=lease_owner.strip(),
                lease_expires_at=lease_expires_at,
            )
            self._update_projection(connection, previous_state=state, state=next_state)
            self._insert_checkpoint(
                connection,
                next_state,
                _state_json(next_state),
                _payload_hash(_state_json(next_state)),
            )
            receipt = self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name=command_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                event=event,
                status=next_state.status,
                receipt_status=f"{next_state.status.value}:{task.task_id}",
            )
            return ClaimedAgentTask(
                run_id=run_id,
                task_id=task.task_id,
                attempt_id=dispatch.attempt_id,
                event_version=receipt.event_version,
                lease_owner=lease_owner.strip(),
                lease_expires_at=lease_expires_at,
                task=task,
                attempt_version=1,
            )

    def heartbeat_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        attempt_id: str,
        expected_attempt_version: int,
        lease_owner: str,
        extend_seconds: int = 120,
    ) -> tuple[int, datetime]:
        """Extend a live lease with optimistic concurrency; no workflow event."""

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        _validate_uuid("attempt_id", attempt_id)
        _require_positive_version(expected_attempt_version)
        if not lease_owner.strip() or not 30 <= extend_seconds <= 900:
            raise ValueError("heartbeat lease parameters are invalid")
        expires = datetime.now(timezone.utc) + timedelta(seconds=extend_seconds)
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            updated = connection.execute(
                """
                UPDATE case_agent_task_attempts
                SET attempt_version = attempt_version + 1, lease_expires_at = %s,
                    last_heartbeat_at = now(), updated_at = now()
                WHERE attempt_id = %s AND matter_id = %s AND firm_id = %s
                  AND attempt_version = %s AND lease_owner = %s
                  AND status IN ('RUNNING', 'RECONCILING')
                  AND lease_expires_at > now()
                """,
                (
                    expires,
                    attempt_id,
                    matter_id,
                    actor.firm_id,
                    expected_attempt_version,
                    lease_owner.strip(),
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("Agent task lease changed, expired, or belongs to another worker")
        return expected_attempt_version + 1, expires

    def record_external_submission_started(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        attempt_id: str,
        expected_attempt_version: int,
        external_request_id: str,
        destination: str,
        request_hash: str,
        approval_id: str,
    ) -> int:
        """Commit the external boundary marker before any network transport.

        The caller must finish this transaction successfully before sending a
        byte.  A crash after this point leaves a durable STARTED marker and the
        reaper will force UNKNOWN/reconciliation instead of resubmission.
        """

        _validate_read_identity(matter_id=matter_id, actor=actor)
        self._require_worker(actor)
        for label, value in (
            ("run_id", run_id), ("attempt_id", attempt_id), ("approval_id", approval_id)
        ):
            _validate_uuid(label, value)
        _require_positive_version(expected_attempt_version)
        _validate_sha256("request_hash", request_hash)
        if not external_request_id.strip() or len(external_request_id.strip()) > 500:
            raise ValueError("external_request_id is required and bounded")
        if not destination.strip() or len(destination.strip()) > 500:
            raise ValueError("external destination is required and bounded")
        with self._transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=self._WORKER_ROLES,
            )
            attempt = connection.execute(
                """
                SELECT a.attempt_version, a.task_id, a.status, t.network_policy,
                       t.external_request_approval_required, t.input_hash,
                       approval.approval_id
                FROM case_agent_task_attempts a
                JOIN case_agent_tasks t
                  ON t.graph_id = a.graph_id AND t.task_id = a.task_id
                 AND t.run_id = a.run_id AND t.firm_id = a.firm_id
                 AND t.matter_id = a.matter_id
                LEFT JOIN case_agent_approvals approval
                  ON approval.approval_id = a.external_approval_id
                 AND approval.run_id = a.run_id AND approval.task_id = a.task_id
                 AND approval.firm_id = a.firm_id AND approval.matter_id = a.matter_id
                 AND approval.task_input_hash = t.input_hash
                WHERE a.attempt_id = %s AND a.run_id = %s
                  AND a.matter_id = %s AND a.firm_id = %s
                FOR UPDATE OF a
                """,
                (attempt_id, run_id, matter_id, actor.firm_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if (
                int(attempt["attempt_version"]) != expected_attempt_version
                or attempt["status"] != "RUNNING"
            ):
                raise VersionConflict("Agent attempt changed before external submission")
            if (
                attempt["network_policy"] != NetworkPolicy.EXACT_ALLOWLIST.value
                or not attempt["external_request_approval_required"]
                or attempt["approval_id"] is None
            ):
                raise CaseLedgerPersistenceBlocked(
                    "external submission requires an exact network task and approval"
                )
            connection.execute(
                """
                INSERT INTO case_agent_external_submissions (
                    run_id, attempt_id, task_id, firm_id, matter_id,
                    external_request_id, destination, request_hash,
                    submission_state, recorded_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'STARTED',%s)
                """,
                (
                    run_id, attempt_id, str(attempt["task_id"]), actor.firm_id,
                    matter_id, external_request_id.strip(), destination.strip(),
                    request_hash, actor.actor_id,
                ),
            )
            updated = connection.execute(
                """
                UPDATE case_agent_task_attempts
                SET attempt_version = attempt_version + 1,
                    external_request_id = %s,
                    external_submission_state = 'SUBMITTED', updated_at = now()
                WHERE attempt_id = %s AND run_id = %s AND matter_id = %s
                  AND firm_id = %s AND attempt_version = %s AND status = 'RUNNING'
                """,
                (
                    external_request_id.strip(), attempt_id, run_id, matter_id,
                    actor.firm_id, expected_attempt_version,
                ),
            )
            if updated.rowcount != 1:
                raise VersionConflict("Agent attempt changed before external submission")
        return expected_attempt_version + 1

    def record_receipt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        receipt: TaskResultReceipt,
    ) -> AgentControlCommandReceipt:
        """Record a terminal or UNKNOWN result as an immutable supervisor event."""

        self._require_worker(actor)
        event = AgentSupervisorEvent(
            event_id=str(uuid4()),
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=expected_event_version + 1,
            event_type=AgentEventType.TASK_RESULT_RECORDED,
            occurred_at=datetime.now(timezone.utc),
            actor_id=actor.actor_id,
            payload=TaskResultPayload(receipt=receipt),
        )
        return self.append_event(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_event_version,
            idempotency_key=idempotency_key,
            event=event,
        )

    def reap_expired_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ReapedAgentAttempt | None:
        """Convert one lost lease into FAILED or UNKNOWN through the reducer.

        A local/not-submitted attempt may become RETRYABLE according to its
        immutable retry policy.  Once an external request may have crossed the
        boundary, the receipt is UNKNOWN and the reducer permits reconciliation
        only; it is never silently dispatched again.
        """

        _validate_command_identity(
            matter_id=matter_id, actor=actor, idempotency_key=idempotency_key
        )
        self._require_worker(actor)
        _validate_uuid("run_id", run_id)
        _require_positive_version(expected_event_version)
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("reaper time must be timezone-aware")
        with self._transaction(actor.firm_id) as connection:
            # A stale, already-waiting run has no task lease to recover.  Do
            # not force that historical snapshot through the current-matter
            # write lock merely to discover there is nothing to reap: doing
            # so turns harmless old WAITING_INPUT runs into a hot retry loop
            # and can starve a newly created current-version run.  This first
            # lookup is deliberately non-locking.  If a lease expires just
            # after it returns false, the next bounded Worker cycle will reap
            # it; when it returns true, the established run -> matter ->
            # attempt lock order below remains unchanged and rechecks the row.
            expired = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM case_agent_task_attempts
                    WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                      AND status = 'RUNNING' AND lease_expires_at <= %s
                ) AS present
                """,
                (run_id, matter_id, actor.firm_id, observed_at),
            ).fetchone()
            if expired is None or not bool(expired["present"]):
                return None
            _advisory_lock(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="REAP_EXPIRED_CASE_AGENT_ATTEMPT",
                idempotency_key=idempotency_key,
            )
            state = self._replay_locked(connection, actor, matter_id, run_id)
            if state.event_version != expected_event_version:
                raise VersionConflict(
                    f"expected Agent event version {expected_event_version}, current version is {state.event_version}"
                )
            self._authorize_current_snapshot(
                connection, actor=actor, matter_id=matter_id, state=state,
                allowed_roles=self._WORKER_ROLES,
            )
            row = connection.execute(
                """
                SELECT attempt_id, task_id, input_hash, adapter_id, adapter_version,
                       external_request_id, external_submission_state
                FROM case_agent_task_attempts
                WHERE run_id = %s AND matter_id = %s AND firm_id = %s
                  AND status = 'RUNNING' AND lease_expires_at <= %s
                ORDER BY lease_expires_at ASC, attempt_id ASC
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """,
                (run_id, matter_id, actor.firm_id, observed_at),
            ).fetchone()
            if row is None:
                return None
            boundary = connection.execute(
                """
                SELECT external_request_id
                FROM case_agent_external_submissions
                WHERE run_id = %s AND attempt_id = %s AND matter_id = %s AND firm_id = %s
                """,
                (run_id, row["attempt_id"], matter_id, actor.firm_id),
            ).fetchone()
            crossed_boundary = boundary is not None or row["external_submission_state"] in {
                "SUBMITTED", "UNKNOWN"
            }
            external_request_id = row["external_request_id"]
            if boundary is not None:
                external_request_id = boundary["external_request_id"]
            if crossed_boundary and not external_request_id:
                raise CaseLedgerPersistenceBlocked(
                    "an externally submitted attempt lacks its reconciliation identifier"
                )
            receipt = TaskResultReceipt(
                receipt_id=str(uuid4()),
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                input_hash=row["input_hash"],
                adapter_id=row["adapter_id"],
                adapter_version=row["adapter_version"],
                status=ResultStatus.UNKNOWN if crossed_boundary else ResultStatus.FAILED,
                external_submission_state=(
                    ExternalSubmissionState.UNKNOWN
                    if crossed_boundary
                    else ExternalSubmissionState(row["external_submission_state"])
                ),
                output_hash=None,
                error_code=None if crossed_boundary else "LEASE_EXPIRED",
                external_request_id=external_request_id,
                runtime_seconds=0,
                cost_minor_units=0,
                external_calls=0,
            )
            event = AgentSupervisorEvent(
                event_id=str(uuid4()),
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=state.event_version + 1,
                event_type=AgentEventType.TASK_RESULT_RECORDED,
                occurred_at=observed_at,
                actor_id=actor.actor_id,
                payload=TaskResultPayload(receipt=receipt),
            )
            next_state = reduce_agent_event(state, event)
            self._insert_event(connection, event)
            self._persist_event_details(
                connection,
                event=event,
                previous_state=state,
                next_state=next_state,
            )
            self._update_projection(connection, previous_state=state, state=next_state)
            projection = _state_json(next_state)
            self._insert_checkpoint(
                connection, next_state, projection, _payload_hash(projection)
            )
            self._finish_control_command(
                connection,
                actor=actor,
                matter_id=matter_id,
                command_name="REAP_EXPIRED_CASE_AGENT_ATTEMPT",
                idempotency_key=idempotency_key,
                request_hash=_payload_hash(
                    {
                        "run_id": run_id,
                        "expected_event_version": expected_event_version,
                        "attempt_id": str(row["attempt_id"]),
                        "observed_at": observed_at,
                    }
                ),
                event=event,
                status=next_state.status,
            )
            return ReapedAgentAttempt(
                run_id=run_id,
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                event_version=next_state.event_version,
                requires_reconciliation=crossed_boundary,
            )

    def _append_event_locked(
        self,
        connection: Any,
        *,
        matter_id: str,
        actor: Actor,
        expected_event_version: int,
        idempotency_key: str,
        command_name: str,
        request_hash: str,
        event: AgentSupervisorEvent,
        allowed_roles: frozenset[Role],
        budget_review_compilation: tuple[object, object] | None = None,
        material_scope_compilation: tuple[object, object] | None = None,
    ) -> AgentControlCommandReceipt:
        _advisory_lock(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
        )
        replay = self._prior_receipt(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        state = self._replay_locked(connection, actor, matter_id, event.run_id)
        if state.event_version != expected_event_version:
            raise VersionConflict(
                f"expected Agent event version {expected_event_version}, current version is {state.event_version}"
            )
        self._authorize_current_snapshot(
            connection,
            actor=actor,
            matter_id=matter_id,
            state=state,
            allowed_roles=allowed_roles,
            proposed_event=event,
        )
        next_state = reduce_agent_event(state, event)
        if event.event_type in {AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED, AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED,
                               AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED}:
            from .case_agent_analysis_stage import current_analysis_candidate_bindings
            assert isinstance(event.payload, CaseAnalysisStageReviewPayload)
            bindings = current_analysis_candidate_bindings(connection,
                firm_id=actor.firm_id, matter_id=matter_id)
            if bindings != event.payload.stage.candidate_bindings:
                raise CaseLedgerPersistenceBlocked("analysis candidates changed before continuation")
        if event.event_type is AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED:
            from .case_agent_material_coverage import read_material_extraction_coverage
            from .case_agent_material_stage import prepare_supplementary_material_stage
            assert isinstance(event.payload, SupplementaryMaterialStageReviewPayload)
            stage = event.payload.stage
            current = prepare_supplementary_material_stage(state=state,
                coverage=read_material_extraction_coverage(connection,
                    firm_id=actor.firm_id, matter_id=matter_id),
                approved_by=actor.actor_id, external_call_cap=1, cost_cap_minor_units=120)
            if current != stage:
                raise CaseLedgerPersistenceBlocked("supplementary material scope changed before review")
        if event.event_type is AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED:
            if not isinstance(material_scope_compilation, tuple) or len(material_scope_compilation) != 2:
                raise CaseLedgerPersistenceBlocked("material scope review has no server compilation")
            assert isinstance(event.payload, PlanningMaterialScopeReviewPayload)
            _assert_retained_material_scope_review(connection, actor=actor, state=state, payload=event.payload,
                compiler=material_scope_compilation[0], planning_snapshot=material_scope_compilation[1])
        if event.event_type is AgentEventType.PLANNING_BUDGET_REVIEWED:
            assert isinstance(event.payload, PlanningBudgetReviewPayload)
            proposal = _assert_retained_planning_budget_review(connection, actor=actor, state=state, payload=event.payload)
            _compile_budget_review_before_write(state=state, payload=event.payload,
                proposal=proposal, compilation=budget_review_compilation)
        if event.event_type is AgentEventType.RUN_COMPLETED:
            assert isinstance(event.payload, RunCompletedPayload)
            _assert_final_document_versions(connection, actor=actor, state=state,
                                            approval=event.payload.final_review)
        self._insert_event(connection, event)
        self._persist_event_details(
            connection, event=event, previous_state=state, next_state=next_state
        )
        self._update_projection(connection, previous_state=state, state=next_state)
        projection = _state_json(next_state)
        self._insert_checkpoint(
            connection, next_state, projection, _payload_hash(projection)
        )
        return self._finish_control_command(
            connection,
            actor=actor,
            matter_id=matter_id,
            command_name=command_name,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            event=event,
            status=next_state.status,
        )

    def _persist_event_details(
        self,
        connection: Any,
        *,
        event: AgentSupervisorEvent,
        previous_state: AgentRunState,
        next_state: AgentRunState,
        command_id: str | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> None:
        if event.event_type is AgentEventType.TASK_GRAPH_ACCEPTED:
            assert isinstance(event.payload, TaskGraphPayload)
            graph = event.payload.graph
            connection.execute(
                """
                UPDATE case_agent_task_heads
                SET is_current = false, head_event_version = %s, updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s AND is_current
                """,
                (event.sequence, event.run_id, event.firm_id, event.matter_id),
            )
            connection.execute(
                """
                INSERT INTO case_agent_task_graphs (
                    graph_id, run_id, firm_id, matter_id, graph_version, goal_hash,
                    snapshot_matter_version, snapshot_schema_version, snapshot_hash,
                    graph_hash, accepted_event_sequence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    graph.graph_id, event.run_id, event.firm_id, event.matter_id,
                    graph.graph_version, graph.goal_hash, graph.snapshot.matter_version,
                    graph.snapshot.schema_version, graph.snapshot.snapshot_hash,
                    graph.graph_hash, event.sequence,
                ),
            )
            for runtime in next_state.tasks:
                task = runtime.spec
                connection.execute(
                    """
                    INSERT INTO case_agent_tasks (
                        graph_id, task_id, run_id, firm_id, matter_id, sequence,
                        title, purpose, rationale, input_refs, input_hash,
                        skill_id, skill_version, tool_id, tool_version, adapter_id,
                        adapter_version, granted_scopes, execution_mode, network_policy,
                        allowed_domains, sandbox_profile, sandbox_policy_version,
                        sandbox_policy_hash, reads_case_objects,
                        writes_managed_derivatives, external_request_approval_required,
                        risk_level, autonomy_level, approval_gate, retry_mode, resource_budget
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    """,
                    _task_insert_params(event, graph, task),
                )
                connection.execute(
                    """
                    INSERT INTO case_agent_task_heads (
                        run_id, task_id, graph_id, firm_id, matter_id, status,
                        is_current, attempt_count, active_attempt_id, head_event_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, true, 0, NULL, %s)
                    """,
                    (
                        event.run_id, task.task_id, graph.graph_id, event.firm_id,
                        event.matter_id, runtime.status.value, event.sequence,
                    ),
                )
            # Insert edges only after every task exists.  The supervisor proves
            # acyclicity but intentionally does not require dependencies to
            # have a lower display sequence.
            for runtime in next_state.tasks:
                for dependency_id in runtime.spec.dependency_ids:
                    connection.execute(
                        """
                        INSERT INTO case_agent_task_dependencies (
                            graph_id, task_id, dependency_task_id, run_id, firm_id, matter_id
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            graph.graph_id, runtime.spec.task_id, dependency_id,
                            event.run_id, event.firm_id, event.matter_id,
                        ),
                    )
        elif event.event_type is AgentEventType.APPROVAL_GRANTED:
            assert isinstance(event.payload, ApprovalPayload)
            approval = event.payload.approval
            graph_id = _require_graph_id(next_state)
            connection.execute(
                """
                INSERT INTO case_agent_approvals (
                    approval_id, run_id, graph_id, task_id, firm_id, matter_id,
                    approval_kind, task_input_hash, graph_hash, gate, approved_by,
                    approval_hash, event_sequence, approved_at
                ) VALUES (%s,%s,%s,%s,%s,%s,'TASK',%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    approval.approval_id, event.run_id, graph_id, approval.task_id,
                    event.firm_id, event.matter_id, approval.task_input_hash,
                    approval.graph_hash, approval.gate.value, approval.approved_by,
                    approval.approval_hash, event.sequence, event.occurred_at,
                ),
            )
        elif event.event_type is AgentEventType.TASK_STARTED:
            assert isinstance(event.payload, TaskStartedPayload)
            runtime = next(
                item for item in next_state.tasks if item.spec.task_id == event.payload.task_id
            )
            connection.execute(
                """
                INSERT INTO case_agent_task_attempts (
                    attempt_id, run_id, graph_id, task_id, firm_id, matter_id,
                    attempt_number, status, command_id, input_hash, retry_mode,
                    adapter_id, adapter_version, lease_owner, lease_expires_at,
                    external_approval_id, external_submission_state,
                    started_event_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'RUNNING',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    event.payload.attempt_id, event.run_id, _require_graph_id(next_state),
                    event.payload.task_id, event.firm_id, event.matter_id,
                    runtime.attempt_count, command_id or _payload_hash(_event_json(event)),
                    runtime.spec.input_hash, runtime.spec.retry_mode.value,
                    runtime.spec.skill.adapter_id, runtime.spec.skill.adapter_version,
                    lease_owner or event.actor_id,
                    lease_expires_at or event.occurred_at + timedelta(seconds=120),
                    _task_approval_id(next_state, runtime.spec.task_id),
                    (
                        ExternalSubmissionState.NOT_SUBMITTED.value
                        if runtime.spec.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST
                        else ExternalSubmissionState.NOT_APPLICABLE.value
                    ),
                    event.sequence,
                ),
            )
        elif event.event_type is AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED:
            # The governed signal row is written by
            # record_lawyer_plan_correction in the same transaction.  Event
            # detail persistence still synchronizes task heads below.
            assert isinstance(event.payload, LawyerPlanCorrectionPayload)
        elif event.event_type is AgentEventType.TASK_RESULT_RECORDED:
            assert isinstance(event.payload, TaskResultPayload)
            receipt = event.payload.receipt
            connection.execute(
                """
                INSERT INTO case_agent_task_receipts (
                    receipt_id, run_id, attempt_id, task_id, firm_id, matter_id,
                    input_hash, adapter_id, adapter_version, result_status,
                    external_submission_state, output_hash, error_code,
                    external_request_id, runtime_seconds, cost_minor_units,
                    external_calls, event_sequence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    receipt.receipt_id, event.run_id, receipt.attempt_id,
                    receipt.task_id, event.firm_id, event.matter_id, receipt.input_hash,
                    receipt.adapter_id, receipt.adapter_version, receipt.status.value,
                    receipt.external_submission_state.value, receipt.output_hash,
                    receipt.error_code, receipt.external_request_id,
                    receipt.runtime_seconds, receipt.cost_minor_units,
                    receipt.external_calls, event.sequence,
                ),
            )
            for artifact in receipt.artifacts:
                connection.execute(
                    """
                    INSERT INTO case_agent_artifacts (
                        artifact_id, run_id, receipt_id, firm_id, matter_id,
                        artifact_kind, content_hash, byte_size, source_input_hash,
                        managed_derivative
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        artifact.artifact_id, event.run_id, receipt.receipt_id,
                        event.firm_id, event.matter_id, artifact.artifact_kind,
                        artifact.content_hash, artifact.byte_size,
                        artifact.source_input_hash, artifact.managed_derivative,
                    ),
                )
            attempt_status = (
                "UNKNOWN" if receipt.status is ResultStatus.UNKNOWN
                else "SUCCEEDED" if receipt.status is ResultStatus.SUCCEEDED
                else "FAILED"
            )
            updated = connection.execute(
                """
                UPDATE case_agent_task_attempts
                SET attempt_version = attempt_version + 1, status = %s,
                    external_request_id = COALESCE(%s, external_request_id),
                    external_submission_state = %s, finished_event_sequence = %s,
                    updated_at = now()
                WHERE attempt_id = %s AND run_id = %s AND firm_id = %s AND matter_id = %s
                  AND status IN ('RUNNING', 'RECONCILING', 'UNKNOWN')
                """,
                (
                    attempt_status, receipt.external_request_id,
                    receipt.external_submission_state.value, event.sequence,
                    receipt.attempt_id, event.run_id, event.firm_id, event.matter_id,
                ),
            )
            if updated.rowcount != 1:
                raise CaseLedgerPersistenceBlocked(
                    "Agent task attempt projection differs from its result event"
                )
        elif event.event_type is AgentEventType.RUN_COMPLETED:
            assert isinstance(event.payload, RunCompletedPayload)
            approval = event.payload.final_review
            connection.execute(
                """
                INSERT INTO case_agent_approvals (
                    approval_id, run_id, graph_id, task_id, firm_id, matter_id,
                    approval_kind, task_input_hash, graph_hash, gate,
                    verification_hash, artifact_manifest_hash, approved_by,
                    approval_hash, event_sequence, approved_at
                ) VALUES (%s,%s,%s,NULL,%s,%s,'FINAL_REVIEW',NULL,%s,NULL,%s,%s,%s,%s,%s,%s)
                """,
                (
                    approval.approval_id, event.run_id, _require_graph_id(next_state),
                    event.firm_id, event.matter_id, approval.graph_hash,
                    approval.verification_hash, approval.artifact_manifest_hash,
                    approval.approved_by, approval.approval_hash, event.sequence,
                    event.occurred_at,
                ),
            )
        self._synchronize_task_heads(
            connection, event=event, previous_state=previous_state, state=next_state
        )

    def _synchronize_task_heads(
        self,
        connection: Any,
        *,
        event: AgentSupervisorEvent,
        previous_state: AgentRunState,
        state: AgentRunState,
    ) -> None:
        if event.event_type is AgentEventType.TASK_GRAPH_ACCEPTED:
            return
        previous = {item.spec.task_id: item for item in previous_state.tasks}
        for runtime in state.tasks:
            old = previous.get(runtime.spec.task_id)
            if old is None or old == runtime:
                continue
            connection.execute(
                """
                UPDATE case_agent_task_heads
                SET status = %s, attempt_count = %s, active_attempt_id = %s,
                    head_event_version = %s, updated_at = now()
                WHERE run_id = %s AND task_id = %s AND firm_id = %s AND matter_id = %s
                  AND graph_id = %s AND is_current AND head_event_version < %s
                """,
                (
                    runtime.status.value, runtime.attempt_count,
                    runtime.active_attempt_id, event.sequence, event.run_id,
                    runtime.spec.task_id, event.firm_id, event.matter_id,
                    _require_graph_id(state),
                    event.sequence,
                ),
            )

    def _update_projection(
        self, connection: Any, *, previous_state: AgentRunState, state: AgentRunState
    ) -> None:
        projection_hash = _payload_hash(_state_json(state))
        graph = state.graph
        updated = connection.execute(
            """
            UPDATE case_agent_runs
            SET status = %s, current_event_version = %s,
                snapshot_matter_version = %s, snapshot_schema_version = %s,
                snapshot_hash = %s, current_graph_id = %s,
                current_graph_version = %s, current_graph_hash = %s,
                projection_hash = %s, paused_from = %s, is_stale = %s,
                is_cancelled = %s, verification_hash = %s, failure_code = %s,
                updated_at = now()
            WHERE run_id = %s AND firm_id = %s AND matter_id = %s
              AND current_event_version = %s
            """,
            (
                state.status.value, state.event_version, state.snapshot.matter_version,
                state.snapshot.schema_version, state.snapshot.snapshot_hash,
                graph.graph_id if graph else None,
                graph.graph_version if graph else None,
                graph.graph_hash if graph else None,
                projection_hash,
                state.paused_from.value if state.paused_from else None,
                state.stale, state.cancelled, state.verification_hash,
                state.failure_code, state.run_id, state.firm_id, state.matter_id,
                previous_state.event_version,
            ),
        )
        if updated.rowcount != 1:
            raise VersionConflict("Agent run projection changed before event persistence")

    def _insert_event(self, connection: Any, event: AgentSupervisorEvent) -> None:
        payload = _event_json(event)
        connection.execute(
            """
            INSERT INTO case_agent_events (
                event_id, run_id, firm_id, matter_id, event_sequence,
                event_type, actor_id, payload, event_hash, occurred_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                event.event_id, event.run_id, event.firm_id, event.matter_id,
                event.sequence, event.event_type.value, event.actor_id,
                Jsonb(payload["payload"]) if payload["payload"] is not None else None,
                _payload_hash(payload), event.occurred_at,
            ),
        )

    def _insert_checkpoint(
        self,
        connection: Any,
        state: AgentRunState,
        projection: dict[str, Any],
        projection_hash: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO case_agent_checkpoints (
                run_id, firm_id, matter_id, event_version, projection_hash, projection
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                state.run_id, state.firm_id, state.matter_id, state.event_version,
                projection_hash, Jsonb(projection),
            ),
        )

    def _load_events(
        self, connection: Any, *, actor: Actor, matter_id: str, run_id: str
    ) -> tuple[AgentSupervisorEvent, ...]:
        rows = connection.execute(
            """
            SELECT event_id, run_id, firm_id, matter_id, event_sequence,
                   event_type, actor_id, payload, event_hash, occurred_at
            FROM case_agent_events
            WHERE run_id = %s AND matter_id = %s AND firm_id = %s
            ORDER BY event_sequence ASC
            """,
            (run_id, matter_id, actor.firm_id),
        ).fetchall()
        if not rows:
            raise KeyError(run_id)
        events: list[AgentSupervisorEvent] = []
        for row in rows:
            event = _event_from_row(row)
            if row["event_hash"] != _payload_hash(_event_json(event)):
                raise CaseLedgerPersistenceBlocked("Agent event hash differs from its contents")
            events.append(event)
        return tuple(events)

    def _replay_locked(
        self, connection: Any, actor: Actor, matter_id: str, run_id: str
    ) -> AgentRunState:
        row = connection.execute(
            """
            SELECT run.current_event_version,
                   EXISTS (
                       SELECT 1
                       FROM matter_actor_roles role
                       JOIN users principal
                         ON principal.user_id = role.user_id
                        AND principal.firm_id = role.firm_id
                       WHERE role.matter_id = run.matter_id
                         AND role.firm_id = run.firm_id
                         AND role.user_id = %s AND role.revoked_at IS NULL
                         AND role.role = ANY(%s) AND principal.status = 'ACTIVE'
                   ) AS permitted
            FROM case_agent_runs run
            WHERE run.run_id = %s AND run.matter_id = %s AND run.firm_id = %s
            FOR UPDATE
            """,
            (
                actor.actor_id, [role.value for role in self._READ_ROLES],
                run_id, matter_id, actor.firm_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if not row["permitted"]:
            raise PermissionError("actor lacks an active database role for this Agent run")
        return replay_agent_events(
            self._load_events(
                connection, actor=actor, matter_id=matter_id, run_id=run_id
            )
        )

    def _authorize_current_snapshot(
        self,
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        state: AgentRunState,
        allowed_roles: frozenset[Role],
        proposed_event: AgentSupervisorEvent | None = None,
    ) -> None:
        # Cancelling an obsolete run is a lifecycle containment action, not a
        # new assertion about the old case snapshot.  Requiring the run's
        # historical matter version here makes exactly the runs that most need
        # cancellation impossible to close after the case ledger advances.
        # Keep tenant, active-user and matter-role authorization, while never
        # treating cancellation as authority to execute against current data.
        if (
            proposed_event is not None
            and proposed_event.event_type is AgentEventType.RUN_CANCELLED
        ):
            _authorize_matter_read(
                connection,
                actor=actor,
                matter_id=matter_id,
                allowed_roles=allowed_roles,
            )
            return
        expected = state.snapshot.matter_version
        if (
            proposed_event is not None
            and proposed_event.event_type is AgentEventType.CASE_SNAPSHOT_CHANGED
            and isinstance(proposed_event.payload, SnapshotChangedPayload)
        ):
            expected = proposed_event.payload.snapshot.matter_version
        _authorize_and_lock_matter(
            connection,
            actor=actor,
            matter_id=matter_id,
            expected_version=expected,
            allowed_roles=allowed_roles,
        )

    def _authorize_verifier_current_snapshot(
        self,
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        state: AgentRunState,
    ) -> None:
        """Lock one exact snapshot through the verifier-only DB authority.

        PostgreSQL requires UPDATE privilege for ``SELECT ... FOR UPDATE``.
        The independent verifier deliberately has no direct matter UPDATE, so
        migration 0060 exposes only this transaction-bound authorization and
        lock operation under the managed schema owner.
        """

        row = connection.execute(
            """
            SELECT public.authorize_case_agent_verification_snapshot(
                %s, %s, %s, %s
            ) AS matter_version
            """,
            (
                actor.firm_id,
                matter_id,
                actor.actor_id,
                state.snapshot.matter_version,
            ),
        ).fetchone()
        if row is None or type(row["matter_version"]) is not int:
            raise CaseLedgerPersistenceBlocked(
                "verifier snapshot authority returned an invalid result"
            )
        if row["matter_version"] != state.snapshot.matter_version:
            raise VersionConflict(
                "Agent verification snapshot is no longer current"
            )

    def _roles_for_event(self, event_type: AgentEventType) -> frozenset[Role]:
        if event_type in self._WORKER_EVENTS:
            return self._WORKER_ROLES
        if event_type in self._HUMAN_EVENTS:
            return self._HUMAN_ROLES
        if event_type in self._APPROVAL_EVENTS:
            return self._APPROVER_ROLES
        raise PermissionError("this Agent event cannot be appended through the control-plane API")

    def _validate_actor_class(
        self, actor: Actor, allowed_roles: frozenset[Role]
    ) -> None:
        if allowed_roles == self._WORKER_ROLES:
            self._require_worker(actor)
        else:
            self._require_human(actor, allowed_roles)

    @staticmethod
    def _require_worker(actor: Actor) -> None:
        if actor.roles != frozenset({Role.SYSTEM_WORKER}):
            raise PermissionError("Agent execution requires a dedicated SYSTEM_WORKER identity")

    @staticmethod
    def _require_human(actor: Actor, allowed_roles: frozenset[Role]) -> None:
        if Role.SYSTEM_WORKER in actor.roles:
            raise PermissionError("SYSTEM_WORKER cannot create goals or grant lawyer approval")
        _require_roles(actor, allowed_roles)

    def _prior_receipt(
        self,
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        command_name: str,
        idempotency_key: str,
        request_hash: str,
    ) -> AgentControlCommandReceipt | None:
        row = connection.execute(
            """
            SELECT request_hash, response_json
            FROM command_idempotency
            WHERE firm_id = %s AND matter_id = %s AND actor_id = %s
              AND command_name = %s AND idempotency_key = %s
            """,
            (
                actor.firm_id, matter_id, actor.actor_id, command_name,
                idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key was reused with different Agent input")
        return AgentControlCommandReceipt(**row["response_json"])

    def _finish_control_command(
        self,
        connection: Any,
        *,
        actor: Actor,
        matter_id: str,
        command_name: str,
        idempotency_key: str,
        request_hash: str,
        event: AgentSupervisorEvent,
        status: AgentRunStatus,
        receipt_status: str | None = None,
    ) -> AgentControlCommandReceipt:
        receipt = AgentControlCommandReceipt(
            command_name=command_name,
            idempotency_key=idempotency_key,
            matter_id=matter_id,
            run_id=event.run_id,
            event_version=event.sequence,
            event_id=event.event_id,
            status=receipt_status or status.value,
        )
        audit_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO case_agent_command_audits (
                audit_id, run_id, firm_id, matter_id, actor_id, command_name,
                request_hash, input_event_version, output_event_version,
                event_id, payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                audit_id, event.run_id, actor.firm_id, matter_id, actor.actor_id,
                command_name, request_hash, event.sequence - 1, event.sequence,
                event.event_id,
                Jsonb(
                    {
                        "version_domain": "AGENT_EVENT_SEQUENCE",
                        "run_id": event.run_id,
                        "event_id": event.event_id,
                        "event_hash": _payload_hash(_event_json(event)),
                        "status": status.value,
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_events (
                firm_id, matter_id, aggregate_version, event_type, payload
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            (
                actor.firm_id, matter_id, event.sequence,
                f"CASE_AGENT_{event.event_type.value}",
                Jsonb(
                    {"run_id": event.run_id, "event_id": event.event_id,
                     "agent_audit_id": audit_id}
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO command_idempotency (
                firm_id, matter_id, actor_id, command_name, idempotency_key,
                request_hash, response_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                actor.firm_id, matter_id, actor.actor_id, command_name,
                idempotency_key, request_hash, Jsonb(asdict(receipt)),
            ),
        )
        return receipt

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _actor_transaction(self, actor: Actor) -> Iterator[Any]:
        """Set tenant and exact service principal for DB-enforced verifier guards."""

        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (actor.firm_id,),
            )
            connection.execute(
                "SELECT set_config('app.actor_id', %s, true)",
                (actor.actor_id,),
            )
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[Any]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _task_from_state(state: AgentRunState, task_id: str) -> AgentTaskSpec:
    for runtime in state.tasks:
        if runtime.spec.task_id == task_id:
            return runtime.spec
    raise AgentSupervisorBlocked("dispatcher selected a task outside the current graph")


def _safe_runtime_identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$", value
    ) is not None


def _safe_semver(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$",
        value,
    ) is not None


def _planning_claim_from_row(
    row: dict[str, Any], *, run_id: str, event_version: int
) -> ClaimedAgentPlanningAttempt:
    lease_expires_at = row["lease_expires_at"]
    if (
        not isinstance(lease_expires_at, datetime)
        or lease_expires_at.tzinfo is None
        or lease_expires_at.utcoffset() is None
    ):
        raise CaseLedgerPersistenceBlocked("planning lease clock is invalid")
    recovered = row.get("structured_proposal")
    if recovered is not None and not isinstance(recovered, dict):
        raise CaseLedgerPersistenceBlocked("stored planning proposal is invalid")
    return ClaimedAgentPlanningAttempt(
        run_id=run_id,
        planning_attempt_id=str(row["planning_attempt_id"]),
        external_request_id=str(row["external_request_id"]),
        event_version=event_version,
        matter_version=int(row["matter_version"]),
        planning_kind=str(row["planning_kind"]),
        planning_hash=str(row["planning_hash"]),
        lease_owner=str(row["lease_owner"]),
        lease_token=str(row["lease_token"]),
        lease_expires_at=lease_expires_at,
        attempt_version=int(row["attempt_version"]),
        recovered_proposal=recovered,
        request_hash=(
            None if row.get("request_hash") is None else str(row["request_hash"])
        ),
        external_ledger_status=(
            None
            if row.get("external_ledger_status") is None
            else str(row["external_ledger_status"])
        ),
        external_ledger_version=(
            None
            if row.get("external_ledger_version") is None
            else int(row["external_ledger_version"])
        ),
    )


def _planner_error_code(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._:-" else "_"
        for character in value.strip().upper()
    )[:180]
    if not normalized or not normalized[0].isalpha():
        normalized = f"ERROR_{normalized}"[:180]
    return normalized if normalized.startswith("PLANNER_") else f"PLANNER_{normalized}"


def _require_active_worker_principal(connection: Any, actor: Actor) -> None:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM users
            WHERE user_id = %s AND firm_id = %s AND status = 'ACTIVE'
        ) AS active
        """,
        (actor.actor_id, actor.firm_id),
    ).fetchone()
    if row is None or not row["active"]:
        raise PermissionError("Agent worker principal is not active in this firm")


def _task_approval_id(state: AgentRunState, task_id: str) -> str | None:
    if state.graph is None:
        return None
    task = _task_from_state(state, task_id)
    for approval in reversed(state.approvals):
        if (
            approval.task_id == task_id
            and approval.task_input_hash == task.input_hash
            and approval.graph_hash == state.graph.graph_hash
            and approval.gate is task.approval_gate
        ):
            return approval.approval_id
    return None


def _require_graph_id(state: AgentRunState) -> str:
    if state.graph is None:
        raise AgentSupervisorBlocked("Agent event requires a current task graph")
    return state.graph.graph_id


def _task_insert_params(
    event: AgentSupervisorEvent, graph: AgentTaskGraph, task: AgentTaskSpec
) -> tuple[Any, ...]:
    return (
        graph.graph_id, task.task_id, event.run_id, event.firm_id, event.matter_id,
        task.sequence, task.title, task.purpose, task.rationale,
        Jsonb(list(task.input_refs)), task.input_hash, task.skill.skill_id,
        task.skill.skill_version, task.skill.tool_id, task.skill.tool_version,
        task.skill.adapter_id, task.skill.adapter_version,
        Jsonb(sorted(scope.value for scope in task.granted_scopes)),
        task.capability.execution_mode.value, task.capability.network_policy.value,
        Jsonb(list(task.capability.allowed_domains)), task.capability.sandbox_profile,
        task.capability.sandbox_policy_version, task.capability.sandbox_policy_hash,
        Jsonb(list(task.capability.reads_case_objects)),
        task.capability.writes_managed_derivatives,
        task.capability.external_request_approval_required, task.risk_level.value,
        task.autonomy_level.value, task.approval_gate.value, task.retry_mode.value,
        Jsonb(asdict(task.budget)),
    )


def _assert_final_document_versions(connection: Any, *, actor: Actor, state: AgentRunState,
                                    approval: RunFinalReviewApproval) -> None:
    """Inside the event transaction, after run/matter locks and before writes.

    Revision request and PASSED-receipt guards use the same root-chain lock.
    Lock all roots in stable order, then re-read their latest verified versions.
    An idempotent completion replay returns before this gate.
    """
    from hashlib import sha256

    document_ids = {item.artifact_id for item in state.artifacts
                    if item.artifact_kind == "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"}
    versions = dict(approval.document_review_versions)
    if set(versions) != document_ids:
        raise CaseLedgerPersistenceBlocked("final review requires the exact document version set")
    if not document_ids:
        return
    from .case_agent_document_revisions import current_document_result_relation, CaseAgentDocumentRevisionBlocked
    try:
        results = current_document_result_relation(connection)
    except CaseAgentDocumentRevisionBlocked as error:
        raise CaseLedgerPersistenceBlocked("final review document result capability is unavailable") from error
    connection.execute("SELECT set_config('app.actor_id', %s, true)", (actor.actor_id,))
    roots = connection.execute(
        """SELECT package_id, candidate_artifact_id FROM case_agent_reviewable_document_packages
           WHERE firm_id = %s AND matter_id = %s AND run_id = %s
             AND generation_mode = 'INITIAL_AGENT_TASK' AND revision_number = 1
             AND current_setting('transaction_isolation') = 'read committed'
             AND candidate_artifact_id = ANY(%s::uuid[]) ORDER BY package_id""",
        (actor.firm_id, state.matter_id, state.run_id, sorted(document_ids)),
    ).fetchall()
    if (len(roots) != len(document_ids)
            or {str(row["candidate_artifact_id"]) for row in roots} != document_ids):
        raise CaseLedgerPersistenceBlocked("final review document roots are unavailable")
    for root in sorted(roots, key=lambda row: str(row["package_id"])):
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"case-agent-document-revision:{actor.firm_id}:{root['package_id']}",))
    for root in roots:
        current = connection.execute(
            f"""SELECT package.package_id, package.package_receipt_hash,
                   EXISTS (SELECT 1 FROM case_agent_document_revision_requests request
                     WHERE request.root_package_id = %s AND request.predecessor_package_id = package.package_id
                       AND request.firm_id = package.firm_id AND request.matter_id = package.matter_id
                       AND request.run_id = package.run_id
                       AND NOT EXISTS (SELECT 1 FROM {results} receipt
                         WHERE receipt.request_id = request.request_id AND receipt.firm_id = request.firm_id
                           AND receipt.matter_id = request.matter_id AND receipt.outcome = 'FAILED')) AS pending_revision
               FROM case_agent_reviewable_document_packages package
               WHERE package.firm_id = %s AND package.matter_id = %s AND package.run_id = %s
                 AND (package.package_id = %s OR (package.root_package_id = %s
                   AND EXISTS (SELECT 1 FROM {results} receipt
                     WHERE receipt.successor_package_id = package.package_id
                       AND receipt.firm_id = package.firm_id AND receipt.matter_id = package.matter_id
                       AND receipt.outcome = 'PASSED'
                       AND receipt.successor_package_receipt_hash = package.package_receipt_hash)))
               ORDER BY package.revision_number DESC LIMIT 1""",
            (str(root["package_id"]), actor.firm_id, state.matter_id, state.run_id,
             str(root["package_id"]), str(root["package_id"])),
        ).fetchone()
        if current is None or current["pending_revision"] is not False:
            raise CaseLedgerPersistenceBlocked("document revision is unresolved before final review")
        observed = sha256(
            f"lawyer-document-review-v1:{current['package_id']}:{current['package_receipt_hash']}".encode("utf-8")
        ).hexdigest()
        if observed != versions[str(root["candidate_artifact_id"])]:
            raise VersionConflict("document changed before final review was committed")


def _event_json(event: AgentSupervisorEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "firm_id": event.firm_id,
        "matter_id": event.matter_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        # timestamptz is returned in the session zone; hash a canonical instant.
        "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(),
        "actor_id": event.actor_id,
        "payload": _payload_json(event.payload),
    }


def _active_plan_execution_json(
    value: ActivePlanExecutionRef | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "plan_id": value.plan_id,
        "plan_hash": value.plan_hash,
        "source_run_id": value.source_run_id,
        "items": [
            {
                "item_id": item.item_id,
                "item_hash": item.item_hash,
                "deliverable_kind": item.deliverable_kind.value,
                "output_format": item.output_format.value,
            }
            for item in value.items
        ],
    }


def _goal_json(goal: AgentGoal) -> dict[str, Any]:
    """Serialize v1 goals byte-for-byte compatibly and v2 goals explicitly."""

    value: dict[str, Any] = {
        "goal_id": goal.goal_id,
        "objective": goal.objective,
        "success_criteria": list(goal.success_criteria),
        "constraints": list(goal.constraints),
        "requested_by": goal.requested_by,
        "goal_hash": goal.goal_hash,
    }
    if goal.requested_deliverables or goal.active_plan_execution is not None:
        value["requested_deliverables"] = [
            item.value for item in goal.requested_deliverables
        ]
        value["active_plan_execution"] = _active_plan_execution_json(
            goal.active_plan_execution
        )
    if goal.material_read_refs:
        value["material_read_refs"] = list(goal.material_read_refs)
    return value


def _goal_from_json(value: object) -> AgentGoal:
    if not isinstance(value, dict):
        raise CaseLedgerPersistenceBlocked("Agent goal payload is invalid")
    legacy_keys = {
        "goal_id",
        "objective",
        "success_criteria",
        "constraints",
        "requested_by",
        "goal_hash",
    }
    v2_keys = legacy_keys | {"requested_deliverables", "active_plan_execution"}
    keys = set(value)
    if keys == legacy_keys | {"material_read_refs"}:
        refs = value["material_read_refs"]
        if not isinstance(refs, list) or not refs:
            raise CaseLedgerPersistenceBlocked("material reading scope payload is invalid")
        goal = AgentGoal.build(goal_id=value["goal_id"], objective=value["objective"],
            success_criteria=value["success_criteria"], constraints=value["constraints"],
            requested_by=value["requested_by"], material_read_refs=tuple(refs))
        if goal.goal_hash != value["goal_hash"]:
            raise CaseLedgerPersistenceBlocked("material reading goal hash differs")
        return goal
    if keys == legacy_keys:
        return AgentGoal(
            goal_id=value["goal_id"],
            objective=value["objective"],
            success_criteria=tuple(value["success_criteria"]),
            constraints=tuple(value["constraints"]),
            requested_by=value["requested_by"],
            goal_hash=value["goal_hash"],
        )
    _exact_keys(value, v2_keys, "Agent goal")
    raw_deliverables = value["requested_deliverables"]
    if not isinstance(raw_deliverables, list):
        raise CaseLedgerPersistenceBlocked(
            "Agent requested deliverables payload is invalid"
        )
    try:
        requested_deliverables = tuple(
            AgentDeliverableKind(item) for item in raw_deliverables
        )
    except (TypeError, ValueError) as error:
        raise CaseLedgerPersistenceBlocked(
            "Agent requested deliverables payload is unsupported"
        ) from error
    raw_execution = value["active_plan_execution"]
    execution: ActivePlanExecutionRef | None = None
    if raw_execution is not None:
        if not isinstance(raw_execution, dict):
            raise CaseLedgerPersistenceBlocked(
                "Agent active-plan execution payload is invalid"
            )
        _exact_keys(
            raw_execution,
            {"plan_id", "plan_hash", "source_run_id", "items"},
            "Agent active-plan execution",
        )
        raw_items = raw_execution["items"]
        if not isinstance(raw_items, list):
            raise CaseLedgerPersistenceBlocked(
                "Agent active-plan deliverable payload is invalid"
            )
        items: list[ActivePlanDeliverableRef] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise CaseLedgerPersistenceBlocked(
                    "Agent active-plan deliverable item is invalid"
                )
            _exact_keys(
                raw_item,
                {"item_id", "item_hash", "deliverable_kind", "output_format"},
                "Agent active-plan deliverable item",
            )
            try:
                items.append(
                    ActivePlanDeliverableRef(
                        item_id=raw_item["item_id"],
                        item_hash=raw_item["item_hash"],
                        deliverable_kind=AgentDeliverableKind(
                            raw_item["deliverable_kind"]
                        ),
                        output_format=AgentDeliverableFormat(raw_item["output_format"]),
                    )
                )
            except (TypeError, ValueError) as error:
                raise CaseLedgerPersistenceBlocked(
                    "Agent active-plan deliverable item is unsupported"
                ) from error
        execution = ActivePlanExecutionRef(
            plan_id=raw_execution["plan_id"],
            plan_hash=raw_execution["plan_hash"],
            source_run_id=raw_execution["source_run_id"],
            items=tuple(items),
        )
    return AgentGoal(
        goal_id=value["goal_id"],
        objective=value["objective"],
        success_criteria=tuple(value["success_criteria"]),
        constraints=tuple(value["constraints"]),
        requested_by=value["requested_by"],
        goal_hash=value["goal_hash"],
        requested_deliverables=requested_deliverables,
        active_plan_execution=execution,
    )


def _assert_retained_planning_budget_review(
    connection: Any, *, actor: Actor, state: AgentRunState, payload: PlanningBudgetReviewPayload
) -> object:
    """Bind the proposed event to the latest immutable successful model proposal.

    The caller owns run/matter locks and current-case authorization. This is
    not a public write entrypoint or a replacement for compiler admission.
    """
    from .case_agent_planner import parse_case_plan_proposal, case_plan_proposal_payload

    _require_roles(actor, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
    if (actor.firm_id != state.firm_id or actor.actor_id != payload.approved_by
            or payload.snapshot != state.snapshot):
        raise CaseLedgerPersistenceBlocked("planning budget review identity or snapshot differs")
    row = connection.execute(
        """
        SELECT attempt.status AS attempt_status, attempt.matter_version, attempt.planning_hash,
               attempt.external_request_id, outcome.external_request_id AS outcome_request_id,
               outcome.status AS outcome_status, outcome.request_hash, outcome.input_hash,
               outcome.structured_proposal
        FROM case_agent_planning_attempts attempt
        LEFT JOIN LATERAL (
            SELECT external_request_id, status, request_hash, input_hash, structured_proposal
            FROM case_agent_planning_external_events external
            WHERE external.planning_attempt_id = attempt.planning_attempt_id
              AND external.run_id = attempt.run_id AND external.firm_id = attempt.firm_id
              AND external.matter_id = attempt.matter_id
            ORDER BY ledger_version DESC LIMIT 1
        ) outcome ON true
        WHERE attempt.run_id = %s AND attempt.firm_id = %s AND attempt.matter_id = %s
        ORDER BY attempt.created_at DESC, attempt.planning_attempt_id DESC
        LIMIT 1 FOR UPDATE OF attempt
        """,
        (state.run_id, state.firm_id, state.matter_id),
    ).fetchone()
    if (row is None or row["attempt_status"] != "SUCCEEDED" or row["outcome_status"] != "SUCCEEDED"
            or row["matter_version"] != state.snapshot.matter_version
            or row["planning_hash"] != payload.planning_hash or row["input_hash"] != payload.planning_hash
            or row["request_hash"] != payload.request_hash
            or row["external_request_id"] != row["outcome_request_id"]
            or not isinstance(row["structured_proposal"], dict)):
        raise CaseLedgerPersistenceBlocked("planning budget review requires the exact successful proposal")
    proposal = row["structured_proposal"]
    parsed = parse_case_plan_proposal(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        expected_goal_hash=state.goal.goal_hash, expected_snapshot_hash=payload.planning_hash,
    )
    canonical = case_plan_proposal_payload(parsed)
    if (_payload_hash(canonical) != _payload_hash(proposal)
            or _payload_hash(canonical) != payload.proposal_hash):
        raise CaseLedgerPersistenceBlocked("planning budget review proposal fingerprint differs")
    return parsed


def _assert_retained_material_scope_review(
    connection: Any, *, actor: Actor, state: AgentRunState,
    payload: PlanningMaterialScopeReviewPayload, compiler: object, planning_snapshot: object,
) -> object:
    """Read/compile guard only; caller must hold current run and matter locks.

    Not a write entrypoint. The dedicated SQL guard and retained-only Worker
    consumption must exist before this review can be persisted.
    """
    from .case_agent_planner import (CaseAgentPlannerCompiler, CasePlanningSnapshot,
        prepare_retained_material_review)

    if not isinstance(compiler, CaseAgentPlannerCompiler) or not isinstance(planning_snapshot, CasePlanningSnapshot):
        raise CaseLedgerPersistenceBlocked("material scope review requires server compilation")
    if (state.status is not AgentRunStatus.WAITING_INPUT or state.graph is not None
            or state.tasks or state.stale or state.cancelled
            or state.failure_code != "PLANNER_PROPOSAL_REJECTED"
            or payload.original_goal_hash != state.goal.goal_hash
            or payload.previous_output_bytes != state.budget.max_output_bytes
            or planning_snapshot.case_snapshot != state.snapshot
            or planning_snapshot.planning_hash != payload.planning_hash):
        raise CaseLedgerPersistenceBlocked("material scope review original state differs")
    # Reuse the existing locked latest-success identity/source proof, not its
    # runtime-extension admission or event writer.
    binding = PlanningBudgetReviewPayload(payload.snapshot, state.budget.max_runtime_seconds,
        state.budget.max_runtime_seconds, payload.request_hash, payload.planning_hash,
        payload.original_proposal_hash, payload.approved_by)
    proposal = _assert_retained_planning_budget_review(connection, actor=actor, state=state, payload=binding)
    candidate = prepare_retained_material_review(compiler=compiler, original_goal=state.goal,
        snapshot=planning_snapshot, retained_proposal=proposal,
        expected_proposal_hash=payload.original_proposal_hash, material_read_refs=payload.material_read_refs,
        run_budget=state.budget, graph_id=state.run_id, output_ceiling_bytes=payload.approved_output_bytes)
    if (candidate.effective_goal.goal_hash != payload.effective_goal_hash
            or candidate.derived_proposal_hash != payload.derived_proposal_hash
            or candidate.graph.graph_hash != payload.compiled_graph_hash
            or candidate.effective_budget.max_output_bytes != payload.approved_output_bytes):
        raise CaseLedgerPersistenceBlocked("material scope review differs from exact server compilation")
    return candidate


def _compile_budget_review_before_write(
    *, state: AgentRunState, payload: PlanningBudgetReviewPayload,
    proposal: object, compilation: tuple[object, object] | None,
) -> None:
    from dataclasses import replace
    from .case_agent_planner import CaseAgentPlannerCompiler, CasePlanningSnapshot

    if not isinstance(compilation, tuple) or len(compilation) != 2:
        raise CaseLedgerPersistenceBlocked("budget review requires server compilation")
    compiler, snapshot = compilation
    if not isinstance(compiler, CaseAgentPlannerCompiler) or not isinstance(snapshot, CasePlanningSnapshot):
        raise CaseLedgerPersistenceBlocked("budget review compilation contract is invalid")
    snapshot.validate()
    if snapshot.case_snapshot != state.snapshot or snapshot.planning_hash != payload.planning_hash:
        raise CaseLedgerPersistenceBlocked("budget review compilation sources changed")
    compiler.compile(graph_id=str(uuid5(UUID(state.run_id), "budget-review-admission")), graph_version=1,
        goal=state.goal, snapshot=snapshot, proposal=proposal,
        run_budget=replace(state.budget, max_runtime_seconds=payload.approved_runtime_seconds))


def _payload_json(payload: object) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, RunCreatedPayload):
        return {
            "goal": _goal_json(payload.goal),
            "snapshot": asdict(payload.snapshot),
            "budget": asdict(payload.budget),
            **({"task_failure_policy": payload.task_failure_policy}
               if payload.task_failure_policy != "STOP_ON_TASK_FAILURE_V1" else {}),
        }
    if isinstance(payload, TaskGraphPayload):
        return {"graph": _graph_json(payload.graph)}
    if isinstance(payload, ApprovalPayload):
        value = asdict(payload.approval)
        value["gate"] = payload.approval.gate.value
        return {"approval": value}
    if isinstance(payload, LawyerPlanCorrectionPayload):
        return asdict(payload)
    if isinstance(payload, TaskStartedPayload):
        return asdict(payload)
    if isinstance(payload, TaskResultPayload):
        return {"receipt": _receipt_json(payload.receipt)}
    if isinstance(payload, SnapshotChangedPayload):
        return {"snapshot": asdict(payload.snapshot)}
    if isinstance(payload, VerificationPayload):
        return asdict(payload)
    if isinstance(payload, PlanningFailurePayload):
        return asdict(payload)
    if isinstance(payload, (PlanningBudgetReviewPayload, PlanningMaterialScopeReviewPayload,
                            SupplementaryMaterialStageReviewPayload, CaseAnalysisStageReviewPayload)):
        return asdict(payload)
    if isinstance(payload, RunCompletedPayload):
        approval = asdict(payload.final_review)
        if not payload.final_review.document_review_versions:
            approval.pop("document_review_versions")
        return {"final_review": approval}
    raise TypeError("unsupported Agent supervisor event payload")


def _graph_json(graph: AgentTaskGraph) -> dict[str, Any]:
    return {
        "graph_id": graph.graph_id,
        "graph_version": graph.graph_version,
        "goal_hash": graph.goal_hash,
        "snapshot": asdict(graph.snapshot),
        "tasks": [_task_json(task) for task in graph.tasks],
        "graph_hash": graph.graph_hash,
    }


def _task_json(task: AgentTaskSpec) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "sequence": task.sequence,
        "title": task.title,
        "purpose": task.purpose,
        "rationale": task.rationale,
        "dependency_ids": list(task.dependency_ids),
        "input_refs": list(task.input_refs),
        "input_hash": task.input_hash,
        "skill": asdict(task.skill),
        "granted_scopes": sorted(scope.value for scope in task.granted_scopes),
        "capability": {
            **asdict(task.capability),
            "execution_mode": task.capability.execution_mode.value,
            "network_policy": task.capability.network_policy.value,
            "allowed_domains": list(task.capability.allowed_domains),
            "reads_case_objects": list(task.capability.reads_case_objects),
        },
        "risk_level": task.risk_level.value,
        "autonomy_level": task.autonomy_level.value,
        "approval_gate": task.approval_gate.value,
        "retry_mode": task.retry_mode.value,
        "budget": asdict(task.budget),
    }


def _receipt_json(receipt: TaskResultReceipt) -> dict[str, Any]:
    return {
        **asdict(receipt),
        "status": receipt.status.value,
        "external_submission_state": receipt.external_submission_state.value,
        "artifacts": [asdict(item) for item in receipt.artifacts],
    }


def _state_json(state: AgentRunState) -> dict[str, Any]:
    """Exact deterministic checkpoint; events remain the source of truth."""

    return {
        "run_id": state.run_id,
        "firm_id": state.firm_id,
        "matter_id": state.matter_id,
        "event_version": state.event_version,
        "goal": _goal_json(state.goal),
        "snapshot": asdict(state.snapshot),
        "budget": asdict(state.budget),
        "status": state.status.value,
        "graph": _graph_json(state.graph) if state.graph else None,
        "tasks": [
            {
                "spec": _task_json(item.spec),
                "status": item.status.value,
                "attempt_count": item.attempt_count,
                "active_attempt_id": item.active_attempt_id,
                "receipts": [_receipt_json(receipt) for receipt in item.receipts],
            }
            for item in state.tasks
        ],
        "approvals": [
            {**asdict(item), "gate": item.gate.value} for item in state.approvals
        ],
        "artifacts": [asdict(item) for item in state.artifacts],
        "budget_usage": asdict(state.budget_usage),
        "paused_from": state.paused_from.value if state.paused_from else None,
        "stale": state.stale,
        "cancelled": state.cancelled,
        "verification_hash": state.verification_hash,
        "failure_code": state.failure_code,
        **({"material_stage": asdict(state.material_stage)} if state.material_stage is not None else {}),
        **({"analysis_stage": asdict(state.analysis_stage)} if state.analysis_stage is not None else {}),
    }


def _event_from_row(row: dict[str, Any]) -> AgentSupervisorEvent:
    event_type = AgentEventType(row["event_type"])
    payload = row["payload"]
    return AgentSupervisorEvent(
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        firm_id=str(row["firm_id"]),
        matter_id=str(row["matter_id"]),
        sequence=int(row["event_sequence"]),
        event_type=event_type,
        occurred_at=row["occurred_at"],
        actor_id=str(row["actor_id"]),
        payload=_payload_from_json(event_type, payload),
    )


def _payload_from_json(
    event_type: AgentEventType, payload: dict[str, Any] | None
) -> object:
    if payload is None:
        return None
    if event_type is AgentEventType.RUN_CREATED:
        keys = {"goal", "snapshot", "budget"}
        if "task_failure_policy" in payload:
            keys.add("task_failure_policy")
        _exact_keys(payload, keys, "run-created payload")
        _exact_keys(payload["snapshot"], _SNAPSHOT_KEYS, "case snapshot")
        _exact_keys(payload["budget"], _RUN_BUDGET_KEYS, "run budget")
        return RunCreatedPayload(
            goal=_goal_from_json(payload["goal"]),
            snapshot=CaseSnapshotRef(**payload["snapshot"]),
            budget=RunResourceBudget(**payload["budget"]),
            task_failure_policy=payload.get("task_failure_policy", "STOP_ON_TASK_FAILURE_V1"),
        )
    if event_type is AgentEventType.TASK_GRAPH_ACCEPTED:
        _exact_keys(payload, {"graph"}, "task-graph payload")
        return TaskGraphPayload(graph=_graph_from_json(payload["graph"]))
    if event_type is AgentEventType.APPROVAL_GRANTED:
        _exact_keys(payload, {"approval"}, "approval payload")
        value = payload["approval"]
        _exact_keys(
            value,
            {"approval_id", "task_id", "task_input_hash", "graph_hash", "gate", "approved_by", "approval_hash"},
            "task approval",
        )
        return ApprovalPayload(
            approval=ApprovalRecord(
                **{**value, "gate": ApprovalGate(value["gate"])}
            )
        )
    if event_type is AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED:
        _exact_keys(
            payload,
            {"signal_id", "task_id", "decision_hash", "subject_hash", "decision_code"},
            "lawyer correction payload",
        )
        return LawyerPlanCorrectionPayload(**payload)
    if event_type is AgentEventType.TASK_STARTED:
        _exact_keys(payload, {"task_id", "attempt_id", "graph_hash", "input_hash"}, "task-start payload")
        return TaskStartedPayload(**payload)
    if event_type is AgentEventType.TASK_RESULT_RECORDED:
        _exact_keys(payload, {"receipt"}, "task-result payload")
        return TaskResultPayload(receipt=_receipt_from_json(payload["receipt"]))
    if event_type is AgentEventType.CASE_SNAPSHOT_CHANGED:
        _exact_keys(payload, {"snapshot"}, "snapshot-change payload")
        _exact_keys(payload["snapshot"], _SNAPSHOT_KEYS, "case snapshot")
        return SnapshotChangedPayload(snapshot=CaseSnapshotRef(**payload["snapshot"]))
    if event_type in {
        AgentEventType.VERIFICATION_PASSED,
        AgentEventType.VERIFICATION_FAILED,
    }:
        _exact_keys(payload, {"verification_hash", "error_code"}, "verification payload")
        return VerificationPayload(**payload)
    if event_type is AgentEventType.PLANNING_FAILED:
        _exact_keys(payload, {"error_code"}, "planning-failure payload")
        return PlanningFailurePayload(**payload)
    if event_type is AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED:
        _exact_keys(payload, {"snapshot", "original_goal_hash", "original_proposal_hash", "request_hash",
            "planning_hash", "material_read_refs", "previous_output_bytes", "approved_output_bytes",
            "effective_goal_hash", "derived_proposal_hash", "compiled_graph_hash", "approved_by"},
            "material scope review")
        _exact_keys(payload["snapshot"], _SNAPSHOT_KEYS, "material scope snapshot")
        refs = payload["material_read_refs"]
        if not isinstance(refs, (list, tuple)):
            raise CaseLedgerPersistenceBlocked("material scope reference payload is invalid")
        return PlanningMaterialScopeReviewPayload(**{**payload,
            "snapshot": CaseSnapshotRef(**payload["snapshot"]), "material_read_refs": tuple(refs)})
    if event_type is AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED:
        from .case_agent_material_stage import SupplementaryMaterialStage
        _exact_keys(payload, {"stage"}, "supplementary material review")
        value = payload["stage"]
        _exact_keys(value, {"run_id", "expected_event_version", "snapshot_hash", "previous_graph_hash",
            "coverage_hash", "page_refs", "previous_budget", "proposed_budget", "approved_by", "stage_hash"},
            "supplementary material stage")
        if not isinstance(value["page_refs"], (list, tuple)):
            raise CaseLedgerPersistenceBlocked("supplementary source scope is invalid")
        return SupplementaryMaterialStageReviewPayload(SupplementaryMaterialStage(**{**value,
            "page_refs": tuple(value["page_refs"]),
            "previous_budget": RunResourceBudget(**value["previous_budget"]),
            "proposed_budget": RunResourceBudget(**value["proposed_budget"])}))
    if event_type in {AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED, AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED,
                      AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED}:
        from .case_agent_analysis_stage import CaseAnalysisStage, CaseAnalysisRevisionStage, CaseAnalysisRequestRepairStage
        is_revision = event_type is AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED
        is_repair = event_type is AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED
        _exact_keys(payload, {"stage"}, "analysis stage review")
        value = payload["stage"]
        _exact_keys(value, {"run_id", "expected_event_version", "snapshot_hash", "previous_graph_hash",
            "candidate_bindings", "previous_budget", "proposed_budget", "approved_by", "stage_hash"}
            | ({"revised_artifact_id", "revised_artifact_hash", "revision_reason"} if is_revision else set())
            | ({"failed_attempt_id", "failed_request_hash", "repaired_request_hash", "repair_reason"} if is_repair else set()), "analysis stage")
        bindings = value["candidate_bindings"]
        if not isinstance(bindings, (list, tuple)) or any(
                not isinstance(item, (list, tuple)) or len(item) != 2 for item in bindings):
            raise CaseLedgerPersistenceBlocked("analysis candidate bindings are invalid")
        stage_type = CaseAnalysisRequestRepairStage if is_repair else (CaseAnalysisRevisionStage if is_revision else CaseAnalysisStage)
        return CaseAnalysisStageReviewPayload(stage_type(**{**value,
            "candidate_bindings": tuple(tuple(item) for item in bindings),
            "previous_budget": RunResourceBudget(**value["previous_budget"]),
            "proposed_budget": RunResourceBudget(**value["proposed_budget"])}))
    if event_type is AgentEventType.PLANNING_BUDGET_REVIEWED:
        _exact_keys(payload, {"snapshot", "previous_runtime_seconds", "approved_runtime_seconds",
                             "request_hash", "planning_hash", "proposal_hash", "approved_by"},
                    "planning budget review payload")
        _exact_keys(payload["snapshot"], _SNAPSHOT_KEYS, "budget review snapshot")
        return PlanningBudgetReviewPayload(**{**payload, "snapshot": CaseSnapshotRef(**payload["snapshot"])})
    if event_type is AgentEventType.RUN_COMPLETED:
        _exact_keys(payload, {"final_review"}, "run-completed payload")
        approval = dict(payload["final_review"])
        has_documents = "document_review_versions" in approval
        _exact_keys(
            approval,
            {"approval_id", "run_id", "graph_hash", "verification_hash", "artifact_manifest_hash", "approved_by", "approval_hash"}
            | ({"document_review_versions"} if has_documents else set()),
            "final review",
        )
        if has_documents:
            versions = approval["document_review_versions"]
            if (not isinstance(versions, (list, tuple)) or not 1 <= len(versions) <= 128
                    or any(not isinstance(item, (list, tuple)) or len(item) != 2
                           or not all(isinstance(value, str) for value in item) for item in versions)):
                raise CaseLedgerPersistenceBlocked("final document review bindings are malformed")
            approval["document_review_versions"] = tuple(tuple(item) for item in versions)
        return RunCompletedPayload(
            final_review=RunFinalReviewApproval(**approval)
        )
    raise CaseLedgerPersistenceBlocked("Agent event payload does not match its event type")


def _graph_from_json(value: dict[str, Any]) -> AgentTaskGraph:
    _exact_keys(
        value,
        {"graph_id", "graph_version", "goal_hash", "snapshot", "tasks", "graph_hash"},
        "task graph",
    )
    _exact_keys(value["snapshot"], _SNAPSHOT_KEYS, "case snapshot")
    return AgentTaskGraph(
        graph_id=value["graph_id"],
        graph_version=int(value["graph_version"]),
        goal_hash=value["goal_hash"],
        snapshot=CaseSnapshotRef(**value["snapshot"]),
        tasks=tuple(_task_from_json(item) for item in value["tasks"]),
        graph_hash=value["graph_hash"],
    )


def _task_from_json(value: dict[str, Any]) -> AgentTaskSpec:
    _exact_keys(
        value,
        {
            "task_id", "sequence", "title", "purpose", "rationale",
            "dependency_ids", "input_refs", "input_hash", "skill",
            "granted_scopes", "capability", "risk_level", "autonomy_level",
            "approval_gate", "retry_mode", "budget",
        },
        "Agent task",
    )
    _exact_keys(
        value["skill"],
        {"skill_id", "skill_version", "tool_id", "tool_version", "adapter_id", "adapter_version"},
        "Skill binding",
    )
    capability = value["capability"]
    _exact_keys(
        capability,
        {
            "execution_mode", "network_policy", "allowed_domains", "sandbox_profile",
            "sandbox_policy_version", "sandbox_policy_hash", "reads_case_objects",
            "writes_managed_derivatives", "external_request_approval_required",
        },
        "task capability",
    )
    _exact_keys(value["budget"], _TASK_BUDGET_KEYS, "task budget")
    return AgentTaskSpec(
        task_id=value["task_id"],
        sequence=int(value["sequence"]),
        title=value["title"],
        purpose=value["purpose"],
        rationale=value["rationale"],
        dependency_ids=tuple(value["dependency_ids"]),
        input_refs=tuple(value["input_refs"]),
        input_hash=value["input_hash"],
        skill=SkillBinding(**value["skill"]),
        granted_scopes=frozenset(CapabilityScope(item) for item in value["granted_scopes"]),
        capability=TaskCapabilityContract(
            execution_mode=AdapterExecutionMode(capability["execution_mode"]),
            network_policy=NetworkPolicy(capability["network_policy"]),
            allowed_domains=tuple(capability["allowed_domains"]),
            sandbox_profile=capability["sandbox_profile"],
            sandbox_policy_version=capability["sandbox_policy_version"],
            sandbox_policy_hash=capability["sandbox_policy_hash"],
            reads_case_objects=tuple(capability["reads_case_objects"]),
            writes_managed_derivatives=capability["writes_managed_derivatives"],
            external_request_approval_required=capability[
                "external_request_approval_required"
            ],
        ),
        risk_level=AgentRiskLevel(value["risk_level"]),
        autonomy_level=AgentAutonomyLevel(value["autonomy_level"]),
        approval_gate=ApprovalGate(value["approval_gate"]),
        retry_mode=RetryMode(value["retry_mode"]),
        budget=TaskResourceBudget(**value["budget"]),
    )


def _receipt_from_json(value: dict[str, Any]) -> TaskResultReceipt:
    _exact_keys(
        value,
        {
            "receipt_id", "task_id", "attempt_id", "input_hash", "adapter_id",
            "adapter_version", "status", "external_submission_state", "output_hash",
            "error_code", "external_request_id", "runtime_seconds", "cost_minor_units",
            "external_calls", "artifacts",
        },
        "task result receipt",
    )
    for artifact in value.get("artifacts", ()):
        _exact_keys(
            artifact,
            {"artifact_id", "artifact_kind", "content_hash", "byte_size", "source_input_hash", "managed_derivative"},
            "artifact receipt",
        )
    return TaskResultReceipt(
        receipt_id=value["receipt_id"],
        task_id=value["task_id"],
        attempt_id=value["attempt_id"],
        input_hash=value["input_hash"],
        adapter_id=value["adapter_id"],
        adapter_version=value["adapter_version"],
        status=ResultStatus(value["status"]),
        external_submission_state=ExternalSubmissionState(
            value["external_submission_state"]
        ),
        output_hash=value.get("output_hash"),
        error_code=value.get("error_code"),
        external_request_id=value.get("external_request_id"),
        runtime_seconds=int(value["runtime_seconds"]),
        cost_minor_units=int(value["cost_minor_units"]),
        external_calls=int(value["external_calls"]),
        artifacts=tuple(ArtifactReceipt(**item) for item in value.get("artifacts", ())),
    )


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise CaseLedgerPersistenceBlocked(f"{label} schema differs from the supervisor contract")


_SNAPSHOT_KEYS = {"matter_id", "matter_version", "snapshot_hash", "schema_version"}
_RUN_BUDGET_KEYS = {
    "max_tasks", "max_total_attempts", "max_external_calls", "max_runtime_seconds",
    "max_cost_minor_units", "max_output_bytes",
}
_TASK_BUDGET_KEYS = {
    "max_attempts", "timeout_seconds", "max_external_calls",
    "max_cost_minor_units", "max_output_bytes",
}


__all__ = (
    "AgentControlCommandReceipt",
    "AgentPlanningOutcome",
    "AgentWorkerHeartbeatRecord",
    "ClaimedAgentPlanningAttempt",
    "ClaimedAgentTask",
    "PersistentAgentRunProjection",
    "PostgresCaseAgentStore",
    "ReapedAgentAttempt",
    "ReapedAgentPlanningAttempt",
)
