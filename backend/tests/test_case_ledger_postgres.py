from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.models import Actor, Role
from case_kernel.transaction_ledger import DatePrecision, TransactionChannel, TransactionDirection
from case_kernel.transaction_ledger import ClassificationOrigin, ObligationAllocation, PaymentNature


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
    def __init__(
        self,
        *,
        permitted: bool = True,
        fact_status: str = "CANDIDATE",
        duplicate_members: tuple[str, ...] = (),
    ) -> None:
        self.permitted = permitted
        self.fact_status = fact_status
        self.duplicate_members = duplicate_members
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
        if "SELECT status, claimed_amount, currency FROM case_claims" in normalized:
            return FakeResult(row={"status": "CONFIRMED_SCOPE", "claimed_amount": Decimal("1000.00"), "currency": "CNY"})
        if "SELECT fact_id, status FROM case_facts WHERE fact_id = ANY" in normalized:
            return FakeResult(rows=[{"fact_id": value, "status": "CONFIRMED"} for value in (params or ([],))[0]])
        if "SELECT claim_id, status FROM case_claims WHERE claim_id = ANY" in normalized:
            return FakeResult(rows=[{"claim_id": value, "status": "CONFIRMED_SCOPE"} for value in (params or ([],))[0]])
        if "SELECT amount, currency, status FROM case_transactions" in normalized:
            return FakeResult(row={"amount": Decimal("1000.00"), "currency": "CNY", "status": "CONFIRMED"})
        if "SELECT pc.status, pc.nature, pc.transaction_id" in normalized:
            return FakeResult(
                row={
                    "status": "CANDIDATE",
                    "nature": "INTEREST_PAYMENT",
                    "transaction_id": str(uuid4()),
                    "amount": Decimal("1000.00"),
                    "currency": "CNY",
                    "transaction_status": "CONFIRMED",
                }
            )
        if "SELECT obligation_id, amount, currency FROM case_payment_allocations" in normalized:
            return FakeResult(rows=[{"obligation_id": "synthetic-obligation", "amount": Decimal("1000.00"), "currency": "CNY"}])
        if "SELECT transaction_id, status FROM case_transactions WHERE transaction_id = ANY" in normalized:
            return FakeResult(rows=[{"transaction_id": value, "status": "CONFIRMED"} for value in (params or ([],))[0]])
        if "SELECT member.transaction_id FROM case_transaction_duplicate_members" in normalized:
            return FakeResult(row=None)
        if "SELECT status FROM case_transaction_duplicate_groups" in normalized:
            return FakeResult(row={"status": "CANDIDATE"})
        if "SELECT transaction_id FROM case_transaction_duplicate_members" in normalized:
            return FakeResult(rows=[{"transaction_id": value} for value in self.duplicate_members])
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

    def test_claim_response_uses_normalized_confirmed_fact_links(self) -> None:
        claim_id = str(uuid4())
        fact_id = str(uuid4())
        connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(connection),
        ):
            receipt = self.store.set_claim_response(
                matter_id=self.matter_id,
                claim_id=claim_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="claim-response-001",
                position=ClaimResponsePosition.PARTIALLY_ADMIT,
                confirmed_fact_ids=(fact_id,),
                partial_amount=Decimal("800.00"),
                currency="CNY",
                approval_hash="d" * 64,
            )
        UUID(receipt.object_id)
        sql = "\n".join(statement for statement, _ in connection.executed)
        self.assertIn("INSERT INTO case_claim_responses", sql)
        self.assertIn("INSERT INTO case_claim_response_facts", sql)
        self.assertIn("UPDATE case_dispute_issues", sql)
        self.assertNotIn("confirmed_fact_ids", sql)

    def test_payment_classification_persists_normalized_allocations_and_approval(self) -> None:
        transaction_id = str(uuid4())
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            candidate = self.store.create_payment_classification_candidate(
                matter_id=self.matter_id,
                transaction_id=transaction_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="classification-create-001",
                origin=ClassificationOrigin.DEFENDANT_STATEMENT,
                nature=PaymentNature.INTEREST_PAYMENT,
                allocations=(ObligationAllocation("synthetic-obligation", Decimal("1000.00"), "CNY"),),
                same_day_sequence=1,
                evidence_links=evidence(),
            )
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_payment_classifications", candidate_sql)
        self.assertIn("INSERT INTO case_payment_allocations", candidate_sql)

        approval_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(approval_connection),
        ):
            approved = self.store.approve_payment_classification(
                matter_id=self.matter_id,
                classification_id=candidate.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="classification-approve-001",
                approval_hash="e" * 64,
            )
        self.assertEqual(approved.object_id, candidate.object_id)
        approval_sql = "\n".join(statement for statement, _ in approval_connection.executed)
        self.assertIn("SET status = 'INVALIDATED'", approval_sql)
        self.assertIn("SET status = 'APPROVED'", approval_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", approval_sql)

    def test_duplicate_group_preserves_all_sources_and_requires_member_canonical(self) -> None:
        first = str(uuid4())
        second = str(uuid4())
        candidate_connection = FakeConnection()
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(candidate_connection),
        ):
            group = self.store.create_duplicate_group_candidate(
                matter_id=self.matter_id,
                transaction_ids=(first, second),
                actor=self.actor,
                expected_version=1,
                idempotency_key="duplicate-create-001",
            )
        candidate_sql = "\n".join(statement for statement, _ in candidate_connection.executed)
        self.assertIn("INSERT INTO case_transaction_duplicate_groups", candidate_sql)
        self.assertEqual(candidate_sql.count("INSERT INTO case_transaction_duplicate_members"), 2)
        self.assertNotIn("DELETE FROM case_transactions", candidate_sql)

        resolution_connection = FakeConnection(duplicate_members=(first, second))
        with patch(
            "case_kernel.case_ledger_postgres.psycopg.connect",
            return_value=FakeConnectionContext(resolution_connection),
        ):
            resolved = self.store.resolve_duplicate_group(
                matter_id=self.matter_id,
                duplicate_group_id=group.object_id,
                actor=self.actor,
                expected_version=1,
                idempotency_key="duplicate-resolve-001",
                same_economic_event=True,
                canonical_transaction_id=first,
                approval_hash="f" * 64,
            )
        self.assertEqual(resolved.object_id, group.object_id)
        resolution_sql = "\n".join(statement for statement, _ in resolution_connection.executed)
        self.assertIn("UPDATE case_transaction_duplicate_groups", resolution_sql)
        self.assertIn("UPDATE submission_bundles SET validity = 'STALE'", resolution_sql)

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
