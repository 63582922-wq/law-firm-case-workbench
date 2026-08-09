from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.transaction_ledger import DatePrecision, TransactionChannel, TransactionDirection


@dataclass
class FakeResult:
    row: dict | None = None
    rows: list[dict] | None = None
    rowcount: int = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows or []


class FakeConnection:
    def __init__(self, *, permitted: bool = True, fact_status: str = "CANDIDATE") -> None:
        self.permitted = permitted
        self.fact_status = fact_status
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> FakeResult:
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "SELECT request_hash, response_json" in normalized:
            return FakeResult(row=None)
        if "SELECT m.version," in normalized:
            return FakeResult(row={"version": 1, "permitted": self.permitted})
        if "SELECT status FROM case_facts" in normalized:
            return FakeResult(row={"status": self.fact_status})
        if "UPDATE matters SET version = version + 1" in normalized:
            return FakeResult(row={"version": 2})
        return FakeResult()


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> FakeConnection:
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def evidence() -> tuple[EvidenceLink, ...]:
    return (
        EvidenceLink(
            evidence_id="synthetic-evidence-id",
            original_file_sha256="a" * 64,
            page_number=1,
            region_id="synthetic-region-id",
            original_label="[合成] 微信账单第1页",
        ),
    )


class PostgresCaseLedgerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.actor = Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.store = PostgresCaseLedgerStore("postgresql://not-used.invalid/lawcase_workbench_test")

    def test_fact_candidate_commits_object_version_audit_outbox_and_idempotency(self) -> None:
        connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.create_fact_candidate(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="fact-create-001",
                original_text="[合成] 被告主张已支付一笔款项。",
                origin=AssertionOrigin.DEFENDANT_STATEMENT,
                evidence_links=evidence(),
            )

        UUID(receipt.object_id)
        self.assertEqual(receipt.matter_version, 2)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_facts", sql)
        self.assertIn("UPDATE matters SET version = version + 1", sql)
        self.assertIn("INSERT INTO audit_events", sql)
        self.assertIn("INSERT INTO outbox_events", sql)
        self.assertIn("INSERT INTO command_idempotency", sql)
        self.assertLess(sql.index("SELECT set_config"), sql.index("INSERT INTO case_facts"))

    def test_fact_decision_revokes_dependent_objects_and_stales_submission(self) -> None:
        connection = FakeConnection()
        fact_id = str(uuid4())
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.decide_fact(
                matter_id=self.matter_id,
                fact_id=fact_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="fact-decide-001",
                status=FactStatus.CONFIRMED,
                decision_hash="b" * 64,
            )

        self.assertEqual(receipt.object_id, fact_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("DELETE FROM case_claim_responses", sql)
        self.assertIn("UPDATE case_dispute_issues", sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", sql)
        self.assertIn("current_submission_bundle_id = CASE WHEN", sql)

    def test_database_membership_is_required_even_when_actor_claims_a_role(self) -> None:
        connection = FakeConnection(permitted=False)
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            with self.assertRaisesRegex(PermissionError, "database role"):
                self.store.create_fact_candidate(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="fact-create-denied",
                    original_text="[合成] 不应写入。",
                    origin=AssertionOrigin.AGENT_CANDIDATE,
                    evidence_links=evidence(),
                )
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertNotIn("INSERT INTO case_facts", sql)

    def test_transaction_candidate_and_confirmation_use_the_same_command_boundary(self) -> None:
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            candidate = self.store.create_transaction_candidate(
                matter_id=self.matter_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="transaction-create-001",
                local_date=date(2020, 8, 20),
                date_precision=DatePrecision.EXACT_DATE,
                amount=Decimal("1000.00"),
                currency="cny",
                direction=TransactionDirection.OUTGOING,
                payer_label="[合成] 被告",
                payee_label="[合成] 原告",
                channel=TransactionChannel.WECHAT,
                transaction_reference="synthetic-reference",
                evidence_links=evidence(),
            )
        UUID(candidate.object_id)
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_transactions", candidate_sql)

        confirmation_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(confirmation_connection),
        ):
            confirmed = self.store.confirm_transaction(
                matter_id=self.matter_id,
                transaction_id=candidate.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="transaction-confirm-001",
                confirmation_hash="c" * 64,
            )
        self.assertEqual(confirmed.object_id, candidate.object_id)
        confirmation_sql = "\n".join(statement for statement, _ in confirmation_connection.executed)
        self.assertIn("UPDATE case_transactions SET status = 'CONFIRMED'", confirmation_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", confirmation_sql)

    def test_inexact_transaction_date_is_rejected_before_connection(self) -> None:
        with patch("case_kernel.case_ledger_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "non-exact"):
                self.store.create_transaction_candidate(
                    matter_id=self.matter_id,
                    actor=self.actor,
                    expected_version=1,
                    idempotency_key="transaction-invalid-date",
                    local_date=date(2020, 8, 20),
                    date_precision=DatePrecision.MONTH_ONLY,
                    amount=Decimal("1000.00"),
                    currency="CNY",
                    direction=TransactionDirection.OUTGOING,
                    payer_label=None,
                    payee_label=None,
                    channel=TransactionChannel.WECHAT,
                    transaction_reference=None,
                    evidence_links=evidence(),
                )
        connect.assert_not_called()

    def test_alpha_identifiers_are_rejected_before_connection(self) -> None:
        with patch("case_kernel.case_ledger_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "requires UUID"):
                self.store.create_fact_candidate(
                    matter_id="alpha_matter_001",
                    actor=Actor("alpha_lead", "alpha_firm", frozenset({Role.LEAD_LAWYER})),
                    expected_version=1,
                    idempotency_key="alpha-rejected",
                    original_text="[合成] 不可持久化。",
                    origin=AssertionOrigin.AGENT_CANDIDATE,
                    evidence_links=evidence(),
                )
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
