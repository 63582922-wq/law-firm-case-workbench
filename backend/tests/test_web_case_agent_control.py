from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_case_agent_control import (
    WebCaseAgentControlBlocked,
    WebCaseAgentControlService,
    _active_plan_artifact_titles,
)
from case_api.web_case_agent_recovery_goal import (
    RECOVERY_CONSTRAINTS,
    RECOVERY_OBJECTIVE,
    RECOVERY_SUCCESS_CRITERIA,
)
from case_api.web_case_agent_run_identity import derive_web_case_agent_entity_id
from case_api.web_case_agent_artifacts import WebCaseAgentSealedRecoveryArtifact
from case_api.web_app import WebCaseAgentArtifactResponse, _project_case_agent_artifact
from case_api.web_case_posture import (
    WebCasePostureBlocked,
    WebCasePostureProfile,
    WebCasePostureState,
    WebCasePostureStatus,
)
from case_kernel.case_agent_postgres import (
    AgentControlCommandReceipt,
    FinalReviewCompletionIntent,
    PersistentAgentRunProjection,
    PersistentAgentRunRecord,
    PreparedActivePlanExecution,
    PostgresCaseAgentStore,
)
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.case_agent_supervisor import (
    ActivePlanDeliverableRef,
    ActivePlanExecutionRef,
    AdapterExecutionMode,
    AgentAutonomyLevel,
    AgentEventType,
    AgentDeliverableFormat,
    AgentDeliverableKind,
    AgentRiskLevel,
    AgentRunState,
    AgentSupervisorBlocked,
    AgentSupervisorEvent,
    AgentTaskSpec,
    ArtifactReceipt,
    ApprovalGate,
    ApprovalPayload,
    ApprovalRecord,
    CaseSnapshotRef,
    ExternalSubmissionState,
    NetworkPolicy,
    PlanningFailurePayload,
    RetryMode,
    ResultStatus,
    RuntimeAdapterManifest,
    RunCreatedPayload,
    SkillBinding,
    TaskCapabilityContract,
    TaskGraphPayload,
    TaskResultPayload,
    TaskResultReceipt,
    TaskResourceBudget,
    TaskStartedPayload,
    VerificationPayload,
    compile_task_graph,
    decide_next_commands,
    reduce_agent_event,
)
from case_kernel.case_ledger_postgres import PersistentCaseSnapshot
from case_kernel.models import Actor, Role
from case_kernel.skill_registry import (
    CapabilityScope,
    CaseSkillRegistry,
    SkillDefinition,
    SkillMaturity,
    ToolDefinition,
)


def _id() -> str:
    return str(uuid4())


@dataclass
class _SnapshotReader:
    snapshot: PersistentCaseSnapshot

    def get_case_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentCaseSnapshot:
        if matter_id != self.snapshot.matter_id:
            raise KeyError(matter_id)
        return self.snapshot


class _PostureReader:
    def __init__(
        self,
        *,
        current: bool = True,
        represented_position: str = "DEFENDANT",
        procedure_stage: str = "FIRST_INSTANCE",
        engagement_state: str = "ACTIVE",
    ) -> None:
        self.current = current
        self.represented_position = represented_position
        self.procedure_stage = procedure_stage
        self.engagement_state = engagement_state

    def state(self, *, identity: ServerIdentityContext, matter_id: str) -> WebCasePostureState:
        del identity
        if not self.current:
            return WebCasePostureState(
                status=WebCasePostureStatus.NOT_CONFIRMED,
                profile=None,
                can_confirm=True,
            )
        return WebCasePostureState(
            status=WebCasePostureStatus.CURRENT,
            profile=WebCasePostureProfile(
                profile_id=_id(),
                profile_version=1,
                represented_party_id=_id(),
                represented_party_display_label="匿名被代理人",
                represented_party_kind="NATURAL_PERSON",
                proceeding_id=_id(),
                forum_type="PEOPLE_COURT",
                position_id=_id(),
                engagement_id=_id(),
                case_type_code="CIVIL.PRIVATE_LENDING",
                procedure_stage=self.procedure_stage,
                represented_position=self.represented_position,
                authority_scope_code="GENERAL_AUTHORITY",
                engagement_state=self.engagement_state,
                confirmed_matter_version=6,
            ),
            can_confirm=True,
        )


class _FinalReviewReadiness:
    def __init__(self) -> None:
        self.blocked = False
        self.calls: list[tuple[str, str, int]] = []

    def assert_ready(
        self, *, identity, matter_id: str, run_id: str, artifacts, expected_document_versions=None
    ) -> None:
        del identity
        self.calls.append((matter_id, run_id, len(artifacts)))
        if self.blocked:
            raise RuntimeError("stale review surface")


class _SealedRecoveryReader:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        self.calls: list[tuple[str, str]] = []

    def list_sealed_recovery_artifacts(self, *, identity, matter_id: str, run_id: str):
        del identity
        self.calls.append((matter_id, run_id))
        return (
            WebCaseAgentSealedRecoveryArtifact(
                artifact_id=self.artifact_id,
                title="封存模型响应恢复 · 律师决策包候选",
                artifact_kind="LAWYER_DECISION_PACKAGE_CANDIDATE",
            ),
        )


