from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest
import zipfile

from reportlab.pdfgen import canvas
from PIL import Image

from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_intake_coordinator import coordinate_claimed_evidence_intake_item
from case_kernel.evidence_intake_postgres import EvidenceIntakeItemLease
from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.local_access_grants import LocalFolderGrantRegistry, LocalSessionProof
from case_kernel.local_case_folder import root_fingerprint
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role


class CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        return FileSafetyScanReceipt("Synthetic local scanner", "test-definitions-1", expected_sha256, "CLEAN")


class FakePersistence:
    def __init__(self, matter_id: str) -> None:
        self.matter_id = matter_id
        self.calls: list[tuple[str, dict]] = []

    def receipt(self, name: str, version: int, object_type: str) -> CaseLedgerCommandReceipt:
        return CaseLedgerCommandReceipt(name, f"key-{name}", self.matter_id, version, str(uuid4()), object_type, str(uuid4()))

    def register_original_file(self, **kwargs):
        self.calls.append(("register", kwargs))
        return self.receipt("REGISTER", kwargs["expected_version"] + 1, "EVIDENCE_ORIGINAL")

    def register_normalized_original_file(self, **kwargs):
        self.calls.append(("register_normalized", kwargs))
        return self.receipt("REGISTER_NORMALIZED", kwargs["expected_version"] + 1, "EVIDENCE_ORIGINAL")

    def complete_evidence_intake_item(self, **kwargs):
        self.calls.append(("complete", kwargs))
        return self.receipt("COMPLETE", kwargs["expected_version"] + 1, "EVIDENCE_INTAKE_ITEM")

    def finalize_evidence_intake_item(self, **kwargs):
        self.calls.append(("finalize", kwargs))
        return self.receipt("FINALIZE", kwargs["expected_version"] + 1, "EVIDENCE_INTAKE_ITEM")


class EvidenceIntakeCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.session = LocalSessionProof(
            str(uuid4()), "OS_BOUND_LOCAL_SESSION", self.now - timedelta(minutes=1), self.now + timedelta(minutes=15)
        )

    def coordinate(self, root: Path, source: Path, detected_kind: str, *, artifact_store=None):
        registry = LocalFolderGrantRegistry()
        grant = registry.issue_read_grant(
            selected_root=root,
            confirmed_root_fingerprint=root_fingerprint(root),
            actor=self.lead,
            matter_id=self.matter_id,
            session=self.session,
        )
        relative = source.relative_to(root).as_posix()
        lease = EvidenceIntakeItemLease(
            run_id=str(uuid4()),
            item_id=str(uuid4()),
            lease_id=str(uuid4()),
            matter_id=self.matter_id,
            scan_id=str(uuid4()),
            scan_manifest_hash="a" * 64,
            relative_path=relative,
            expected_byte_size=source.stat().st_size,
            expected_sha256=sha256(source.read_bytes()).hexdigest(),
            detected_kind=detected_kind,
            attempt_count=1,
            lease_expires_at=self.now + timedelta(minutes=2),
            matter_version=5,
        )
        persistence = FakePersistence(self.matter_id)
        result = coordinate_claimed_evidence_intake_item(
            lease=lease,
            folder_grants=registry,
            folder_grant_id=grant.grant_id,
            grant_actor=self.lead,
            grant_session=self.session,
            scanner=CleanScanner(),
            persistence=persistence,
            system_actor=self.worker,
            artifact_store=artifact_store,
        )
        return result, persistence

    def test_clean_pdf_registers_real_page_count_then_completes_item(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"
            root.mkdir()
            source = root / "法院送达资料" / "起诉状.pdf"
            source.parent.mkdir()
            document = canvas.Canvas(str(source))
            document.drawString(30, 700, "synthetic")
            document.save()
            result, persistence = self.coordinate(root, source, "PDF")
        self.assertEqual(result.outcome, "REGISTERABLE")
        self.assertEqual([name for name, _ in persistence.calls], ["register", "complete"])
        self.assertEqual(persistence.calls[0][1]["page_count"], 1)
        self.assertEqual(persistence.calls[0][1]["original_label"], "法院送达资料/起诉状.pdf")
        self.assertEqual(result.final_matter_version, 7)

    def test_spreadsheet_is_review_required_without_original_registration(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"
            root.mkdir()
            source = root / "微信转账记录" / "2022.xlsx"
            source.parent.mkdir()
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("xl/workbook.xml", "<workbook/>")
            result, persistence = self.coordinate(root, source, "SPREADSHEET")
        self.assertEqual(result.outcome, "REVIEW_REQUIRED")
        self.assertEqual(result.reason_code, "SPREADSHEET_CONVERSION_REQUIRED")
        self.assertEqual([name for name, _ in persistence.calls], ["finalize"])
        self.assertEqual(persistence.calls[0][1]["outcome_code"], "SPREADSHEET_CONVERSION_REQUIRED")

    def test_clean_image_is_normalized_into_encrypted_pdf_then_registered(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "案卷"
            root.mkdir()
            source = root / "微信转账记录" / "付款截图.png"
            source.parent.mkdir()
            Image.new("RGB", (120, 80), color="white").save(source)
            store = LocalEncryptedArtifactStore(
                Path(temporary) / "managed-artifacts",
                key_id="test-key-v1",
                encryption_key=b"x" * 32,
            )
            result, persistence = self.coordinate(root, source, "IMAGE", artifact_store=store)
            artifact_key = persistence.calls[0][1]["normalized_pdf_object_key"]
            artifact_sha = persistence.calls[0][1]["normalized_pdf_sha256"]
            self.assertTrue(store.read_bytes(artifact_key, expected_sha256=artifact_sha).startswith(b"%PDF-"))
        self.assertEqual(result.outcome, "REGISTERABLE")
        self.assertIsNone(result.reason_code)
        self.assertEqual([name for name, _ in persistence.calls], ["register_normalized", "complete"])
        self.assertEqual(persistence.calls[0][1]["source_media_type"], "image/png")
        self.assertEqual(persistence.calls[0][1]["page_count"], 1)


if __name__ == "__main__":
    unittest.main()
