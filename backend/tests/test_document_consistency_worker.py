from __future__ import annotations

from uuid import uuid4
import unittest

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.document_consistency_postgres import ReviewedWorkProduct
from case_kernel.document_consistency_reviewer import ApprovedDocumentSnapshot, CanonicalDocumentField
from case_kernel.document_consistency_worker import (
    DocumentConsistencyWorkerBlocked,
    run_document_consistency_review,
)
from case_kernel.models import Actor, Role


class RecordingStore:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def record_review(self, **kwargs):
        self.kwargs = kwargs
        return CaseLedgerCommandReceipt(
            command_name="RECORD_DOCUMENT_CONSISTENCY_REVIEW",
            idempotency_key=kwargs["idempotency_key"], matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1, audit_event_id=str(uuid4()),
            object_type="DOCUMENT_CONSISTENCY_REVIEW", object_id=str(uuid4()),
        )


class DocumentConsistencyWorkerTests(unittest.TestCase):
    def test_worker_derives_review_and_persists_only_safe_projection(self) -> None:
        firm_id, matter_id, product_id = str(uuid4()), str(uuid4()), str(uuid4())
        worker = Actor(str(uuid4()), firm_id, frozenset({Role.SYSTEM_WORKER}))
        draft = ApprovedDraft(
            title="民事答辩状",
            sections=(ApprovedSection("答辩意见", ("案号为（2026）粤01民初100号。",), ("fact:1",)),),
            approval_hash="a" * 64,
        )
        document = ApprovedDocumentSnapshot(product_id, "DEFENCE_STATEMENT", draft)
        store = RecordingStore()
        receipt = run_document_consistency_review(
            matter_id=matter_id, expected_version=7, worker=worker,
            idempotency_key="document-consistency-worker-001",
            documents=(document,),
            canonical_fields=(CanonicalDocumentField(
                "case_no", "案号", "（2026）粤01民初100号", ("DEFENCE_STATEMENT",)
            ),),
            work_product_by_document_id={
                product_id: ReviewedWorkProduct(product_id, "b" * 64)
            },
            persistence=store,
        )
        self.assertEqual(receipt.matter_version, 8)
        assert store.kwargs is not None
        self.assertEqual(store.kwargs["actor"], worker)
        self.assertEqual(store.kwargs["documents"][0].work_product_id, product_id)
        self.assertEqual(store.kwargs["findings"], ())
        self.assertNotIn("粤01民初100号", repr(store.kwargs))

    def test_non_worker_is_rejected_before_review_or_persistence(self) -> None:
        firm_id, matter_id, product_id = str(uuid4()), str(uuid4()), str(uuid4())
        store = RecordingStore()
        with self.assertRaisesRegex(DocumentConsistencyWorkerBlocked, "SYSTEM_WORKER"):
            run_document_consistency_review(
                matter_id=matter_id, expected_version=1,
                worker=Actor(str(uuid4()), firm_id, frozenset({Role.LEAD_LAWYER})),
                idempotency_key="document-consistency-worker-bad-001",
                documents=(), canonical_fields=(),
                work_product_by_document_id={product_id: ReviewedWorkProduct(product_id, "b" * 64)},
                persistence=store,
            )
        self.assertIsNone(store.kwargs)


if __name__ == "__main__":
    unittest.main()
