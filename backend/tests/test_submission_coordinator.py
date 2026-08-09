from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zipfile import ZipFile
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.submission_coordinator import (
    SubmissionCoordinationBlocked,
    coordinate_locked_submission_export,
)
from case_kernel.submission_postgres import PersistentLockedSubmissionCompilation


def synthetic_pdf(label: str) -> bytes:
    return b"%PDF-1.4\n% " + label.encode() + b"\ntrailer <<>>\n%%EOF\n"


class FakeSubmissionPersistence:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def register_verified_export(self, **kwargs) -> CaseLedgerCommandReceipt:
        self.calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="REGISTER_VERIFIED_SUBMISSION_EXPORT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            audit_event_id=str(uuid4()),
            object_type="SUBMISSION_EXPORT",
            object_id=str(uuid4()),
        )


class SubmissionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.bundle_id = str(uuid4())
        self.system = Actor(
            str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )

    def test_locked_bundle_is_compiled_verified_encrypted_and_registered(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_root = root / "case"
            case_root.mkdir()
            source_staging = root / "source-staging"
            source_staging.mkdir()
            managed_root = root / "managed"
            export_staging = root / "export-staging"
            store = LocalEncryptedArtifactStore(
                managed_root,
                key_id="test-key-v1",
                encryption_key=b"k" * 32,
            )
            components = []
            for sequence, (kind, filename) in enumerate(
                (
                    ("DEFENCE_STATEMENT", "01_民事答辩状.pdf"),
                    ("EVIDENCE_MATERIAL", "02_证据材料.pdf"),
                ),
                start=1,
            ):
                payload = synthetic_pdf(kind)
                digest = sha256(payload).hexdigest()
                source = source_staging / f"{sequence}.pdf"
                source.write_bytes(payload)
                encrypted = store.put_file(
                    source,
                    expected_sha256=digest,
                    case_root=case_root,
                )
                components.append(
                    {
                        "work_product_id": str(uuid4()),
                        "sequence": sequence,
                        "document_kind": kind,
                        "court_filename": filename,
                        "media_type": "application/pdf",
                        "storage_object_key": encrypted.object_key,
                        "artifact_sha256": digest,
                        "byte_size": len(payload),
                        "approval_hash": str(sequence) * 64,
                        "work_product_status": "APPROVED",
                        "work_product_audience": "COURT_SUBMISSION",
                    }
                )
            snapshot = self._snapshot(tuple(components))
            persistence = FakeSubmissionPersistence()
            result = coordinate_locked_submission_export(
                snapshot=snapshot,
                artifact_store=store,
                persistence=persistence,
                system_actor=self.system,
                case_root=case_root,
                staging_root=export_staging,
                idempotency_key="submission-export:synthetic-001",
            )
            self.assertTrue(result.verification.verified)
            self.assertEqual(result.input_hash, "8" * 64)
            self.assertEqual(result.registration_receipt.matter_version, 42)
            self.assertEqual(len(persistence.calls), 1)
            call = persistence.calls[0]
            self.assertEqual(call["court_zip_sha256"], result.encrypted_court_zip.plaintext_sha256)
            self.assertEqual(
                call["internal_manifest_sha256"],
                result.encrypted_internal_manifest.plaintext_sha256,
            )
            court_zip = store.read_bytes(
                result.encrypted_court_zip.object_key,
                expected_sha256=result.encrypted_court_zip.plaintext_sha256,
            )
            zip_path = source_staging / "inspect.zip"
            zip_path.write_bytes(court_zip)
            with ZipFile(zip_path) as archive:
                self.assertEqual(
                    archive.namelist(), ["01_民事答辩状.pdf", "02_证据材料.pdf"]
                )
                self.assertNotIn("提交包内部清单.json", archive.namelist())
            self.assertEqual(list(export_staging.iterdir()), [])

    def test_non_system_or_stale_snapshot_is_blocked_before_compilation(self) -> None:
        snapshot = self._snapshot(())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_root = root / "case"
            case_root.mkdir()
            store = LocalEncryptedArtifactStore(
                root / "managed",
                key_id="test-key-v1",
                encryption_key=b"k" * 32,
            )
            lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
            with self.assertRaisesRegex(SubmissionCoordinationBlocked, "SYSTEM_WORKER"):
                coordinate_locked_submission_export(
                    snapshot=snapshot,
                    artifact_store=store,
                    persistence=FakeSubmissionPersistence(),
                    system_actor=lead,
                    case_root=case_root,
                    staging_root=root / "staging-one",
                    idempotency_key="submission-export:wrong-role",
                )
            stale = replace(snapshot, bundle={**snapshot.bundle, "validity": "STALE"})
            with self.assertRaisesRegex(SubmissionCoordinationBlocked, "current valid locked"):
                coordinate_locked_submission_export(
                    snapshot=stale,
                    artifact_store=store,
                    persistence=FakeSubmissionPersistence(),
                    system_actor=self.system,
                    case_root=case_root,
                    staging_root=root / "staging-two",
                    idempotency_key="submission-export:stale",
                )

    def _snapshot(
        self, components: tuple[dict, ...]
    ) -> PersistentLockedSubmissionCompilation:
        return PersistentLockedSubmissionCompilation(
            matter_id=self.matter_id,
            matter_version=41,
            bundle={
                "stage": "READY_TO_EXPORT",
                "current_submission_bundle_id": self.bundle_id,
                "bundle_id": self.bundle_id,
                "lifecycle": "LOCKED",
                "validity": "VALID",
                "final_text_hash": "d" * 64,
                "approved_by": str(uuid4()),
                "export_profile": "COURT_PDF_ONLY_V1",
                "currency": "CNY",
                "input_hash": "8" * 64,
                "required_document_kinds": (
                    "DEFENCE_STATEMENT",
                    "EVIDENCE_MATERIAL",
                ),
                "evidence_manifest_id": str(uuid4()),
                "evidence_manifest_hash": "a" * 64,
                "legal_bundle_id": str(uuid4()),
                "legal_bundle_hash": "b" * 64,
                "calculation_run_id": str(uuid4()),
                "calculation_output_hash": "c" * 64,
                "final_text_approval_id": str(uuid4()),
                "qa_hash": "8" * 64,
                "qa_approved_by": str(uuid4()),
                "qa_approved_at": "2026-08-10T09:00:00+00:00",
            },
            components=components,
            snapshot_hash="f" * 64,
        )


if __name__ == "__main__":
    unittest.main()
