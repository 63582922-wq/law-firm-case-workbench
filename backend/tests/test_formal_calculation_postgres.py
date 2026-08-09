from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.calculation_engine import AllocationPolicy
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.formal_calculation_postgres import (
    PostgresFormalCalculationStore,
)
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


class FakeCalculationConnection:
    def __init__(self, *, duplicate_status: str | None = None, currency: str = "CNY") -> None:
        self.bundle_id = str(uuid4())
        self.first_transaction_id = str(uuid4())
        self.second_transaction_id = str(uuid4())
        self.first_classification_id = str(uuid4())
        self.second_classification_id = str(uuid4())
        self.approved_by = str(uuid4())
        self.segment_id = str(uuid4())
        self.duplicate_status = duplicate_status
        self.currency = currency
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": True})
        if "SELECT bundle_id, bundle_hash, status FROM case_legal_bundles" in normalized:
            return FakeResult(row={"bundle_id": self.bundle_id, "bundle_hash": "a" * 64, "status": "APPROVED"})
        if "SELECT rule_version FROM case_legal_bundle_rule_versions" in normalized:
            return FakeResult(rows=[{"rule_version": "SYNTHETIC-RULE-2020"}])
        if "FROM case_legal_bundle_segments" in normalized:
            return FakeResult(
                rows=[
                    {
                        "segment_id": self.segment_id,
                        "start_date": date(2020, 1, 1),
                        "end_date": date(2021, 1, 1),
                        "annual_rate": Decimal("0.12"),
                        "source_rule_version": "SYNTHETIC-RULE-2020",
                        "applicability_anchor": "synthetic approved event",
                        "approval_hash": "c" * 64,
                        "trigger_event_id": str(uuid4()),
                    }
                ]
            )
        if "FROM case_payment_allocations allocation" in normalized:
            common = {
                "date_precision": "EXACT_DATE",
                "transaction_status": "CONFIRMED",
                "transaction_evidence_links": [{"evidence_id": "evidence-ledger-page"}],
                "classification_evidence_links": [{"evidence_id": "evidence-classification"}],
                "classification_status": "APPROVED",
                "approval_hash": "b" * 64,
                "approved_by": self.approved_by,
                "currency": self.currency,
            }
            return FakeResult(
                rows=[
                    {
                        **common,
                        "transaction_id": self.first_transaction_id,
                        "classification_id": self.first_classification_id,
                        "local_date": date(2020, 1, 1),
                        "nature": "DISBURSEMENT",
                        "same_day_sequence": 1,
                        "amount": Decimal("10000.00"),
                    },
                    {
                        **common,
                        "transaction_id": self.second_transaction_id,
                        "classification_id": self.second_classification_id,
                        "local_date": date(2020, 6, 1),
                        "nature": "REPAYMENT_UNSPECIFIED",
                        "same_day_sequence": 1,
                        "amount": Decimal("1000.00"),
                    },
                ]
            )
        if "FROM case_transaction_duplicate_groups group_row" in normalized:
            if self.duplicate_status is None:
                return FakeResult(rows=[])
            return FakeResult(
                rows=[
                    {
                        "duplicate_group_id": str(uuid4()),
                        "status": self.duplicate_status,
                        "canonical_transaction_id": self.first_transaction_id,
                        "transaction_id": self.first_transaction_id,
                    }
                ]
            )
        if "SELECT COALESCE(MAX(version), 0) + 1 AS next_version" in normalized:
            return FakeResult(row={"next_version": 1})
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeCalculationConnection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class FormalCalculationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresFormalCalculationStore("postgresql://not-used.invalid/lawcase_test")

    def calculate(self, connection: FakeCalculationConnection):
        with patch(
            "case_kernel.formal_calculation_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            return self.store.create_formal_calculation(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="formal-calculation-001",
                obligation_id="synthetic-obligation-001",
                start_date=date(2020, 1, 1),
                end_date=date(2021, 1, 1),
                legal_bundle_id=connection.bundle_id,
                legal_bundle_hash="a" * 64,
                allocation_policy=AllocationPolicy.INTEREST_THEN_PRINCIPAL,
                approval_hash="d" * 64,
            )

    def test_formal_run_derives_events_from_approved_transactions_and_persists_full_trace(self) -> None:
        connection = FakeCalculationConnection()
        receipt = self.calculate(connection)
        UUID(receipt.object_id)
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO calculation_scenarios", sql)
        self.assertEqual(sql.count("INSERT INTO calculation_scenario_events"), 2)
        self.assertEqual(sql.count("INSERT INTO calculation_line_items"), 2)
        self.assertEqual(sql.count("INSERT INTO calculation_payment_allocations"), 1)
        self.assertIn("INSERT INTO calculation_runs", sql)
        self.assertIn("INSERT INTO audit_events", sql)
        self.assertIn("INSERT INTO outbox_events", sql)
        event_parameters = [
            params for statement, params in connection.executed if "INSERT INTO calculation_scenario_events" in statement
        ]
        self.assertEqual(event_parameters[0][8:11], (Decimal("10000.00"), "BY_POLICY", '["evidence-classification", "evidence-ledger-page"]'))
        self.assertEqual(event_parameters[1][8:11], (Decimal("1000.00"), "BY_POLICY", '["evidence-classification", "evidence-ledger-page"]'))

    def test_unresolved_duplicate_and_non_cny_allocation_fail_before_any_run_insert(self) -> None:
        unresolved = FakeCalculationConnection(duplicate_status="CANDIDATE")
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "unresolved duplicate"):
            self.calculate(unresolved)
        self.assertFalse(any("INSERT INTO calculation_runs" in sql for sql, _ in unresolved.executed))
        foreign = FakeCalculationConnection(currency="USD")
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "CNY-cent"):
            self.calculate(foreign)
        self.assertFalse(any("INSERT INTO calculation_runs" in sql for sql, _ in foreign.executed))


if __name__ == "__main__":
    unittest.main()
