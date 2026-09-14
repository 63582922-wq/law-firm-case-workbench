from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import inspect
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.evidence_manifest_postgres import PostgresEvidenceManifestStore
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredWebEvidenceOriginal
from case_kernel.web_upload_staging import AdmittedWebPdfUpload


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None
    rowcount: int = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeEvidenceBindingConnection:
    def __init__(
        self,
        *,
        bound: bool = False,
        locator_row: dict | None = None,
        fail_reconciliation: bool = False,
    ) -> None:
        self.bound = bound
        self.locator_row = locator_row
        self.fail_reconciliation = fail_reconciliation
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if normalized.startswith("SELECT m.version,"):
            return FakeResult(row={"version": 1, "permitted": True})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"permitted": 1})
        if normalized.startswith("SELECT evidence_file_id FROM web_evidence_original_source_objects"):
            return FakeResult(row={"evidence_file_id": str(uuid4())} if self.bound else None)
        if normalized.startswith("SELECT 1 FROM evidence_original_files"):
            return FakeResult(row=None)
        if normalized.startswith("SELECT source.evidence_file_id"):
            return FakeResult(row=self.locator_row)
        if normalized.startswith("SELECT 1 FROM web_evidence_original_source_objects"):
            if self.fail_reconciliation:
                raise RuntimeError("database connection disappeared")
            return FakeResult(row={"bound": 1} if self.bound else None)
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeEvidenceBindingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> FakeEvidenceBindingConnection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class WebEvidenceSourceObjectBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.human = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.store = PostgresEvidenceManifestStore("postgresql://not-used.invalid/lawcase_workbench_test")
        self.upload = AdmittedWebPdfUpload(
            upload_id=str(uuid4()),
            display_name="微信转账记录.pdf",
            byte_size=4096,
            content_sha256="a" * 64,
            media_type="application/pdf",
            page_count=2,
            inspection_hash="b" * 64,
            scanner_name="test-scanner",
            scanner_definitions_version="definitions-1",
            path=Path("/private/server-staging/upload.part"),
        )
        self.stored = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{self.firm_id}/{self.matter_id}/"
                f"{self.upload.content_sha256[:2]}/{self.upload.content_sha256}/{uuid4()}.pdf"
            ),
            content_sha256=self.upload.content_sha256,
            byte_size=self.upload.byte_size,
            object_version_id="version-1",
        )

    def test_registers_original_pages_and_private_binding_in_one_ledger_transaction(self) -> None:
        connection = FakeEvidenceBindingConnection()
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            result = self.store.register_web_uploaded_pdf_original(
                matter_id=self.matter_id,
                actor=self.human,
                expected_version=1,
                idempotency_key="web-evidence-original-001",
                upload=self.upload,
                stored_object=self.stored,
            )

        UUID(result.receipt.object_id)
        self.assertEqual(result.source_reference_hash, sha256(self.stored.object_key.encode("ascii")).hexdigest())
        self.assertNotIn(self.stored.object_key, repr(result))
        sql = [statement for statement, _ in connection.executed]
        original_index = next(index for index, statement in enumerate(sql) if "INSERT INTO evidence_original_files" in statement)
        page_indexes = [index for index, statement in enumerate(sql) if "INSERT INTO evidence_pages" in statement]
        binding_index = next(
            index for index, statement in enumerate(sql) if "INSERT INTO web_evidence_original_source_objects" in statement
        )
        self.assertEqual(len(page_indexes), 2)
        self.assertLess(original_index, min(page_indexes))
        self.assertLess(max(page_indexes), binding_index)
        self.assertTrue(any("SELECT pg_advisory_xact_lock" in statement for statement in sql))
        self.assertTrue(any("INSERT INTO audit_events" in statement for statement in sql))
        self.assertTrue(any("INSERT INTO outbox_events" in statement for statement in sql))
        self.assertTrue(any("INSERT INTO command_idempotency" in statement for statement in sql))
        for statement, params in connection.executed:
            if params is not None and self.stored.object_key in params:
                self.assertIn("INSERT INTO web_evidence_original_source_objects", statement)
        self.assertFalse(
            any(
                self.stored.object_key in str(params)
                for statement, params in connection.executed
                if "INSERT INTO web_evidence_original_source_objects" not in statement and params is not None
            )
        )

    def test_mismatched_or_cross_matter_object_is_rejected_before_database_access(self) -> None:
        wrong_hash = "c" * 64
        mismatched = StoredWebEvidenceOriginal(
            object_key=(
                f"originals/v1/{self.firm_id}/{self.matter_id}/"
                f"{wrong_hash[:2]}/{wrong_hash}/{uuid4()}.pdf"
            ),
            content_sha256=wrong_hash,
            byte_size=self.upload.byte_size,
            object_version_id="version-1",
        )
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "differs from the admitted upload"):
                self.store.register_web_uploaded_pdf_original(
                    matter_id=self.matter_id,
                    actor=self.human,
                    expected_version=1,
                    idempotency_key="web-evidence-original-002",
                    upload=self.upload,
                    stored_object=mismatched,
                )
        connect.assert_not_called()

    def test_only_a_system_worker_can_read_the_private_locator(self) -> None:
        evidence_file_id = str(uuid4())
        locator_row = {
            "evidence_file_id": evidence_file_id,
            "original_file_sha256": self.upload.content_sha256,
            "byte_size": self.upload.byte_size,
            "page_count": self.upload.page_count,
            "source_reference_hash": sha256(self.stored.object_key.encode("ascii")).hexdigest(),
            "source_object_key": self.stored.object_key,
            "source_object_version_id": self.stored.object_version_id,
        }
        connection = FakeEvidenceBindingConnection(locator_row=locator_row)
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            locator = self.store.get_web_uploaded_original_source_locator(
                matter_id=self.matter_id,
                evidence_file_id=evidence_file_id,
                actor=self.worker,
            )
        self.assertNotIn(self.stored.object_key, repr(locator))
        self.assertNotIn(self.stored.object_version_id or "", repr(locator))
        self.assertEqual(locator.stored_object(), self.stored)

        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect") as connect:
            with self.assertRaises(PermissionError):
                self.store.get_web_uploaded_original_source_locator(
                    matter_id=self.matter_id,
                    evidence_file_id=evidence_file_id,
                    actor=self.human,
                )
        connect.assert_not_called()

    def test_failure_reconciliation_is_worker_only_and_fails_closed(self) -> None:
        unbound = FakeEvidenceBindingConnection(bound=False)
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(unbound),
        ):
            self.assertFalse(
                self.store.is_web_uploaded_object_bound(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    stored_object=self.stored,
                )
            )
        self.assertTrue(
            any("SELECT pg_advisory_xact_lock" in statement for statement, _ in unbound.executed)
        )

        bound = FakeEvidenceBindingConnection(bound=True)
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(bound),
        ):
            self.assertTrue(
                self.store.is_web_uploaded_object_bound(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    stored_object=self.stored,
                )
            )

        unavailable = FakeEvidenceBindingConnection(fail_reconciliation=True)
        with patch(
            "case_kernel.evidence_manifest_postgres.psycopg.connect",
            return_value=FakeConnectionContext(unavailable),
        ):
            with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "binding state is unavailable"):
                self.store.is_web_uploaded_object_bound(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    stored_object=self.stored,
                )

    def test_snapshot_and_summary_remain_object_key_free(self) -> None:
        snapshot_source = inspect.getsource(PostgresEvidenceManifestStore.get_evidence_snapshot)
        summary_source = inspect.getsource(PostgresEvidenceManifestStore.get_evidence_review_summary)
        self.assertNotIn("web_evidence_original_source_objects", snapshot_source)
        self.assertNotIn("source_object_key", snapshot_source)
        self.assertNotIn("web_evidence_original_source_objects", summary_source)
        self.assertNotIn("source_object_key", summary_source)


if __name__ == "__main__":
    unittest.main()
