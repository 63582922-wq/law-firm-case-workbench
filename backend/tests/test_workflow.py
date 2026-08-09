from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from case_kernel.errors import AuthorizationDenied, IdempotencyConflict, PreconditionBlocked, VersionConflict
from case_kernel.models import Actor, MatterStage, Role, SubmissionValidity
from case_kernel.store import InMemoryMatterStore
from case_kernel.workflow import MatterWorkflow


class MatterWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMatterStore()
        self.workflow = MatterWorkflow(self.store)
        self.lead = Actor("lawyer_1", "firm_alpha", frozenset({Role.LEAD_LAWYER}))
        self.assistant = Actor("assistant_1", "firm_alpha", frozenset({Role.ASSISTANT}))
        self.other_firm_lawyer = Actor("lawyer_2", "firm_beta", frozenset({Role.LEAD_LAWYER}))
        self.workflow.create_matter(
            self.lead,
            matter_id="matter_synthetic_001",
            title="合成民间借贷案件",
            idempotency_key="create-synthetic-001",
        )

    def advance_to(self, stage: MatterStage) -> None:
        index = 0
        while self.store.get("matter_synthetic_001").stage is not stage:
            matter = self.store.get("matter_synthetic_001")
            self.workflow.advance(
                self.lead,
                matter_id=matter.matter_id,
                expected_version=matter.version,
                idempotency_key=f"advance-{index}",
            )
            index += 1

    def test_only_lead_lawyer_in_same_firm_can_advance(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self.workflow.advance(
                self.assistant,
                matter_id="matter_synthetic_001",
                expected_version=1,
                idempotency_key="assistant-advance",
            )
        with self.assertRaises(AuthorizationDenied):
            self.workflow.advance(
                self.other_firm_lawyer,
                matter_id="matter_synthetic_001",
                expected_version=1,
                idempotency_key="cross-firm-advance",
            )
        self.assertEqual(MatterStage.CREATED, self.store.get("matter_synthetic_001").stage)

    def test_creation_is_audited_and_idempotent(self) -> None:
        first = self.workflow.create_matter(
            self.lead,
            matter_id="matter_synthetic_002",
            title="第二个合成案件",
            idempotency_key="create-synthetic-002",
        )
        second = self.workflow.create_matter(
            self.lead,
            matter_id="matter_synthetic_002",
            title="第二个合成案件",
            idempotency_key="create-synthetic-002",
        )
        self.assertEqual(first, second)
        events = self.store.audit_events("matter_synthetic_002")
        self.assertEqual(["MATTER_CREATED"], [event.event_type for event in events])

    def test_stale_version_is_rejected(self) -> None:
        self.workflow.advance(
            self.lead,
            matter_id="matter_synthetic_001",
            expected_version=1,
            idempotency_key="advance-once",
        )
        with self.assertRaises(VersionConflict):
            self.workflow.advance(
                self.lead,
                matter_id="matter_synthetic_001",
                expected_version=1,
                idempotency_key="advance-stale",
            )

    def test_same_idempotency_key_returns_original_receipt_and_does_not_advance_twice(self) -> None:
        first = self.workflow.advance(
            self.lead,
            matter_id="matter_synthetic_001",
            expected_version=1,
            idempotency_key="advance-idempotent",
        )
        second = self.workflow.advance(
            self.lead,
            matter_id="matter_synthetic_001",
            expected_version=1,
            idempotency_key="advance-idempotent",
        )
        self.assertEqual(first, second)
        self.assertEqual(MatterStage.INGESTING, self.store.get("matter_synthetic_001").stage)
        with self.assertRaises(IdempotencyConflict):
            self.workflow.advance(
                self.lead,
                matter_id="matter_synthetic_001",
                expected_version=2,
                idempotency_key="advance-idempotent",
            )

    def test_lock_requires_current_version_final_text_approval(self) -> None:
        self.advance_to(MatterStage.READY_TO_EXPORT)
        matter = self.store.get("matter_synthetic_001")
        final_text = "合成答辩状"
        final_text_hash = sha256(final_text.encode("utf-8")).hexdigest()
        with self.assertRaises(PreconditionBlocked):
            self.workflow.lock_submission(
                self.lead,
                matter_id=matter.matter_id,
                expected_version=matter.version,
                idempotency_key="lock-without-approval",
                final_text=final_text,
            )
        self.workflow.record_approval(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="approve-final-text",
            approval_type="FINAL_TEXT",
            approved_object_hash=final_text_hash,
        )
        matter = self.store.get("matter_synthetic_001")
        self.workflow.lock_submission(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="lock-approved",
            final_text=final_text,
        )
        matter = self.store.get("matter_synthetic_001")
        self.assertIsNotNone(matter.current_submission_bundle_id)
        self.assertEqual(SubmissionValidity.VALID, matter.bundles[matter.current_submission_bundle_id].validity)

    def test_lock_rejects_text_that_differs_from_the_approved_hash(self) -> None:
        self.advance_to(MatterStage.READY_TO_EXPORT)
        matter = self.store.get("matter_synthetic_001")
        approved_text = "合成答辩状第一稿"
        self.workflow.record_approval(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="approve-specific-final-text",
            approval_type="FINAL_TEXT",
            approved_object_hash=sha256(approved_text.encode("utf-8")).hexdigest(),
        )
        matter = self.store.get("matter_synthetic_001")
        with self.assertRaises(PreconditionBlocked):
            self.workflow.lock_submission(
                self.lead,
                matter_id=matter.matter_id,
                expected_version=matter.version,
                idempotency_key="lock-different-text",
                final_text="合成答辩状第二稿",
            )
        self.assertIsNone(self.store.get("matter_synthetic_001").current_submission_bundle_id)

    def test_export_stage_cannot_be_reached_without_a_valid_locked_bundle(self) -> None:
        self.advance_to(MatterStage.READY_TO_EXPORT)
        matter = self.store.get("matter_synthetic_001")
        with self.assertRaises(PreconditionBlocked):
            self.workflow.advance(
                self.lead,
                matter_id=matter.matter_id,
                expected_version=matter.version,
                idempotency_key="export-without-lock",
            )
        self.assertEqual(MatterStage.READY_TO_EXPORT, self.store.get("matter_synthetic_001").stage)

    def test_upstream_change_stales_current_submission_and_clears_current_pointer(self) -> None:
        self.advance_to(MatterStage.READY_TO_EXPORT)
        matter = self.store.get("matter_synthetic_001")
        final_text = "合成答辩状"
        final_text_hash = sha256(final_text.encode("utf-8")).hexdigest()
        self.workflow.record_approval(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="approve-a",
            approval_type="FINAL_TEXT",
            approved_object_hash=final_text_hash,
        )
        matter = self.store.get("matter_synthetic_001")
        self.workflow.lock_submission(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="lock-before-change",
            final_text=final_text,
        )
        matter = self.store.get("matter_synthetic_001")
        bundle_id = matter.current_submission_bundle_id
        self.workflow.invalidate_from_upstream_change(
            self.lead,
            matter_id=matter.matter_id,
            expected_version=matter.version,
            idempotency_key="fact-changed",
            change_kind="FACT_CHANGED",
        )
        matter = self.store.get("matter_synthetic_001")
        self.assertEqual(MatterStage.FACT_REVIEW, matter.stage)
        self.assertIsNone(matter.current_submission_bundle_id)
        self.assertEqual(SubmissionValidity.STALE, matter.bundles[bundle_id].validity)


if __name__ == "__main__":
    unittest.main(verbosity=2)
