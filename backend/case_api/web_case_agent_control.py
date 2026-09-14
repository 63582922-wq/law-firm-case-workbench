"""Application service for the unified lawyer-facing case Agent.

This adapter is intentionally small: it turns one verified lawyer goal into
the first event of the durable supervisor aggregate, and projects replayed
state into the browser-safe dataclasses owned by :mod:`case_api.web_app`.
Planning and task execution stay with the worker/control-plane boundary; this
service never accepts a task graph, Tool, URL, file path, provider or command
from the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from case_kernel.case_agent_lawyer_decisions import (
    GovernedLawyerPlanningDecision,
    LawyerPlanningDecisionBlocked,
    LawyerPlanningDecisionCode,
    lawyer_planning_decision_subject_hash,
    lawyer_planning_decision_options,
)
from case_kernel.case_agent_postgres import (
    PersistentAgentRunRecord,
    PostgresCaseAgentStore,
)
from case_kernel.errors import VersionConflict
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    AgentGoal,
    AgentDeliverableKind,
    AgentRunState,
    AgentRunStatus,
    AgentSupervisorEvent,
    AgentSupervisorBlocked,
    AgentTaskStatus,
    ApprovalPayload,
    ApprovalRecord,
    CaseAnalysisStageReviewPayload,
    CaseSnapshotRef,
    RunCompletedPayload,
    RunCreatedPayload,
    RunFinalReviewApproval,
    RunResourceBudget,
    LawyerPlanCorrectionPayload,
)
from case_kernel.case_agent_analysis_stage import prepare_case_analysis_stage
from case_kernel.case_ledger_postgres import PersistentCaseSnapshot
from case_kernel.models import Actor, Role

from .persistent_identity import AuthenticationMethod, ServerIdentityContext
from .web_app import (
    WebCaseAgentApprovalResponse,
    WebCaseAgentArtifactResponse,
    WebCaseAgentCompletionReceipt,
    WebCaseAgentCompletionResponse,
    WebCaseAgentControlRunResponse,
    WebCaseAgentCurrentWorkResponse,
    WebCaseAgentDecisionResponse,
    WebCaseAgentDecisionOptionResponse,
)
from .web_case_posture import (
    WebCasePostureBlocked,
    WebCasePostureState,
    WebCasePostureStatus,
)
from .web_case_agent_run_identity import derive_web_case_agent_entity_id
from .web_case_agent_recovery_goal import build_web_case_agent_recovery_goal
from .web_case_agent_artifacts import (
    WebCaseAgentSealedRecoveryArtifact,
    WebCaseAgentSealedRecoveryArtifactReader,
)


class WebCaseAgentControlBlocked(PermissionError):
    """The current identity or run state cannot perform a control action."""


class CaseSnapshotReader(Protocol):
    def get_case_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentCaseSnapshot: ...


class CasePostureReader(Protocol):
    def state(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> WebCasePostureState: ...


class FinalReviewReadinessPort(Protocol):
    def assert_ready(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        artifacts: tuple[object, ...],
        expected_document_versions: tuple[tuple[str, str], ...] | None = None,
    ) -> None: ...


class SealedRecoveryArtifactReader(Protocol):
    def list_sealed_recovery_artifacts(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
    ) -> tuple[WebCaseAgentSealedRecoveryArtifact, ...]: ...


def _snapshot_has_confirmed_fact(snapshot: PersistentCaseSnapshot) -> bool:
    """Return whether the server-owned snapshot contains a confirmed fact.

    Snapshot rows are persistence projections rather than browser payloads.
    Treat malformed rows as unconfirmed so an incomplete projection can never
    accidentally obtain the narrower analysis-only budget.
    """

    return any(
        isinstance(item, dict) and item.get("status") == "CONFIRMED"
        for item in snapshot.facts
    )


@dataclass(frozen=True)
class WebCaseAgentControlPolicy:
    run_budget: RunResourceBudget = RunResourceBudget(
        max_tasks=100,
        max_total_attempts=250,
        max_external_calls=50,
        max_runtime_seconds=24 * 60 * 60,
        max_cost_minor_units=100_000,
        max_output_bytes=2 * 1024 * 1024 * 1024,
    )
    defence_run_budget: RunResourceBudget = RunResourceBudget(
        max_tasks=10,
        max_total_attempts=20,
        max_external_calls=1,
        max_runtime_seconds=1_200,
        max_cost_minor_units=120,
        max_output_bytes=256 * 1024 * 1024,
    )
    defence_material_run_budget: RunResourceBudget = RunResourceBudget(
        max_tasks=16,
        max_total_attempts=32,
        max_external_calls=2,
        max_runtime_seconds=1_800,
        max_cost_minor_units=240,
        max_output_bytes=256 * 1024 * 1024,
    )
    active_plan_document_run_budget: RunResourceBudget = RunResourceBudget(
        max_tasks=4,
        max_total_attempts=4,
        max_external_calls=0,
        max_runtime_seconds=1_200,
        max_cost_minor_units=0,
        max_output_bytes=512 * 1024 * 1024,
    )

    def __post_init__(self) -> None:
        self.run_budget.validate()
        self.defence_run_budget.validate()
        self.defence_material_run_budget.validate()
        self.active_plan_document_run_budget.validate()
        if self.defence_run_budget.max_external_calls != 1:
            raise ValueError("defence Agent runs require exactly one external-call slot")
        if self.defence_material_run_budget.max_external_calls != 2:
            raise ValueError(
                "raw-material defence Agent runs require exactly two external-call slots"
            )
        if (
            self.defence_material_run_budget.max_cost_minor_units
            != self.defence_run_budget.max_cost_minor_units * 2
        ):
            raise ValueError(
                "raw-material defence Agent runs require the cumulative two-stage cost cap"
            )


class WebCaseAgentControlService:
    """OIDC-only Web adapter over the event-sourced Agent store."""

    _HUMAN_ROLES = frozenset(
        {
            Role.ASSISTANT,
            Role.COLLABORATING_LAWYER,
            Role.LEAD_LAWYER,
            Role.REVIEWER,
        }
    )

    def __init__(
        self,
        *,
        store: PostgresCaseAgentStore,
        snapshot_reader: CaseSnapshotReader,
        posture_reader: CasePostureReader,
        final_review_readiness: FinalReviewReadinessPort,
        sealed_recovery_artifact_reader: SealedRecoveryArtifactReader | None = None,
        policy: WebCaseAgentControlPolicy | None = None,
    ) -> None:
        if not isinstance(store, PostgresCaseAgentStore):
            raise ValueError("case Agent store is invalid")
        if not callable(getattr(snapshot_reader, "get_case_snapshot", None)):
            raise ValueError("case Agent snapshot reader is invalid")
        if not callable(getattr(posture_reader, "state", None)):
            raise ValueError("case Agent posture reader is invalid")
        if not callable(getattr(final_review_readiness, "assert_ready", None)):
            raise ValueError("case Agent final-review readiness port is invalid")
        if sealed_recovery_artifact_reader is not None and not callable(
            getattr(sealed_recovery_artifact_reader, "list_sealed_recovery_artifacts", None)
        ):
            raise ValueError("case Agent sealed-response recovery reader is invalid")
        self._store = store
        self._snapshot_reader = snapshot_reader
        self._posture_reader = posture_reader
        self._final_review_readiness = final_review_readiness
        self._sealed_recovery_artifact_reader = sealed_recovery_artifact_reader
        self._policy = policy or WebCaseAgentControlPolicy()

    def _run_budget_for(
        self,
        requested_kinds: tuple[AgentDeliverableKind, ...],
        *,
        snapshot: PersistentCaseSnapshot,
    ) -> RunResourceBudget:
        """Select the server-owned budget before an Agent event is persisted."""

        if AgentDeliverableKind.DEFENCE_STATEMENT not in requested_kinds:
            return self._policy.run_budget
        # A raw-material defendant route has two distinct, independently
        # governed external stages: candidate extraction, then a later
        # source-bound lawyer analysis after the lawyer confirms the facts.
        # The browser cannot select this cap; it is derived only from the
        # server snapshot captured in the RUN_CREATED event.
        return (
            self._policy.defence_run_budget
            if _snapshot_has_confirmed_fact(snapshot)
            else self._policy.defence_material_run_budget
        )

    def create_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        objective: str,
        success_criteria: tuple[str, ...],
        constraints: tuple[str, ...],
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
        requested_deliverables: tuple[AgentDeliverableKind | str, ...] = (),
    ) -> WebCaseAgentControlRunResponse:
        actor = self._actor(identity, now=now)
        posture = self._posture_reader.state(identity=identity, matter_id=matter_id)
        if (
            not isinstance(posture, WebCasePostureState)
            or posture.status is not WebCasePostureStatus.CURRENT
            or posture.profile is None
        ):
            raise WebCasePostureBlocked(
                "请先由主办律师确认本所代理对象、程序阶段和诉讼地位，再交给 Agent 制定办案计划。"
            )
        requested_kinds = _requested_deliverable_kinds(requested_deliverables)
        if (
            AgentDeliverableKind.DEFENCE_STATEMENT in requested_kinds
            and not _is_defence_candidate_posture(posture.profile)
        ):
            raise WebCaseAgentControlBlocked(
                "民事答辩状候选仅适用于当前已确认、有效的一审被告代理情境；系统不会按目标文本推断或变更代理地位。"
            )
        snapshot = self._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=actor
        )
        if snapshot.version != expected_matter_version:
            raise WebCaseAgentControlBlocked("案件已发生变化，请刷新后再交给 Agent。")
        goal_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="goal",
        )
        run_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="run",
        )
        event_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="event",
        )
        goal = AgentGoal.build(
            goal_id=goal_id,
            objective=objective,
            success_criteria=success_criteria,
            constraints=constraints,
            requested_by=actor.actor_id,
            requested_deliverables=requested_kinds,
        )
        event = AgentSupervisorEvent(
            event_id=event_id,
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=RunCreatedPayload(
                goal=goal,
                snapshot=CaseSnapshotRef(
                    matter_id=matter_id,
                    matter_version=snapshot.version,
                    snapshot_hash=snapshot.snapshot_hash,
                    schema_version="case-ledger-snapshot-v1",
                ),
                budget=self._run_budget_for(requested_kinds, snapshot=snapshot),
                task_failure_policy="ISOLATE_KNOWN_TASK_FAILURES_V1",
            ),
        )
        try:
            self._store.create_run(
                matter_id=matter_id,
                actor=actor,
                expected_matter_version=expected_matter_version,
                idempotency_key=idempotency_key,
                event=event,
            )
        except AgentSupervisorBlocked as error:
            # The persistence boundary deliberately refuses a second run for
            # an unchanged snapshot while an earlier candidate still awaits
            # lawyer review.  That is an actionable, governed conflict—not a
            # server error, and the browser must not encourage a retry that
            # could fork competing legal candidates.
            raise WebCaseAgentControlBlocked(
                "当前案件已有待律师审阅的办案成果；请先审阅、完成终审，或在案件材料发生变化后重新研判。"
            ) from error
        return self.get_run(identity=identity, matter_id=matter_id, run_id=run_id)

    def execute_active_plan(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        """Create the one explicit second run for the exact current ACTIVE plan."""

        actor = self._actor(identity, now=now)
        if Role.LEAD_LAWYER not in actor.roles:
            raise WebCaseAgentControlBlocked(
                "只有主办律师可以执行已激活的动态办案计划。"
            )
        posture = self._posture_reader.state(identity=identity, matter_id=matter_id)
        if (
            not isinstance(posture, WebCasePostureState)
            or posture.status is not WebCasePostureStatus.CURRENT
            or posture.profile is None
        ):
            raise WebCasePostureBlocked(
                "当前代理情境已变化，请先由主办律师重新确认后再执行计划。"
            )
        prepared = self._store.prepare_active_plan_execution(
            matter_id=matter_id,
            actor=actor,
            expected_matter_version=expected_matter_version,
        )
        if prepared.existing_run_id is not None:
            return self.get_run(
                identity=identity,
                matter_id=matter_id,
                run_id=prepared.existing_run_id,
            )
        snapshot = self._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=actor
        )
        if snapshot.version != expected_matter_version:
            raise WebCaseAgentControlBlocked(
                "案件已发生变化，请刷新后重新执行已激活计划。"
            )
        goal_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="goal",
        )
        run_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="run",
        )
        event_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="event",
        )
        kinds = tuple(item.deliverable_kind for item in prepared.execution.items)
        labels = {
            AgentDeliverableKind.CASE_REVIEW_MEMO: "案件审阅意见Word及PDF审阅稿",
            AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST: "补证清单Word及PDF审阅稿",
            AgentDeliverableKind.DEFENCE_STATEMENT: "民事答辩状Word及PDF审阅稿",
            AgentDeliverableKind.EVIDENCE_CATALOGUE: "证据目录Excel及PDF审阅稿",
            AgentDeliverableKind.PAYMENT_LEDGER: "收付款核对表Excel及PDF审阅稿",
        }
        goal = AgentGoal.build(
            goal_id=goal_id,
            objective="执行主办律师已激活的动态计划并生成全部可复核成果候选",
            success_criteria=tuple(
                f"生成并独立校验{labels[kind]}" for kind in kinds
            ),
            constraints=(
                "只使用当前ACTIVE计划及其经治理来源，不补造事实、金额或法源",
                "所有成果仅供律师复核，不得自动批准、锁定或对外提交",
            ),
            requested_by=actor.actor_id,
            requested_deliverables=kinds,
            active_plan_execution=prepared.execution,
        )
        event = AgentSupervisorEvent(
            event_id=event_id,
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=RunCreatedPayload(
                goal=goal,
                snapshot=CaseSnapshotRef(
                    matter_id=matter_id,
                    matter_version=snapshot.version,
                    snapshot_hash=snapshot.snapshot_hash,
                    schema_version="case-ledger-snapshot-v1",
                ),
                budget=self._policy.active_plan_document_run_budget,
                task_failure_policy="ISOLATE_KNOWN_TASK_FAILURES_V1",
            ),
        )
        actual_run_id = self._store.create_active_plan_execution_run(
            matter_id=matter_id,
            actor=actor,
            expected_matter_version=expected_matter_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return self.get_run(
            identity=identity,
            matter_id=matter_id,
            run_id=actual_run_id,
        )

    def create_recovery_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        replacement_run_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        """Create only the server-owned 0052 ledger recovery goal/run pair."""

        actor = self._actor(identity, now=now)
        posture = self._posture_reader.state(identity=identity, matter_id=matter_id)
        if (
            not isinstance(posture, WebCasePostureState)
            or posture.status is not WebCasePostureStatus.CURRENT
            or posture.profile is None
        ):
            raise WebCasePostureBlocked(
                "请先由主办律师确认本所代理对象、程序阶段和诉讼地位，再恢复 Agent 办理。"
            )
        snapshot = self._snapshot_reader.get_case_snapshot(
            matter_id=matter_id, actor=actor
        )
        if snapshot.version != expected_matter_version:
            raise WebCaseAgentControlBlocked("案件已发生变化，请刷新后再恢复 Agent。")
        expected_run_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="run",
        )
        if replacement_run_id != expected_run_id:
            raise WebCaseAgentControlBlocked("恢复运行编号与服务器恢复意图不一致。")
        event_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="event",
        )
        goal = build_web_case_agent_recovery_goal(
            actor=actor, replacement_run_id=replacement_run_id
        )
        event = AgentSupervisorEvent(
            event_id=event_id,
            run_id=replacement_run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=1,
            event_type=AgentEventType.RUN_CREATED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=RunCreatedPayload(
                goal=goal,
                snapshot=CaseSnapshotRef(
                    matter_id=matter_id,
                    matter_version=snapshot.version,
                    snapshot_hash=snapshot.snapshot_hash,
                    schema_version="case-ledger-snapshot-v1",
                ),
                budget=self._policy.run_budget,
                task_failure_policy="ISOLATE_KNOWN_TASK_FAILURES_V1",
            ),
        )
        self._store.create_run(
            matter_id=matter_id,
            actor=actor,
            expected_matter_version=expected_matter_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return self.get_run(
            identity=identity, matter_id=matter_id, run_id=replacement_run_id
        )

    def get_current_run(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> WebCaseAgentControlRunResponse | None:
        actor = self._actor(identity)
        record = self._store.current_run_record(matter_id=matter_id, actor=actor)
        return None if record is None else self._project(record)

    def continue_from_material_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        """Move one verified material-reading run into its analysis phase.

        This is deliberately not a new goal or a generic retry.  The server
        reconstructs the exact current candidate set, retains the original
        run and its accumulated limits, and appends one lawyer-reviewed
        continuation.  The Worker will then assemble context locally and
        still presents its normal approval gate before any external analysis.
        """
        actor = self._actor(identity, now=now)
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise WebCaseAgentControlBlocked("只有主办律师或复核律师可以启动材料后的办案研判。")
        posture = self._posture_reader.state(identity=identity, matter_id=matter_id)
        if (
            not isinstance(posture, WebCasePostureState)
            or posture.status is not WebCasePostureStatus.CURRENT
            or posture.profile is None
        ):
            raise WebCasePostureBlocked("当前代理情境已变化，请先由主办律师重新确认后再形成办案研判。")
        state, candidate_bindings = self._store.current_case_analysis_stage_inputs(
            matter_id=matter_id, actor=actor, run_id=run_id,
        )
        if state.event_version != expected_run_version:
            raise VersionConflict("材料处理结果已变化，请刷新后再开始办案研判")
        stage = prepare_case_analysis_stage(
            state=state,
            candidate_bindings=candidate_bindings,
            approved_by=actor.actor_id,
        )
        event = AgentSupervisorEvent(
            event_id=derive_web_case_agent_entity_id(
                actor=actor, matter_id=matter_id, idempotency_key=idempotency_key,
                entity="event",
            ),
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=state.event_version + 1,
            event_type=AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=CaseAnalysisStageReviewPayload(stage),
        )
        self._store.review_case_analysis_stage(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return self.get_run(identity=identity, matter_id=matter_id, run_id=run_id)

    def reconcile_active_plan_execution(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        plan_id: str,
        expected_matter_version: int,
        idempotency_key: str,
    ) -> WebCaseAgentControlRunResponse | None:
        """Read only the run derived from one exact browser execution intent.

        A generic "current run is an execution" check is insufficient after a
        response is lost: another tab or an older plan could be current.  This
        lookup derives the expected run id from the original actor/key and then
        revalidates the server-owned plan and snapshot binding before returning
        it.  It never creates or retries work.
        """

        actor = self._actor(identity)
        expected_run_id = derive_web_case_agent_entity_id(
            actor=actor,
            matter_id=matter_id,
            idempotency_key=idempotency_key,
            entity="run",
        )
        binding = self._store.active_plan_execution_intent(
            matter_id=matter_id,
            actor=actor,
            run_id=expected_run_id,
        )
        if binding is None:
            return None
        bound_plan_id, activated_matter_version = binding
        try:
            record = self._store.run_record(
                matter_id=matter_id, actor=actor, run_id=expected_run_id
            )
        except KeyError as error:
            raise WebCaseAgentControlBlocked(
                "已激活计划执行绑定存在，但对应任务不可读取。"
            ) from error
        state = record.projection.state
        execution = state.goal.active_plan_execution
        if (
            execution is None
            or execution.plan_id != plan_id
            or bound_plan_id != plan_id
            or activated_matter_version != expected_matter_version
        ):
            raise WebCaseAgentControlBlocked(
                "当前任务与原已激活计划执行请求不一致，不能确认该次提交。"
            )
        return self._project(record)

    def get_run(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> WebCaseAgentControlRunResponse:
        actor = self._actor(identity)
        return self._project(
            self._store.run_record(
                matter_id=matter_id, actor=actor, run_id=run_id
            )
        )

    def pause_run(self, **kwargs: object) -> WebCaseAgentControlRunResponse:
        return self._simple_control_event(AgentEventType.RUN_PAUSED, **kwargs)

    def resume_run(self, **kwargs: object) -> WebCaseAgentControlRunResponse:
        return self._simple_control_event(AgentEventType.RUN_RESUMED, **kwargs)

    def cancel_run(self, **kwargs: object) -> WebCaseAgentControlRunResponse:
        return self._simple_control_event(AgentEventType.RUN_CANCELLED, **kwargs)

    def complete_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
        document_review_versions: tuple[tuple[str, str], ...] = (),
    ) -> WebCaseAgentCompletionResponse:
        """Record one server-bound final lawyer review.

        The browser supplies only the version it reviewed. Graph, verification
        and artifact bindings are rebuilt from the authoritative aggregate
        immediately before append. A completed state exactly one event ahead
        is the sole replay shape: rebuilding that immutable predecessor lets
        the store return its prior same-key receipt while a different key
        still fails the store's version check.
        """

        actor = self._actor(identity, now=now)
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise WebCaseAgentControlBlocked(
                "只有主办律师或复核律师可以完成最终审阅。"
            )
        state = self._store.replay_run(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if (
            state.firm_id != actor.firm_id
            or state.matter_id != matter_id
            or state.run_id != run_id
        ):
            raise WebCaseAgentControlBlocked("Agent 运行不属于当前案件或律所。")

        reviewed_state = state
        if (
            state.status is AgentRunStatus.COMPLETED
            and state.event_version == expected_run_version + 1
        ):
            reviewed_state = replace(
                state,
                status=AgentRunStatus.READY_FOR_REVIEW,
                event_version=expected_run_version,
            )
        elif state.event_version != expected_run_version:
            raise VersionConflict("Agent run changed before final review completion")
        elif state.status is AgentRunStatus.COMPLETED:
            raise VersionConflict("Agent run is already completed")

        if (
            reviewed_state.status is not AgentRunStatus.READY_FOR_REVIEW
            or reviewed_state.graph is None
            or reviewed_state.verification_hash is None
            or reviewed_state.cancelled
            or reviewed_state.stale
        ):
            raise WebCaseAgentControlBlocked(
                "Agent 尚未通过服务器核验并进入律师终审状态。"
            )

        # A PASSED verification receipt proves the bytes observed by the
        # verifier at that time.  Final lawyer review has a stricter and later
        # responsibility: every current review surface must still open, and a
        # document package must still match its installed template and source
        # bindings.  Do not rerun this gate for an idempotent replay of an
        # already-recorded completion event.
        if state.status is not AgentRunStatus.COMPLETED:
            if any(item.artifact_kind == "REVIEWABLE_DOCUMENT_CANDIDATE_JSON" for item in reviewed_state.artifacts) and not document_review_versions:
                raise WebCaseAgentControlBlocked("文书终审必须携带律师审阅的版本，请重新打开当前文书。")
            try:
                self._final_review_readiness.assert_ready(
                    identity=identity,
                    matter_id=matter_id,
                    run_id=run_id,
                    artifacts=reviewed_state.artifacts,
                    expected_document_versions=document_review_versions,
                )
            except Exception as error:
                raise WebCaseAgentControlBlocked(
                    "Agent 成果当前无法完整复核，不能记录终审完成。"
                ) from error

        approval_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"final-review:{idempotency_key}",
            )
        )
        approval = RunFinalReviewApproval.build(
            approval_id=approval_id,
            state=reviewed_state,
            approved_by=actor.actor_id,
            document_review_versions=document_review_versions,
        )
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"final-review-event:{idempotency_key}",
            )
        )
        event = AgentSupervisorEvent(
            event_id=event_id,
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=expected_run_version + 1,
            event_type=AgentEventType.RUN_COMPLETED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=RunCompletedPayload(final_review=approval),
        )
        command_receipt = self._store.append_event(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        record = self._store.run_record(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        run = self._project(record)
        receipt_event_id = getattr(command_receipt, "event_id", None)
        receipt_event_version = getattr(command_receipt, "event_version", None)
        receipt_status = getattr(command_receipt, "status", None)
        if (
            run.status != AgentRunStatus.COMPLETED.value
            or receipt_event_id != event_id
            or receipt_event_version != expected_run_version + 1
            or receipt_status != AgentRunStatus.COMPLETED.value
        ):
            raise WebCaseAgentControlBlocked("Agent 终审回执尚未确认。")
        return WebCaseAgentCompletionResponse(
            receipt=WebCaseAgentCompletionReceipt(
                completion_id=receipt_event_id,
                matter_id=matter_id,
                run_id=run_id,
                reviewed_run_version=expected_run_version,
                completed_run_version=receipt_event_version,
                run_status=receipt_status,
                verification_status="PASSED",
                reviewed_artifact_count=len(reviewed_state.artifacts),
            ),
            run=run,
        )

    def reconcile_completion(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
    ) -> WebCaseAgentCompletionReceipt | None:
        """Read exact proof for a completion response lost by the browser.

        This never treats an arbitrary current ``COMPLETED`` projection as
        proof.  The store binds the original actor, idempotency key, reviewed
        version, immutable command audit and deterministic completion event.
        """

        actor = self._actor(identity)
        intent = self._store.final_review_completion_intent(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
        )
        if intent is None:
            return None
        receipt = intent.receipt
        return WebCaseAgentCompletionReceipt(
            completion_id=receipt.event_id,
            matter_id=matter_id,
            run_id=run_id,
            reviewed_run_version=expected_run_version,
            completed_run_version=receipt.event_version,
            run_status=receipt.status,
            verification_status="PASSED",
            reviewed_artifact_count=intent.reviewed_artifact_count,
        )

    def list_decisions(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentDecisionResponse, ...]:
        record = self._store.run_record(
            matter_id=matter_id,
            actor=self._actor(identity),
            run_id=run_id,
        )
        options = tuple(
            WebCaseAgentDecisionOptionResponse(
                option_id=code,
                label=_lawyer_decision_label(code),
                consequence=(
                    "记录为受治理的律师纠正，使当前计划和未完成成果过期；"
                    "Agent 将从新的权威案件快照重新规划，不会执行浏览器指令。"
                ),
                requires_note=requires_note,
            )
            for code, _, requires_note in lawyer_planning_decision_options()
        )
        return tuple(
            WebCaseAgentDecisionResponse(
                decision_id=task.spec.task_id,
                title=f"调整：{task.spec.title}",
                question="如果不同意该任务，请选择最接近的原因；需要时补充事实性说明。",
                options=options,
                allow_note=True,
                blocking=True,
                status="OPEN",
            )
            for task in record.projection.state.tasks
            if task.status is AgentTaskStatus.WAITING_APPROVAL
        )

    def submit_decision(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        decision_id: str,
        option_id: str | None,
        note: str | None,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        actor = self._actor(identity, now=now)
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise WebCaseAgentControlBlocked("当前律师角色不能修正 Agent 计划。")
        try:
            code = LawyerPlanningDecisionCode(option_id or "")
        except ValueError as error:
            raise WebCaseAgentControlBlocked("请选择明确的计划调整原因。") from error
        state = self._store.replay_run(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if state.event_version != expected_run_version or state.graph is None:
            raise WebCaseAgentControlBlocked("Agent 状态已变化，请刷新后重试。")
        task = next(
            (
                item.spec
                for item in state.tasks
                if item.spec.task_id == decision_id
                and item.status is AgentTaskStatus.WAITING_APPROVAL
            ),
            None,
        )
        if task is None:
            raise WebCaseAgentControlBlocked("当前任务已不在等待律师决定。")
        signal_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"lawyer-correction:{decision_id}:{idempotency_key}",
            )
        )
        source_ref_ids = tuple(sorted(task.input_refs))
        try:
            subject_hash = lawyer_planning_decision_subject_hash(
                matter_id=matter_id,
                decision_code=code,
                source_ref_ids=source_ref_ids,
            )
        except LawyerPlanningDecisionBlocked as error:
            raise WebCaseAgentControlBlocked(str(error)) from error
        prior = self._store.lawyer_plan_decision_head(
            matter_id=matter_id,
            actor=actor,
            subject_hash=subject_hash,
        )
        signal_version = 1 if prior is None else prior[0] + 1
        supersedes_signal_id = None if prior is None else prior[1]
        try:
            decision = GovernedLawyerPlanningDecision.build(
                signal_id=signal_id,
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                graph_id=state.graph.graph_id,
                task_id=task.task_id,
                signal_version=signal_version,
                decision_code=code,
                note=note,
                source_ref_ids=source_ref_ids,
                task_input_hash=task.input_hash,
                graph_hash=state.graph.graph_hash,
                recorded_event_sequence=expected_run_version + 1,
                recorded_by=actor.actor_id,
                decided_at=now,
                supersedes_signal_id=supersedes_signal_id,
            )
        except LawyerPlanningDecisionBlocked as error:
            raise WebCaseAgentControlBlocked(str(error)) from error
        event = AgentSupervisorEvent(
            event_id=str(uuid5(NAMESPACE_URL, f"{signal_id}:event")),
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=expected_run_version + 1,
            event_type=AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=LawyerPlanCorrectionPayload(
                signal_id=decision.signal_id,
                task_id=decision.task_id,
                decision_hash=decision.decision_hash,
                subject_hash=decision.subject_hash,
                decision_code=decision.decision_code.value,
            ),
        )
        self._store.record_lawyer_plan_correction(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
            event=event,
            decision=decision,
        )
        return self.get_run(identity=identity, matter_id=matter_id, run_id=run_id)

    def list_approvals(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentApprovalResponse, ...]:
        record = self._store.run_record(
            matter_id=matter_id,
            actor=self._actor(identity),
            run_id=run_id,
        )
        state = record.projection.state
        items: list[WebCaseAgentApprovalResponse] = []
        for task in state.tasks:
            if task.status is AgentTaskStatus.WAITING_APPROVAL:
                items.append(
                    WebCaseAgentApprovalResponse(
                        approval_id=task.spec.task_id,
                        action_label=task.spec.title,
                        reason=task.spec.rationale,
                        impact="批准后 Agent 可执行该项内部任务；不会因此自动对外提交。",
                        status="OPEN",
                    )
                )
        return tuple(items)

    def submit_approval(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        approval_id: str,
        approved: bool,
        note: str | None,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        actor = self._actor(identity, now=now)
        if not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise WebCaseAgentControlBlocked("当前律师角色不能批准 Agent 执行任务。")
        if not approved:
            raise WebCaseAgentControlBlocked(
                "请在“调整任务”中选择原因；系统会记录纠正并重新规划。"
            )
        state = self._store.replay_run(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if state.event_version != expected_run_version or state.graph is None:
            raise WebCaseAgentControlBlocked("Agent 状态已变化，请刷新后重试。")
        task_runtime = next(
            (
                item for item in state.tasks
                if item.spec.task_id == approval_id
                and item.status is AgentTaskStatus.WAITING_APPROVAL
            ),
            None,
        )
        if task_runtime is None:
            raise WebCaseAgentControlBlocked("当前任务已不在等待审批。")
        persisted_approval_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                f"approval:{approval_id}:{idempotency_key}",
            )
        )
        approval = ApprovalRecord.build(
            approval_id=persisted_approval_id,
            task=task_runtime.spec,
            graph_hash=state.graph.graph_hash,
            gate=task_runtime.spec.approval_gate,
            approved_by=actor.actor_id,
        )
        event = AgentSupervisorEvent(
            event_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                    f"approval-event:{approval_id}:{idempotency_key}",
                )
            ),
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=expected_run_version + 1,
            event_type=AgentEventType.APPROVAL_GRANTED,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=ApprovalPayload(approval=approval),
        )
        # Free-form browser notes never become executable input.  Approval is
        # fully determined by the current task, graph and lawyer identity.
        _ = note
        self._store.append_event(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return self.get_run(identity=identity, matter_id=matter_id, run_id=run_id)

    def list_artifacts(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentArtifactResponse, ...]:
        record = self._store.run_record(
            matter_id=matter_id,
            actor=self._actor(identity),
            run_id=run_id,
        )
        active_plan_titles = _active_plan_artifact_titles(record.projection.state)
        normal_artifacts = tuple(
            WebCaseAgentArtifactResponse(
                artifact_id=item.artifact_id,
                title=active_plan_titles.get(
                    item.artifact_id, _artifact_title(item.artifact_kind)
                ),
                artifact_type=item.artifact_kind,
                status=(
                    "READY_FOR_REVIEW"
                    if record.projection.state.status
                    in {AgentRunStatus.READY_FOR_REVIEW, AgentRunStatus.COMPLETED}
                    else "CANDIDATE"
                ),
                review_required=True,
                recovery_review_only=False,
            )
            for item in record.projection.state.artifacts
        )
        if self._sealed_recovery_artifact_reader is None:
            return normal_artifacts
        recovered = self._sealed_recovery_artifact_reader.list_sealed_recovery_artifacts(
            identity=identity,
            matter_id=matter_id,
            run_id=run_id,
        )
        recovery_artifacts: list[WebCaseAgentArtifactResponse] = []
        known_ids = {item.artifact_id for item in normal_artifacts}
        for item in recovered:
            if (
                not isinstance(item, WebCaseAgentSealedRecoveryArtifact)
                or item.artifact_id in known_ids
            ):
                raise WebCaseAgentControlBlocked(
                    "封存响应恢复候选的当前审阅边界无效。"
                )
            recovery_artifacts.append(
                WebCaseAgentArtifactResponse(
                    artifact_id=item.artifact_id,
                    title=item.title,
                    artifact_type=item.artifact_kind,
                    status="READY_FOR_REVIEW",
                    review_required=True,
                    recovery_review_only=True,
                )
            )
        return (*recovery_artifacts, *normal_artifacts)

    def _simple_control_event(
        self,
        event_type: AgentEventType,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse:
        actor = self._actor(identity, now=now)
        state = self._store.replay_run(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if state.event_version != expected_run_version:
            raise WebCaseAgentControlBlocked("Agent 状态已变化，请刷新后重试。")
        event = AgentSupervisorEvent(
            event_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"lawcase-agent:{actor.firm_id}:{matter_id}:{run_id}:"
                    f"{event_type.value}:{idempotency_key}",
                )
            ),
            run_id=run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=expected_run_version + 1,
            event_type=event_type,
            occurred_at=now,
            actor_id=actor.actor_id,
            payload=None,
        )
        self._store.append_event(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=expected_run_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return self.get_run(identity=identity, matter_id=matter_id, run_id=run_id)

    def _actor(
        self, identity: ServerIdentityContext, *, now: datetime | None = None
    ) -> Actor:
        if not isinstance(identity, ServerIdentityContext):
            raise WebCaseAgentControlBlocked("律师身份无效。")
        identity.validate(now=now)
        if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
            raise WebCaseAgentControlBlocked("统一办案 Agent 只接受已通过 MFA 的 Web 律师身份。")
        if Role.SYSTEM_WORKER in identity.actor.roles or not identity.actor.roles.intersection(
            self._HUMAN_ROLES
        ):
            raise WebCaseAgentControlBlocked("当前身份不能操作律师办案 Agent。")
        return identity.actor

    @staticmethod
    def _project(record: PersistentAgentRunRecord) -> WebCaseAgentControlRunResponse:
        state = record.projection.state
        completed = sum(
            task.status is AgentTaskStatus.SUCCEEDED for task in state.tasks
        )
        current = next(
            (
                task
                for task in state.tasks
                if task.status
                in {
                    AgentTaskStatus.RUNNING,
                    AgentTaskStatus.READY,
                    AgentTaskStatus.WAITING_APPROVAL,
                    AgentTaskStatus.UNKNOWN,
                }
            ),
            None,
        )
        current_work = (
            None
            if current is None
            else WebCaseAgentCurrentWorkResponse(
                title=current.spec.title,
                detail=current.spec.purpose,
                status=current.status.value,
            )
        )
        status = state.status
        open_approvals = sum(
            task.status is AgentTaskStatus.WAITING_APPROVAL for task in state.tasks
        )
        return WebCaseAgentControlRunResponse(
            run_id=state.run_id,
            matter_id=state.matter_id,
            objective=state.goal.objective,
            status=status.value,
            phase_label=_phase_label(status),
            progress_completed=completed,
            progress_total=len(state.tasks),
            current_work=current_work,
            # One correction card and one approval card refer to the same
            # waiting task.  The UI renders both choices in one intervention
            # column; count the task once rather than double-counting it.
            open_decision_count=0,
            open_approval_count=open_approvals,
            artifact_count=len(state.artifacts),
            status_message=_status_message(state),
            failure_message=(
                None
                if state.failure_code is None
                else _lawyer_failure_message(state.failure_code)
            ),
            failure_code=state.failure_code,
            version=state.event_version,
            snapshot_matter_version=state.snapshot.matter_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            can_pause=status
            not in {
                AgentRunStatus.PAUSED,
                AgentRunStatus.COMPLETED,
                AgentRunStatus.CANCELLED,
                AgentRunStatus.FAILED,
            },
            can_resume=status is AgentRunStatus.PAUSED,
            can_cancel=status
            not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.CANCELLED,
                AgentRunStatus.FAILED,
            },
            active_plan_execution=state.goal.active_plan_execution is not None,
            required_document_deliverables=tuple(
                item.deliverable_kind.value
                for item in state.goal.active_plan_execution.items
            ) if state.goal.active_plan_execution is not None else (),
            input_snapshot_status=record.input_snapshot_status,
        )


def _lawyer_failure_message(failure_code: str) -> str:
    messages = {
        "NEXT_STAGE_BUDGET_REVIEW_REQUIRED": (
            "已完成阶段的成果已保留。剩余预算不足以执行下一阶段，尚未分派下一阶段任务；"
            "请核对本次任务的预算后继续，无需重新上传材料或重做已完成阶段。"
        ),
        "REEXTRACTION_PAGE_LIMIT_EXCEEDED": (
            "本次重新提取的证据范围超过单次 64 页安全上限。系统已停止且未提交"
            "不完整分析；请由主办律师拆分或调整该项范围后重新规划。"
        ),
        "REEXTRACTION_GRAPH_CAPACITY_EXCEEDED": (
            "当前待重新提取事项超过本次计划容量。系统已停止且未遗漏静默执行；"
            "请由主办律师分批处理待办事项后重新规划。"
        ),
    }
    return messages.get(
        failure_code,
        "Agent 未能安全完成当前任务；请刷新案件状态，仍无法继续时由管理员查看审计原因。",
    )


def _phase_label(status: AgentRunStatus) -> str:
    labels = {
        AgentRunStatus.CREATED: "准备分析案件",
        AgentRunStatus.PLANNING: "制定动态办案计划",
        AgentRunStatus.WAITING_APPROVAL: "等待律师决定",
        AgentRunStatus.EXECUTING: "执行办案任务",
        AgentRunStatus.WAITING_INPUT: "任务受阻，待处理",
        AgentRunStatus.RECONCILIATION_REQUIRED: "核验外部任务状态",
        AgentRunStatus.VERIFYING: "独立复核成果",
        AgentRunStatus.READY_FOR_REVIEW: "等待律师终审",
        AgentRunStatus.COMPLETED: "本次目标已完成",
        AgentRunStatus.PAUSED: "已暂停",
        AgentRunStatus.STALE: "案件已变化，等待重规划",
        AgentRunStatus.CANCELLED: "已取消",
        AgentRunStatus.FAILED: "未能完成",
    }
    return labels[status]


def _requested_deliverable_kinds(
    values: tuple[AgentDeliverableKind | str, ...],
) -> tuple[AgentDeliverableKind, ...]:
    """Normalize the browser's closed catalogue before posture policy checks."""

    try:
        return tuple(
            sorted(
                {AgentDeliverableKind(value) for value in values},
                key=lambda item: item.value,
            )
        )
    except (TypeError, ValueError) as error:
        raise WebCaseAgentControlBlocked(
            "请求的可复核成果不在当前服务目录中。"
        ) from error


