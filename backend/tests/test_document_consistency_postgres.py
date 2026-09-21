from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch
from uuid import uuid4
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.document_consistency_postgres import (
    PersistedDocumentConsistencyFinding,
    PostgresDocumentConsistencyReviewStore,
    ReviewedWorkProduct,
    build_document_consistency_persistence_record,
    document_consistency_output_hash,
)
from case_kernel.document_consistency_reviewer import (
    ApprovedDocumentSnapshot,
    CanonicalDocumentField,
    review_document_consistency,
)
from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.models import Actor, Role


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeConnection:
    def __init__(self) -> None:
        self.work_product_id = str(uuid4())
        self.review_input_hash = "a" * 64
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SET TRANSACTION") or "SELECT set_config" in normalized:
            return FakeResult()
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": True})
        if "FROM submission_work_products" in normalized:
            return FakeResult(rows=[{
                "work_product_id": self.work_product_id,
                "review_input_hash": self.review_input_hash,
                "status": "APPROVED",
            }])
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"authorized": 1})
        if normalized.startswith("SELECT version FROM matters"):
            return FakeResult(row={"version": 2})
        if "FROM document_consistency_reviews" in normalized and normalized.startswith("SELECT review_id"):
            return FakeResult(rows=[])
        if "FROM document_consistency_review_findings" in normalized:
            return FakeResult(rows=[])
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class DocumentConsistencyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.system = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresDocumentConsistencyReviewStore(
            "postgresql://not-used.invalid/lawcase_test"
        )
        self.matter_id = str(uuid4())

    def run_with(self, connection: FakeConnection, callback):
        with patch(
            "case_kernel.document_consistency_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            return callback()

    def test_system_records_hash_bound_pass_only_for_current_approved_work_product(self) -> None:
        connection = FakeConnection()
        documents = (ReviewedWorkProduct(connection.work_product_id, connection.review_input_hash),)
        output_hash = document_consistency_output_hash(input_hash="b" * 64, findings=())
        receipt = self.run_with(
            connection,
            lambda: self.store.record_review(
                matter_id=self.matter_id, actor=self.system, expected_version=1,
                idempotency_key="document-consistency-record-001",
                canonical_fields_hash="c" * 64, input_hash="b" * 64,
                output_hash=output_hash, documents=documents, findings=(),
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO document_consistency_reviews", sql)
        self.assertIn("INSERT INTO document_consistency_review_documents", sql)
        values = next(
            params for statement, params in connection.executed
            if "INSERT INTO document_consistency_reviews" in statement
        )
        self.assertEqual(values[8], 0)
        self.assertEqual(values[9], "PASS")

    def test_record_rejects_modified_output_hash_before_database_access(self) -> None:
        connection = FakeConnection()
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "output hash"):
            self.store.record_review(
                matter_id=self.matter_id, actor=self.system, expected_version=1,
                idempotency_key="document-consistency-bad-output-001",
                canonical_fields_hash="c" * 64, input_hash="b" * 64,
                output_hash="d" * 64,
                documents=(ReviewedWorkProduct(connection.work_product_id, connection.review_input_hash),),
                findings=(),
            )
        self.assertEqual(connection.executed, [])

    def test_blocking_finding_cannot_be_forged_as_pass_and_non_worker_cannot_record(self) -> None:
        connection = FakeConnection()
        documents = (ReviewedWorkProduct(connection.work_product_id, connection.review_input_hash),)
        finding = PersistedDocumentConsistencyFinding(
            finding_id="d" * 64, work_product_id=connection.work_product_id,
            severity="BLOCKING", code="MISSING_CANONICAL_VALUE", field_id_hash="e" * 64,
            source_refs_hash="f" * 64,
        )
        output_hash = document_consistency_output_hash(input_hash="b" * 64, findings=(finding,))
        self.run_with(
            connection,
            lambda: self.store.record_review(
                matter_id=self.matter_id, actor=self.system, expected_version=1,
                idempotency_key="document-consistency-blocked-001",
                canonical_fields_hash="c" * 64, input_hash="b" * 64,
                output_hash=output_hash, documents=documents, findings=(finding,),
            ),
        )
        values = next(
            params for statement, params in connection.executed
            if "INSERT INTO document_consistency_reviews" in statement
        )
        self.assertEqual(values[7], 1)
        self.assertEqual(values[9], "BLOCKED")
        with self.assertRaisesRegex(PermissionError, "permitted role"):
            self.store.record_review(
                matter_id=self.matter_id, actor=self.lead, expected_version=1,
                idempotency_key="document-consistency-lead-001",
                canonical_fields_hash="c" * 64, input_hash="b" * 64,
                output_hash=document_consistency_output_hash(input_hash="b" * 64, findings=()),
                documents=documents, findings=(),
            )

    def test_adapter_derives_safe_hash_only_record_from_the_actual_reviewer_output(self) -> None:
        work_product_id = str(uuid4())
        draft = ApprovedDraft(
            title="民事答辩状",
            sections=(ApprovedSection("答辩意见", ("本金为 USD 10,000。",), ("fact:1",)),),
            approval_hash="a" * 64,
        )
        document = ApprovedDocumentSnapshot(work_product_id, "DEFENCE_STATEMENT", draft)
        fields = (
            CanonicalDocumentField(
                "currency", "币种", "CNY", ("DEFENCE_STATEMENT",), ("USD",)
            ),
        )
        report = review_document_consistency(documents=(document,), canonical_fields=fields)
        record = build_document_consistency_persistence_record(
            report=report, documents=(document,), canonical_fields=fields,
            work_product_by_document_id={
                work_product_id: ReviewedWorkProduct(work_product_id, "b" * 64)
            },
        )
        self.assertEqual(len(record.findings), 2)
        self.assertEqual({item.severity for item in record.findings}, {"BLOCKING"})
        self.assertEqual(len(record.output_hash), 64)
        self.assertNotIn("USD", repr(record))
        self.assertNotIn("CNY", repr(record))


if __name__ == "__main__":
    unittest.main()
