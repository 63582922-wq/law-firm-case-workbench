from dataclasses import asdict
from unittest.mock import Mock
import unittest

import backend.tests.test_visual_candidate_recovery as fixture
from case_api.web_case_agent_artifacts import (
    PostgresWebCaseAgentArtifactReviewService, WebCaseAgentArtifactReviewBlocked,
)
from case_kernel.models import Actor, Role


class WebVisualRecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        f = fixture.VisualCandidateRecoveryTests()
        f.setUp()
        self.draft = fixture.prepare_visual_candidate_recovery(source=f.source, original=f.raw)
        source = f.source
        self.objects = Mock()
        self.service = PostgresWebCaseAgentArtifactReviewService(dsn="fixture",object_store=self.objects)
        c = {key:getattr(source,key) for key in ("firm_id","matter_id","run_id","task_id","artifact_id",
                                                "content_sha256","byte_size","task_input_hash")}
        c.update(source_object_key="original",source_object_version_id="v1")
        self.row = dict(source_candidate=c,recovery=dict(content_sha256=self.draft.content_sha256,
            byte_size=len(self.draft.payload),object_key="recovery",object_version_id="v1"),
            snapshot_matter_version=source.matter_version,current_event_version=10,run_status="FAILED",
            failure_code=source.failure_code,result_status="SUCCEEDED",attempt_id=source.attempt_id,
            external_request_id=source.external_request_id,cost_minor_units=6,input_refs=source.input_refs)
        self.service._load_visual_recovery_row = Mock(return_value=self.row)
        self.objects.read_case_agent_review_candidate.side_effect = [f.raw,self.draft.payload]
        self.args = dict(actor=Actor(actor_id=source.attempt_id,firm_id=source.firm_id,
            roles=frozenset({Role.LEAD_LAWYER})),matter_id=source.matter_id,
            run_id=source.run_id,recovery_id=self.draft.recovery_id)

    def test_review_keeps_failure_notice_and_does_not_expose_receipts(self):
        review = self.service._read_visual_recovery_review(**self.args)
        self.assertEqual(review.title,"图片识别恢复件（待复核）")
        self.assertIn("历史失败",review.review_notice)
        self.assertIn("全文完整性待核对",str(asdict(review)))
        self.assertNotIn(self.draft.content_sha256,str(asdict(review)))
        self.assertEqual(self.service._load_visual_recovery_row.call_count,2)

    def test_denied_access_never_reads_objects(self):
        self.service._load_visual_recovery_row.return_value=None
        self.assertIsNone(self.service._read_visual_recovery_review(**self.args))
        self.objects.read_case_agent_review_candidate.assert_not_called()

    def test_access_revoked_during_object_io_blocks_return(self):
        self.service._load_visual_recovery_row.side_effect=[self.row,None]
        with self.assertRaises(WebCaseAgentArtifactReviewBlocked):
            self.service._read_visual_recovery_review(**self.args)

    def test_modified_recovery_bytes_are_not_projected(self):
        raw = self.objects.read_case_agent_review_candidate.side_effect
        original = next(raw)
        self.objects.read_case_agent_review_candidate.side_effect=[original,b'{}']
        with self.assertRaises(WebCaseAgentArtifactReviewBlocked):
            self.service._read_visual_recovery_review(**self.args)
