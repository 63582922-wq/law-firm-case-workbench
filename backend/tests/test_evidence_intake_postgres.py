from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.evidence_intake_postgres import PostgresEvidenceIntakeStore
from case_kernel.models import Actor, Role


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None
    rowcount: int = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class IntakeConnection:
    def __init__(self, *, version: int = 1) -> None:
        self.version = version
        self.executed: list[tuple[str, tuple | None]] = []
        self.scan = None
        self.files: list[dict] = []
        self.existing_run = None
        self.run = None
        self.item = None
        self.claimed = None
        self.original = None
        self.counts = {"active_count": 0, "registered_count": 1, "total_count": 1}
        self.summary = None
        self.exhausted: list[dict] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": self.version, "permitted": True})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"permitted": 1})
        if normalized.startswith("SELECT version FROM matters"):
            return FakeResult(row={"version": self.version})
        if normalized.startswith("SELECT scan_id, manifest_hash, status"):
            return FakeResult(row=self.scan)
        if normalized.startswith("SELECT status FROM evidence_intake_runs"):
            return FakeResult(row=self.existing_run)
        if normalized.startswith("SELECT relative_path, byte_size, file_sha256, detected_kind"):
            return FakeResult(rows=self.files)
        if normalized.startswith("SELECT run.status, run.scan_id"):
            return FakeResult(row=self.run)
        if normalized.startswith("SELECT item_id, scan_id, relative_path") and "FOR UPDATE SKIP LOCKED" in normalized:
            return FakeResult(row=self.item)
        if normalized.startswith("UPDATE evidence_intake_items") and "RETURNING item_id, scan_id" in normalized:
            return FakeResult(row=self.claimed)
        if normalized.startswith("SELECT item.status, item.lease_id"):
            return FakeResult(row=self.item)
        if normalized.startswith("SELECT %s > now() AS active"):
            return FakeResult(row={"active": True})
        if normalized.startswith("SELECT evidence_file_id FROM evidence_original_files"):
            return FakeResult(row=self.original)
        if normalized.startswith("SELECT COUNT(*) FILTER (WHERE status IN"):
            return FakeResult(row=self.counts)
        if normalized.startswith("SELECT run.run_id, run.scan_id"):
            return FakeResult(row=self.summary)
        if normalized.startswith("SELECT run.scan_manifest_hash, scan.status"):
            return FakeResult(row=self.run)
        if normalized.startswith("UPDATE evidence_intake_items") and "RECOVERY_ATTEMPTS_EXHAUSTED" in normalized:
            return FakeResult(rows=self.exhausted)
        if normalized.startswith("UPDATE matters SET version = version + 1"):
            return FakeResult(row={"version": self.version + 1})
        return FakeResult()


