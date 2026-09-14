from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace
import unittest
from uuid import uuid4

from case_kernel.case_agent_planner import (
    CasePlanProposal,
    CasePlanningSignal,
    CasePlanningSnapshot,
    PlannerRiskHint,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlanningSignalCategory,
    ProposedPlannerTask,
    case_plan_proposal_payload,
)
from case_kernel.case_agent_postgres import (
    ClaimedAgentPlanningAttempt,
    PostgresCaseAgentStore,
)
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    AgentRunStatus,
    PlanningFailurePayload,
    SupervisorCommandKind,
    TaskGraphPayload,
    decide_next_commands,
    reduce_agent_event,
    _task_graph_hash,
)
from case_kernel.case_agent_worker import (
    AgentWorkerStep,
    CaseAgentWorker,
    CaseAgentWorkerBlocked,
)
from case_kernel.case_agent_worker_postgres import (
    PlanningLookupOutcome,
    PlanningLookupStatus,
    PostgresCaseAgentWorkerAdapter,
)
from case_kernel.models import Actor, Role
from backend.tests import test_case_agent_supervisor as supervisor_fixture


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _FakePostgresStore(PostgresCaseAgentStore):
    """Contract fake: no SQL, but exact public Store records and transitions."""

    def __init__(self, *, helper, state, current=None):
        self.helper = helper
        self.state = state
        self.current = current
        self.planning_claims = []
        self.reconciliation_claims = []
        self.reconciliation_outcomes = []
        self.boundaries = []
        self.provider_outcomes = []
        self.local_outcomes = []
        self.heartbeat = None
        self.reaped_plan = None

    def read_projection(self, **_):
        return SimpleNamespace(
            state=self.state,
            next_commands=decide_next_commands(self.state),
            checkpoint_verified=True,
        )

    def apply_pending_snapshot_refresh(self, **_):
        return None

    def claim_planning_attempt(self, **kwargs):
        self.planning_claims.append(kwargs)
        self.state = reduce_agent_event(
            self.state,
            self.helper.event(
                self.state.event_version + 1,
                AgentEventType.PLANNING_STARTED,
                actor_id=kwargs["actor"].actor_id,
            ),
        )
        self.current = ClaimedAgentPlanningAttempt(
            run_id=self.state.run_id,
            planning_attempt_id=str(uuid4()),
            external_request_id=str(uuid4()),
            event_version=self.state.event_version,
            matter_version=self.state.snapshot.matter_version,
            planning_kind=kwargs["planning_kind"],
            planning_hash=kwargs["planning_hash"],
            lease_owner=kwargs["lease_owner"],
            lease_token=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=kwargs["lease_seconds"]),
            attempt_version=1,
        )
        return self.current

    def current_planning_attempt(self, **_):
        return self.current

    def claim_planning_reconciliation(self, **kwargs):
        self.reconciliation_claims.append(kwargs)
        assert self.current is not None
        self.current = replace(
            self.current,
            status="RECONCILING",
            lease_owner=kwargs["lease_owner"],
            lease_token=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=kwargs["lease_seconds"]),
            attempt_version=self.current.attempt_version + 1,
        )
        return self.current

    def record_planning_reconciliation_outcome(self, **kwargs):
        self.reconciliation_outcomes.append(kwargs)
        assert self.current is not None
        self.assert_reconciliation_lease(kwargs)
        if kwargs["status"] == "SUCCEEDED":
            self.current = replace(
                self.current,
                status="SUCCEEDED",
                recovered_proposal=dict(kwargs["structured_proposal"]),
                attempt_version=self.current.attempt_version + 1,
            )
        else:
            self.state = reduce_agent_event(
                self.state,
                self.helper.event(
                    self.state.event_version + 1,
                    AgentEventType.PLANNING_FAILED,
                    PlanningFailurePayload(error_code=kwargs["error_code"]),
                    actor_id=kwargs["actor"].actor_id,
                ),
            )
            self.current = replace(
                self.current,
                status="FAILED",
                attempt_version=self.current.attempt_version + 1,
            )
        return SimpleNamespace(status=kwargs["status"])

    def assert_reconciliation_lease(self, kwargs):
        assert self.current is not None
        if (
            kwargs["lease_owner"] != self.current.lease_owner
            or kwargs["expected_attempt_version"] != self.current.attempt_version
        ):
            raise AssertionError("adapter lost the exact reconciliation lease")

    def append_event(self, *, event, **_):
        self.state = reduce_agent_event(self.state, event)
        return SimpleNamespace(event_version=self.state.event_version)

    def begin_planning_submission(self, **kwargs):
        assert self.current is not None
        if kwargs["lease_token"] != self.current.lease_token:
            raise AssertionError("planner submission lost its lease token")
        self.boundaries.append(kwargs)
        return 1

    def record_planning_outcome(self, **kwargs):
        assert self.current is not None
        if kwargs["lease_token"] != self.current.lease_token:
            raise AssertionError("planner outcome lost its lease token")
        self.provider_outcomes.append(kwargs)
        self.current = replace(
            self.current,
            status=kwargs["status"],
            request_hash=kwargs["request_hash"],
            recovered_proposal=(
                dict(kwargs["structured_proposal"])
                if kwargs["structured_proposal"] is not None
                else None
            ),
            attempt_version=self.current.attempt_version + 1,
        )

    def record_local_planning_outcome(self, **kwargs):
        assert self.current is not None
        if kwargs["lease_token"] != self.current.lease_token:
            raise AssertionError("local planner outcome lost its lease token")
        self.local_outcomes.append(kwargs)
        self.current = replace(
            self.current,
            status="SUCCEEDED",
            recovered_proposal=case_plan_proposal_payload(kwargs["proposal"]),
            attempt_version=self.current.attempt_version + 1,
        )

    def reap_expired_attempt(self, **_):
        return None

    def reap_expired_planning_attempt(self, **_):
        result, self.reaped_plan = self.reaped_plan, None
        return result

    def claim_next_dispatchable_task(self, **_):
        raise AssertionError("this test must not dispatch a task")

    def claim_next_reconciliation(self, **_):
        raise AssertionError("this test must not reconcile a task")

    def probe_case_agent_store(self, **_):
        return True

    def record_worker_heartbeat(self, heartbeat):
        self.heartbeat = heartbeat

    def latest_worker_heartbeat(self, **_):
        return self.heartbeat


