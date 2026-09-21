from dataclasses import replace
import unittest
from uuid import uuid4

import test_case_agent_supervisor as fixtures
from case_kernel.case_agent_material_coverage import MaterialExtractionCoverage
from case_kernel.case_agent_material_stage import prepare_supplementary_material_stage, validate_supplementary_stage_graph
from case_kernel.case_agent_supervisor import (AgentRunStatus, AgentSupervisorBlocked, BudgetUsage,
    AgentEventType, SupplementaryMaterialStageReviewPayload, reduce_agent_event, decide_next_commands,
    SupervisorCommandKind)
from case_kernel.case_agent_postgres import _payload_json, _payload_from_json, _state_json


class SupplementaryMaterialStageTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CaseAgentSupervisorTests()
        fixture.setUp()
        self.fixture = fixture
        initial = fixture.state_with_graph(fixture.graph((fixture.local_task(),)))
        self.state = replace(initial,
            status=AgentRunStatus.READY_FOR_REVIEW, verification_hash="a" * 64,
            budget=replace(initial.budget, max_external_calls=1, max_cost_minor_units=120),
            budget_usage=BudgetUsage(attempts=3, external_calls=1,
                runtime_seconds=90, cost_minor_units=5, output_bytes=2048))
        self.old_page, self.new_page = str(uuid4()), str(uuid4())
        self.coverage = (MaterialExtractionCoverage(str(uuid4()), "b" * 64,
            (self.old_page, self.new_page), (self.old_page,)),)
        self.reviewer = str(uuid4())

    def prepare(self):
        return prepare_supplementary_material_stage(**dict(
            state=self.state, coverage=self.coverage, approved_by=self.reviewer,
            external_call_cap=1, cost_cap_minor_units=120))

    def test_new_page_only_and_cumulative_usage_never_reset(self):
        result = self.prepare()
        self.assertEqual(result.page_refs, (f"evidence-page:{self.new_page}",))
        self.assertEqual(result.proposed_budget.max_external_calls, 2)
        self.assertEqual(result.proposed_budget.max_cost_minor_units, 125)
        self.assertEqual(self.state.budget_usage.external_calls, 1)
        self.assertEqual(self.state.budget.max_external_calls, 1)
        self.assertEqual(result.previous_graph_hash, self.state.graph.graph_hash)

    def test_same_review_is_deterministic_and_version_change_invalidates(self):
        first = self.prepare()
        self.assertEqual(first, self.prepare())
        self.state = replace(self.state, event_version=self.state.event_version + 1)
        self.assertNotEqual(first.stage_hash, self.prepare().stage_hash)

    def test_processed_page_cannot_be_selected_again(self):
        self.coverage = (replace(self.coverage[0], extracted_page_ids=(self.old_page, self.new_page)),)
        with self.assertRaisesRegex(AgentSupervisorBlocked, "no unprocessed"):
            self.prepare()

    def test_unknown_running_failed_or_stale_stage_cannot_be_retried(self):
        old = self.state
        for status in (AgentRunStatus.EXECUTING, AgentRunStatus.RECONCILIATION_REQUIRED,
                       AgentRunStatus.FAILED, AgentRunStatus.WAITING_INPUT):
            self.state = replace(old, status=status)
            with self.assertRaises(AgentSupervisorBlocked):
                self.prepare()
        self.state = replace(old, stale=True)
        with self.assertRaises(AgentSupervisorBlocked):
            self.prepare()

    def test_caps_cannot_be_missing_boolean_or_multiple_calls(self):
        for calls, cost in ((True, 120), (2, 120), (1, 121), (1, 0), (1, True)):
            with self.assertRaises(AgentSupervisorBlocked):
                prepare_supplementary_material_stage(state=self.state, coverage=self.coverage,
                    approved_by=self.reviewer, external_call_cap=calls, cost_cap_minor_units=cost)

    def test_duplicate_identity_or_foreign_extracted_page_is_rejected(self):
        for coverage in ((self.coverage[0], self.coverage[0]),
                         (replace(self.coverage[0], extracted_page_ids=(str(uuid4()),)),)):
            self.coverage = coverage
            with self.assertRaises(AgentSupervisorBlocked):
                self.prepare()

    def test_review_roundtrip_preserves_goal_usage_and_old_outputs(self):
        payload = SupplementaryMaterialStageReviewPayload(self.prepare())
        kind = AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED
        self.assertEqual(_payload_from_json(kind, _payload_json(payload)), payload)
        event = replace(self.fixture.event(self.state.event_version + 1, kind, payload), actor_id=self.reviewer)
        state = reduce_agent_event(self.state, event)
        self.assertEqual(state.status, AgentRunStatus.STALE)
        self.assertEqual(state.goal, self.state.goal)
        self.assertEqual(state.artifacts, self.state.artifacts)
        self.assertEqual(state.budget_usage, self.state.budget_usage)
        self.assertEqual(decide_next_commands(state)[0].kind, SupervisorCommandKind.REQUEST_REPLAN)
        self.assertIn("material_stage", _state_json(state))
        self.assertNotIn("material_stage", _state_json(self.state))

    def test_actor_or_fingerprint_changes_cannot_review_stage(self):
        kind = AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED
        for stage in (replace(self.prepare(), approved_by=str(uuid4())),
                      replace(self.prepare(), coverage_hash="0" * 64)):
            event = replace(self.fixture.event(self.state.event_version + 1, kind,
                SupplementaryMaterialStageReviewPayload(stage)), actor_id=self.reviewer)
            with self.assertRaises(AgentSupervisorBlocked):
                reduce_agent_event(self.state, event)

    def test_compiled_stage_cannot_skip_pages_add_analysis_or_repeat_extraction(self):
        stage = self.prepare()
        base = self.fixture.local_task()
        reader = replace(base, input_refs=stage.page_refs,
            skill=replace(base.skill, tool_id="extract_pdf_text", skill_id="pdf_reading"),
            budget=replace(base.budget, max_external_calls=0))
        extractor = replace(base, task_id=str(uuid4()), input_refs=stage.page_refs,
            dependency_ids=(reader.task_id,),
            skill=replace(base.skill, tool_id="extract_case_ledger", skill_id="case_ledger_extraction"),
            budget=replace(base.budget, max_attempts=1, max_external_calls=1, max_cost_minor_units=120))
        graph = replace(self.state.graph, tasks=(reader, extractor))
        validate_supplementary_stage_graph(stage=stage, graph=graph)
        for tasks in ((reader, replace(extractor, input_refs=())),
                      (reader, replace(extractor, dependency_ids=())),
                      (reader, extractor, extractor),
                      (reader, replace(extractor, skill=replace(extractor.skill,
                          tool_id="analyze_lawyer_decision_package", skill_id="lawyer_decision_package")))):
            with self.assertRaises(AgentSupervisorBlocked):
                validate_supplementary_stage_graph(stage=stage, graph=replace(graph, tasks=tasks))
