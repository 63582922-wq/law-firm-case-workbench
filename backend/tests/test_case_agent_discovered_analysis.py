import json
import unittest
from uuid import uuid4
import test_case_agent_lawyer_analysis as fixtures
from case_kernel.case_agent_discovered_analysis import (build_discovered_analysis_contract,
    prepare_discovered_analysis_request, validate_discovered_analysis_core)
from case_kernel.case_agent_lawyer_analysis import LawyerAnalysisBlocked, qwen_lawyer_analysis_host


class DiscoveredAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.projection = fixtures._projection()
        self.contract = build_discovered_analysis_contract(self.projection)
        refs = self.contract.source_ids
        self.core = {"issues": [{"key": "payment", "title": "款项归属",
            "question": "现有证据是否足以证明款项归属？", "our_position": "须结合材料核对。",
            "opponent_position": "对方可能提出关联性质疑。", "source_refs": [refs[0]],
            "needs_lawyer_decision": True, "strengths": [], "weaknesses": [], "missing_evidence": [],
            "rebuttal_route": "回查原始凭证。", "residual_risk": "仍需核验主体。",
            "next_action": "核对证据来源。", "authority_refs": []}],
            "source_dispositions": [{"source_ref": ref,
                "disposition": "ISSUE_RELEVANT" if ref == refs[0] else "BACKGROUND",
                "issue_keys": ["payment"] if ref == refs[0] else [], "reason": "保留原始来源供复核。"}
                for ref in refs]}

    def test_request_combines_discovery_and_analysis_without_forced_source_issues(self):
        contract, request = prepare_discovered_analysis_request(projection=self.projection,
            task_id=str(uuid4()), attempt_id=str(uuid4()), endpoint_host=qwen_lawyer_analysis_host("ws-test-workspace"))
        body = json.loads(request.body)
        self.assertEqual(body["response_format"]["json_schema"]["name"], "case_agent_discovered_analysis_v1")
        self.assertNotIn("required_issue_refs_once_each", contract.user_prompt)
        self.assertNotIn("每个issues的supporting_source_refs", contract.system_prompt)
        fields = contract.schema["properties"]["issues"]["items"]["properties"]
        self.assertIn("question", fields)
        self.assertIn("rebuttal_route", fields)
        self.assertIn("next_action", fields)
        self.assertEqual(request.input_refs, self.projection.input_refs)
        self.assertLessEqual(request.worst_case_cost_minor_units, 120)

    def test_single_cross_source_issue_can_account_for_many_background_records(self):
        discovery = validate_discovered_analysis_core(self.core, contract=self.contract)
        self.assertEqual(len(discovery.issues), 1)
        self.assertEqual(len(discovery.source_dispositions), len(self.contract.source_ids))

    def test_source_disposition_issue_keys_are_derived_from_validated_issue_sources(self):
        self.core["source_dispositions"][0]["issue_keys"] = []
        self.core["source_dispositions"][1]["disposition"] = "ISSUE_RELEVANT"
        self.core["source_dispositions"][1]["issue_keys"] = ["payment"]
        discovery = validate_discovered_analysis_core(self.core, contract=self.contract)
        self.assertEqual(
            discovery.source_dispositions[0].issue_ids,
            (discovery.issues[0].issue_id,),
        )
        self.assertEqual(discovery.source_dispositions[1].disposition, "BACKGROUND")

    def test_official_calculation_or_source_omission_still_rejected(self):
        self.core["issues"][0]["next_action"] = "正式余额为123456元。"
        with self.assertRaises(LawyerAnalysisBlocked):
            validate_discovered_analysis_core(self.core, contract=self.contract)

    def test_absent_authority_does_not_emit_invalid_empty_enum(self):
        from case_kernel.case_agent_case_context import BoundCaseContextProjection
        p = self.projection
        sources = tuple(source for source in p.sources if source.source_type.value != "VERIFIED_LEGAL_SOURCE")
        projection = BoundCaseContextProjection.build(run_id=p.run_id, task_id=p.task_id,
            task_input_hash=p.task_input_hash, firm_id=p.firm_id, matter_id=p.matter_id,
            matter_version=p.matter_version, case_snapshot_hash=p.case_snapshot_hash,
            input_refs=tuple(source.input_ref for source in sources), sources=sources)
        contract = build_discovered_analysis_contract(projection)
        schema = contract.schema["properties"]["issues"]["items"]["properties"]["authority_refs"]
        self.assertEqual(schema["maxItems"], 0)
        self.assertEqual(schema["items"]["enum"], ["NO_AUTHORITY_AVAILABLE"])
        self.core["issues"][0]["next_action"] = "核对凭证。"
        self.core["source_dispositions"].pop()
        with self.assertRaises(LawyerAnalysisBlocked):
            validate_discovered_analysis_core(self.core, contract=self.contract)