class _LookupOnlyReconciler:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def lookup(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


class _NeverCalledPlanner:
    planner_id = "deepseek"

    def __init__(self):
        self.calls = 0

    def plan(self, **_):
        self.calls += 1
        raise AssertionError("durable recovery must not call the planner")


class _Compiler:
    def __init__(self, graph):
        self.graph = graph
        self.calls = 0

    def semantic_skill_catalog(self):
        return (SimpleNamespace(skill_id="case_reading"),)

    def compile(self, **args):
        self.calls += 1
        fields = dict(graph_id=args["graph_id"], graph_version=args["graph_version"],
            goal_hash=self.graph.goal_hash, snapshot=self.graph.snapshot, tasks=self.graph.tasks)
        return replace(self.graph, **fields, graph_hash=_task_graph_hash(**fields))


class _SnapshotProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def build_for_run(self, **_):
        return self.snapshot


class _NoopTaskAdapter:
    def __init__(self, manifest):
        self.manifest = manifest

    def execute(self, **_):
        raise AssertionError("recovered planning must not execute a graph task yet")

    def reconcile(self, **_):
        raise AssertionError("recovered planning must not reconcile a graph task")


class PostgresCaseAgentWorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.helper = supervisor_fixture.CaseAgentSupervisorTests(methodName="runTest")
        self.helper.setUp()
        self.actor = Actor(
            str(uuid4()), self.helper.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.snapshot = CasePlanningSnapshot.build(
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
        self.proposal = CasePlanProposal(
            goal_hash=self.helper.goal.goal_hash,
            planning_snapshot_hash=self.snapshot.planning_hash,
            tasks=(
                ProposedPlannerTask(
                    proposal_id="read",
                    skill_id="case_reading",
                    purpose="读取已授权的案件材料并形成可复核观察候选",
                    dependency_ids=(),
                    input_ref_ids=("case-object-1",),
                    risk_hint=PlannerRiskHint.LOW,
                ),
            ),
        )
        self.proposal_payload = case_plan_proposal_payload(self.proposal)
        self.graph = self.helper.graph((self.helper.local_task(),))

    def _planning_state(self):
        created = reduce_agent_event(None, self.helper.created())
        return reduce_agent_event(
            created,
            self.helper.event(
                2, AgentEventType.PLANNING_STARTED, actor_id=self.actor.actor_id
            ),
        )

    def _unknown_state(self):
        return reduce_agent_event(
            self._planning_state(),
            self.helper.event(
                3,
                AgentEventType.PLANNING_RESULT_UNKNOWN,
                actor_id=self.actor.actor_id,
            ),
        )

    def _planning_record(self, *, state, status, request_hash=None, proposal=None):
        return ClaimedAgentPlanningAttempt(
            run_id=state.run_id,
            planning_attempt_id=str(uuid4()),
            external_request_id=str(uuid4()),
            event_version=state.event_version,
            matter_version=state.snapshot.matter_version,
            planning_kind="PLAN",
            planning_hash=self.snapshot.planning_hash,
            lease_owner="worker-a",
            lease_token=str(uuid4()),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
            attempt_version=2,
            status=status,
            request_hash=request_hash,
            recovered_proposal=proposal,
        )

    def _worker(self, *, store, adapter):
        planner = _NeverCalledPlanner()
        compiler = _Compiler(self.graph)
        task_manifest = self.helper.adapters[self.helper.local_task().skill.tool_id]
        worker = CaseAgentWorker(
            worker_id="worker-a",
            actor=self.actor,
            store=adapter,
            snapshot_provider=_SnapshotProvider(self.snapshot),
            planner=planner,
            planner_compiler=compiler,
            adapters={task_manifest.tool_id: _NoopTaskAdapter(task_manifest)},
            heartbeat_interval_seconds=29,
        )
        return worker, planner, compiler

    def test_claim_uses_reducer_command_and_server_built_planning_hash(self):
        state = reduce_agent_event(None, self.helper.created())
        command = decide_next_commands(state)[0]
        store = _FakePostgresStore(helper=self.helper, state=state)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)

        claim = adapter.claim_next_planning_command(
            matter_id=self.helper.matter_id,
            actor=self.actor,
            run_id=self.helper.run_id,
            expected_event_version=1,
            planning_hash=self.snapshot.planning_hash,
            idempotency_key="claim-plan-1",
            lease_owner="worker-a",
            lease_seconds=120,
        )

        claim.validate()
        self.assertEqual(command.kind, SupervisorCommandKind.REQUEST_PLAN)
        self.assertEqual(claim.command_id, command.command_id)
        self.assertEqual(claim.planning_hash, self.snapshot.planning_hash)
        self.assertEqual(store.planning_claims[0]["planning_kind"], "PLAN")
        self.assertEqual(store.state.status, AgentRunStatus.PLANNING)

    def test_local_planner_outcome_uses_the_same_claim_without_provider_boundary(self):
        state = reduce_agent_event(None, self.helper.created())
        store = _FakePostgresStore(helper=self.helper, state=state)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        claim = adapter.claim_next_planning_command(
            matter_id=self.helper.matter_id,
            actor=self.actor,
            run_id=self.helper.run_id,
            expected_event_version=1,
            planning_hash=self.snapshot.planning_hash,
            idempotency_key="claim-local-plan-1",
            lease_owner="worker-a",
            lease_seconds=120,
        )

        adapter.record_local_planning_outcome(
            matter_id=self.helper.matter_id,
            claim=claim,
            planner_id="controlled-first-release-defence-planner-v1",
            proposal=self.proposal,
        )

        self.assertEqual(store.boundaries, [])
        self.assertEqual(store.provider_outcomes, [])
        self.assertEqual(len(store.local_outcomes), 1)
        self.assertEqual(store.current.status, "SUCCEEDED")
        self.assertEqual(store.current.recovered_proposal, self.proposal_payload)

    def test_succeeded_proposal_is_compiled_after_restart_without_network_call(self):
        state = self._planning_state()
        record = self._planning_record(
            state=state, status="SUCCEEDED", proposal=self.proposal_payload
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        worker, planner, compiler = self._worker(store=store, adapter=adapter)

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNED)
        self.assertEqual(result.reason_code, "PLANNER_RESULT_RECOVERED_AFTER_RESTART")
        self.assertEqual(planner.calls, 0)
        self.assertEqual(compiler.calls, 1)
        self.assertEqual(store.state.graph.tasks, self.graph.tasks)

    def test_restart_rejects_a_proposal_when_authorized_planning_projection_drifted(self):
        state = self._planning_state()
        record = self._planning_record(
            state=state, status="SUCCEEDED", proposal=self.proposal_payload
        )
        drifted_snapshot = CasePlanningSnapshot.build(
            case_snapshot=self.snapshot.case_snapshot,
            authorized_inputs=self.snapshot.authorized_inputs,
            signals=(
                CasePlanningSignal(
                    signal_id="new-gap",
                    category=PlanningSignalCategory.LEGAL_GAP,
                    code="NEW_GAP",
                    status=PlanningInputStatus.OPEN,
                    summary="重启前新增且尚未进入旧规划的授权案件状态。",
                    source_ref_ids=("case-object-1",),
                ),
            ),
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        planner = _NeverCalledPlanner()
        compiler = _Compiler(self.graph)
        manifest = self.helper.adapters[self.helper.local_task().skill.tool_id]
        worker = CaseAgentWorker(
            worker_id="worker-a",
            actor=self.actor,
            store=adapter,
            snapshot_provider=_SnapshotProvider(drifted_snapshot),
            planner=planner,
            planner_compiler=compiler,
            adapters={manifest.tool_id: _NoopTaskAdapter(manifest)},
            heartbeat_interval_seconds=29,
        )

        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "obsolete authorized"):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )

        self.assertEqual(planner.calls, 0)
        self.assertEqual(compiler.calls, 0)
        self.assertIsNone(store.state.graph)

    def test_unknown_result_is_looked_up_by_original_ids_then_compiled(self):
        state = self._unknown_state()
        request_hash = digest("original-deepseek-request")
        record = self._planning_record(
            state=state, status="UNKNOWN_SUBMISSION", request_hash=request_hash
        )
        lookup = _LookupOnlyReconciler(
            PlanningLookupOutcome(
                status=PlanningLookupStatus.SUCCEEDED,
                output_hash=digest("original-deepseek-response"),
                structured_proposal=self.proposal_payload,
            )
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(
            store=store, actor=self.actor, planning_reconciler=lookup
        )
        worker, planner, compiler = self._worker(store=store, adapter=adapter)

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNING_RECONCILED)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(compiler.calls, 1)
        self.assertEqual(
            lookup.calls,
            [
                {
                    "external_request_id": record.external_request_id,
                    "request_hash": request_hash,
                }
            ],
        )
        self.assertEqual(len(store.reconciliation_outcomes), 1)
        self.assertEqual(store.reconciliation_outcomes[0]["status"], "SUCCEEDED")
        self.assertEqual(store.boundaries, [])

    def test_reconciled_success_is_recovered_after_crash_without_second_lookup(self):
        state = self._unknown_state()
        record = self._planning_record(
            state=state,
            status="SUCCEEDED",
            request_hash=digest("original-request"),
            proposal=self.proposal_payload,
        )
        lookup = _LookupOnlyReconciler(
            PlanningLookupOutcome(status=PlanningLookupStatus.PENDING)
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(
            store=store, actor=self.actor, planning_reconciler=lookup
        )
        worker, planner, compiler = self._worker(store=store, adapter=adapter)

        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )

        self.assertEqual(result.step, AgentWorkerStep.PLANNING_RECONCILED)
        self.assertEqual(planner.calls, 0)
        self.assertEqual(lookup.calls, [])
        self.assertEqual(compiler.calls, 1)
        self.assertEqual(store.reconciliation_claims, [])
        self.assertEqual(store.state.graph.tasks, self.graph.tasks)

    def test_reconciliation_outcome_uses_the_exact_rotated_lease_fence(self):
        state = self._unknown_state()
        record = self._planning_record(
            state=state,
            status="UNKNOWN_SUBMISSION",
            request_hash=digest("original-request"),
        )
        original_token = record.lease_token
        lookup = _LookupOnlyReconciler(
            PlanningLookupOutcome(
                status=PlanningLookupStatus.SUCCEEDED,
                output_hash=digest("provider-response"),
                structured_proposal=self.proposal_payload,
            )
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(
            store=store, actor=self.actor, planning_reconciler=lookup
        )

        claim = adapter.recover_planning_attempt(
            matter_id=self.helper.matter_id,
            actor=self.actor,
            run_id=self.helper.run_id,
            expected_event_version=state.event_version,
            idempotency_key="reconcile-fence-1",
            lease_owner="worker-b",
            lease_seconds=120,
        )

        write = store.reconciliation_outcomes[0]
        self.assertEqual(write["lease_token"], claim.lease_token)
        self.assertEqual(write["expected_attempt_version"], 3)
        self.assertNotEqual(write["lease_token"], original_token)
        self.assertEqual(write["lease_owner"], "worker-b")

    def test_reviewed_material_recovery_rebinds_only_retained_proposal(self):
        from case_kernel.case_agent_supervisor import AgentGoal, PlanningMaterialScopeReviewPayload
        from case_kernel.case_agent_worker_postgres import _digest
        original = self.helper.goal
        goal = AgentGoal.build(goal_id=original.goal_id, objective=original.objective,
            success_criteria=original.success_criteria, constraints=original.constraints,
            requested_by=original.requested_by, material_read_refs=("evidence-page:" + str(uuid4()),))
        state = replace(self._unknown_state(), goal=goal, status=AgentRunStatus.PLANNING,
            failure_code=None, stale=False)
        derived = replace(self.proposal, goal_hash=goal.goal_hash)
        request_hash = digest("original-request")
        review = PlanningMaterialScopeReviewPayload(state.snapshot, original.goal_hash,
            _digest(self.proposal_payload), request_hash, self.snapshot.planning_hash, goal.material_read_refs,
            8 * 1024 * 1024, 32 * 1024 * 1024, goal.goal_hash,
            _digest(case_plan_proposal_payload(derived)), digest("reviewed-graph"), original.requested_by)
        record = self._planning_record(state=state, status="SUCCEEDED", request_hash=request_hash)
        record = replace(record, recovered_proposal=self.proposal_payload)
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        store.material_scope_review = lambda **_: review
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        args = dict(matter_id=state.matter_id, actor=self.actor, run_id=state.run_id,
            expected_event_version=state.event_version, idempotency_key="material-recovery-1",
            lease_owner="worker-a", lease_seconds=120)
        claim = adapter.recover_planning_attempt(**args)
        self.assertEqual(claim.recovered_proposal, derived)
        self.assertEqual(claim.graph_id, state.run_id)
        self.assertEqual(store.current.recovered_proposal, self.proposal_payload)
        self.assertEqual(store.planning_claims + store.reconciliation_claims + store.provider_outcomes, [])
        for corrupt in (None, replace(review, request_hash=digest("other")),
                        replace(review, derived_proposal_hash=digest("other"))):
            store.material_scope_review = lambda **_: corrupt
            with self.assertRaises(CaseAgentWorkerBlocked):
                adapter.recover_planning_attempt(**args)
        store.material_scope_review = lambda **_: review
        store.current = replace(record, status="UNKNOWN_SUBMISSION", recovered_proposal=None)
        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "no lookup or submission"):
            adapter.recover_planning_attempt(**args)
        self.assertEqual(store.reconciliation_claims, [])

    def test_missing_lookup_connector_never_claims_or_resubmits_unknown_plan(self):
        state = self._unknown_state()
        store = _FakePostgresStore(
            helper=self.helper,
            state=state,
            current=self._planning_record(
                state=state,
                status="UNKNOWN_SUBMISSION",
                request_hash=digest("original-request"),
            ),
        )
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)

        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "lookup-only"):
            adapter.recover_planning_attempt(
                matter_id=self.helper.matter_id,
                actor=self.actor,
                run_id=self.helper.run_id,
                expected_event_version=state.event_version,
                idempotency_key="reconcile-plan-1",
                lease_owner="worker-a",
                lease_seconds=120,
            )

        self.assertEqual(store.reconciliation_claims, [])
        self.assertEqual(store.boundaries, [])

    def test_nonterminal_lookup_is_not_written_as_a_terminal_outcome(self):
        state = self._unknown_state()
        store = _FakePostgresStore(
            helper=self.helper,
            state=state,
            current=self._planning_record(
                state=state,
                status="UNKNOWN_SUBMISSION",
                request_hash=digest("original-request"),
            ),
        )
        lookup = _LookupOnlyReconciler(
            PlanningLookupOutcome(status=PlanningLookupStatus.PENDING)
        )
        adapter = PostgresCaseAgentWorkerAdapter(
            store=store, actor=self.actor, planning_reconciler=lookup
        )

        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "non-terminal"):
            adapter.recover_planning_attempt(
                matter_id=self.helper.matter_id,
                actor=self.actor,
                run_id=self.helper.run_id,
                expected_event_version=state.event_version,
                idempotency_key="reconcile-plan-2",
                lease_owner="worker-a",
                lease_seconds=120,
            )

        self.assertEqual(len(lookup.calls), 1)
        self.assertEqual(store.reconciliation_outcomes, [])
        self.assertEqual(store.boundaries, [])

    def test_known_failed_lookup_is_persisted_and_never_compiled_or_retried(self):
        state = self._unknown_state()
        store = _FakePostgresStore(
            helper=self.helper,
            state=state,
            current=self._planning_record(
                state=state,
                status="UNKNOWN_SUBMISSION",
                request_hash=digest("original-request"),
            ),
        )
        lookup = _LookupOnlyReconciler(
            PlanningLookupOutcome(
                status=PlanningLookupStatus.FAILED,
                error_code="PLANNER_PROVIDER_REQUEST_FAILED",
            )
        )
        adapter = PostgresCaseAgentWorkerAdapter(
            store=store, actor=self.actor, planning_reconciler=lookup
        )
        worker, planner, compiler = self._worker(store=store, adapter=adapter)

        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "known failed"):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )

        self.assertEqual(planner.calls, 0)
        self.assertEqual(compiler.calls, 0)
        self.assertEqual(store.reconciliation_outcomes[0]["status"], "FAILED")
        self.assertEqual(store.state.status, AgentRunStatus.WAITING_INPUT)
        self.assertEqual(decide_next_commands(store.state), ())
        self.assertEqual(store.boundaries, [])

    def test_deepseek_guard_maps_the_exact_claim_and_external_ledger(self):
        state = self._planning_state()
        record = self._planning_record(state=state, status="CLAIMED")
        store = _FakePostgresStore(helper=self.helper, state=state, current=record)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        request_hash = digest("planner-request")

        version = adapter.begin_submission(
            external_request_id=record.external_request_id,
            run_id=record.run_id,
            claim_lease_id=record.planning_attempt_id,
            lease_token=record.lease_token,
            matter_id=self.helper.matter_id,
            matter_version=self.helper.snapshot.matter_version,
            provider_id="deepseek",
            service_id="deepseek-chat",
            input_hash=self.snapshot.planning_hash,
            request_hash=request_hash,
        )
        adapter.record_outcome(
            external_request_id=record.external_request_id,
            run_id=record.run_id,
            matter_id=self.helper.matter_id,
            matter_version=self.helper.snapshot.matter_version,
            lease_token=record.lease_token,
            expected_external_ledger_version=version,
            request_hash=request_hash,
            status="SUCCEEDED",
            output_hash=digest("planner-response"),
            error_code=None,
            structured_proposal=self.proposal_payload,
        )

        self.assertEqual(store.boundaries[0]["planning_attempt_id"], record.planning_attempt_id)
        self.assertEqual(store.boundaries[0]["request_hash"], request_hash)
        self.assertEqual(store.boundaries[0]["lease_token"], record.lease_token)
        self.assertEqual(store.provider_outcomes[0]["planning_attempt_id"], record.planning_attempt_id)
        self.assertEqual(store.provider_outcomes[0]["request_hash"], request_hash)
        self.assertEqual(store.provider_outcomes[0]["lease_token"], record.lease_token)
        self.assertEqual(store.provider_outcomes[0]["status"], "SUCCEEDED")

    def test_stale_planning_lease_cannot_classify_or_accept_a_new_owner_result(self):
        state = self._planning_state()
        current = self._planning_record(
            state=state,
            status="SUCCEEDED",
            proposal=self.proposal_payload,
        )
        store = _FakePostgresStore(helper=self.helper, state=state, current=current)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        stale_claim = adapter._planning_claim(
            record=replace(current, lease_token=str(uuid4())),
            state=state,
            command_kind=SupervisorCommandKind.REQUEST_PLAN,
            command_id=digest("stale-command"),
            proposal=self.proposal,
        )

        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "stale planning lease"):
            adapter.record_planning_failure(
                matter_id=self.helper.matter_id,
                actor=self.actor,
                claim=stale_claim,
                expected_attempt_version=stale_claim.attempt_version,
                status="FAILED",
                error_code="PLANNING_CONTROL_BOUNDARY_BLOCKED",
            )
        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "no successful durable"):
            adapter.accept_planning_graph(
                matter_id=self.helper.matter_id,
                actor=self.actor,
                claim=stale_claim,
                expected_attempt_version=stale_claim.attempt_version,
                idempotency_key="stale-accept-1",
                graph=self.graph,
            )
        self.assertIsNone(store.state.graph)

    def test_acceptance_binds_graph_identity_and_durable_input_before_append(self):
        state = self._planning_state()
        current = self._planning_record(state=state, status="SUCCEEDED", proposal=self.proposal_payload)
        store = _FakePostgresStore(helper=self.helper, state=state, current=current)
        adapter = PostgresCaseAgentWorkerAdapter(store=store, actor=self.actor)
        claim = adapter._planning_claim(record=current, state=state,
            command_kind=SupervisorCommandKind.REQUEST_PLAN, command_id=digest("accept-command"),
            proposal=self.proposal)
        graph = _Compiler(self.graph).compile(graph_id=claim.graph_id, graph_version=claim.graph_version)
        args = dict(matter_id=self.helper.matter_id, actor=self.actor, claim=claim,
            expected_attempt_version=claim.attempt_version, idempotency_key="accept-bound-graph")
        for invalid in (replace(graph, graph_id=str(uuid4())),
                        replace(graph, graph_version=graph.graph_version + 1),
                        replace(graph, goal_hash=digest("other-goal")),
                        replace(graph, snapshot=replace(graph.snapshot, matter_version=99))):
            with self.subTest(graph=invalid.graph_id), self.assertRaisesRegex(CaseAgentWorkerBlocked, "differs"):
                adapter.accept_planning_graph(**args, graph=invalid)
            self.assertIsNone(store.state.graph)
        store.current = replace(current, planning_hash=digest("other-input"))
        with self.assertRaisesRegex(CaseAgentWorkerBlocked, "differs"):
            adapter.accept_planning_graph(**args, graph=graph)
        self.assertIsNone(store.state.graph)
        store.current = current
        adapter.accept_planning_graph(**args, graph=graph)
        self.assertEqual(store.state.graph, graph)

    def test_lookup_outcome_rejects_malformed_terminal_data(self):
        with self.assertRaises(CaseAgentWorkerBlocked):
            PlanningLookupOutcome(
                status=PlanningLookupStatus.SUCCEEDED,
                output_hash="not-a-digest",
                structured_proposal=self.proposal_payload,
            ).validate()
        with self.assertRaises(CaseAgentWorkerBlocked):
            PlanningLookupOutcome(
                status=PlanningLookupStatus.FAILED,
                error_code="bad error with spaces",
            ).validate()
        PlanningLookupOutcome(
            status=PlanningLookupStatus.FAILED,
            output_hash=digest("provider-response-with-invalid-proposal"),
            error_code="PLANNER_INVALID_STRUCTURED_PROPOSAL",
        ).validate()


if __name__ == "__main__":
    unittest.main()
