from dataclasses import replace
import unittest
from uuid import uuid4

import test_case_agent_material_stage as fixtures
from case_kernel.case_agent_analysis_stage import (prepare_case_analysis_stage,
    validate_case_analysis_stage_graph)
from case_kernel.case_agent_supervisor import (AgentEventType, CaseAnalysisStageReviewPayload,
    AgentSupervisorBlocked, reduce_agent_event)
from case_kernel.case_agent_postgres import _payload_json, _payload_from_json


class AnalysisStageTests(unittest.TestCase):
    def test_known_request_repair_requires_changed_bytes_and_cannot_repeat(self):
        from types import SimpleNamespace
        from case_kernel.case_agent_analysis_stage import prepare_case_analysis_request_repair
        from case_kernel.case_agent_supervisor import AgentRunStatus
        attempt = str(uuid4())
        task = self.state.tasks[0]
        receipt = SimpleNamespace(attempt_id=attempt, error_code="LAWYER_ANALYSIS_HTTP_400",
            status=SimpleNamespace(value="FAILED"), artifacts=(), cost_minor_units=0)
        task = replace(task, spec=replace(task.spec, skill=replace(task.spec.skill,
            tool_id="analyze_lawyer_decision_package")), receipts=(receipt,))
        state = replace(self.state, status=AgentRunStatus.WAITING_INPUT, verification_hash=None, tasks=(task,))
        args = dict(candidate_bindings=self.bindings, approved_by=self.fixture.reviewer,
            failed_attempt_id=attempt, failed_request_hash="c" * 64, repaired_request_hash="d" * 64,
            repair_reason="修复已知请求格式错误，保留原失败记录，仅执行一次。")
        stage = prepare_case_analysis_request_repair(state=state, **args)
        kind = AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED
        payload = CaseAnalysisStageReviewPayload(stage)
        self.assertEqual(_payload_from_json(kind, _payload_json(payload)), payload)
        with self.assertRaises(AgentSupervisorBlocked):
            prepare_case_analysis_request_repair(state=state, **{**args, "repaired_request_hash": "c" * 64})
        with self.assertRaises(AgentSupervisorBlocked):
            prepare_case_analysis_request_repair(state=replace(state, analysis_stage=stage), **args)

    def setUp(self):
        self.fixture = fixtures.SupplementaryMaterialStageTests()
        self.fixture.setUp()
        state = self.fixture.state
        extraction = replace(state.graph.tasks[0], skill=replace(state.graph.tasks[0].skill,
            tool_id="extract_case_ledger", skill_id="case_ledger_extraction"))
        self.state = replace(state, graph=replace(state.graph, tasks=(extraction,)))
        self.bindings = (("fact-candidate:" + str(uuid4()), "b" * 64),)

    def prepare(self):
        return prepare_case_analysis_stage(state=self.state,
            candidate_bindings=self.bindings, approved_by=self.fixture.reviewer)

    def test_cumulative_budget_and_replay_preserve_history(self):
        stage = self.prepare()
        payload = CaseAnalysisStageReviewPayload(stage)
        kind = AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED
        self.assertEqual(_payload_from_json(kind, _payload_json(payload)), payload)
        event = replace(self.fixture.fixture.event(self.state.event_version + 1, kind, payload),
            actor_id=self.fixture.reviewer)
        after = reduce_agent_event(self.state, event)
        self.assertEqual(after.budget_usage, self.state.budget_usage)
        self.assertEqual(after.artifacts, self.state.artifacts)
        self.assertEqual(after.goal, self.state.goal)
        self.assertIsNone(after.material_stage)
        self.assertEqual(after.analysis_stage, stage)
        self.assertEqual(after.budget.max_external_calls, 2)
        self.assertEqual(after.budget.max_cost_minor_units, 125)

    def test_analysis_cannot_renew_itself(self):
        task = self.state.graph.tasks[0]
        self.state = replace(self.state, graph=replace(self.state.graph,
            tasks=(replace(task, skill=replace(task.skill, tool_id="analyze_lawyer_decision_package")),)))
        with self.assertRaisesRegex(AgentSupervisorBlocked, "completed extraction"):
            self.prepare()

    def test_graph_cannot_extract_again_omit_sources_or_retry(self):
        stage = self.prepare()
        base = self.fixture.fixture.local_task()
        context = replace(base, input_refs=tuple(ref for ref, _ in self.bindings),
            skill=replace(base.skill, tool_id="review_case_context", skill_id="case_context_review"),
            budget=replace(base.budget, max_attempts=1, max_external_calls=0))
        analysis = replace(context, task_id=str(uuid4()), dependency_ids=(context.task_id,),
            skill=replace(base.skill, tool_id="analyze_lawyer_decision_package", skill_id="lawyer_decision_package"),
            budget=replace(base.budget, max_attempts=1, max_external_calls=1, max_cost_minor_units=120))
        graph = replace(self.state.graph, tasks=(context, analysis))
        validate_case_analysis_stage_graph(stage=stage, graph=graph)
        for bad in (replace(analysis, input_refs=()),
                    replace(analysis, budget=replace(analysis.budget, max_attempts=2)),
                    replace(analysis, skill=replace(analysis.skill, tool_id="extract_case_ledger"))):
            with self.assertRaises(AgentSupervisorBlocked):
                validate_case_analysis_stage_graph(stage=stage, graph=replace(graph, tasks=(context, bad)))

    def test_stale_source_or_actor_cannot_review(self):
        stage = replace(self.prepare(), candidate_bindings=((self.bindings[0][0], "c" * 64),))
        event = replace(self.fixture.fixture.event(self.state.event_version + 1,
            AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED, CaseAnalysisStageReviewPayload(stage)),
            actor_id=self.fixture.reviewer)
        with self.assertRaises(AgentSupervisorBlocked):
            reduce_agent_event(self.state, event)

    def test_reviewed_revision_preserves_output_and_cannot_repeat_itself(self):
        from case_kernel.case_agent_analysis_stage import prepare_case_analysis_revision
        from case_kernel.case_agent_supervisor import ArtifactReceipt
        task = self.state.graph.tasks[0]
        task = replace(task, skill=replace(task.skill, tool_id="analyze_lawyer_decision_package"))
        artifact = ArtifactReceipt(str(uuid4()), "LAWYER_DECISION_PACKAGE_CANDIDATE", "c" * 64,
            100, task.input_hash, True)
        state = replace(self.state, graph=replace(self.state.graph, tasks=(task,)), artifacts=(artifact,))
        kwargs = dict(candidate_bindings=self.bindings, approved_by=self.fixture.reviewer,
            revised_artifact_id=artifact.artifact_id, revised_artifact_hash=artifact.content_hash,
            revision_reason="旧输出遗漏关键材料，已更改分析结构后执行一次修订。")
        stage = prepare_case_analysis_revision(state=state, **kwargs)
        payload = CaseAnalysisStageReviewPayload(stage)
        kind = AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED
        self.assertEqual(_payload_from_json(kind, _payload_json(payload)), payload)
        event = replace(self.fixture.fixture.event(state.event_version + 1, kind, payload), actor_id=self.fixture.reviewer)
        after = reduce_agent_event(state, event)
        self.assertEqual(after.artifacts, state.artifacts)
        self.assertEqual(after.budget_usage, state.budget_usage)
        with self.assertRaises(AgentSupervisorBlocked):
            prepare_case_analysis_revision(state=replace(state, analysis_stage=stage), **kwargs)
        with self.assertRaises(AgentSupervisorBlocked):
            prepare_case_analysis_revision(state=state, **{**kwargs, "revised_artifact_hash": "f" * 64})
