from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
import inspect
import unittest
from uuid import UUID, uuid4, uuid5

from case_kernel.case_agent_external_failure import CaseAgentKnownExternalFailure
from case_kernel.case_agent_planner import (
    CasePlannerAdmissionBlocked,
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
)
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    AgentTaskStatus,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
    SupervisorCommandKind,
    TaskGraphPayload,
    TaskResultPayload,
    VerificationPayload,
    reduce_agent_event,
)
from case_kernel.case_agent_worker import (
    AgentWorkerReadinessProbe,
    AgentWorkerStep,
    CaseAgentReconciliationUnavailable,
    CaseAgentWorker,
    CaseAgentWorkerBlocked,
    DurablePlanningClaim,
    DurableTaskClaim,
    ReapedAttemptResult,
    ReapedPlanningAttemptResult,
    TaskAdapterOutcome,
)
from case_kernel.case_agent_verifier import CaseAgentRunVerifier
from case_kernel.deepseek_case_agent_planner import (
    DeepSeekPlannerPreDispatchFailure,
    DeepSeekPlannerUnknownSubmission,
)
from case_kernel.models import Actor, Role
from backend.tests import test_case_agent_supervisor as supervisor_fixture


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Adapter:
    def __init__(
        self,
        manifest,
        *,
        outcome=None,
        raise_after_boundary=False,
        known_failure_after_boundary=False,
        reconciliation_error=None,
    ):
        self.manifest = manifest
        self.outcome = outcome
        self.raise_after_boundary = raise_after_boundary
        self.known_failure_after_boundary = known_failure_after_boundary
        self.reconciliation_error = reconciliation_error
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, *, context):
        self.execute_calls += 1
        if self.raise_after_boundary:
            external_request_id = str(uuid5(UUID(context.claim.attempt_id), "provider"))
            context.begin_external_submission(
                external_request_id=external_request_id,
                destination=context.task.capability.allowed_domains[0],
                request_hash=digest("provider-request"),
            )
            raise TimeoutError("provider response was lost")
        if self.known_failure_after_boundary:
            external_request_id = str(uuid5(UUID(context.claim.attempt_id), "provider"))
            context.begin_external_submission(
                external_request_id=external_request_id,
                destination=context.task.capability.allowed_domains[0],
                request_hash=digest("provider-request"),
            )
            raise CaseAgentKnownExternalFailure(
                external_request_id=external_request_id,
                error_code="QWEN_VISUAL_OCR_HTTP_400",
            )
        return self.outcome

    def reconcile(self, *, context):
        self.reconcile_calls += 1
        if self.reconciliation_error is not None:
            raise self.reconciliation_error
        return self.outcome


class _Planner:
    planner_id = "bounded-test-planner"

    def __init__(self, proposal=object(), error=None):
        self.proposal = proposal
        self.error = error
        self.calls = 0

    def plan(self, **_):
        self.calls += 1
        if self.error:
            raise self.error
        return self.proposal


class _Compiler:
    def __init__(self, graph, *, error=None):
        self.graph = graph
        self.error = error
        self.compile_calls = 0

    def semantic_skill_catalog(self):
        return (SimpleNamespace(skill_id="case_reading"),)

    def compile(self, **_):
        self.compile_calls += 1
        if self.error is not None:
            raise self.error
        return self.graph


class _SnapshotProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def build_for_run(self, **_):
        return self.snapshot


class _NoopArtifactAccess:
    def read_managed_artifact(self, **_):
        raise AssertionError("readiness must not access an artifact")


