from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4
import unittest

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.models import Actor, Role
from case_kernel.official_source_capture_postgres import PostgresOfficialSourceCaptureStore


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeCaptureConnection:
    def __init__(self, *, status: str = "QUEUED") -> None:
        self.run_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.source_id = "CFETS-LPR-HISTORY"
        self.target_url = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN"
        self.now = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)
        self.run_row = {
            "run_id": self.run_id,
            "firm_id": str(uuid4()),
            "matter_id": self.matter_id,
            "source_id": self.source_id,
            "publisher": "全国银行间同业拆借中心",
            "source_tier": "OFFICIAL_RATE_DATA",
            "target_url": self.target_url,
            "query_sha256": "a" * 64,
            "authorization_hash": "b" * 64,
            "max_response_bytes": 32 * 1024 * 1024,
            "status": status,
            "attempt_count": 1 if status != "QUEUED" else 0,
            "lease_id": str(uuid4()) if status == "RUNNING" else None,
            "lease_expires_at": self.now + timedelta(minutes=2) if status == "RUNNING" else None,
            "authorized_by": str(uuid4()),
            "authorized_at": self.now - timedelta(minutes=1),
            "authorization_expires_at": self.now + timedelta(minutes=14),
            "content_sha256": "c" * 64 if status == "REVIEW_REQUIRED" else None,
            "parsed_output_hash": "d" * 64 if status == "REVIEW_REQUIRED" else None,
        }
        self.executed: list[tuple[str, tuple | None]] = []
        self.next_claimable_row: dict | None = None

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SET TRANSACTION") or "SELECT set_config" in normalized:
            return FakeResult()
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if normalized.startswith("SELECT r.run_id, r.matter_id, m.version"):
            return FakeResult(row=self.next_claimable_row)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": True})
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"):
            return FakeResult(row={"authorized": 1})
        if normalized.startswith("SELECT version FROM matters"):
            return FakeResult(row={"version": 2})
        if normalized.startswith("SELECT * FROM official_source_capture_runs"):
            return FakeResult(row=self.run_row)
        if normalized.startswith("SELECT authorization_expires_at > now()"):
            return FakeResult(row={"valid": True})
        if normalized.startswith("SELECT %s > now() AS active"):
            return FakeResult(row={"active": True})
        if normalized.startswith("UPDATE official_source_capture_runs SET status = 'RUNNING'"):
            self.run_row.update(
                {
                    "status": "RUNNING",
                    "attempt_count": 1,
                    "lease_id": params[0],
                    "lease_expires_at": self.now + timedelta(seconds=params[1]),
                }
            )
            return FakeResult(row=self.run_row)
        if normalized.startswith("UPDATE matters SET version = version + 1"):
            return FakeResult(row={"version": 2})
        if normalized.startswith("SELECT run_id, source_id, publisher"):
            public = {
                key: value
                for key, value in self.run_row.items()
                if key not in {"firm_id", "storage_object_key", "authorization_hash", "lease_id", "lease_expires_at"}
            }
            return FakeResult(rows=[public])
        if normalized.startswith("SELECT review_id, run_id, decision"):
            return FakeResult(rows=[])
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeCaptureConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class OfficialSourceCaptureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.lead = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.reviewer = Actor(str(uuid4()), self.firm_id, frozenset({Role.REVIEWER}))
        self.system = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.body = b'{"head":{"rep_code":"200"},"records":[{"showDateCN":"2026-07-20","1Y":"3.00","5Y":"3.50"}]}'
        self.body_hash = sha256(self.body).hexdigest()
        self.store = PostgresOfficialSourceCaptureStore(
            "postgresql://not-used.invalid/lawcase_test",
            artifact_reader=lambda _key, _expected: self.body,
        )

    def run_with(self, connection: FakeCaptureConnection, callback):
        with patch(
            "case_kernel.official_source_capture_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            return callback()

    def test_queue_accepts_only_registered_allowlisted_source_and_records_lawyer_authorization(self) -> None:
        connection = FakeCaptureConnection()
        receipt = self.run_with(
            connection,
            lambda: self.store.queue_capture(
                matter_id=connection.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="source-queue-001",
                source_id=connection.source_id,
                target_url=connection.target_url,
                query_sha256="a" * 64,
                authorization_hash="b" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO official_source_capture_runs", sql)
        self.assertIn("OFFICIAL_SOURCE_CAPTURE_QUEUED", str(connection.executed))
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "allowlisted"):
            self.store.queue_capture(
                matter_id=connection.matter_id,
                actor=self.lead,
                expected_version=1,
                idempotency_key="source-queue-bad-url-001",
                source_id=connection.source_id,
                target_url="https://example.com/lpr",
                query_sha256="a" * 64,
                authorization_hash="b" * 64,
            )

    def test_system_claims_exactly_one_short_lease(self) -> None:
        connection = FakeCaptureConnection(status="QUEUED")
        lease = self.run_with(
            connection,
            lambda: self.store.claim_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                actor=self.system,
                expected_version=1,
                idempotency_key="source-claim-001",
            ),
        )
        self.assertEqual(lease.source_id, connection.source_id)
        self.assertEqual(lease.matter_version, 2)
        self.assertEqual(connection.run_row["attempt_count"], 1)

    def test_worker_discovers_only_one_same_firm_authorized_queued_run(self) -> None:
        connection = FakeCaptureConnection(status="QUEUED")
        connection.next_claimable_row = {
            "run_id": connection.run_id,
            "matter_id": connection.matter_id,
            "version": 7,
        }
        candidate = self.run_with(
            connection,
            lambda: self.store.find_next_claimable_capture(actor=self.system),
        )
        self.assertEqual(candidate, (connection.matter_id, connection.run_id, 7))
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("r.firm_id = %s", sql)
        self.assertIn("u.status = 'ACTIVE'", sql)
        self.assertIn("mar.role = 'SYSTEM_WORKER'", sql)
        self.assertIn("mar.revoked_at IS NULL", sql)
        self.assertIn("r.status = 'QUEUED'", sql)
        self.assertIn("r.attempt_count = 0", sql)
        self.assertIn("r.authorization_expires_at > now()", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertNotIn("FOR UPDATE", sql)
        query_params = next(
            params for statement, params in connection.executed
            if statement.startswith("SELECT r.run_id, r.matter_id, m.version")
        )
        self.assertEqual(query_params, (self.system.actor_id, self.system.actor_id, self.firm_id))

    def test_next_capture_discovery_rejects_non_worker_without_database_read(self) -> None:
        connection = FakeCaptureConnection(status="QUEUED")
        with self.assertRaisesRegex(PermissionError, "permitted role"):
            self.store.find_next_claimable_capture(actor=self.lead)
        self.assertEqual(connection.executed, [])

    def test_completion_authenticates_encrypted_object_and_enters_review_required_only(self) -> None:
        connection = FakeCaptureConnection(status="RUNNING")
        receipt = self.run_with(
            connection,
            lambda: self.store.complete_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                lease_id=connection.run_row["lease_id"],
                actor=self.system,
                expected_version=1,
                idempotency_key="source-complete-001",
                final_url=connection.target_url,
                retrieved_at=connection.now,
                peer_ip="8.8.8.8",
                content_media_type="application/json",
                content_sha256=self.body_hash,
                content_bytes=len(self.body),
                storage_object_key=f"{self.body_hash[:2]}/{self.body_hash[2:4]}/{self.body_hash}.lca",
                capture_verification_hash="e" * 64,
                parser_kind="CFETS_LPR_JSON",
                parsed_output_hash="f" * 64,
                parsed_summary={"observations": [{"date": "2026-07-20", "one_year_rate": "0.030000"}]},
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("status = 'REVIEW_REQUIRED'", sql)
        self.assertNotIn("INSERT INTO official_legal_source_snapshots", sql)

    def test_completion_blocks_raw_source_text_in_parsed_summary(self) -> None:
        connection = FakeCaptureConnection(status="RUNNING")
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "must not duplicate"):
            self.store.complete_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                lease_id=connection.run_row["lease_id"],
                actor=self.system,
                expected_version=1,
                idempotency_key="source-complete-raw-001",
                final_url=connection.target_url,
                retrieved_at=connection.now,
                peer_ip="8.8.8.8",
                content_media_type="application/json",
                content_sha256=self.body_hash,
                content_bytes=len(self.body),
                storage_object_key=f"{self.body_hash[:2]}/{self.body_hash[2:4]}/{self.body_hash}.lca",
                capture_verification_hash="e" * 64,
                parser_kind="CFETS_LPR_JSON",
                parsed_output_hash="f" * 64,
                parsed_summary={"normalized_text": "must not be copied"},
            )

    def test_worker_failure_uses_bounded_code_and_clears_the_active_lease(self) -> None:
        connection = FakeCaptureConnection(status="RUNNING")
        receipt = self.run_with(
            connection,
            lambda: self.store.fail_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                lease_id=connection.run_row["lease_id"],
                actor=self.system,
                expected_version=1,
                idempotency_key="source-fail-001",
                failure_code="OFFICIAL_FETCH_BLOCKED",
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("status = 'FAILED'", sql)
        self.assertIn("OFFICIAL_SOURCE_CAPTURE_FAILED", str(connection.executed))
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "failure code"):
            self.store.fail_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                lease_id=str(uuid4()),
                actor=self.system,
                expected_version=1,
                idempotency_key="source-fail-bad-001",
                failure_code="contains unsafe detail",
            )

    def test_lawyer_review_is_separate_append_only_decision(self) -> None:
        connection = FakeCaptureConnection(status="REVIEW_REQUIRED")
        receipt = self.run_with(
            connection,
            lambda: self.store.review_capture(
                matter_id=connection.matter_id,
                run_id=connection.run_id,
                actor=self.reviewer,
                expected_version=1,
                idempotency_key="source-review-001",
                decision="APPROVE_FOR_REGISTRATION",
                provision_locator="一年期LPR records[0]",
                review_hash="9" * 64,
            ),
        )
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO official_source_capture_reviews", sql)
        self.assertNotIn("INSERT INTO official_legal_source_snapshots", sql)

    def test_read_snapshot_does_not_expose_encrypted_object_locator(self) -> None:
        connection = FakeCaptureConnection(status="REVIEW_REQUIRED")
        connection.run_row["storage_object_key"] = "cc/cc/" + "c" * 64 + ".lca"
        snapshot = self.run_with(
            connection,
            lambda: self.store.get_snapshot(matter_id=connection.matter_id, actor=self.lead),
        )
        self.assertEqual(len(snapshot.runs), 1)
        self.assertNotIn("storage_object_key", snapshot.runs[0])
        self.assertNotIn("lease_id", snapshot.runs[0])


if __name__ == "__main__":
    unittest.main()
