from dataclasses import replace
from types import SimpleNamespace
import unittest
from uuid import uuid4
from backend.tests import test_case_agent_ledger_extraction as fixture_module
from case_api.web_fact_correction import WebFactCorrectionOriginalReader
from case_kernel.case_agent_ledger_extraction import build_case_ledger_extraction_candidate
from case_kernel.case_agent_fact_correction_postgres import FactCorrectionBlocked
from case_kernel.models import Actor, Role
from case_kernel.web_agent_material_review import AgentEvidencePageProjection


class FactCorrectionSourceTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.CaseLedgerExtractionTests(); fixture.setUp()
        self.raw,_ = build_case_ledger_extraction_candidate(task_input_hash=fixture_module.digest("task"),
            source_pages=(fixture.page,),candidates=(fixture._fact(),))
        self.page = AgentEvidencePageProjection.build(evidence_page_id=fixture.page_id,
            source_file_sha256=fixture.page.source_file_sha256,page_number=1,extracted_text="2020年1月1日转账100元")
        self.version = 11
        self.reader = WebFactCorrectionOriginalReader(
            artifact_review_service=SimpleNamespace(read_verified_extraction_original=lambda **kw:self.raw),
            evidence_projection=SimpleNamespace(load_pages=lambda **kw:(self.page,)),
            matter_store=SimpleNamespace(get=lambda *a,**kw:SimpleNamespace(version=self.version)))
        self.args = dict(actor=Actor(str(uuid4()),str(uuid4()),frozenset({Role.LEAD_LAWYER})),
            matter_id=str(uuid4()),artifact_id=str(uuid4()),expected_matter_version=11)

    def test_original_bytes_returned_after_page_reverification(self):
        self.assertEqual(self.reader.read_verified_original(**self.args),self.raw)

    def test_changed_file_page_or_text_rejected(self):
        original = self.page
        for change in (dict(source_file_sha256="0"*64),dict(page_number=2),dict(extracted_text="其他文字"),
                       dict(extracted_text_sha256="0"*64),dict(evidence_page_id=str(uuid4()))):
            self.page=replace(original,**change)
            with self.subTest(change=change):
                with self.assertRaises(FactCorrectionBlocked): self.reader.read_verified_original(**self.args)

    def test_concurrent_version_change_and_worker_rejected(self):
        def changing(**kw):
            self.version=12
            return (self.page,)
        self.reader._pages=SimpleNamespace(load_pages=changing)
        with self.assertRaisesRegex(FactCorrectionBlocked,"during source review"):
            self.reader.read_verified_original(**self.args)
        worker=replace(self.args["actor"],roles=frozenset({Role.SYSTEM_WORKER}))
        with self.assertRaisesRegex(FactCorrectionBlocked,"lawyer"):
            self.reader.read_verified_original(**{**self.args,"actor":worker})


if __name__ == "__main__": unittest.main()