class _WorkPlanPromotion:
    def __init__(self):
        self.calls = []

    def promote_verified_graph(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class _CommitLostWorkPlanPromotion(_WorkPlanPromotion):
    def promote_verified_graph(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise TimeoutError("commit result was lost")
        return object()


class _LedgerExtractionStaging:
    def __init__(
        self,
        artifact_ids=(),
        *,
        lose_first_commit=False,
        order=None,
        candidate_count=0,
    ):
        self.artifact_ids = tuple(sorted(artifact_ids))
        self.lose_first_commit = lose_first_commit
        self.discover_calls = []
        self.stage_calls = []
        self.order = order
        self.candidate_count = candidate_count

    def discover_verified_artifact_ids(self, **kwargs):
        self.discover_calls.append(kwargs)
        if self.order is not None:
            self.order.append("discover")
        return self.artifact_ids

    def stage_verified_artifact(self, **kwargs):
        self.stage_calls.append(kwargs)
        if self.order is not None:
            self.order.append(f"stage:{kwargs['artifact_id']}")
        if self.lose_first_commit and len(self.stage_calls) == 1:
            raise TimeoutError("private staging commit result was lost")
        return SimpleNamespace(staged_candidate_count=self.candidate_count)


class _LedgerExceptionFollowups:
    def __init__(
        self,
        *,
        satisfy_result=None,
        satisfy_error=None,
        lose_first_satisfy=False,
    ):
        self.bind_calls = []
        self.satisfy_calls = []
        self.satisfy_result = satisfy_result
        self.satisfy_error = satisfy_error
        self.lose_first_satisfy = lose_first_satisfy

    def bind_reextraction_task_for_claim(self, **kwargs):
        self.bind_calls.append(kwargs)
        return None

    def satisfy_reextraction_graph(self, **kwargs):
        self.satisfy_calls.append(kwargs)
        if self.lose_first_satisfy and len(self.satisfy_calls) == 1:
            raise TimeoutError("re-extraction set commit result was lost")
        if self.satisfy_error is not None:
            raise self.satisfy_error
        return self.satisfy_result


class _Store:
    def __init__(self, state):
        self.state = state
        self.reaped = None
        self.reaped_plan = None
        self.receipts = []
        self.planning_failures = []
        self.boundaries = []
        self.heartbeat = None
        self.reachable = True
        self.helper = None
        self.planning_heartbeats = 0
        self.snapshot_refresh_version = None

    def apply_pending_snapshot_refresh(self, **_):
        result, self.snapshot_refresh_version = (
            self.snapshot_refresh_version,
            None,
        )
        return result

    def read_projection(self, **_):
        from case_kernel.case_agent_supervisor import decide_next_commands

        return SimpleNamespace(
            state=self.state,
            next_commands=decide_next_commands(self.state),
            checkpoint_verified=True,
        )

    def reap_expired_attempt(self, **_):
        result, self.reaped = self.reaped, None
        return result

    def reap_expired_planning_attempt(self, **_):
        result, self.reaped_plan = self.reaped_plan, None
        return result

    def claim_next_dispatchable_task(self, **kwargs):
        helper = self.helper
        assert helper is not None
        self.state = helper.start_ready_task(
            self.state, sequence=self.state.event_version + 1
        )
        runtime = next(
            item for item in self.state.tasks if item.status is AgentTaskStatus.RUNNING
        )
        return DurableTaskClaim(
            run_id=self.state.run_id,
            task_id=runtime.spec.task_id,
            attempt_id=runtime.active_attempt_id,
            event_version=self.state.event_version,
            attempt_version=1,
            lease_owner=kwargs["lease_owner"],
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
            task=runtime.spec,
        )

    def claim_next_reconciliation(self, **kwargs):
        runtime = next(
            item for item in self.state.tasks if item.status is AgentTaskStatus.UNKNOWN
        )
        return DurableTaskClaim(
            run_id=self.state.run_id,
            task_id=runtime.spec.task_id,
            attempt_id=runtime.active_attempt_id,
            event_version=self.state.event_version,
            attempt_version=3,
            lease_owner=kwargs["lease_owner"],
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
            task=runtime.spec,
            reconciliation=True,
            external_request_id=runtime.receipts[-1].external_request_id,
        )

    def heartbeat_attempt(self, **_):
        return _["expected_attempt_version"] + 1, datetime.now(timezone.utc) + timedelta(seconds=120)

    def record_external_submission_started(self, **kwargs):
        self.boundaries.append(kwargs)
        return kwargs["expected_attempt_version"] + 1

    def record_receipt(self, *, receipt, **_):
        self.receipts.append(receipt)
        helper = self.helper
        assert helper is not None
        self.state = reduce_agent_event(
            self.state,
            helper.event(
                self.state.event_version + 1,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(receipt),
                actor_id=helper.lawyer_id,
            ),
        )

    def claim_next_planning_command(self, **kwargs):
        helper = self.helper
        assert helper is not None
        kind = next(iter(self.read_projection().next_commands)).kind
        self.state = reduce_agent_event(
            self.state,
            helper.event(
                self.state.event_version + 1,
                AgentEventType.PLANNING_STARTED,
                actor_id=helper.lawyer_id,
            ),
        )
        return DurablePlanningClaim(
            planning_attempt_id=str(uuid4()),
            run_id=self.state.run_id,
            command_kind=kind,
            command_id=digest(f"{kind}:{self.state.run_id}"),
            planning_hash=kwargs["planning_hash"],
            claim_lease_id=str(uuid4()),
            lease_token=str(uuid4()),
            external_request_id=str(uuid4()),
            event_version=self.state.event_version,
            attempt_version=1,
            lease_owner=kwargs["lease_owner"],
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
            graph_id=str(uuid4()),
            graph_version=1 if self.state.graph is None else self.state.graph.graph_version + 1,
            state=self.state,
        )

    def heartbeat_planning_attempt(self, **_):
        self.planning_heartbeats += 1
        return _["expected_attempt_version"] + 1, datetime.now(timezone.utc) + timedelta(seconds=120)

    def accept_planning_graph(self, *, graph, **_):
        helper = self.helper
        assert helper is not None
        self.state = reduce_agent_event(
            self.state,
            helper.event(
                self.state.event_version + 1,
                AgentEventType.TASK_GRAPH_ACCEPTED,
                TaskGraphPayload(graph),
                actor_id=helper.lawyer_id,
            ),
        )
        return self.state.event_version

    def record_planning_failure(self, **kwargs):
        self.planning_failures.append(kwargs)

    def probe_case_agent_store(self, *, firm_id=None):
        return self.reachable

    def record_worker_heartbeat(self, heartbeat):
        self.heartbeat = heartbeat

    def latest_worker_heartbeat(self, **_):
        return self.heartbeat


class CaseAgentWorkerTests(unittest.TestCase):
    def test_replanning_uses_remaining_budget_without_mutating_original(self):
        from dataclasses import replace
        from case_kernel.case_agent_supervisor import BudgetUsage
        from case_kernel.case_agent_worker import remaining_planning_budget
        original = reduce_agent_event(None, self.helper.created())
        used = BudgetUsage(attempts=2, external_calls=1, runtime_seconds=100,
                           cost_minor_units=30, output_bytes=500)
        state = replace(original, budget_usage=used)
        remaining = remaining_planning_budget(state)
        self.assertEqual(remaining.max_total_attempts, original.budget.max_total_attempts - 2)
        self.assertEqual(remaining.max_external_calls, original.budget.max_external_calls - 1)
        self.assertEqual(remaining.max_runtime_seconds, original.budget.max_runtime_seconds - 100)
        self.assertEqual(remaining.max_cost_minor_units, original.budget.max_cost_minor_units - 30)
        self.assertEqual(remaining.max_output_bytes, original.budget.max_output_bytes - 500)
        self.assertEqual(state.budget, original.budget)
        self.assertEqual(state.budget_usage, used)

    def test_next_stage_budget_failure_is_a_preserved_checkpoint(self):
        from dataclasses import replace
        from case_kernel.case_agent_supervisor import BudgetUsage
        from case_kernel.case_agent_planner import CasePlannerBudgetExceeded
        state = replace(reduce_agent_event(None, self.helper.created()),
                        budget_usage=BudgetUsage(external_calls=1))
        task = self.helper.local_task()
        compiler = _Compiler(object(), error=CasePlannerBudgetExceeded(
            dimension="external-call", required=1, available=0, task_count=2))
        worker, store = self._worker(state=state,
            adapter=_Adapter(self.helper.adapters[task.skill.tool_id]), compiler=compiler)
        result = worker.process_run_once(matter_id=self.helper.matter_id, run_id=self.helper.run_id)
        self.assertEqual(result.reason_code, "NEXT_STAGE_BUDGET_REVIEW_REQUIRED")
        self.assertEqual(store.planning_failures[-1]["error_code"], result.reason_code)
        self.assertEqual(store.state.budget_usage, state.budget_usage)

    def setUp(self):
        self.helper = supervisor_fixture.CaseAgentSupervisorTests(methodName="runTest")
        self.helper.setUp()
        self.worker_actor = Actor(
            str(uuid4()), self.helper.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.verifier_actor = Actor(
            str(uuid4()), self.helper.firm_id, frozenset({Role.SYSTEM_WORKER})
        )

    def _readiness_verifier(self):
        return CaseAgentRunVerifier(
            verifier_id="test-run-verifier",
            verifier_version="1.0.0",
            artifact_access=_NoopArtifactAccess(),
            artifact_verifiers={},
        )

    def _planning_snapshot(self):
        return CasePlanningSnapshot.build(
            case_snapshot=self.helper.snapshot,
            authorized_inputs=(
                PlanningInputRef(
                    ref_id="case-object-1",
                    kind=PlanningInputKind.MATERIAL,
                    object_version="v1",
                    content_hash=digest("case-object-1"),
                    status=PlanningInputStatus.AVAILABLE,
                    allowed_skill_ids=("case_reading",),
                ),
            ),
            signals=(),
        )

    def test_exact_ledger_exchange_absence_remains_known_not_submitted(self):
        external_request_id = str(uuid4())
        task = SimpleNamespace(
            skill=SimpleNamespace(
                skill_id="case_ledger_extraction",
                skill_version="1.0.0",
                tool_id="extract_case_ledger",
                tool_version="1.0.0",
            ),
            capability=SimpleNamespace(
                network_policy=NetworkPolicy.EXACT_ALLOWLIST
            ),
        )
        claim = SimpleNamespace(task=task)
        context = SimpleNamespace(
            external_boundary_committed=True,
            external_request_id=external_request_id,
        )
        outcome = TaskAdapterOutcome(
            status=ResultStatus.FAILED,
            external_submission_state=ExternalSubmissionState.NOT_SUBMITTED,
            output_hash=None,
            error_code="LEDGER_EXCHANGE_NOT_CREATED",
            external_request_id=external_request_id,
            runtime_seconds=0,
            cost_minor_units=0,
            external_calls=0,
        )

        CaseAgentWorker._validate_outcome(
            claim=claim,
            context=context,
            outcome=outcome,
            reconcile=True,
        )

    def test_exact_ledger_connect_failure_remains_known_not_submitted(self):
        external_request_id = str(uuid4())
        task = SimpleNamespace(
            skill=SimpleNamespace(
                skill_id="case_ledger_extraction",
                skill_version="1.0.0",
                tool_id="extract_case_ledger",
                tool_version="1.0.0",
            ),
            capability=SimpleNamespace(
                network_policy=NetworkPolicy.EXACT_ALLOWLIST
            ),
        )
        outcome = TaskAdapterOutcome(
            status=ResultStatus.FAILED,
            external_submission_state=ExternalSubmissionState.NOT_SUBMITTED,
            output_hash=None,
            error_code="LEDGER_PROVIDER_CONNECT_FAILED",
            external_request_id=external_request_id,
            runtime_seconds=0,
            cost_minor_units=0,
            external_calls=0,
        )

        CaseAgentWorker._validate_outcome(
            claim=SimpleNamespace(task=task),
            context=SimpleNamespace(
                external_boundary_committed=True,
                external_request_id=external_request_id,
            ),
            outcome=outcome,
            reconcile=False,
        )

    def test_exact_lawyer_analysis_connect_failure_remains_not_submitted(self):
        external_request_id = str(uuid4())
        task = SimpleNamespace(
            skill=SimpleNamespace(
                skill_id="lawyer_decision_package",
                skill_version="1.0.0",
                tool_id="analyze_lawyer_decision_package",
                tool_version="1.0.0",
            ),
            capability=SimpleNamespace(
                network_policy=NetworkPolicy.EXACT_ALLOWLIST
            ),
        )
        outcome = TaskAdapterOutcome(
            status=ResultStatus.FAILED,
            external_submission_state=ExternalSubmissionState.NOT_SUBMITTED,
            output_hash=None,
            error_code="LAWYER_ANALYSIS_CONNECT_FAILED",
            external_request_id=external_request_id,
            runtime_seconds=1,
            cost_minor_units=0,
            external_calls=0,
        )

        CaseAgentWorker._validate_outcome(
            claim=SimpleNamespace(task=task),
            context=SimpleNamespace(
                external_boundary_committed=True,
                external_request_id=external_request_id,
            ),
            outcome=outcome,
            reconcile=False,
        )

    def _verified_final_review_fixture(self, verification_hash):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.start_ready_task(state, sequence=3)
        receipt = self.helper.result_receipt(
            state, task_id=task.task_id, status=ResultStatus.SUCCEEDED
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                4,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(receipt),
            ),
        )
        state = reduce_agent_event(
            state, self.helper.event(5, AgentEventType.VERIFICATION_STARTED)
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                6,
                AgentEventType.VERIFICATION_PASSED,
                VerificationPayload(verification_hash),
            ),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        store = _Store(state)
        store.helper = self.helper
        return task, state, adapter, store

    def _worker(self, *, state, adapter, planner=None, compiler=None):
        store = _Store(state)
        store.helper = self.helper
        task = adapter.manifest.tool_id
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=planner or _Planner(),
            planner_compiler=compiler or _Compiler(object()),
            adapters={task: adapter},
            heartbeat_interval_seconds=29,
        )
        return worker, store

    def test_dedicated_system_worker_identity_is_required(self):
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        with self.assertRaises(PermissionError):
            CaseAgentWorker(
                worker_id="worker-1",
                actor=Actor(
                    str(uuid4()), self.helper.firm_id, frozenset({Role.LEAD_LAWYER})
                ),
                store=_Store(self.helper.created()),
                snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
                planner=_Planner(),
                planner_compiler=_Compiler(object()),
                adapters={task.skill.tool_id: adapter},
            )

    def test_local_task_is_claimed_dispatched_and_recorded_by_exact_adapter(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("worker-output"),
                None,
                None,
                2,
                0,
                0,
            ),
        )
        worker, store = self._worker(state=state, adapter=adapter)
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.TASK_SUCCEEDED)
        self.assertEqual(adapter.execute_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 0)
        self.assertEqual(len(store.receipts), 1)
        self.assertEqual(store.receipts[0].adapter_id, task.skill.adapter_id)

    def test_cancelled_run_is_idle_before_refresh_or_reaping(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = reduce_agent_event(
            state,
            self.helper.event(3, AgentEventType.RUN_CANCELLED),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("must-not-run"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        worker, store = self._worker(state=state, adapter=adapter)
        pending_refresh = state.event_version + 1
        pending_reap = ReapedAttemptResult(
            self.helper.run_id,
            task.task_id,
            str(uuid4()),
            state.event_version + 1,
            False,
        )
        store.snapshot_refresh_version = pending_refresh
        store.reaped = pending_reap

        result = worker.process_run_once(
            matter_id=self.helper.matter_id,
            run_id=self.helper.run_id,
        )

        self.assertEqual(result.step, AgentWorkerStep.IDLE)
        self.assertEqual(result.event_version, state.event_version)
        self.assertEqual(adapter.execute_calls, 0)
        self.assertEqual(store.snapshot_refresh_version, pending_refresh)
        self.assertIs(store.reaped, pending_reap)

    def test_restart_reaps_a_lost_lease_before_any_second_execution(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        worker, store = self._worker(state=state, adapter=adapter)
        store.reaped = ReapedAttemptResult(
            self.helper.run_id,
            task.task_id,
            str(uuid4()),
            state.event_version + 1,
            False,
        )
        # A new object represents a restarted process.  Recovery is driven by
        # the store, not by any memory retained in the old worker.
        restarted = CaseAgentWorker(
            worker_id="worker-2",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={task.skill.tool_id: adapter},
        )
        result = restarted.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.ATTEMPT_REAPED)
        self.assertEqual(adapter.execute_calls, 0)

    def test_unknown_external_task_only_uses_reconciliation_adapter_method(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        state = self.helper.start_ready_task(state, sequence=4)
        unknown = self.helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.UNKNOWN,
            external_state=ExternalSubmissionState.UNKNOWN,
            external_request_id="provider-request-unknown",
            external_calls=1,
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                5, AgentEventType.TASK_RESULT_RECORDED, TaskResultPayload(unknown)
            ),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.SUBMITTED,
                digest("reconciled-output"),
                None,
                "provider-request-unknown",
                1,
                0,
                0,
            ),
        )
        worker, store = self._worker(state=state, adapter=adapter)
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.TASK_SUCCEEDED)
        self.assertEqual(adapter.execute_calls, 0)
        self.assertEqual(adapter.reconcile_calls, 1)
        self.assertEqual(len(store.boundaries), 0)

    def test_unresolved_external_recovery_defers_without_a_second_submission(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        state = self.helper.start_ready_task(state, sequence=4)
        unknown = self.helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.UNKNOWN,
            external_state=ExternalSubmissionState.UNKNOWN,
            external_request_id="provider-request-unknown",
            external_calls=1,
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                5, AgentEventType.TASK_RESULT_RECORDED, TaskResultPayload(unknown)
            ),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            reconciliation_error=CaseAgentReconciliationUnavailable(
                "LAWYER_ANALYSIS_RECOVERY_UNRESOLVED"
            ),
        )
        worker, store = self._worker(state=state, adapter=adapter)

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.RECONCILIATION_DEFERRED)
        self.assertEqual(
            result.reason_code, "LAWYER_ANALYSIS_RECOVERY_UNRESOLVED"
        )
        self.assertEqual(adapter.execute_calls, 0)
        self.assertEqual(adapter.reconcile_calls, 1)
        self.assertEqual(store.receipts, [])
        self.assertEqual(store.boundaries, [])

    def test_network_timeout_after_persisted_boundary_becomes_unknown_not_retry(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id], raise_after_boundary=True
        )
        worker, store = self._worker(state=state, adapter=adapter)
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.TASK_RECONCILIATION_REQUIRED)
        self.assertEqual(len(store.boundaries), 1)
        self.assertEqual(store.receipts[0].status, ResultStatus.UNKNOWN)
        self.assertEqual(
            store.receipts[0].external_submission_state, ExternalSubmissionState.UNKNOWN
        )
        self.assertEqual(store.state.tasks[0].status, AgentTaskStatus.UNKNOWN)

    def test_known_provider_rejection_after_boundary_is_terminal_failed(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            known_failure_after_boundary=True,
        )
        worker, store = self._worker(state=state, adapter=adapter)
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.TASK_FAILED)
        self.assertEqual(len(store.boundaries), 1)
        self.assertEqual(store.receipts[0].status, ResultStatus.FAILED)
        self.assertEqual(
            store.receipts[0].external_submission_state,
            ExternalSubmissionState.SUBMITTED,
        )
        self.assertEqual(
            store.receipts[0].error_code, "QWEN_VISUAL_OCR_HTTP_400"
        )
        self.assertEqual(store.state.tasks[0].status, AgentTaskStatus.FAILED)

    def test_adapter_manifest_drift_blocks_execution_before_adapter_call(self):
        from dataclasses import replace

        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        adapter = _Adapter(
            replace(
                self.helper.adapters[task.skill.tool_id],
                adapter_version="2.0.0",
            ),
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unsafe-output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        worker, _ = self._worker(state=state, adapter=adapter)
        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "compiled task binding"):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        self.assertEqual(adapter.execute_calls, 0)

    def test_plan_command_uses_durable_claim_then_accepts_compiled_graph(self):
        created = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        graph = self.helper.graph((task,))
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        planner, compiler = _Planner(proposal=object()), _Compiler(graph)
        worker, store = self._worker(
            state=created, adapter=adapter, planner=planner, compiler=compiler
        )
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.PLANNED)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(compiler.compile_calls, 1)
        self.assertEqual(store.state.graph, graph)

    def test_passed_graph_is_idempotently_promoted_before_final_review(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.start_ready_task(state, sequence=3)
        receipt = self.helper.result_receipt(
            state, task_id=task.task_id, status=ResultStatus.SUCCEEDED
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                4,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(receipt),
            ),
        )
        state = reduce_agent_event(
            state, self.helper.event(5, AgentEventType.VERIFICATION_STARTED)
        )
        verification_hash = digest("verified-graph-for-work-plan")
        state = reduce_agent_event(
            state,
            self.helper.event(
                6,
                AgentEventType.VERIFICATION_PASSED,
                VerificationPayload(verification_hash),
            ),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        store = _Store(state)
        store.helper = self.helper
        promotion = _WorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={task.skill.tool_id: adapter},
            work_plan_promotion=promotion,
        )

        first = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        second = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(first.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(second.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(len(promotion.calls), 2)
        self.assertEqual(promotion.calls[0], promotion.calls[1])
        self.assertEqual(promotion.calls[0]["run_id"], self.helper.run_id)
        self.assertEqual(
            promotion.calls[0]["expected_version"], self.helper.snapshot.matter_version
        )
        self.assertEqual(promotion.calls[0]["actor"], self.worker_actor)
        self.assertIn(verification_hash, promotion.calls[0]["idempotency_key"])

    def test_lost_promotion_commit_replays_the_same_server_command(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.start_ready_task(state, sequence=3)
        receipt = self.helper.result_receipt(
            state, task_id=task.task_id, status=ResultStatus.SUCCEEDED
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                4,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(receipt),
            ),
        )
        state = reduce_agent_event(
            state, self.helper.event(5, AgentEventType.VERIFICATION_STARTED)
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                6,
                AgentEventType.VERIFICATION_PASSED,
                VerificationPayload(digest("commit-lost-verification")),
            ),
        )
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        store = _Store(state)
        store.helper = self.helper
        promotion = _CommitLostWorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={task.skill.tool_id: adapter},
            work_plan_promotion=promotion,
        )

        with self.assertRaises(TimeoutError):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        recovered = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(recovered.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(len(promotion.calls), 2)
        self.assertEqual(promotion.calls[0], promotion.calls[1])

    def test_final_review_stages_every_verified_extraction_before_plan_promotion(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("verified-extractions-before-plan")
        )
        artifact_ids = tuple(sorted((str(uuid4()), str(uuid4()))))
        order = []
        staging = _LedgerExtractionStaging(artifact_ids, order=order)

        class _OrderedPromotion(_WorkPlanPromotion):
            def promote_verified_graph(inner_self, **kwargs):
                order.append("promote-plan")
                return super().promote_verified_graph(**kwargs)

        promotion = _OrderedPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_extraction_staging=staging,
            work_plan_promotion=promotion,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(
            order,
            [
                "discover",
                *(f"stage:{artifact_id}" for artifact_id in artifact_ids),
                "promote-plan",
            ],
        )
        self.assertEqual(
            [call["artifact_id"] for call in staging.stage_calls],
            list(artifact_ids),
        )
        for call in staging.stage_calls:
            self.assertEqual(call["run_id"], self.helper.run_id)
            self.assertEqual(
                call["expected_version"], self.helper.snapshot.matter_version
            )
            self.assertEqual(call["actor"], self.worker_actor)
            self.assertEqual(
                call["idempotency_key"],
                f"verified-ledger-extraction-stage:{self.helper.run_id}:"
                f"{call['artifact_id']}",
            )

    def test_nonempty_ledger_review_pauses_before_work_plan_promotion(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("ledger-review-pauses-plan")
        )
        staging = _LedgerExtractionStaging(
            (str(uuid4()),), candidate_count=3
        )
        promotion = _WorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_extraction_staging=staging,
            work_plan_promotion=promotion,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(
            result.reason_code, "LEDGER_EXTRACTION_REVIEW_REQUIRED"
        )
        self.assertEqual(promotion.calls, [])

    def test_atomic_reextraction_set_stops_before_stale_plan_promotion(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("atomic-reextraction-set")
        )
        staging = _LedgerExtractionStaging(
            tuple(sorted((str(uuid4()), str(uuid4())))), candidate_count=2
        )
        followups = _LedgerExceptionFollowups(
            satisfy_result=SimpleNamespace(
                followup_count=2,
                matter_version=state.snapshot.matter_version + 1,
            )
        )
        promotion = _WorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_extraction_staging=staging,
            ledger_exception_followups=followups,
            work_plan_promotion=promotion,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(
            result.reason_code,
            "LEDGER_EXCEPTION_REEXTRACTION_SET_SATISFIED",
        )
        self.assertEqual(len(followups.satisfy_calls), 1)
        command = followups.satisfy_calls[0]
        self.assertEqual(command["run_id"], state.run_id)
        self.assertEqual(command["graph_id"], state.graph.graph_id)
        self.assertNotIn("followup_id", command)
        self.assertNotIn("reextraction_batch_id", command)
        self.assertRegex(
            command["idempotency_key"],
            r"^ledger-reextract-set\.[0-9a-f-]{36}$",
        )
        self.assertEqual(promotion.calls, [])
        self.assertEqual(len(staging.stage_calls), 2)

    def test_lost_atomic_reextraction_set_commit_reuses_exact_key(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("lost-atomic-reextraction-set")
        )
        followups = _LedgerExceptionFollowups(
            satisfy_result=SimpleNamespace(
                followup_count=1,
                matter_version=state.snapshot.matter_version + 1,
            ),
            lose_first_satisfy=True,
        )
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_exception_followups=followups,
        )

        with self.assertRaisesRegex(
            CaseAgentWorkerBlocked,
            "re-extraction graph completion failed closed",
        ):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        recovered = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(recovered.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(len(followups.satisfy_calls), 2)
        self.assertEqual(
            followups.satisfy_calls[0], followups.satisfy_calls[1]
        )

    def test_unsatisfied_reextraction_obligation_blocks_plan_promotion(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("unsatisfied-reextraction-obligation")
        )
        followups = _LedgerExceptionFollowups(
            satisfy_error=CaseAgentWorkerBlocked(
                "REEXTRACTION_OBLIGATION_UNSATISFIED"
            )
        )
        promotion = _WorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_exception_followups=followups,
            work_plan_promotion=promotion,
        )

        with self.assertRaisesRegex(
            CaseAgentWorkerBlocked,
            "REEXTRACTION_OBLIGATION_UNSATISFIED",
        ):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        self.assertEqual(promotion.calls, [])

    def test_dispatch_binds_reextraction_before_adapter_execution(self):
        source = inspect.getsource(CaseAgentWorker._dispatch)
        self.assertLess(
            source.index("self._bind_reextraction_claim"),
            source.index("self._execute_claim"),
        )

    def test_pending_snapshot_refresh_precedes_all_supervisor_commands(self):
        task = self.helper.local_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        store = _Store(state)
        store.helper = self.helper
        store.snapshot_refresh_version = state.event_version + 1
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused-refresh-precedence"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={task.skill.tool_id: adapter},
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.SNAPSHOT_REFRESHED)
        self.assertEqual(result.event_version, state.event_version + 1)
        self.assertEqual(result.reason_code, "CASE_SNAPSHOT_CHANGED")

    def test_lost_private_staging_commit_replays_exact_artifact_command(self):
        _task, state, adapter, store = self._verified_final_review_fixture(
            digest("lost-private-stage")
        )
        artifact_id = str(uuid4())
        staging = _LedgerExtractionStaging(
            (artifact_id,), lose_first_commit=True
        )
        promotion = _WorkPlanPromotion()
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={adapter.manifest.tool_id: adapter},
            ledger_extraction_staging=staging,
            work_plan_promotion=promotion,
        )

        with self.assertRaises(TimeoutError):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        recovered = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(recovered.step, AgentWorkerStep.WAITING_HUMAN)
        self.assertEqual(len(staging.stage_calls), 2)
        self.assertEqual(staging.stage_calls[0], staging.stage_calls[1])
        self.assertEqual(len(promotion.calls), 1)

    def test_planning_provider_latency_refreshes_the_durable_lease(self):
        created = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        graph = self.helper.graph((task,))

        class SlowPlanner(_Planner):
            def plan(inner_self, **kwargs):
                import time

                time.sleep(1.1)
                return super(SlowPlanner, inner_self).plan(**kwargs)

        planner = SlowPlanner(proposal=object())
        compiler = _Compiler(graph)
        store = _Store(created)
        store.helper = self.helper
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.worker_actor,
            store=store,
            snapshot_provider=_SnapshotProvider(self._planning_snapshot()),
            planner=planner,
            planner_compiler=compiler,
            adapters={task.skill.tool_id: adapter},
            lease_seconds=30,
            heartbeat_interval_seconds=1,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNED)
        self.assertGreaterEqual(store.planning_heartbeats, 1)

    def test_uncertain_planner_result_is_recorded_unknown_and_not_compiled(self):
        created = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        planner = _Planner(error=DeepSeekPlannerUnknownSubmission("uncertain"))
        compiler = _Compiler(self.helper.graph((task,)))
        worker, store = self._worker(
            state=created, adapter=adapter, planner=planner, compiler=compiler
        )
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(
            result.step, AgentWorkerStep.PLANNING_RECONCILIATION_REQUIRED
        )
        self.assertEqual(store.planning_failures[-1]["status"], "UNKNOWN")
        self.assertEqual(compiler.compile_calls, 0)

    def test_known_pre_dispatch_planner_failure_is_not_marked_unknown(self):
        created = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        planner = _Planner(
            error=DeepSeekPlannerPreDispatchFailure("TRANSPORT_CONNECT_FAILED")
        )
        compiler = _Compiler(self.helper.graph((task,)))
        worker, store = self._worker(
            state=created, adapter=adapter, planner=planner, compiler=compiler
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNING_FAILED)
        self.assertEqual(result.reason_code, "TRANSPORT_CONNECT_FAILED")
        self.assertEqual(store.planning_failures[-1]["status"], "FAILED")
        self.assertEqual(
            store.planning_failures[-1]["error_code"], "TRANSPORT_CONNECT_FAILED"
        )
        self.assertEqual(compiler.compile_calls, 0)

    def test_reextraction_admission_block_is_persisted_not_retried(self):
        created = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        error = CasePlannerAdmissionBlocked(
            "active re-extraction obligation exceeds the executable page limit",
            error_code="REEXTRACTION_PAGE_LIMIT_EXCEEDED",
        )
        compiler = _Compiler(object(), error=error)
        worker, store = self._worker(
            state=created,
            adapter=adapter,
            planner=_Planner(),
            compiler=compiler,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id,
            run_id=self.helper.run_id,
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNING_FAILED)
        self.assertEqual(
            result.reason_code, "REEXTRACTION_PAGE_LIMIT_EXCEEDED"
        )
        self.assertEqual(store.planning_failures[-1]["status"], "FAILED")
        self.assertEqual(
            store.planning_failures[-1]["error_code"],
            "REEXTRACTION_PAGE_LIMIT_EXCEEDED",
        )
        self.assertEqual(compiler.compile_calls, 1)

    def test_expired_planning_before_submission_is_failed_without_provider_retry(self):
        created = reduce_agent_event(None, self.helper.created())
        planning = reduce_agent_event(
            created,
            self.helper.event(
                2,
                AgentEventType.PLANNING_STARTED,
                actor_id=self.worker_actor.actor_id,
            ),
        )
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        planner = _Planner()
        worker, store = self._worker(state=planning, adapter=adapter, planner=planner)
        planning_attempt_id = str(uuid4())
        store.reaped_plan = ReapedPlanningAttemptResult(
            run_id=planning.run_id,
            planning_attempt_id=planning_attempt_id,
            event_version=3,
            requires_reconciliation=False,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNING_FAILED)
        self.assertEqual(result.attempt_id, planning_attempt_id)
        self.assertEqual(
            result.reason_code, "PLANNER_LEASE_EXPIRED_BEFORE_SUBMISSION"
        )
        self.assertEqual(planner.calls, 0)

    def test_expired_planning_after_submission_requires_lookup_not_resubmission(self):
        created = reduce_agent_event(None, self.helper.created())
        planning = reduce_agent_event(
            created,
            self.helper.event(
                2,
                AgentEventType.PLANNING_STARTED,
                actor_id=self.worker_actor.actor_id,
            ),
        )
        task = self.helper.local_task()
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("unused"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        planner = _Planner()
        worker, store = self._worker(state=planning, adapter=adapter, planner=planner)
        store.reaped_plan = ReapedPlanningAttemptResult(
            run_id=planning.run_id,
            planning_attempt_id=str(uuid4()),
            event_version=3,
            requires_reconciliation=True,
        )

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(
            result.step, AgentWorkerStep.PLANNING_RECONCILIATION_REQUIRED
        )
        self.assertEqual(result.reason_code, "PLANNER_LEASE_EXPIRED_RESULT_UNKNOWN")
        self.assertEqual(planner.calls, 0)

    def test_readiness_requires_real_planner_adapter_store_and_fresh_heartbeat(self):
        state = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        store = _Store(state)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        now = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)
        probe = AgentWorkerReadinessProbe(
            worker_id="worker-1",
            actor=self.worker_actor,
            planner=_Planner(),
            adapters={task.skill.tool_id: adapter},
            health_store=store,
            verifier=self._readiness_verifier(),
            verifier_actor=self.verifier_actor,
            clock=lambda: now,
        )
        self.assertFalse(probe.snapshot().ready)
        heartbeat = probe.publish_heartbeat(ttl_seconds=90)
        self.assertEqual(heartbeat.observed_at, now)
        self.assertEqual(heartbeat.verifier_actor_id, self.verifier_actor.actor_id)
        self.assertEqual(heartbeat.verifier_id, "test-run-verifier")
        self.assertEqual(
            heartbeat.verifier_policy_hash, self._readiness_verifier().policy_hash
        )
        self.assertTrue(probe.snapshot().ready)
        stale = AgentWorkerReadinessProbe(
            worker_id="worker-1",
            actor=self.worker_actor,
            planner=_Planner(),
            adapters={task.skill.tool_id: adapter},
            health_store=store,
            verifier=self._readiness_verifier(),
            verifier_actor=self.verifier_actor,
            clock=lambda: now + timedelta(seconds=91),
        ).snapshot()
        self.assertFalse(stale.ready)
        self.assertIn("WORKER_HEARTBEAT_STALE", stale.reason_codes)

    def test_readiness_fails_closed_without_an_independent_verifier(self):
        state = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        store = _Store(state)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        probe = AgentWorkerReadinessProbe(
            worker_id="worker-1",
            actor=self.worker_actor,
            planner=_Planner(),
            adapters={task.skill.tool_id: adapter},
            health_store=store,
        )
        snapshot = probe.snapshot()
        self.assertFalse(snapshot.ready)
        self.assertFalse(snapshot.independent_verifier_ready)
        self.assertIn(
            "INDEPENDENT_VERIFIER_NOT_REGISTERED", snapshot.reason_codes
        )
        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "independent verifier"):
            probe.publish_heartbeat()

    def test_readiness_rejects_a_heartbeat_from_another_firm(self):
        state = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        store = _Store(state)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        now = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)
        probe = AgentWorkerReadinessProbe(
            worker_id="worker-1",
            actor=self.worker_actor,
            planner=_Planner(),
            adapters={task.skill.tool_id: adapter},
            health_store=store,
            verifier=self._readiness_verifier(),
            verifier_actor=self.verifier_actor,
            clock=lambda: now,
        )
        heartbeat = probe.publish_heartbeat()
        store.heartbeat = type(heartbeat)(
            **{**heartbeat.__dict__, "firm_id": str(uuid4())}
        )
        snapshot = probe.snapshot()
        self.assertFalse(snapshot.ready)
        self.assertFalse(snapshot.heartbeat_fresh)

    def test_readiness_rejects_persisted_verifier_policy_drift(self):
        from dataclasses import replace

        state = reduce_agent_event(None, self.helper.created())
        task = self.helper.local_task()
        store = _Store(state)
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        now = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)
        verifier = self._readiness_verifier()
        probe = AgentWorkerReadinessProbe(
            worker_id="worker-1",
            actor=self.worker_actor,
            planner=_Planner(),
            adapters={task.skill.tool_id: adapter},
            health_store=store,
            verifier=verifier,
            verifier_actor=self.verifier_actor,
            clock=lambda: now,
        )
        heartbeat = probe.publish_heartbeat()
        store.heartbeat = replace(
            heartbeat, verifier_policy_hash=digest("old-policy")
        )
        snapshot = probe.snapshot()
        self.assertFalse(snapshot.ready)
        self.assertFalse(snapshot.heartbeat_fresh)


if __name__ == "__main__":
    unittest.main()
