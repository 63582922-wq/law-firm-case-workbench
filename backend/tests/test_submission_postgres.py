from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from unittest.mock import patch
from uuid import uuid4
import unittest
from zipfile import ZIP_STORED, ZipFile

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked, _payload_hash
from case_kernel.models import Actor, Role
from case_kernel.submission_postgres import (
    PostgresSubmissionStore,
    SubmissionComponentSelection,
    _compilation_input_payload,
)


def synthetic_pdf(label: str) -> bytes:
    return b"%PDF-1.4\n% " + label.encode() + b"\ntrailer <<>>\n%%EOF\n"


def synthetic_submission_export(bundle_id: str, input_hash: str) -> tuple[bytes, bytes]:
    files = {
        f"0{index}_{label}.pdf": synthetic_pdf(label)
        for index, label in enumerate(("答辩状", "证据目录", "证据材料", "利息测算表"), start=1)
    }
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    manifest = {
        "schema_version": "court-submission-internal-manifest-v1",
        "bundle": {"bundle_id": bundle_id, "input_hash": input_hash},
        "court_files": [
            {
                "court_filename": name,
                "byte_size": len(content),
                "artifact_sha256": sha256(content).hexdigest(),
            }
            for name, content in files.items()
        ],
        "court_zip_contains_internal_metadata": False,
    }
    return stream.getvalue(), json.dumps(manifest, ensure_ascii=False).encode("utf-8")


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeSubmissionConnection:
    def __init__(self, *, stage: str = "FINAL_QA") -> None:
        self.stage = stage
        self.current_bundle_id: str | None = None
        self.bundle_id = str(uuid4())
        self.manifest_id = str(uuid4())
        self.legal_bundle_id = str(uuid4())
        self.calculation_run_id = str(uuid4())
        self.final_approval_id = str(uuid4())
        self.consistency_review_id = str(uuid4())
        self.consistency_input_hash = "8" * 64
        self.consistency_output_hash = "9" * 64
        self.consistency_status = "PASS"
        self.consistency_blocking_count = 0
        self.consistency_reviewed_version = 1
        self.final_text_hash = "d" * 64
        self.work_product_ids = [str(uuid4()) for _ in range(4)]
        self.product_rows = [
            {
                "work_product_id": self.work_product_ids[0],
                "document_kind": "DEFENCE_STATEMENT",
                "audience": "COURT_SUBMISSION",
                "media_type": "application/pdf",
                "storage_object_key": f"aa/aa/{'a' * 64}.lca",
                "artifact_sha256": "a" * 64,
                "byte_size": 101,
                "semantic_text_sha256": self.final_text_hash,
                "review_input_hash": "f" * 64,
                "status": "APPROVED",
                "approval_hash": "1" * 64,
            },
            {
                "work_product_id": self.work_product_ids[1],
                "document_kind": "EVIDENCE_INDEX",
                "audience": "COURT_SUBMISSION",
                "media_type": "application/pdf",
                "storage_object_key": f"bb/bb/{'b' * 64}.lca",
                "artifact_sha256": "b" * 64,
                "byte_size": 102,
                "semantic_text_sha256": None,
                "review_input_hash": "e" * 64,
                "status": "APPROVED",
                "approval_hash": "2" * 64,
            },
            {
                "work_product_id": self.work_product_ids[2],
                "document_kind": "EVIDENCE_MATERIAL",
                "audience": "COURT_SUBMISSION",
                "media_type": "application/pdf",
                "storage_object_key": f"cc/cc/{'c' * 64}.lca",
                "artifact_sha256": "c" * 64,
                "byte_size": 103,
                "semantic_text_sha256": None,
                "review_input_hash": "c" * 64,
                "status": "APPROVED",
                "approval_hash": "3" * 64,
            },
            {
                "work_product_id": self.work_product_ids[3],
                "document_kind": "INTEREST_CALCULATION",
                "audience": "COURT_SUBMISSION",
                "media_type": "application/pdf",
                "storage_object_key": f"ee/ee/{'e' * 64}.lca",
                "artifact_sha256": "e" * 64,
                "byte_size": 104,
                "semantic_text_sha256": None,
                "review_input_hash": "b" * 64,
                "status": "APPROVED",
                "approval_hash": "4" * 64,
            },
        ]
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SET TRANSACTION") or "SELECT set_config" in normalized:
            return FakeResult()
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"authorized": 1})
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": True})
        if normalized.startswith("SELECT stage, current_submission_bundle_id FROM matters"):
            return FakeResult(
                row={
                    "stage": self.stage,
                    "current_submission_bundle_id": self.current_bundle_id,
                }
            )
        if "FROM submission_work_products" in normalized and "work_product_id = ANY" in normalized:
            requested = [str(value) for value in params[0]]
            by_id = {row["work_product_id"]: row for row in self.product_rows}
            return FakeResult(rows=[by_id[value] for value in reversed(requested) if value in by_id])
        if "FROM submission_work_products" in normalized and "FOR UPDATE" in normalized:
            return FakeResult(
                row={
                    "document_kind": "DEFENCE_STATEMENT",
                    "audience": "COURT_SUBMISSION",
                    "artifact_sha256": "a" * 64,
                    "review_input_hash": "f" * 64,
                    "status": "CANDIDATE",
                }
            )
        if "FROM evidence_manifests" in normalized:
            return FakeResult(
                row={
                    "manifest_id": self.manifest_id,
                    "content_hash": "5" * 64,
                    "status": "LOCKED",
                }
            )
        if "FROM case_legal_bundles" in normalized:
            return FakeResult(
                row={
                    "bundle_id": self.legal_bundle_id,
                    "bundle_hash": "6" * 64,
                    "status": "APPROVED",
                }
            )
        if "FROM calculation_runs" in normalized:
            return FakeResult(
                row={
                    "run_id": self.calculation_run_id,
                    "output_hash": "7" * 64,
                    "status": "VERIFIED",
                    "legal_bundle_id": self.legal_bundle_id,
                    "legal_bundle_hash": "6" * 64,
                }
            )
        if "FROM approvals" in normalized:
            return FakeResult(
                row={
                    "approval_id": self.final_approval_id,
                    "object_hash": self.final_text_hash,
                    "approval_type": "FINAL_TEXT",
                    "approved_matter_version": 1,
                    "revoked_at": None,
                }
            )
        if "FROM document_consistency_reviews" in normalized:
            return FakeResult(
                row={
                    "review_id": self.consistency_review_id,
                    "input_hash": self.consistency_input_hash,
                    "output_hash": self.consistency_output_hash,
                    "blocking_count": self.consistency_blocking_count,
                    "reviewed_matter_version": self.consistency_reviewed_version,
                    "status": self.consistency_status,
                }
            )
        if "FROM document_consistency_review_documents" in normalized:
            return FakeResult(rows=[
                {
                    "work_product_id": row["work_product_id"],
                    "review_input_hash": row["review_input_hash"],
                }
                for row in self.product_rows
            ])
        if "SELECT bundle.lifecycle" in normalized and "manifest.status" in normalized:
            return FakeResult(
                row={
                    "lifecycle": "QA_READY",
                    "validity": "VALID",
                    "input_hash": "8" * 64,
                    "manifest_status": "LOCKED",
                    "legal_status": "APPROVED",
                    "calculation_status": "VERIFIED",
                    "revoked_at": None,
                    "stage": "READY_TO_EXPORT",
                    "current_submission_bundle_id": None,
                }
            )
        if "SELECT bundle.lifecycle" in normalized and "component_count" in normalized:
            return FakeResult(
                row={
                    "lifecycle": "LOCKED",
                    "validity": "VALID",
                    "input_hash": "8" * 64,
                    "current_submission_bundle_id": self.bundle_id,
                    "component_count": 4,
                }
            )
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        if normalized.startswith("SELECT export.export_id, export.bundle_id"):
            return FakeResult(
                row={
                    "export_id": params[0],
                    "bundle_id": self.bundle_id,
                    "court_zip_object_key": f"aa/aa/{'a' * 64}.lca",
                    "court_zip_sha256": "a" * 64,
                    "court_zip_bytes": 1024,
                    "lifecycle": "EXPORTED",
                    "validity": "VALID",
                }
            )
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeSubmissionConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class SubmissionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.system = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.pdf = synthetic_pdf("work-product")
        self.pdf_hash = sha256(self.pdf).hexdigest()
        self.store = PostgresSubmissionStore(
            "postgresql://not-used.invalid/lawcase_test",
            artifact_reader=lambda _key, _expected: self.pdf,
        )

    def run_with(self, connection: FakeSubmissionConnection, callback):
        with patch(
            "case_kernel.submission_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            return callback()

    def selections(self, connection: FakeSubmissionConnection):
        return tuple(
            SubmissionComponentSelection(product_id, index, filename)
            for index, (product_id, filename) in enumerate(
                zip(
                    connection.work_product_ids,
                    (
                        "01_民事答辩状.pdf",
                        "02_证据目录.pdf",
                        "03_相关证据材料.pdf",
                        "04_利息测算表.pdf",
                    ),
                    strict=True,
                ),
                start=1,
            )
        )

    def qa_hash(self, connection: FakeSubmissionConnection) -> str:
        selections = self.selections(connection)
        products_by_id = {row["work_product_id"]: row for row in connection.product_rows}
        products = tuple(products_by_id[item.work_product_id] for item in selections)
        return _payload_hash(
            _compilation_input_payload(
                matter_id=self.matter_id,
                matter_version=1,
                required_document_kinds=(
                    "DEFENCE_STATEMENT",
                    "EVIDENCE_INDEX",
                    "EVIDENCE_MATERIAL",
                    "INTEREST_CALCULATION",
                ),
                selections=selections,
                products=products,
                evidence_manifest_id=connection.manifest_id,
                evidence_manifest_hash="5" * 64,
                legal_bundle_id=connection.legal_bundle_id,
                legal_bundle_hash="6" * 64,
                calculation_run_id=connection.calculation_run_id,
                calculation_output_hash="7" * 64,
                final_text_approval_id=connection.final_approval_id,
                final_text_hash=connection.final_text_hash,
                consistency_review_id=connection.consistency_review_id,
                consistency_input_hash=connection.consistency_input_hash,
                consistency_output_hash=connection.consistency_output_hash,
                approved_by=self.lead.actor_id,
            )
        )

    def test_candidate_registration_authenticates_encrypted_pdf_before_database(self) -> None:
        connection = FakeSubmissionConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.register_work_product_candidate(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="work-product-register-001",
                document_kind="DEFENCE_STATEMENT",
                audience="COURT_SUBMISSION",
                media_type="application/pdf",
                storage_object_key=f"{self.pdf_hash[:2]}/{self.pdf_hash[2:4]}/{self.pdf_hash}.lca",
                artifact_sha256=self.pdf_hash,
                byte_size=len(self.pdf),
                page_count=1,
                semantic_text_sha256="d" * 64,
                review_input_hash="f" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO submission_work_products", sql)
        self.assertNotIn("UPDATE submission_bundles SET validity = 'STALE'", sql)

    def test_candidate_registration_fails_closed_without_artifact_reader(self) -> None:
        store = PostgresSubmissionStore("postgresql://not-used.invalid/lawcase_test")
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "encrypted-object verifier"):
            store.register_work_product_candidate(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="work-product-no-reader-001",
                document_kind="DEFENCE_STATEMENT",
                audience="COURT_SUBMISSION",
                media_type="application/pdf",
                storage_object_key=f"{self.pdf_hash[:2]}/{self.pdf_hash[2:4]}/{self.pdf_hash}.lca",
                artifact_sha256=self.pdf_hash,
                byte_size=len(self.pdf),
                page_count=1,
                semantic_text_sha256="d" * 64,
                review_input_hash="f" * 64,
            )

    def test_approval_stales_old_submission_but_not_verified_calculation(self) -> None:
        connection = FakeSubmissionConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.approve_work_product(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="work-product-approve-001",
                work_product_id=connection.work_product_ids[0],
                approval_hash="f" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("UPDATE submission_work_products", sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", sql)
        self.assertNotIn("UPDATE calculation_runs", sql)

    def test_approval_rejects_a_hash_not_bound_to_the_candidate_output(self) -> None:
        connection = FakeSubmissionConnection()
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "exact candidate review hash"):
            self.run_with(
                connection,
                lambda: self.store.approve_work_product(
                    matter_id=self.matter_id,
                    actor=self.lead,
                    expected_version=1,
                    idempotency_key="work-product-approve-mismatch-001",
                    work_product_id=connection.work_product_ids[0],
                    approval_hash="e" * 64,
                ),
            )

    def test_qa_bundle_binds_exact_filenames_and_all_current_dependencies(self) -> None:
        connection = FakeSubmissionConnection()
        selections = self.selections(connection)
        receipt = self.run_with(
            connection,
            lambda: self.store.create_qa_ready_bundle(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="submission-qa-001",
                selections=selections,
                required_document_kinds=(
                    "DEFENCE_STATEMENT",
                    "EVIDENCE_INDEX",
                    "EVIDENCE_MATERIAL",
                    "INTEREST_CALCULATION",
                ),
                evidence_manifest_id=connection.manifest_id,
                legal_bundle_id=connection.legal_bundle_id,
                calculation_run_id=connection.calculation_run_id,
                final_text_approval_id=connection.final_approval_id,
                consistency_review_id=connection.consistency_review_id,
                consistency_output_hash=connection.consistency_output_hash,
                expected_qa_hash=self.qa_hash(connection),
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO submission_compilation_specs", sql)
        self.assertEqual(sql.count("INSERT INTO submission_bundle_components"), 4)
        self.assertIn("stage = 'READY_TO_EXPORT'", sql)
        component_params = [
            params
            for statement, params in connection.executed
            if "INSERT INTO submission_bundle_components" in statement
        ]
        self.assertEqual([params[6] for params in component_params], [item.court_filename for item in selections])

    def test_qa_bundle_rejects_hash_when_filename_or_dependency_changed(self) -> None:
        connection = FakeSubmissionConnection()
        selections = list(self.selections(connection))
        selections[0] = SubmissionComponentSelection(
            selections[0].work_product_id, 1, "01_答辩状.pdf"
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "QA hash differs"):
            self.run_with(
                connection,
                lambda: self.store.create_qa_ready_bundle(
                    matter_id=self.matter_id,
                    actor=self.lead,
                    expected_version=1,
                    idempotency_key="submission-qa-name-changed-001",
                    selections=tuple(selections),
                    required_document_kinds=(
                        "DEFENCE_STATEMENT",
                        "EVIDENCE_INDEX",
                        "EVIDENCE_MATERIAL",
                        "INTEREST_CALCULATION",
                    ),
                    evidence_manifest_id=connection.manifest_id,
                    legal_bundle_id=connection.legal_bundle_id,
                    calculation_run_id=connection.calculation_run_id,
                    final_text_approval_id=connection.final_approval_id,
                    consistency_review_id=connection.consistency_review_id,
                    consistency_output_hash=connection.consistency_output_hash,
                    expected_qa_hash=self.qa_hash(connection),
                ),
            )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO submission_bundles", sql)

    def test_qa_bundle_rejects_blocked_or_stale_document_consistency_review(self) -> None:
        connection = FakeSubmissionConnection()
        connection.consistency_status = "BLOCKED"
        connection.consistency_blocking_count = 1
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "current passing document consistency"):
            self.run_with(
                connection,
                lambda: self.store.create_qa_ready_bundle(
                    matter_id=self.matter_id, actor=self.lead, expected_version=1,
                    idempotency_key="submission-qa-blocked-consistency-001",
                    selections=self.selections(connection),
                    required_document_kinds=(
                        "DEFENCE_STATEMENT", "EVIDENCE_INDEX", "EVIDENCE_MATERIAL", "INTEREST_CALCULATION",
                    ),
                    evidence_manifest_id=connection.manifest_id, legal_bundle_id=connection.legal_bundle_id,
                    calculation_run_id=connection.calculation_run_id,
                    final_text_approval_id=connection.final_approval_id,
                    consistency_review_id=connection.consistency_review_id,
                    consistency_output_hash=connection.consistency_output_hash,
                    expected_qa_hash=self.qa_hash(connection),
                ),
            )
        self.assertNotIn(
            "INSERT INTO submission_bundles",
            "\n".join(statement for statement, _ in connection.executed),
        )

    def test_lock_sets_unique_current_pointer_without_staling_dependencies(self) -> None:
        connection = FakeSubmissionConnection(stage="READY_TO_EXPORT")
        receipt = self.run_with(
            connection,
            lambda: self.store.lock_submission_bundle(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="submission-lock-001",
                bundle_id=connection.bundle_id,
                expected_input_hash="8" * 64,
                lock_approval_hash="9" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("SET lifecycle = 'LOCKED'", sql)
        self.assertIn("SET current_submission_bundle_id", sql)
        self.assertNotIn("UPDATE calculation_runs", sql)

    def test_system_registers_only_authenticated_current_locked_exact_export(self) -> None:
        connection = FakeSubmissionConnection(stage="READY_TO_EXPORT")
        zip_bytes, manifest_bytes = synthetic_submission_export(
            connection.bundle_id, "8" * 64
        )
        zip_hash = sha256(zip_bytes).hexdigest()
        manifest_hash = sha256(manifest_bytes).hexdigest()
        store = PostgresSubmissionStore(
            "postgresql://not-used.invalid/lawcase_test",
            artifact_reader=lambda _key, expected: (
                zip_bytes if expected == zip_hash else manifest_bytes
            ),
        )
        receipt = self.run_with(
            connection,
            lambda: store.register_verified_export(
                matter_id=self.matter_id,
                actor=self.system,
                expected_version=1,
                idempotency_key="submission-export-001",
                bundle_id=connection.bundle_id,
                input_hash="8" * 64,
                court_zip_object_key=f"{zip_hash[:2]}/{zip_hash[2:4]}/{zip_hash}.lca",
                court_zip_sha256=zip_hash,
                court_zip_bytes=len(zip_bytes),
                internal_manifest_object_key=f"{manifest_hash[:2]}/{manifest_hash[2:4]}/{manifest_hash}.lca",
                internal_manifest_sha256=manifest_hash,
                component_count=4,
                verification_hash="f" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO submission_compilation_exports", sql)
        self.assertIn("SET lifecycle = 'EXPORTED'", sql)
        self.assertIn("SET stage = 'EXPORTED'", sql)

    def test_system_cannot_register_hash_valid_but_structurally_invalid_export(self) -> None:
        connection = FakeSubmissionConnection(stage="READY_TO_EXPORT")
        zip_bytes = b"not-a-zip"
        manifest_bytes = b"{}"
        zip_hash = sha256(zip_bytes).hexdigest()
        manifest_hash = sha256(manifest_bytes).hexdigest()
        store = PostgresSubmissionStore(
            "postgresql://not-used.invalid/lawcase_test",
            artifact_reader=lambda _key, expected: (
                zip_bytes if expected == zip_hash else manifest_bytes
            ),
        )
        with self.assertRaisesRegex(
            CaseLedgerPersistenceBlocked, "independent ZIP and manifest verification"
        ):
            store.register_verified_export(
                matter_id=self.matter_id,
                actor=self.system,
                expected_version=1,
                idempotency_key="submission-export-invalid-001",
                bundle_id=connection.bundle_id,
                input_hash="8" * 64,
                court_zip_object_key=f"{zip_hash[:2]}/{zip_hash[2:4]}/{zip_hash}.lca",
                court_zip_sha256=zip_hash,
                court_zip_bytes=len(zip_bytes),
                internal_manifest_object_key=f"{manifest_hash[:2]}/{manifest_hash[2:4]}/{manifest_hash}.lca",
                internal_manifest_sha256=manifest_hash,
                component_count=4,
                verification_hash="f" * 64,
            )

    def test_lawyer_locator_returns_only_current_verified_export_without_leaking_in_repr(self) -> None:
        connection = FakeSubmissionConnection(stage="EXPORTED")
        export_id = str(uuid4())
        locator = self.run_with(
            connection,
            lambda: self.store.get_verified_export_locator(
                matter_id=self.matter_id,
                export_id=export_id,
                actor=self.lead,
            ),
        )
        self.assertEqual(locator.export_id, export_id)
        self.assertEqual(locator.lifecycle, "EXPORTED")
        self.assertNotIn(locator.object_key, repr(locator))


if __name__ == "__main__":
    unittest.main()