class _Store(PostgresCaseAgentStore):
    def __init__(self) -> None:
        super().__init__("postgresql://not-used.invalid/test")
        self.events: dict[str, list[AgentSupervisorEvent]] = {}
        self.created_at: dict[str, datetime] = {}
        self.correction_heads: dict[str, tuple[int, str]] = {}
        self.append_receipts: dict[
            tuple[str, str, AgentEventType],
            tuple[tuple[object, ...], AgentControlCommandReceipt],
        ] = {}
        self.active_plan_execution = ActivePlanExecutionRef(
            plan_id=_id(),
            plan_hash=sha256(b"active-plan").hexdigest(),
            source_run_id=_id(),
            items=(
                ActivePlanDeliverableRef(
                    item_id=_id(),
                    item_hash=sha256(b"memo-item").hexdigest(),
                    deliverable_kind=AgentDeliverableKind.CASE_REVIEW_MEMO,
                    output_format=AgentDeliverableFormat.DOCX,
                ),
                ActivePlanDeliverableRef(
                    item_id=_id(),
                    item_hash=sha256(b"ledger-item").hexdigest(),
                    deliverable_kind=AgentDeliverableKind.PAYMENT_LEDGER,
                    output_format=AgentDeliverableFormat.XLSX,
                ),
            ),
        )
        self.active_execution_run_id: str | None = None
        self.create_run_error: Exception | None = None

    def create_run(self, *, event: AgentSupervisorEvent, **_: object):
        if self.create_run_error is not None:
            raise self.create_run_error
        self.events.setdefault(event.run_id, [event])
        self.created_at.setdefault(event.run_id, event.occurred_at)

    def prepare_active_plan_execution(
        self, *, expected_matter_version: int, **_: object
    ) -> PreparedActivePlanExecution:
        return PreparedActivePlanExecution(
            execution=self.active_plan_execution,
            matter_version=expected_matter_version,
            existing_run_id=self.active_execution_run_id,
        )

    def create_active_plan_execution_run(
        self, *, event: AgentSupervisorEvent, **_: object
    ) -> str:
        assert isinstance(event.payload, RunCreatedPayload)
        self.assert_execution(event.payload.goal.active_plan_execution)
        self.create_run(event=event)
        self.active_execution_run_id = event.run_id
        return event.run_id

    def active_plan_execution_intent(self, *, run_id: str, **_: object):
        if run_id != self.active_execution_run_id:
            return None
        return self.active_plan_execution.plan_id, 7

    def final_review_completion_intent(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        **_: object,
    ) -> FinalReviewCompletionIntent | None:
        prior = self.append_receipts.get(
            (actor.actor_id, idempotency_key, AgentEventType.RUN_COMPLETED)
        )
        if prior is None:
            return None
        receipt = prior[1]
        if (
            receipt.run_id != run_id
            or receipt.event_version != expected_event_version + 1
        ):
            return None
        predecessor = reduce_agent_event(None, self.events[run_id][0])
        for event in self.events[run_id][1:expected_event_version]:
            predecessor = reduce_agent_event(predecessor, event)
        return FinalReviewCompletionIntent(
            receipt=receipt,
            reviewed_artifact_count=len(predecessor.artifacts),
        )

    def assert_execution(self, execution: ActivePlanExecutionRef | None) -> None:
        if execution != self.active_plan_execution:
            raise AssertionError("active plan execution differs")

    def append_event(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_event_version: int,
        idempotency_key: str,
        event: AgentSupervisorEvent,
        **_: object,
    ) -> AgentControlCommandReceipt:
        receipt_key = (actor.actor_id, idempotency_key, event.event_type)
        request = (
            matter_id,
            expected_event_version,
            event.run_id,
            event.event_type,
            repr(event.payload),
        )
        prior = self.append_receipts.get(receipt_key)
        if prior is not None:
            if prior[0] != request:
                raise IdempotencyConflict(
                    "idempotency key was reused with different Agent input"
                )
            return prior[1]
        state = self.replay_run(run_id=event.run_id)
        if state.event_version != expected_event_version:
            raise VersionConflict("Agent run changed before append")
        next_state = reduce_agent_event(state, event)
        self.events[event.run_id].append(event)
        receipt = AgentControlCommandReceipt(
            command_name=f"APPEND_CASE_AGENT_{event.event_type.value}",
            idempotency_key=idempotency_key,
            matter_id=matter_id,
            run_id=event.run_id,
            event_version=event.sequence,
            event_id=event.event_id,
            status=next_state.status.value,
        )
        self.append_receipts[receipt_key] = (request, receipt)
        return receipt

    def record_lawyer_plan_correction(
        self, *, event: AgentSupervisorEvent, decision, **_: object
    ):
        decision.validate()
        self.events[event.run_id].append(event)
        self.correction_heads[decision.subject_hash] = (
            decision.signal_version,
            decision.signal_id,
        )

    def lawyer_plan_decision_head(self, *, subject_hash: str, **_: object):
        return self.correction_heads.get(subject_hash)

    def replay_run(self, *, run_id: str, **_: object) -> AgentRunState:
        state = None
        for event in self.events[run_id]:
            state = reduce_agent_event(state, event)
        assert state is not None
        return state

    def _record(self, run_id: str) -> PersistentAgentRunRecord:
        state = self.replay_run(run_id=run_id)
        projection = PersistentAgentRunProjection(
            state=state, next_commands=(), checkpoint_verified=True
        )
        return PersistentAgentRunRecord(
            projection=projection,
            created_at=self.created_at[run_id],
            updated_at=self.events[run_id][-1].occurred_at,
        )

    def current_run_record(self, **_: object):
        if not self.events:
            return None
        run_id = next(reversed(self.events))
        return self._record(run_id)

    def run_record(self, *, run_id: str, **_: object):
        return self._record(run_id)

    def current_case_analysis_stage_inputs(self, *, run_id: str, **_: object):
        return self.replay_run(run_id=run_id), ((f"fact-candidate:{_id()}", "a" * 64),)

    def review_case_analysis_stage(self, *, matter_id: str, event: AgentSupervisorEvent, **_: object):
        self.last_analysis_stage_event = event
        return AgentControlCommandReceipt(
            command_name="REVIEW_CASE_ANALYSIS_STAGE",
            idempotency_key="test-analysis-stage",
            matter_id=matter_id,
            run_id=event.run_id,
            event_version=event.sequence,
            event_id=event.event_id,
            status="CREATED",
        )


class WebCaseAgentControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.matter_id, self.firm_id, self.actor_id = _id(), _id(), _id()
        self.actor = Actor(
            self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.identity = ServerIdentityContext(
            actor=self.actor,
            session_id=_id(),
            issuer="https://identity.example.cn",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=self.now - timedelta(minutes=5),
            expires_at=self.now + timedelta(hours=1),
        )
        snapshot = PersistentCaseSnapshot(
            matter_id=self.matter_id,
            title="匿名化测试案件",
            stage="MATERIAL_REVIEW",
            version=7,
            snapshot_hash=sha256(b"snapshot").hexdigest(),
            facts=(), claims=(), issues=(), transactions=(),
            payment_classifications=(), duplicate_groups=(),
        )
        self.store = _Store()
        self.final_review_readiness = _FinalReviewReadiness()
        self.service = WebCaseAgentControlService(
            store=self.store,
            snapshot_reader=_SnapshotReader(snapshot),
            posture_reader=_PostureReader(),
            final_review_readiness=self.final_review_readiness,
        )

    def create(self):
        return self.service.create_run(
            identity=self.identity,
            matter_id=self.matter_id,
            objective="全面审阅现有材料并形成可复核的办案方案。",
            success_criteria=("列明证据缺口和法律问题",),
            constraints=("不得自动向法院提交",),
            expected_matter_version=7,
            idempotency_key="case-agent-create-0001",
            now=self.now,
        )

    def test_active_plan_document_cards_use_exact_lawyer_facing_names(self) -> None:
        memo_item, ledger_item = self.store.active_plan_execution.items
        memo_artifact = ArtifactReceipt(
            artifact_id=_id(),
            artifact_kind="REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
            content_hash="1" * 64,
            byte_size=100,
            source_input_hash="2" * 64,
            managed_derivative=True,
        )
        ledger_artifact = replace(memo_artifact, artifact_id=_id())
        state = SimpleNamespace(
            goal=SimpleNamespace(
                active_plan_execution=self.store.active_plan_execution
            ),
            tasks=(
                SimpleNamespace(
                    spec=SimpleNamespace(
                        input_refs=(f"work-plan-item:{memo_item.item_id}",)
                    ),
                    receipts=(SimpleNamespace(artifacts=(memo_artifact,)),),
                ),
                SimpleNamespace(
                    spec=SimpleNamespace(
                        input_refs=(f"work-plan-item:{ledger_item.item_id}",)
                    ),
                    receipts=(SimpleNamespace(artifacts=(ledger_artifact,)),),
                ),
            ),
        )

        self.assertEqual(
            _active_plan_artifact_titles(state),
            {
                memo_artifact.artifact_id: "案件审阅意见候选（Word / PDF）",
                ledger_artifact.artifact_id: "收付款核对表候选（Excel / PDF）",
            },
        )

    def ready_for_review(self) -> tuple[str, int, ArtifactReceipt]:
        created = self.create()
        state = self.store.replay_run(run_id=created.run_id)
        tool = ToolDefinition(
            "write_case_derivative",
            "1.0.0",
            frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            False,
            False,
            True,
        )
        registry = CaseSkillRegistry(
            tools=(tool,),
            skills=(
                SkillDefinition(
                    "case_derivative",
                    "1.0.0",
                    "案件派生件",
                    SkillMaturity.IMPLEMENTED,
                    frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                    (tool.tool_id,),
                    ApprovalGate.LAWYER_REVIEW,
                    "ManagedDerivative",
                    ("不得改写原件",),
                ),
            ),
        )
        manifest = RuntimeAdapterManifest(
            tool_id=tool.tool_id,
            adapter_id="draft-worker",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.ISOLATED_CONTAINER,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash="3" * 64,
        )
        source_ref = f"work-plan-item:{uuid4()}"
        task = AgentTaskSpec(
            task_id=_id(),
            sequence=1,
            title="形成可编辑文书候选",
            purpose="根据当前案件计划形成律师可复核的候选",
            rationale="当前动态计划要求该内部成果。",
            dependency_ids=(),
            input_refs=(source_ref,),
            input_hash="4" * 64,
            skill=SkillBinding(
                "case_derivative",
                "1.0.0",
                tool.tool_id,
                "1.0.0",
                manifest.adapter_id,
                manifest.adapter_version,
            ),
            granted_scopes=frozenset(
                {CapabilityScope.MANAGED_DERIVATIVE_WRITE}
            ),
            capability=TaskCapabilityContract(
                execution_mode=AdapterExecutionMode.ISOLATED_CONTAINER,
                network_policy=NetworkPolicy.DENY,
                allowed_domains=(),
                sandbox_profile="draft-only",
                sandbox_policy_version=manifest.sandbox_policy_version,
                sandbox_policy_hash=manifest.sandbox_policy_hash,
                reads_case_objects=(source_ref,),
                writes_managed_derivatives=True,
                external_request_approval_required=False,
            ),
            risk_level=AgentRiskLevel.HIGH,
            autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
            approval_gate=ApprovalGate.LAWYER_REVIEW,
            retry_mode=RetryMode.IDEMPOTENT,
            budget=TaskResourceBudget(2, 120, 0, 0, 1_000_000),
        )
        graph = compile_task_graph(
            graph_id=_id(),
            graph_version=1,
            goal=state.goal,
            snapshot=state.snapshot,
            tasks=(task,),
            registry=registry,
            adapters={tool.tool_id: manifest},
            run_budget=state.budget,
        )

        def append(
            sequence: int, event_type: AgentEventType, payload: object = None
        ) -> None:
            self.store.append_event(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_event_version=sequence - 1,
                idempotency_key=f"ready-for-review-{sequence:04d}",
                event=AgentSupervisorEvent(
                    event_id=_id(),
                    run_id=created.run_id,
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=self.now + timedelta(seconds=sequence),
                    actor_id=self.actor_id,
                    payload=payload,
                ),
            )

        append(2, AgentEventType.TASK_GRAPH_ACCEPTED, TaskGraphPayload(graph))
        state = self.store.replay_run(run_id=created.run_id)
        approval = ApprovalRecord.build(
            approval_id=_id(),
            task=task,
            graph_hash=graph.graph_hash,
            gate=task.approval_gate,
            approved_by=self.actor_id,
        )
        append(3, AgentEventType.APPROVAL_GRANTED, ApprovalPayload(approval))
        state = self.store.replay_run(run_id=created.run_id)
        attempt_id = next(
            command.attempt_id
            for command in decide_next_commands(state)
            if command.task_id == task.task_id and command.attempt_id is not None
        )
        append(
            4,
            AgentEventType.TASK_STARTED,
            TaskStartedPayload(
                task_id=task.task_id,
                attempt_id=attempt_id,
                graph_hash=graph.graph_hash,
                input_hash=task.input_hash,
            ),
        )
        artifact = ArtifactReceipt(
            artifact_id=_id(),
            artifact_kind="CASE_REVIEW_MEMO",
            content_hash=sha256(b"reviewed-document").hexdigest(),
            byte_size=4096,
            source_input_hash=task.input_hash,
            managed_derivative=True,
        )
        append(
            5,
            AgentEventType.TASK_RESULT_RECORDED,
            TaskResultPayload(
                TaskResultReceipt(
                    receipt_id=_id(),
                    task_id=task.task_id,
                    attempt_id=attempt_id,
                    input_hash=task.input_hash,
                    adapter_id=manifest.adapter_id,
                    adapter_version=manifest.adapter_version,
                    status=ResultStatus.SUCCEEDED,
                    external_submission_state=(
                        ExternalSubmissionState.NOT_APPLICABLE
                    ),
                    output_hash=sha256(b"reviewed-output").hexdigest(),
                    error_code=None,
                    external_request_id=None,
                    runtime_seconds=1,
                    cost_minor_units=0,
                    external_calls=0,
                    artifacts=(artifact,),
                )
            ),
        )
        append(6, AgentEventType.VERIFICATION_STARTED)
        append(
            7,
            AgentEventType.VERIFICATION_PASSED,
            VerificationPayload(sha256(b"verification-passed").hexdigest()),
        )
        return created.run_id, 7, artifact

    def test_create_run_persists_only_goal_and_waits_for_real_planner(self) -> None:
        result = self.create()
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(result.phase_label, "准备分析案件")
        self.assertEqual(result.progress_total, 0)
        self.assertEqual(result.snapshot_matter_version, 7)
        event = self.store.events[result.run_id][0]
        self.assertIsInstance(event.payload, RunCreatedPayload)
        assert isinstance(event.payload, RunCreatedPayload)
        self.assertEqual(event.payload.snapshot.snapshot_hash, sha256(b"snapshot").hexdigest())
        self.assertEqual(event.payload.budget.max_external_calls, 50)
        self.assertEqual(event.payload.budget.max_cost_minor_units, 100_000)
        self.assertNotIn("tool", result.__dict__)
        self.assertNotIn("provider", result.__dict__)
        self.assertFalse(result.active_plan_execution)

    def test_material_review_continuation_uses_only_server_owned_candidate_bindings(self) -> None:
        created = self.create()
        prepared_stage = SimpleNamespace(stage_hash="a" * 64)
        with patch(
            "case_api.web_case_agent_control.prepare_case_analysis_stage",
            return_value=prepared_stage,
        ) as prepare:
            result = self.service.continue_from_material_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=created.run_id,
                expected_run_version=1,
                idempotency_key="case-agent-analysis-stage-0001",
                now=self.now + timedelta(seconds=1),
            )
        self.assertEqual(result.run_id, created.run_id)
        self.assertEqual(
            self.store.last_analysis_stage_event.event_type,
            AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED,
        )
        self.assertEqual(
            self.store.last_analysis_stage_event.sequence,
            2,
        )
        prepare.assert_called_once()
        _, arguments = prepare.call_args
        self.assertEqual(arguments["approved_by"], self.actor_id)
        self.assertEqual(len(arguments["candidate_bindings"]), 1)
        self.assertNotIn("candidate_bindings", self.store.last_analysis_stage_event.__dict__)

    def test_sealed_recovery_is_listed_as_review_only_without_changing_run_or_final_review(self) -> None:
        created = self.create()
        reader = _SealedRecoveryReader(_id())
        service = WebCaseAgentControlService(
            store=self.store,
            snapshot_reader=self.service._snapshot_reader,
            posture_reader=self.service._posture_reader,
            final_review_readiness=self.final_review_readiness,
            sealed_recovery_artifact_reader=reader,
        )

        artifacts = service.list_artifacts(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
        )

        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0].recovery_review_only)
        self.assertEqual(artifacts[0].status, "READY_FOR_REVIEW")
        self.assertEqual(reader.calls, [(self.matter_id, created.run_id)])
        self.assertEqual(self.final_review_readiness.calls, [])

    def test_web_projection_keeps_the_recovery_only_marker(self) -> None:
        projected = _project_case_agent_artifact(
            WebCaseAgentArtifactResponse(
                artifact_id=_id(),
                title="封存模型响应恢复 · 律师决策包候选",
                artifact_type="LAWYER_DECISION_PACKAGE_CANDIDATE",
                status="READY_FOR_REVIEW",
                review_required=True,
                recovery_review_only=True,
            )
        )
        self.assertTrue(projected["recovery_review_only"])

    def test_project_exposes_server_owned_input_lineage_without_plan_details(self) -> None:
        created = self.create()
        record = self.store.run_record(
            matter_id=self.matter_id,
            actor=self.actor,
            run_id=created.run_id,
        )
        result = self.service._project(
            replace(record, input_snapshot_status="PLAN_CANDIDATE_REGISTERED")
        )
        self.assertEqual(result.input_snapshot_status, "PLAN_CANDIDATE_REGISTERED")
        self.assertNotIn("plan", result.__dict__)
        self.assertNotIn("graph", result.__dict__)

    def test_execute_active_plan_builds_one_server_owned_v2_run(self) -> None:
        result = self.service.execute_active_plan(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=7,
            idempotency_key="execute-active-plan-0001",
            now=self.now,
        )
        event = self.store.events[result.run_id][0]
        assert isinstance(event.payload, RunCreatedPayload)
        # An activated plan can contain several local reviewable documents.
        # Its server-selected envelope must cover the aggregate document
        # budgets rather than reusing the one-shot analysis envelope.
        self.assertEqual(
            event.payload.budget,
            self.service._policy.active_plan_document_run_budget,
        )
        self.assertEqual(event.payload.budget.max_external_calls, 0)
        self.assertGreaterEqual(
            event.payload.budget.max_output_bytes,
            3 * 128 * 1024 * 1024,
        )
        goal = event.payload.goal
        self.assertEqual(
            goal.requested_deliverables,
            (
                AgentDeliverableKind.CASE_REVIEW_MEMO,
                AgentDeliverableKind.PAYMENT_LEDGER,
            ),
        )
        self.assertEqual(
            goal.active_plan_execution,
            self.store.active_plan_execution,
        )
        self.assertTrue(result.active_plan_execution)
        self.assertEqual(result.required_document_deliverables, ("CASE_REVIEW_MEMO", "PAYMENT_LEDGER"))
        from dataclasses import replace
        from case_api.web_app import _project_case_agent_run, WebRequestBlocked

        projected = _project_case_agent_run(result, expected_matter_id=self.matter_id)
        self.assertEqual(projected["required_document_deliverables"], ["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"])
        defence = replace(result, required_document_deliverables=("DEFENCE_STATEMENT",))
        self.assertEqual(_project_case_agent_run(defence, expected_matter_id=self.matter_id)["required_document_deliverables"], ["DEFENCE_STATEMENT"])
        for invalid in (("UNKNOWN",), ("DEFENCE_STATEMENT", "DEFENCE_STATEMENT"), ([],)):
            with self.assertRaises(WebRequestBlocked):
                _project_case_agent_run(replace(result, required_document_deliverables=invalid), expected_matter_id=self.matter_id)
        self.assertIn("已激活的动态计划", goal.objective)
        self.assertNotIn(self.store.active_plan_execution.plan_hash, goal.objective)
        replay = self.service.execute_active_plan(
            identity=self.identity,
            matter_id=self.matter_id,
            expected_matter_version=7,
            idempotency_key="a-different-retry-key",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(replay.run_id, result.run_id)
        self.assertEqual(len(self.store.events[result.run_id]), 1)

        reconciled = self.service.reconcile_active_plan_execution(
            identity=self.identity,
            matter_id=self.matter_id,
            plan_id=self.store.active_plan_execution.plan_id,
            expected_matter_version=7,
            idempotency_key="execute-active-plan-0001",
        )
        self.assertIsNotNone(reconciled)
        assert reconciled is not None
        self.assertEqual(reconciled.run_id, result.run_id)
        original_run_record = self.store.run_record

        def progressed_run_record(**kwargs):
            record = original_run_record(**kwargs)
            progressed_state = replace(
                record.projection.state,
                snapshot=CaseSnapshotRef(
                    matter_id=self.matter_id,
                    matter_version=9,
                    snapshot_hash=sha256(b"progressed snapshot").hexdigest(),
                    schema_version="case-ledger-snapshot-v1",
                ),
            )
            return replace(
                record,
                projection=replace(record.projection, state=progressed_state),
            )

        with patch.object(self.store, "run_record", side_effect=progressed_run_record):
            progressed = self.service.reconcile_active_plan_execution(
                identity=self.identity,
                matter_id=self.matter_id,
                plan_id=self.store.active_plan_execution.plan_id,
                expected_matter_version=7,
                idempotency_key="execute-active-plan-0001",
            )
        self.assertIsNotNone(progressed)
        self.assertIsNone(
            self.service.reconcile_active_plan_execution(
                identity=self.identity,
                matter_id=self.matter_id,
                plan_id=self.store.active_plan_execution.plan_id,
                expected_matter_version=7,
                idempotency_key="never-submitted-execution-key",
            )
        )
        with self.assertRaisesRegex(WebCaseAgentControlBlocked, "不一致"):
            self.service.reconcile_active_plan_execution(
                identity=self.identity,
                matter_id=self.matter_id,
                plan_id=_id(),
                expected_matter_version=7,
                idempotency_key="execute-active-plan-0001",
            )

    def test_recovery_run_uses_run_uuid_as_fixed_canonical_goal_uuid(self) -> None:
        run_key = "case-agent-recovery-run-0001"
        run_id = derive_web_case_agent_entity_id(
            actor=self.actor,
            matter_id=self.matter_id,
            idempotency_key=run_key,
            entity="run",
        )
        result = self.service.create_recovery_run(
            identity=self.identity,
            matter_id=self.matter_id,
            replacement_run_id=run_id,
            expected_matter_version=7,
            idempotency_key=run_key,
            now=self.now,
        )
        event = self.store.events[result.run_id][0]
        assert isinstance(event.payload, RunCreatedPayload)
        self.assertEqual(event.payload.goal.goal_id, run_id)
        self.assertEqual(event.payload.goal.objective, RECOVERY_OBJECTIVE)
        self.assertEqual(
            event.payload.goal.success_criteria, RECOVERY_SUCCESS_CRITERIA
        )
        self.assertEqual(event.payload.goal.constraints, RECOVERY_CONSTRAINTS)

    def test_recovery_run_rejects_non_server_run_identity(self) -> None:
        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "恢复运行编号与服务器恢复意图不一致"
        ):
            self.service.create_recovery_run(
                identity=self.identity,
                matter_id=self.matter_id,
                replacement_run_id=_id(),
                expected_matter_version=7,
                idempotency_key="case-agent-recovery-run-0002",
                now=self.now,
            )

    def test_create_run_requires_current_confirmed_case_posture(self) -> None:
        self.service._posture_reader = _PostureReader(current=False)
        with self.assertRaisesRegex(WebCasePostureBlocked, "先由主办律师确认"):
            self.create()
        self.assertEqual(self.store.events, {})

    def test_create_run_maps_existing_candidate_conflict_to_actionable_boundary(self) -> None:
        self.store.create_run_error = AgentSupervisorBlocked(
            "the current Agent result still awaits lawyer review for this case snapshot"
        )

        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "已有待律师审阅的办案成果"
        ):
            self.create()
        self.assertEqual(self.store.events, {})

    def test_defence_candidate_requires_current_first_instance_defendant_posture(self) -> None:
        self.service._posture_reader = _PostureReader(represented_position="PLAINTIFF")
        with self.assertRaisesRegex(WebCaseAgentControlBlocked, "一审被告代理情境"):
            self.service.create_run(
                identity=self.identity,
                matter_id=self.matter_id,
                objective="围绕已确认诉请形成被告侧应诉候选。",
                success_criteria=("逐项回应已确认诉请",),
                constraints=("不得自动对外发送",),
                requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
                expected_matter_version=7,
                idempotency_key="case-agent-defence-wrong-posture-0001",
                now=self.now,
            )
        self.assertEqual(self.store.events, {})

        self.service._posture_reader = _PostureReader()
        created = self.service.create_run(
            identity=self.identity,
            matter_id=self.matter_id,
            objective="围绕已确认诉请形成被告侧应诉候选。",
            success_criteria=("逐项回应已确认诉请",),
            constraints=("不得自动对外发送",),
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            expected_matter_version=7,
            idempotency_key="case-agent-defence-valid-posture-0001",
            now=self.now,
        )
        event = self.store.events[created.run_id][0]
        assert isinstance(event.payload, RunCreatedPayload)
        self.assertEqual(
            event.payload.goal.requested_deliverables,
            (AgentDeliverableKind.DEFENCE_STATEMENT,),
        )
        self.assertEqual(event.payload.budget.max_external_calls, 2)
        self.assertEqual(event.payload.budget.max_cost_minor_units, 240)

    def test_defence_candidate_with_confirmed_fact_uses_analysis_only_budget(self) -> None:
        snapshot_reader = self.service._snapshot_reader
        assert isinstance(snapshot_reader, _SnapshotReader)
        snapshot_reader.snapshot = replace(
            snapshot_reader.snapshot,
            facts=(
                {
                    "fact_id": _id(),
                    "original_text": "借款交付已由主办律师确认。",
                    "status": "CONFIRMED",
                },
            ),
        )

        created = self.service.create_run(
            identity=self.identity,
            matter_id=self.matter_id,
            objective="围绕已确认事实形成被告侧应诉候选。",
            success_criteria=("形成来源受控的律师决策包候选",),
            constraints=("不得自动对外发送",),
            requested_deliverables=(AgentDeliverableKind.DEFENCE_STATEMENT,),
            expected_matter_version=7,
            idempotency_key="case-agent-defence-confirmed-fact-0001",
            now=self.now,
        )

        event = self.store.events[created.run_id][0]
        assert isinstance(event.payload, RunCreatedPayload)
        self.assertEqual(event.payload.budget.max_external_calls, 1)
        self.assertEqual(event.payload.budget.max_cost_minor_units, 120)

    def test_same_idempotency_key_derives_same_run_identity(self) -> None:
        first = self.create()
        second = self.create()
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.store.events[first.run_id]), 1)

    def test_pause_and_resume_are_reduced_supervisor_events(self) -> None:
        created = self.create()
        paused = self.service.pause_run(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
            expected_run_version=created.version,
            idempotency_key="case-agent-pause-0001",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(paused.status, "PAUSED")
        resumed = self.service.resume_run(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
            expected_run_version=paused.version,
            idempotency_key="case-agent-resume-0001",
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(resumed.status, "CREATED")

    def test_final_review_completion_is_built_from_authoritative_run(self) -> None:
        run_id, reviewed_version, artifact = self.ready_for_review()
        before = self.store.replay_run(run_id=run_id)

        result = self.service.complete_run(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=run_id,
            expected_run_version=reviewed_version,
            idempotency_key="case-agent-final-review-0001",
            now=self.now + timedelta(seconds=20),
        )

        self.assertEqual(result.run.status, "COMPLETED")
        self.assertEqual(result.run.version, reviewed_version + 1)
        self.assertEqual(result.receipt.run_status, "COMPLETED")
        self.assertEqual(result.receipt.verification_status, "PASSED")
        self.assertEqual(result.receipt.reviewed_artifact_count, 1)
        self.assertEqual(
            result.receipt.completed_run_version, reviewed_version + 1
        )
        event = self.store.events[run_id][-1]
        self.assertEqual(event.event_type, AgentEventType.RUN_COMPLETED)
        self.assertEqual(event.actor_id, self.actor_id)
        final_review = event.payload.final_review
        self.assertEqual(final_review.run_id, run_id)
        self.assertEqual(final_review.graph_hash, before.graph.graph_hash)
        self.assertEqual(
            final_review.verification_hash, before.verification_hash
        )
        self.assertEqual(final_review.approved_by, self.actor_id)
        self.assertNotEqual(final_review.artifact_manifest_hash, artifact.content_hash)
        self.assertEqual(
            self.final_review_readiness.calls,
            [(self.matter_id, run_id, 1)],
        )

    def test_final_review_blocks_when_any_review_surface_is_stale(self) -> None:
        run_id, reviewed_version, _ = self.ready_for_review()
        self.final_review_readiness.blocked = True

        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "无法完整复核"
        ):
            self.service.complete_run(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=run_id,
                expected_run_version=reviewed_version,
                idempotency_key="case-agent-final-review-stale-0001",
                now=self.now + timedelta(seconds=20),
            )

        self.assertEqual(
            self.store.replay_run(run_id=run_id).status.value,
            "READY_FOR_REVIEW",
        )
        self.assertEqual(len(self.store.events[run_id]), reviewed_version)

    def test_final_review_same_key_replays_but_new_key_conflicts(self) -> None:
        run_id, reviewed_version, _ = self.ready_for_review()
        kwargs = {
            "identity": self.identity,
            "matter_id": self.matter_id,
            "run_id": run_id,
            "expected_run_version": reviewed_version,
            "idempotency_key": "case-agent-final-review-replay-0001",
        }
        first = self.service.complete_run(
            **kwargs,
            now=self.now + timedelta(seconds=20),
        )
        event_count = len(self.store.events[run_id])
        self.final_review_readiness.blocked = True
        replay = self.service.complete_run(
            **kwargs,
            now=self.now + timedelta(seconds=40),
        )
        self.assertEqual(replay, first)
        self.assertEqual(len(self.store.events[run_id]), event_count)

        exact = self.service.reconcile_completion(**kwargs)
        self.assertEqual(exact, first.receipt)
        self.assertIsNone(
            self.service.reconcile_completion(
                **{
                    **kwargs,
                    "idempotency_key": "case-agent-final-review-other-0001",
                }
            )
        )
        self.assertIsNone(
            self.service.reconcile_completion(
                **{
                    **kwargs,
                    "expected_run_version": reviewed_version + 1,
                }
            )
        )

        with self.assertRaises(VersionConflict):
            self.service.complete_run(
                **{
                    **kwargs,
                    "idempotency_key": "case-agent-final-review-newkey-0001",
                },
                now=self.now + timedelta(seconds=60),
            )
        with self.assertRaises(VersionConflict):
            self.service.complete_run(
                **{
                    **kwargs,
                    "expected_run_version": reviewed_version + 1,
                },
                now=self.now + timedelta(seconds=80),
            )

    def test_final_review_requires_approver_role_and_verified_ready_state(self) -> None:
        run_id, reviewed_version, _ = self.ready_for_review()
        assistant_identity = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "actor": Actor(
                    self.actor_id,
                    self.firm_id,
                    frozenset({Role.ASSISTANT}),
                ),
            }
        )
        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "主办律师或复核律师"
        ):
            self.service.complete_run(
                identity=assistant_identity,
                matter_id=self.matter_id,
                run_id=run_id,
                expected_run_version=reviewed_version,
                idempotency_key="case-agent-final-review-role-0001",
                now=self.now + timedelta(seconds=20),
            )
        reviewer_id = _id()
        reviewer_identity = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "actor": Actor(
                    reviewer_id,
                    self.firm_id,
                    frozenset({Role.REVIEWER}),
                ),
            }
        )
        reviewed = self.service.complete_run(
            identity=reviewer_identity,
            matter_id=self.matter_id,
            run_id=run_id,
            expected_run_version=reviewed_version,
            idempotency_key="case-agent-final-review-reviewer-0001",
            now=self.now + timedelta(seconds=21),
        )
        self.assertEqual(reviewed.run.status, "COMPLETED")
        self.assertEqual(
            self.store.events[run_id][-1].payload.final_review.approved_by,
            reviewer_id,
        )

        created = self.service.create_run(
            identity=self.identity,
            matter_id=self.matter_id,
            objective="核验未就绪状态不能终审。",
            success_criteria=("必须先完成服务器核验",),
            constraints=("不得绕过终审前置条件",),
            expected_matter_version=7,
            idempotency_key="case-agent-create-unready-0001",
            now=self.now,
        )
        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "通过服务器核验"
        ):
            self.service.complete_run(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=created.run_id,
                expected_run_version=created.version,
                idempotency_key="case-agent-final-review-unready-0001",
                now=self.now + timedelta(seconds=20),
            )

    def test_final_review_binds_firm_matter_and_run(self) -> None:
        run_id, reviewed_version, _ = self.ready_for_review()
        foreign_identity = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "actor": Actor(
                    self.actor_id,
                    _id(),
                    frozenset({Role.LEAD_LAWYER}),
                ),
            }
        )
        attempts = (
            (foreign_identity, self.matter_id, run_id),
            (self.identity, _id(), run_id),
        )
        for identity, matter_id, candidate_run_id in attempts:
            with self.subTest(matter_id=matter_id, run_id=candidate_run_id):
                with self.assertRaisesRegex(
                    WebCaseAgentControlBlocked, "不属于当前案件或律所"
                ):
                    self.service.complete_run(
                        identity=identity,
                        matter_id=matter_id,
                        run_id=candidate_run_id,
                        expected_run_version=reviewed_version,
                        idempotency_key="case-agent-final-review-scope-0001",
                        now=self.now + timedelta(seconds=20),
                    )

        foreign_run_id = _id()
        self.store.events[foreign_run_id] = list(self.store.events[run_id])
        self.store.created_at[foreign_run_id] = self.store.created_at[run_id]
        with self.assertRaisesRegex(
            WebCaseAgentControlBlocked, "不属于当前案件或律所"
        ):
            self.service.complete_run(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=foreign_run_id,
                expected_run_version=reviewed_version,
                idempotency_key="case-agent-final-review-run-scope-0001",
                now=self.now + timedelta(seconds=20),
            )

    def test_non_oidc_identity_and_worker_are_rejected(self) -> None:
        local = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "authentication_method": AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            }
        )
        with self.assertRaisesRegex(WebCaseAgentControlBlocked, "MFA"):
            self.service.get_current_run(identity=local, matter_id=self.matter_id)
        worker = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "actor": Actor(
                    self.actor_id, self.firm_id, frozenset({Role.SYSTEM_WORKER})
                ),
            }
        )
        with self.assertRaisesRegex(WebCaseAgentControlBlocked, "不能操作"):
            self.service.get_current_run(identity=worker, matter_id=self.matter_id)

    def test_empty_decisions_and_approvals_are_reported_honestly(self) -> None:
        created = self.create()
        self.assertEqual(
            self.service.list_decisions(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=created.run_id,
            ),
            (),
        )

    def test_reextraction_admission_failure_is_lawyer_readable(self) -> None:
        created = self.create()
        self.store.events[created.run_id].extend(
            (
                AgentSupervisorEvent(
                    event_id=_id(),
                    run_id=created.run_id,
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    sequence=2,
                    event_type=AgentEventType.PLANNING_STARTED,
                    occurred_at=self.now + timedelta(seconds=1),
                    actor_id=self.actor_id,
                    payload=None,
                ),
                AgentSupervisorEvent(
                    event_id=_id(),
                    run_id=created.run_id,
                    firm_id=self.firm_id,
                    matter_id=self.matter_id,
                    sequence=3,
                    event_type=AgentEventType.PLANNING_FAILED,
                    occurred_at=self.now + timedelta(seconds=2),
                    actor_id=self.actor_id,
                    payload=PlanningFailurePayload(
                        "REEXTRACTION_PAGE_LIMIT_EXCEEDED"
                    ),
                ),
            )
        )

        result = self.service.get_run(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
        )

        self.assertEqual(result.status, "WAITING_INPUT")
        self.assertEqual(
            result.failure_code, "REEXTRACTION_PAGE_LIMIT_EXCEEDED"
        )
        self.assertIn("超过单次 64 页", result.failure_message or "")
        self.assertIn("未提交不完整分析", result.failure_message or "")

    def test_structured_rejection_records_correction_and_replans(self) -> None:
        created = self.create()
        state = self.store.replay_run(run_id=created.run_id)
        tool = ToolDefinition(
            "write_case_derivative",
            "1.0.0",
            frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
            False,
            False,
            True,
        )
        registry = CaseSkillRegistry(
            tools=(tool,),
            skills=(
                SkillDefinition(
                    "case_derivative",
                    "1.0.0",
                    "案件派生件",
                    SkillMaturity.IMPLEMENTED,
                    frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                    (tool.tool_id,),
                    ApprovalGate.LAWYER_REVIEW,
                    "ManagedDerivative",
                    ("不得改写原件",),
                ),
            ),
        )
        manifest = RuntimeAdapterManifest(
            tool_id=tool.tool_id,
            adapter_id="draft-worker",
            adapter_version="1.0.0",
            execution_mode=AdapterExecutionMode.ISOLATED_CONTAINER,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash="3" * 64,
        )
        source_ref = f"work-plan-item:{uuid4()}"
        task_id = _id()
        graph = compile_task_graph(
            graph_id=_id(),
            graph_version=1,
            goal=state.goal,
            snapshot=CaseSnapshotRef(
                matter_id=self.matter_id,
                matter_version=state.snapshot.matter_version,
                snapshot_hash=state.snapshot.snapshot_hash,
                schema_version=state.snapshot.schema_version,
            ),
            tasks=(
                AgentTaskSpec(
                    task_id=task_id,
                    sequence=1,
                    title="形成可编辑文书候选",
                    purpose="根据当前案件计划形成律师可复核的候选",
                    rationale="当前动态计划要求该内部成果。",
                    dependency_ids=(),
                    input_refs=(source_ref,),
                    input_hash="4" * 64,
                    skill=SkillBinding(
                        "case_derivative",
                        "1.0.0",
                        tool.tool_id,
                        "1.0.0",
                        manifest.adapter_id,
                        manifest.adapter_version,
                    ),
                    granted_scopes=frozenset({CapabilityScope.MANAGED_DERIVATIVE_WRITE}),
                    capability=TaskCapabilityContract(
                        execution_mode=AdapterExecutionMode.ISOLATED_CONTAINER,
                        network_policy=NetworkPolicy.DENY,
                        allowed_domains=(),
                        sandbox_profile="draft-only",
                        sandbox_policy_version=manifest.sandbox_policy_version,
                        sandbox_policy_hash=manifest.sandbox_policy_hash,
                        reads_case_objects=(source_ref,),
                        writes_managed_derivatives=True,
                        external_request_approval_required=False,
                    ),
                    risk_level=AgentRiskLevel.HIGH,
                    autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
                    approval_gate=ApprovalGate.LAWYER_REVIEW,
                    retry_mode=RetryMode.IDEMPOTENT,
                    budget=TaskResourceBudget(2, 120, 0, 0, 1_000_000),
                ),
            ),
            registry=registry,
            adapters={tool.tool_id: manifest},
            run_budget=state.budget,
        )
        self.store.events[created.run_id].append(
            AgentSupervisorEvent(
                event_id=_id(),
                run_id=created.run_id,
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                sequence=2,
                event_type=AgentEventType.TASK_GRAPH_ACCEPTED,
                occurred_at=self.now + timedelta(seconds=1),
                actor_id=self.actor_id,
                payload=TaskGraphPayload(graph),
            )
        )
        decisions = self.service.list_decisions(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
        )
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].decision_id, task_id)
        corrected = self.service.submit_decision(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=created.run_id,
            decision_id=task_id,
            option_id="LAWYER_REJECT_WRONG_LEGAL_DIRECTION",
            note="该任务应先核对现行与争议期间的法律规则。",
            expected_run_version=2,
            idempotency_key="lawyer-correction-0001",
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(corrected.status, "STALE")
        self.assertEqual(corrected.phase_label, "案件已变化，等待重规划")
        self.assertEqual(
            self.service.list_approvals(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=created.run_id,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
