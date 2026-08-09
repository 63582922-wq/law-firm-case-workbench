from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_derivative_coordinator import (
    EvidenceDerivativeCoordinationBlocked,
    coordinate_evidence_derivatives,
)
from case_kernel.evidence_derivative_worker import SourcePdfBinding
from case_kernel.evidence_manifest_postgres import PersistentEvidenceSnapshot
from case_kernel.local_case_folder import root_fingerprint
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role


class FakeDerivativePersistence:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def register_derivative_candidate(self, **kwargs):
        self.calls.append(("register", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="REGISTER_EVIDENCE_DERIVATIVE_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="EVIDENCE_DERIVATIVE",
            object_id=str(uuid4()),
        )

    def verify_derivative(self, **kwargs):
        self.calls.append(("verify", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="VERIFY_EVIDENCE_DERIVATIVE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="EVIDENCE_DERIVATIVE",
            object_id=kwargs["derivative_id"],
        )


def create_source(path: Path) -> Path:
    document = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    for page_number in (1, 2):
        document.setFont("Helvetica-Bold", 16)
        document.drawString(72, 750, "SYNTHETIC COORDINATOR SOURCE")
        document.setFont("Helvetica", 12)
        document.drawString(72, 660, f"Synthetic page {page_number}")
        document.showPage()
    document.save()
    return path


class EvidenceDerivativeCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="evidence-coordinator-test-")
        self.root = Path(self.temporary.name)
        self.case_root = self.root / "case-folder"
        self.case_root.mkdir()
        self.source = create_source(self.case_root / "synthetic.pdf")
        self.source_hash = sha256(self.source.read_bytes()).hexdigest()
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.file_id = str(uuid4())
        self.page_id = str(uuid4())
        self.decision_id = str(uuid4())
        self.annotation_id = str(uuid4())
        self.manifest_id = str(uuid4())
        self.snapshot = PersistentEvidenceSnapshot(
            matter_id=self.matter_id,
            version=10,
            snapshot_hash="f" * 64,
            original_files=(
                {
                    "evidence_file_id": self.file_id,
                    "original_label": "[合成] 微信流水.pdf",
                    "original_file_sha256": self.source_hash,
                    "byte_size": self.source.stat().st_size,
                    "media_type": "application/pdf",
                    "page_count": 2,
                    "source_scan_fingerprint": "e" * 64,
                    "supersedes_file_id": None,
                    "created_at": "2026-08-09T00:00:00+00:00",
                },
            ),
            pages=(
                {
                    "evidence_page_id": self.page_id,
                    "evidence_file_id": self.file_id,
                    "page_number": 1,
                    "rendered_page_sha256": None,
                    "decision": {
                        "decision_id": self.decision_id,
                        "disposition": "INCLUDE",
                        "reason": "[合成] 与目标主体相关。",
                        "approval_hash": "d" * 64,
                        "approved_by": str(uuid4()),
                    },
                    "annotations": (
                        {
                            "annotation_id": self.annotation_id,
                            "purpose": "HIGHLIGHT_RELEVANT_REGION",
                            "x0": "0.08",
                            "y0": "0.12",
                            "x1": "0.80",
                            "y1": "0.28",
                            "label": "[合成] 交易行",
                            "status": "APPROVED",
                            "approval_hash": "c" * 64,
                            "approved_by": str(uuid4()),
                        },
                    ),
                },
            ),
            duplicate_groups=(),
            locked_manifest={
                "manifest_id": self.manifest_id,
                "ledger_version": 9,
                "status": "LOCKED",
                "content_hash": "b" * 64,
                "total_pages": 2,
                "included_pages": 1,
                "excluded_pages": 1,
                "approval_hash": "a" * 64,
                "approved_by": str(uuid4()),
                "entries": (
                    {
                        "evidence_page_id": self.page_id,
                        "decision_id": self.decision_id,
                        "disposition": "INCLUDE",
                        "derivative_sequence": 1,
                    },
                ),
            },
            derivatives=(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verified_outputs_are_encrypted_then_registered_in_recoverable_order(self) -> None:
        artifact_store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-key-v1",
            encryption_key=b"q" * 32,
        )
        persistence = FakeDerivativePersistence()
        result = coordinate_evidence_derivatives(
            snapshot=self.snapshot,
            source_bindings=(
                SourcePdfBinding(
                    evidence_file_id=self.file_id,
                    relative_path="synthetic.pdf",
                    expected_sha256=self.source_hash,
                    expected_page_count=2,
                ),
            ),
            case_root=self.case_root,
            confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
            staging_root=self.root / "staging",
            artifact_store=artifact_store,
            persistence=persistence,
            system_actor=self.actor,
            idempotency_prefix="synthetic-derivative-run-001",
        )
        self.assertEqual(result.final_matter_version, 14)
        self.assertEqual([name for name, _ in persistence.calls], ["register", "verify", "register", "verify"])
        self.assertEqual([item.artifact_type for item in result.artifacts], ["RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"])
        self.assertEqual(len(list((self.root / "managed").rglob("*.lca"))), 2)
        self.assertEqual(list((self.root / "staging").rglob("*.pdf")), [])
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        for item in result.artifacts:
            decrypted = artifact_store.read_bytes(item.object_key, expected_sha256=item.artifact_sha256)
            self.assertTrue(decrypted.startswith(b"%PDF-"))
            self.assertEqual(len(item.verification_hash), 64)

    def test_non_system_identity_and_snapshot_hash_mismatch_are_blocked_before_persistence(self) -> None:
        persistence = FakeDerivativePersistence()
        store = LocalEncryptedArtifactStore(
            self.root / "managed",
            key_id="synthetic-key-v1",
            encryption_key=b"q" * 32,
        )
        lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        with self.assertRaisesRegex(EvidenceDerivativeCoordinationBlocked, "SYSTEM_WORKER"):
            coordinate_evidence_derivatives(
                snapshot=self.snapshot,
                source_bindings=(),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                staging_root=self.root / "staging",
                artifact_store=store,
                persistence=persistence,
                system_actor=lead,
                idempotency_prefix="synthetic-derivative-run-002",
            )
        wrong_binding = SourcePdfBinding(
            evidence_file_id=self.file_id,
            relative_path="synthetic.pdf",
            expected_sha256="0" * 64,
            expected_page_count=2,
        )
        with self.assertRaisesRegex(EvidenceDerivativeCoordinationBlocked, "not the original hash"):
            coordinate_evidence_derivatives(
                snapshot=self.snapshot,
                source_bindings=(wrong_binding,),
                case_root=self.case_root,
                confirmed_case_root_fingerprint=root_fingerprint(self.case_root),
                staging_root=self.root / "staging",
                artifact_store=store,
                persistence=persistence,
                system_actor=self.actor,
                idempotency_prefix="synthetic-derivative-run-003",
            )
        self.assertEqual(persistence.calls, [])


if __name__ == "__main__":
    unittest.main()
