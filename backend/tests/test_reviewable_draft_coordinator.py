from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4
from reportlab.pdfgen import canvas
from case_kernel.approved_draft_worker import DraftArtifact
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf
from case_kernel.reviewable_draft_coordinator import coordinate_reviewable_office_draft
from case_kernel.reviewable_draft_worker import ReviewableOfficeDraft

class FakePersistence:
    def register_reviewable_office_draft_pair(self, **kwargs):
        self.payload = kwargs
        return CaseLedgerCommandReceipt("REGISTER_REVIEWABLE_OFFICE_DRAFT_PAIR", kwargs["idempotency_key"], kwargs["matter_id"], kwargs["expected_version"] + 1, str(uuid4()), "REVIEWABLE_OFFICE_DRAFT_PAIR", str(uuid4()))

class ReviewableDraftCoordinatorTests(TestCase):
    def test_pair_is_encrypted_and_registered_with_the_exact_review_binding(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"; root.mkdir()
            output = BytesIO(); document = canvas.Canvas(output); document.drawString(20, 700, "review"); document.save()
            pdf, editable = output.getvalue(), b"PK\x03\x04editable"
            persistence = FakePersistence()
            pair = ReviewableOfficeDraft(DraftArtifact("application/vnd.openxmlformats-officedocument.wordprocessingml.document", editable, sha256(editable).hexdigest()), ConvertedOfficePdf(sha256(editable).hexdigest(), "WORD_DOCUMENT", "test", "1", "a" * 64, sha256(pdf).hexdigest(), len(pdf), 1, "b" * 64, pdf), "c" * 64, "d" * 64)
            store = LocalEncryptedArtifactStore(Path(temporary) / "managed", key_id="t", encryption_key=b"k" * 32)
            result = coordinate_reviewable_office_draft(matter_id=str(uuid4()), expected_version=1, idempotency_key="office-draft-1", document_kind="DEFENCE_STATEMENT", draft=pair, case_root=root, artifact_store=store, persistence=persistence, system_actor=Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER})))
            self.assertEqual(persistence.payload["review_input_hash"], result.review_input_hash)
            self.assertEqual(store.read_bytes(result.review_pdf.object_key, expected_sha256=result.review_pdf.plaintext_sha256), pdf)
