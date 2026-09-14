from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock
import unittest

import backend.tests.test_visual_candidate_recovery as fixture
from case_kernel.models import Actor, Role
from case_kernel.visual_candidate_recovery import VisualCandidateRecoveryBlocked
from case_kernel.visual_candidate_recovery_postgres import PostgresVisualRecoveryStore
from case_kernel.web_object_store import StoredCaseAgentReviewCandidate


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        data = fixture.VisualCandidateRecoveryTests()
        data.setUp()
        self.data = data
        self.objects = Mock()
        self.db = Mock()
        self.store = PostgresVisualRecoveryStore(dsn="fixture", worker_actor=Actor(
            actor_id=data.source.attempt_id, firm_id=data.source.firm_id,
            roles=frozenset({Role.SYSTEM_WORKER})), object_store=self.objects)
        @contextmanager
        def transaction(**kwargs):
            yield self.db
        self.store._transaction = transaction
        self.row = {"source_object_key": "original", "content_sha256": data.source.content_sha256,
                    "byte_size": len(data.raw), "source_object_version_id": "original-v1"}
        self.store._binding = Mock(return_value=(self.row, data.source))
        self.draft = fixture.prepare_visual_candidate_recovery(source=data.source, original=data.raw)
        self.prior = dict(recovery_id=self.draft.recovery_id, source_artifact_id=data.source.artifact_id,
            run_id=data.source.run_id, firm_id=data.source.firm_id, matter_id=data.source.matter_id,
            source_event_version=10,source_content_sha256=data.source.content_sha256,
            interpretation_policy="visual-candidate-cost-separation-v1",content_sha256=self.draft.content_sha256,
            byte_size=len(self.draft.payload),review_status="NEEDS_LAWYER_REVIEW",
            object_key="recovery",object_version_id="v1")
        self.objects.put_case_agent_review_candidate.return_value = StoredCaseAgentReviewCandidate(
            object_key="recovery",content_sha256=self.draft.content_sha256,
            byte_size=len(self.draft.payload),object_version_id="v1")
        self.objects.read_case_agent_review_candidate.side_effect = lambda stored, **kwargs: (
            data.raw if stored.object_key == "original" else self.draft.payload)
        self.args = dict(matter_id=data.source.matter_id,run_id=data.source.run_id,
                         source_artifact_id=data.source.artifact_id,expected_event_version=10)

    def test_stage_rereads_source_and_appends_only_recovery_metadata(self):
        self.store._prior = Mock(side_effect=[None,self.prior])
        self.assertEqual(self.store.stage(**self.args), self.draft)
        self.assertEqual(self.store._binding.call_count, 2)
        writes = [call.args[0] for call in self.db.execute.call_args_list if "INSERT" in call.args[0]]
        self.assertEqual(len(writes), 1)
        self.assertIn("INSERT INTO case_agent_visual_recovery_candidates", writes[0])
        self.assertIn("ON CONFLICT", writes[0])

    def test_idempotent_prior_is_readback_verified_without_new_put(self):
        self.store._prior = Mock(return_value=self.prior)
        self.store.stage(**self.args)
        self.objects.put_case_agent_review_candidate.assert_not_called()
        self.db.execute.assert_not_called()

    def test_changed_source_after_object_write_cannot_commit_metadata(self):
        self.store._prior = Mock(return_value=None)
        self.store._binding.side_effect = [(self.row,self.data.source),
            (self.row,replace(self.data.source,matter_version=self.data.source.matter_version+1))]
        with self.assertRaises(VisualCandidateRecoveryBlocked):
            self.store.stage(**self.args)
        self.assertFalse(any("INSERT" in call.args[0] for call in self.db.execute.call_args_list))

    def test_conflicting_idempotent_row_is_not_overwritten(self):
        self.store._prior = Mock(return_value={**self.prior,"content_sha256":"0"*64})
        with self.assertRaises(VisualCandidateRecoveryBlocked):
            self.store.stage(**self.args)
        self.objects.put_case_agent_review_candidate.assert_not_called()

    def test_object_corruption_prevents_metadata_write(self):
        self.store._prior = Mock(return_value=None)
        self.objects.read_case_agent_review_candidate.side_effect = [self.data.raw,b"wrong"]
        with self.assertRaises(VisualCandidateRecoveryBlocked):
            self.store.stage(**self.args)
        self.db.execute.assert_not_called()
