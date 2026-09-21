from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.submission_work_product_coordinator import (
    SubmissionWorkProductCoordinationBlocked,
    coordinate_pdf_draft_work_product,
)


class FakeSubmissionWorkProductPersistence:
    def __init__(self, matter_id: str) -> None:
        self.matter_id = matter_id
        self.calls: list[dict] = []

    def register_work_product_candidate(self, **kwargs) -> CaseLedgerCommandReceipt:
        self.calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            "REGISTER_SUBMISSION_WORK_PRODUCT_CANDIDATE",
            kwargs["idempotency_key"],
            self.matter_id,
            kwargs["expected_version"] + 1,
            str(uuid4()),
            "SUBMISSION_WORK_PRODUCT",
            str(uuid4()),
        )


class SubmissionWorkProductCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        self.draft = ApprovedDraft(
            "民事答辩状",
            (ApprovedSection("答辩意见", ("被告对本金金额无异议。",), ("FACT-001", "RULE-001")),),
            "a" * 64,
        )

    def test_pdf_candidate_is_encrypted_and_review_hash_binds_exact_output(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"
            root.mkdir()
            store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed-artifacts", key_id="test-v1", encryption_key=b"k" * 32
            )
            persistence = FakeSubmissionWorkProductPersistence(self.matter_id)
            result = coordinate_pdf_draft_work_product(
                matter_id=self.matter_id,
                expected_version=8,
                idempotency_key="document-draft-001",
                document_kind="DEFENCE_STATEMENT",
                audience="COURT_SUBMISSION",
                draft=self.draft,
                case_root=root,
                artifact_store=store,
                persistence=persistence,
                system_actor=self.actor,
            )
            encrypted = store.read_bytes(result.artifact.object_key, expected_sha256=result.artifact.plaintext_sha256)
        self.assertTrue(encrypted.startswith(b"%PDF-"))
        self.assertEqual(result.page_count, 1)
        self.assertEqual(len(result.review_input_hash), 64)
        self.assertEqual(persistence.calls[0]["review_input_hash"], result.review_input_hash)
        self.assertEqual(persistence.calls[0]["semantic_text_sha256"], result.semantic_text_sha256)
        self.assertNotEqual(result.review_input_hash, result.artifact.plaintext_sha256)

    def test_non_worker_cannot_generate_a_reviewable_work_product(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"
            root.mkdir()
            store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed-artifacts", key_id="test-v1", encryption_key=b"k" * 32
            )
            non_worker = Actor(str(uuid4()), self.actor.firm_id, frozenset({Role.LEAD_LAWYER}))
            with self.assertRaisesRegex(SubmissionWorkProductCoordinationBlocked, "SYSTEM_WORKER"):
                coordinate_pdf_draft_work_product(
                    matter_id=self.matter_id,
                    expected_version=1,
                    idempotency_key="document-draft-002",
                    document_kind="DEFENCE_STATEMENT",
                    audience="COURT_SUBMISSION",
                    draft=self.draft,
                    case_root=root,
                    artifact_store=store,
                    persistence=FakeSubmissionWorkProductPersistence(self.matter_id),
                    system_actor=non_worker,
                )


if __name__ == "__main__":
    unittest.main()