class Context:
    def __init__(self, connection: IntakeConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class EvidenceIntakePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.store = PostgresEvidenceIntakeStore("postgresql://not-used.invalid/lawcase_workbench_test")

    def test_enqueue_is_bound_to_approved_scan_and_copies_relative_inventory_only(self) -> None:
        scan_id = str(uuid4())
        connection = IntakeConnection()
        connection.scan = {"scan_id": scan_id, "manifest_hash": "a" * 64, "status": "APPROVED"}
        connection.files = [
            {"relative_path": "法院送达资料/起诉状.pdf", "byte_size": 1024, "file_sha256": "b" * 64, "detected_kind": "PDF"},
            {"relative_path": "微信转账记录/2022.xlsx", "byte_size": 2048, "file_sha256": "c" * 64, "detected_kind": "SPREADSHEET"},
        ]
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect", return_value=Context(connection)):
            receipt = self.store.enqueue_evidence_intake_run(
                matter_id=self.matter_id,
                scan_id=scan_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="intake-enqueue-001",
                scan_manifest_hash="a" * 64,
                approval_hash="d" * 64,
            )
        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertEqual(sql.count("INSERT INTO evidence_intake_items"), 2)
        self.assertNotIn("absolute_path", sql)
        self.assertNotIn("folder_grant_id", sql)

    def test_claim_uses_short_lease_and_returns_no_absolute_path(self) -> None:
        run_id = str(uuid4())
        item_id = str(uuid4())
        scan_id = str(uuid4())
        expires = datetime.now(timezone.utc) + timedelta(minutes=2)
        row = {
            "item_id": item_id,
            "scan_id": scan_id,
            "relative_path": "法院送达资料/起诉状.pdf",
            "expected_byte_size": 1024,
            "expected_sha256": "b" * 64,
            "detected_kind": "PDF",
            "attempt_count": 1,
            "lease_expires_at": expires,
        }
        connection = IntakeConnection()
        connection.run = {
            "status": "QUEUED",
            "scan_id": scan_id,
            "scan_manifest_hash": "a" * 64,
            "scan_status": "APPROVED",
            "current_manifest_hash": "a" * 64,
        }
        connection.item = {**row, "status": "QUEUED"}
        connection.claimed = row
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect", return_value=Context(connection)):
            lease = self.store.claim_evidence_intake_item(
                matter_id=self.matter_id,
                run_id=run_id,
                actor=self.worker,
                expected_version=1,
                idempotency_key="intake-claim-001",
            )
        self.assertEqual(lease.item_id, item_id)
        self.assertEqual(lease.relative_path, "法院送达资料/起诉状.pdf")
        self.assertNotIn("/Users/", repr(lease))

    def test_registered_completion_requires_matching_immutable_original(self) -> None:
        run_id = str(uuid4())
        item_id = str(uuid4())
        lease_id = str(uuid4())
        evidence_file_id = str(uuid4())
        connection = IntakeConnection(version=3)
        connection.item = {
            "status": "RUNNING",
            "lease_id": lease_id,
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
            "relative_path": "法院送达资料/起诉状.pdf",
            "expected_byte_size": 1024,
            "expected_sha256": "b" * 64,
            "scan_manifest_hash": "a" * 64,
            "scan_status": "APPROVED",
            "current_manifest_hash": "a" * 64,
        }
        connection.original = {"evidence_file_id": evidence_file_id}
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect", return_value=Context(connection)):
            receipt = self.store.complete_evidence_intake_item(
                matter_id=self.matter_id,
                run_id=run_id,
                item_id=item_id,
                lease_id=lease_id,
                evidence_file_id=evidence_file_id,
                inspection_hash="c" * 64,
                scanner_name="ClamAV",
                scanner_definitions_version="20260810",
                actor=self.worker,
                expected_version=3,
                idempotency_key="intake-complete-001",
            )
        self.assertEqual(receipt.matter_version, 4)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("status = %s, lease_id = NULL", sql)
        self.assertIn("status = %s, completed_at = now()", sql)

    def test_summary_exposes_counts_but_not_paths_or_leases(self) -> None:
        now = datetime.now(timezone.utc)
        connection = IntakeConnection(version=8)
        connection.summary = {
            "run_id": str(uuid4()),
            "scan_id": str(uuid4()),
            "scan_manifest_hash": "a" * 64,
            "status": "PARTIAL",
            "created_at": now,
            "completed_at": now,
            "total_items": 3,
            "queued_items": 0,
            "running_items": 0,
            "registered_items": 1,
            "review_required_items": 1,
            "blocked_items": 1,
            "failed_items": 0,
        }
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect", return_value=Context(connection)):
            summary = self.store.get_current_evidence_intake_summary(matter_id=self.matter_id, actor=self.lead)
        self.assertEqual(summary.run["registered_items"], 1)
        self.assertNotIn("relative_path", summary.run)
        self.assertNotIn("lease_id", summary.run)

    def test_expired_third_attempt_is_reaped_to_explicit_failure(self) -> None:
        run_id = str(uuid4())
        connection = IntakeConnection(version=5)
        connection.run = {
            "scan_manifest_hash": "a" * 64,
            "scan_status": "APPROVED",
            "current_manifest_hash": "a" * 64,
        }
        connection.exhausted = [{"item_id": str(uuid4())}]
        connection.counts = {"active_count": 0, "registered_count": 0, "total_count": 1}
        with patch("case_kernel.evidence_manifest_postgres.psycopg.connect", return_value=Context(connection)):
            receipt = self.store.reap_exhausted_evidence_intake_items(
                matter_id=self.matter_id,
                run_id=run_id,
                actor=self.worker,
                expected_version=5,
                idempotency_key="intake-reap-001",
            )
        self.assertEqual(receipt.object_id, run_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("RECOVERY_ATTEMPTS_EXHAUSTED", sql)
        self.assertIn("status = %s, completed_at = now()", sql)


if __name__ == "__main__":
    unittest.main()
