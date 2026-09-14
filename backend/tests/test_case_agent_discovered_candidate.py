from dataclasses import replace
import json
import unittest
from uuid import uuid4
import test_case_agent_discovered_analysis as fixtures
from case_kernel.case_agent_discovered_candidate import compile_discovered_candidate
from case_kernel.case_agent_lawyer_analysis import (ParsedLawyerAnalysisResponse,
    parse_lawyer_decision_package_candidate, lawyer_decision_package_source_refs, LawyerAnalysisBlocked,
    price_qwen37_minor_units, _json_bytes)
from case_api.web_case_agent_artifacts import _lawyer_decision_package_sections
from case_kernel.case_agent_verifier import (
    ManagedArtifactRead,
    first_release_review_candidate_verifiers,
)


class DiscoveredCandidateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DiscoveredAnalysisTests()
        self.fixture.setUp()
        self.parsed = ParsedLawyerAnalysisResponse(self.fixture.core, (), "synthetic-response",
            "c" * 64, 1000, 1000, 2000, price_qwen37_minor_units(1000, 1000))

    def compile(self):
        return compile_discovered_candidate(projection=self.fixture.projection, contract=self.fixture.contract,
            parsed=self.parsed, external_request_id=str(uuid4()), request_hash="d" * 64)

    def test_shared_reader_retains_sources_and_web_view_uses_discovered_title(self):
        payload = self.compile()
        value = parse_lawyer_decision_package_candidate(payload)
        self.assertEqual(lawyer_decision_package_source_refs(payload), frozenset(self.fixture.projection.input_refs))
        title, sections = _lawyer_decision_package_sections(value)
        self.assertEqual(title, "争点与办案建议")
        self.assertEqual(sections[0].items[0].title, "款项归属")
        self.assertEqual(len(sections[0].items), 1)
        self.assertIn("我方立场", sections[0].items[0].detail)
        self.assertEqual(len(sections[0].items[0].sources), 1)

    def test_first_release_verifier_accepts_discovered_candidate(self):
        payload = self.compile()
        verifier = first_release_review_candidate_verifiers()[
            "LAWYER_DECISION_PACKAGE_CANDIDATE"
        ]
        receipt = verifier.verify(
            ManagedArtifactRead(
                artifact_id=str(uuid4()),
                artifact_kind="LAWYER_DECISION_PACKAGE_CANDIDATE",
                content=payload,
                source_input_hash=self.fixture.projection.task_input_hash,
                object_receipt_hash="e" * 64,
                media_type="application/json",
            )
        )
        self.assertEqual(receipt.artifact_kind, "LAWYER_DECISION_PACKAGE_CANDIDATE")

    def test_tampering_with_source_approval_or_discovered_id_rejected(self):
        original = json.loads(self.compile())
        for mode in ("source", "approval", "issue", "cost"):
            value = json.loads(_json_bytes(original))
            if mode == "source": value["projection"]["sources"][0]["primary_text"] = "改变原始材料"
            if mode == "approval": value["court_ready"] = True
            if mode == "issue": value["discovery"]["issues"][0]["title"] = "另一个结论"
            if mode == "cost": value["provider_receipt"]["cost_minor_units"] += 1
            with self.assertRaises(LawyerAnalysisBlocked):
                parse_lawyer_decision_package_candidate(_json_bytes(value))

    def test_contract_drift_cannot_compile(self):
        self.fixture.contract = replace(self.fixture.contract, source_hash="f" * 64)
        with self.assertRaises(LawyerAnalysisBlocked): self.compile()

    def test_internal_report_preserves_issues_without_promoting_source_authority(self):
        from case_kernel.case_agent_document_delivery import _discovered_case_review_sections
        package_ref = "lawyer-decision-package:" + str(uuid4())
        posture_ref = self.fixture.projection.input_refs[0]
        value = parse_lawyer_decision_package_candidate(self.compile())
        sections = _discovered_case_review_sections(decision_package=value, represented_party="合成当事人",
            posture_refs=(posture_ref,), decision_package_refs=(package_ref,),
            authorized_refs=frozenset((package_ref, posture_ref)), fact_paragraphs=())
        body = "\n".join(paragraph.text for section in sections for paragraph in section.paragraphs)
        self.assertIn("款项归属", body)
        self.assertIn("我方待确认立场", body)
        self.assertNotIn("模型运行与验证说明", body)
        self.assertTrue(all(set(paragraph.source_refs).issubset({package_ref, posture_ref})
            for section in sections for paragraph in section.paragraphs))

    def test_actual_adapter_routes_unconfirmed_sources_through_new_request_and_staging(self):
        import test_case_agent_lawyer_analysis as legacy
        from case_kernel.case_agent_case_context import BoundCaseContextProjection, CaseContextSourceType
        from case_kernel.case_agent_planner import PlanningInputStatus
        from case_kernel.case_agent_lawyer_analysis_adapters import QwenLawyerAnalysisTaskAdapter
        base = self.fixture.projection
        old = next(source for source in base.sources if source.source_type is CaseContextSourceType.CASE_FACT)
        new = replace(old, source_type=CaseContextSourceType.FACT_CANDIDATE,
            input_ref="fact-candidate:" + old.object_id, status=PlanningInputStatus.REVIEW_REQUIRED)
        sources = tuple(new if source is old else source for source in base.sources)
        projection = BoundCaseContextProjection.build(run_id=base.run_id, task_id=base.task_id,
            task_input_hash=base.task_input_hash, firm_id=base.firm_id, matter_id=base.matter_id,
            matter_version=base.matter_version, case_snapshot_hash=base.case_snapshot_hash,
            input_refs=tuple(source.input_ref for source in sources), sources=sources)
        core = json.loads(json.dumps(self.fixture.core).replace(old.input_ref, new.input_ref))
        context, staging = legacy._Context(projection), legacy._Staging()
        exchange = legacy._Exchange(legacy._provider_response(core))
        adapter = QwenLawyerAnalysisTaskAdapter(projection_port=legacy._ProjectionPort(projection),
            exchange=exchange, staging_port=staging)
        outcome = adapter.execute(context=context)
        self.assertEqual(outcome.status.value, "SUCCEEDED")
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(json.loads(exchange.requests[0].body)["response_format"]["json_schema"]["name"],
            "case_agent_discovered_analysis_v1")
        value = parse_lawyer_decision_package_candidate(staging.request.payload)
        self.assertEqual(value["schema_version"], "agent-discovered-lawyer-analysis-candidate-v1")