def _is_defence_candidate_posture(profile: object) -> bool:
    """Keep the Web boundary aligned with the compiler's narrow scope."""

    return (
        getattr(profile, "represented_position", None) == "DEFENDANT"
        and getattr(profile, "procedure_stage", None) == "FIRST_INSTANCE"
        and getattr(profile, "engagement_state", None) == "ACTIVE"
    )


def _status_message(state: AgentRunState) -> str:
    if any(task.status is AgentTaskStatus.FAILED for task in state.tasks):
        if state.status is AgentRunStatus.EXECUTING:
            return "部分任务未完成；系统继续执行不受影响的工作，失败任务及其依赖不会自动重跑。整案尚未完成。"
        if state.status is AgentRunStatus.WAITING_APPROVAL:
            return "部分任务未完成；其他独立工作等待律师审批。已有成果保留，整案尚未完成。"
        if state.status is AgentRunStatus.WAITING_INPUT:
            return "当前可继续的工作已停止，已有成果保留。失败任务需要按具体原因恢复，并不一定需要补充案件资料；整案尚未完成。"
    if state.status is AgentRunStatus.CREATED:
        return "目标已安全保存，等待受控规划 Worker 生成本案动态任务。"
    if state.status is AgentRunStatus.RECONCILIATION_REQUIRED:
        return "外部任务结果不明确；系统已停止自动重试并等待核验。"
    if state.status is AgentRunStatus.STALE:
        return "案件内容已变化，旧计划不会继续执行。"
    if state.status is AgentRunStatus.COMPLETED:
        return "已通过独立验证和律师最终复核。"
    return "Agent 只会按当前案件权限、预算和审批门继续工作。"


