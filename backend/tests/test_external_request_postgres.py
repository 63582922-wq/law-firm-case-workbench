from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.external_request_postgres import ExternalRequestPreflight, PostgresExternalRequestStore
from case_kernel.models import Actor, Role


@dataclass
class _Result:
    row: dict | None = None
    rows: tuple[dict, ...] = ()
    def fetchone(self): return self.row
    def fetchall(self): return list(self.rows)


class _Connection:
    def __init__(self, *, attempts: tuple[dict, ...] = ()) -> None:
        self.request_id = str(uuid4())
        self.attempts = attempts
        self.executed: list[tuple[str, tuple | None]] = []
    def execute(self, sql: str, params: tuple | None = None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT 1 FROM matters m JOIN matter_actor_roles"): return _Result({"authorized": 1})
        if "SELECT request_hash, response_json" in normalized: return _Result()
        if normalized.startswith("SELECT m.version,"): return _Result({"version": 3, "permitted": True})
        if "FROM external_request_authorizations" in normalized and "FOR KEY SHARE" in normalized:
            return _Result({"request_id": self.request_id, "call_cap": 3, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)})
        if "FROM external_request_attempts" in normalized and "FOR UPDATE" in normalized: return _Result(rows=self.attempts)
        if "UPDATE matters SET version = version + 1" in normalized: return _Result({"version": 4})
        return _Result()


class _Context:
    def __init__(self, connection): self.connection = connection
    def __enter__(self): return self.connection
    def __exit__(self, *_): return False


class ExternalRequestPostgresTests(TestCase):
    def setUp(self) -> None:
        self.matter_id, self.firm_id = str(uuid4()), str(uuid4())
        self.lawyer = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.worker = Actor(str(uuid4()), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.store = PostgresExternalRequestStore("postgresql://not-used.invalid/lawcase_test")

    def _preflight(self):
        return ExternalRequestPreflight("MODEL", "提取日期", "approved-provider", "CN", "30D", "NO_TRAINING", ("evidence:page:1",), "model-x", 3, "CNY", 1000, "a" * 64, "b" * 64, datetime.now(timezone.utc) + timedelta(hours=1))

    def _run(self, connection, callback):
        with patch("case_kernel.external_request_postgres.psycopg.connect", return_value=_Context(connection)):
            return callback()

    def test_lawyer_preflight_records_minimized_metadata_only(self) -> None:
        connection = _Connection()
        receipt = self._run(connection, lambda: self.store.authorize_external_request(
            matter_id=self.matter_id, actor=self.lawyer, expected_version=3,
            idempotency_key="external-preflight-001", preflight=self._preflight(),
        ))
        self.assertEqual(receipt.matter_version, 4)
        sql = "\n".join(item[0] for item in connection.executed)
        self.assertIn("INSERT INTO external_request_authorizations", sql)
        self.assertIn("cost_currency", sql)

    def test_preflight_rejects_ambiguous_cost_currency(self) -> None:
        ambiguous = ExternalRequestPreflight(
            "MODEL", "提取日期", "approved-provider", "CN", "30D", "NO_TRAINING",
            ("evidence:page:1",), "model-x", 3, "XXX", 1000, "a" * 64, "b" * 64,
            datetime.now(timezone.utc) + timedelta(hours=1),
        )
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "cost currency"):
            self._run(_Connection(), lambda: self.store.authorize_external_request(
                matter_id=self.matter_id, actor=self.lawyer, expected_version=3,
                idempotency_key="external-preflight-currency", preflight=ambiguous,
            ))

    def test_unknown_submission_blocks_any_automatic_retry(self) -> None:
        connection = _Connection(attempts=({"sequence": 1, "status": "UNKNOWN_SUBMISSION"},))
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "must be reconciled"):
            self._run(connection, lambda: self.store.record_external_attempt(
                matter_id=self.matter_id, actor=self.worker, expected_version=3,
                idempotency_key="external-attempt-001", request_id=connection.request_id,
                status="SUBMISSION_STARTED", provider_request_ref_hash="c" * 64,
            ))

    def test_outcome_requires_started_receipt_and_worker_role(self) -> None:
        connection = _Connection()
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "prior submission-started"):
            self._run(connection, lambda: self.store.record_external_attempt(
                matter_id=self.matter_id, actor=self.worker, expected_version=3,
                idempotency_key="external-outcome-001", request_id=connection.request_id,
                status="SUCCEEDED", output_hash="d" * 64,
            ))


if __name__ == "__main__":
    import unittest
    unittest.main()
