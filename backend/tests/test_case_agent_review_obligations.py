from dataclasses import replace
import unittest
from uuid import uuid4

from case_kernel.case_agent_planner import PlanningInputStatus, PlanningSignalCategory
from case_kernel.case_agent_planning_snapshot import GovernedLawyerPlanningSignal
from case_kernel.case_agent_review_obligations import bind_review_obligation


class ReviewObligationTests(unittest.TestCase):
    def setUp(self):
        self.refs = frozenset({f"evidence-page:{uuid4()}"})
        self.signal = GovernedLawyerPlanningSignal(signal_id=str(uuid4()), signal_version="v14",
            decision_hash="a" * 64, category=PlanningSignalCategory.WORK_PLAN,
            code="LAWYER_DEFERRED_LEDGER_EXCEPTION", status=PlanningInputStatus.BLOCKED,
            summary="付款主体待核对，不确认实际还本。", source_ref_ids=tuple(self.refs))

    def test_preserves_pending_state_note_and_original_pages(self):
        result = bind_review_obligation(signal=self.signal, authorized_refs=self.refs)
        self.assertEqual(result.status, PlanningInputStatus.BLOCKED)
        self.assertEqual(result.review_note, self.signal.summary)
        self.assertEqual(set(result.source_ref_ids), self.refs)
        self.assertEqual(result.decision_hash, self.signal.decision_hash)
        self.assertEqual(result, bind_review_obligation(signal=self.signal, authorized_refs=self.refs))

    def test_changed_review_text_changes_source_hash(self):
        first = bind_review_obligation(signal=self.signal, authorized_refs=self.refs)
        second = bind_review_obligation(signal=replace(self.signal, summary="改稿仍待复核。"), authorized_refs=self.refs)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_missing_page_and_approval_promotion_are_rejected(self):
        with self.assertRaises(RuntimeError):
            bind_review_obligation(signal=self.signal, authorized_refs=frozenset())
        with self.assertRaises(ValueError):
            bind_review_obligation(signal=replace(self.signal, status=PlanningInputStatus.CONFIRMED), authorized_refs=self.refs)

    def test_reextraction_cannot_be_replaced_by_reading_a_note(self):
        with self.assertRaises(ValueError):
            bind_review_obligation(signal=replace(self.signal, code="LAWYER_REQUESTED_LEDGER_REEXTRACTION",
                status=PlanningInputStatus.OPEN), authorized_refs=self.refs)