def _artifact_title(kind: str) -> str:
    return {
        "CASE_REVIEW_MEMO": "案件审阅意见",
        "SUPPLEMENTARY_EVIDENCE_CHECKLIST": "补证清单",
        "LEGAL_RESEARCH_MEMO": "法律检索意见",
        "EVIDENCE_MATRIX": "证据矩阵",
        "DOCUMENT_DRAFT": "文书候选",
        "CALCULATION": "测算结果",
        "REVIEWABLE_DOCUMENT_CANDIDATE_JSON": "文书候选与来源清单",
        "REVIEWABLE_DOCUMENT_EDITABLE": "可编辑文书",
        "REVIEWABLE_DOCUMENT_PDF_PREVIEW": "文书 PDF 预览",
        "PUBLIC_RESEARCH_LEADS_CANDIDATE": "网络研究线索",
        "VISUAL_PAGE_REVIEW_CANDIDATE": "图片与扫描页识别候选",
        "CASE_CONTEXT_REVIEW_CANDIDATE": "案件台账核对（非整案分析）",
    }.get(kind, "办案成果")


def _active_plan_artifact_titles(state: AgentRunState) -> dict[str, str]:
    """Name server-owned document artifacts by the exact ACTIVE-plan output.

    Several document tasks intentionally emit the same governed artifact kind.
    The browser must not make lawyers open indistinguishable cards to discover
    whether a package is a memo or a ledger.  The label is derived only from
    the immutable task-to-work-plan binding already held by the supervisor;
    artifact bytes, model text and browser input cannot choose it.
    """

    execution = state.goal.active_plan_execution
    if execution is None:
        return {}
    deliverables_by_ref = {
        f"work-plan-item:{item.item_id}": item.deliverable_kind
        for item in execution.items
    }
    labels = {
        AgentDeliverableKind.CASE_REVIEW_MEMO: "案件审阅意见候选（Word / PDF）",
        AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST: "补证清单候选（Word / PDF）",
        AgentDeliverableKind.DEFENCE_STATEMENT: "民事答辩状候选（Word / PDF）",
        AgentDeliverableKind.EVIDENCE_CATALOGUE: "证据目录候选（Excel / PDF）",
        AgentDeliverableKind.PAYMENT_LEDGER: "收付款核对表候选（Excel / PDF）",
    }
    result: dict[str, str] = {}
    for runtime in state.tasks:
        matched = {
            deliverables_by_ref[ref]
            for ref in runtime.spec.input_refs
            if ref in deliverables_by_ref
        }
        if len(matched) != 1:
            continue
        label = labels[next(iter(matched))]
        for receipt in runtime.receipts:
            for artifact in receipt.artifacts:
                if artifact.artifact_kind.startswith("REVIEWABLE_DOCUMENT_"):
                    result[artifact.artifact_id] = label
    return result


def _lawyer_decision_label(value: str) -> str:
    return {
        LawyerPlanningDecisionCode.WRONG_SCOPE.value: "范围或目标不对",
        LawyerPlanningDecisionCode.MISSING_MATERIAL.value: "缺少必要材料",
        LawyerPlanningDecisionCode.WRONG_FACT_ASSUMPTION.value: "事实前提不对",
        LawyerPlanningDecisionCode.WRONG_LEGAL_DIRECTION.value: "法律方向需调整",
        LawyerPlanningDecisionCode.DUPLICATE_OR_UNNECESSARY.value: "重复或当前无必要",
        LawyerPlanningDecisionCode.OTHER.value: "其他需要调整",
    }.get(value, "调整当前计划")


__all__ = (
    "WebCaseAgentControlBlocked",
    "WebCaseAgentControlPolicy",
    "WebCaseAgentControlService",
)
