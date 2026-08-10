"""Opt-in destructive integration tests for an explicitly dedicated PostgreSQL test database."""

from __future__ import annotations

import os
from pathlib import Path
from datetime import date
from decimal import Decimal
import unittest
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.models import Actor, MatterStage, Role
from case_kernel.postgres_store import PostgresMatterStore
from case_kernel.workflow import MatterWorkflow
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
)


TEST_DSN = os.environ.get("CASE_WORKBENCH_TEST_DATABASE_URL", "")
ALLOW_DESTRUCTIVE = os.environ.get("CASE_WORKBENCH_ALLOW_DESTRUCTIVE_TEST_DB") == "YES"
MIGRATIONS = tuple(sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")))


def _configured_test_database() -> bool:
    if not TEST_DSN or not ALLOW_DESTRUCTIVE:
        return False
    database_name = conninfo_to_dict(TEST_DSN).get("dbname", "")
    return database_name.endswith("_test")


@unittest.skipUnless(
    _configured_test_database(),
    "requires CASE_WORKBENCH_TEST_DATABASE_URL ending in _test and CASE_WORKBENCH_ALLOW_DESTRUCTIVE_TEST_DB=YES",
)
class PostgresMatterStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with psycopg.connect(TEST_DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA public CASCADE")
            connection.execute("CREATE SCHEMA public")
            for migration in MIGRATIONS:
                connection.execute(migration.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.firm_id = str(uuid4())
        self.actor_id = str(uuid4())
        self.actor = Actor(self.actor_id, self.firm_id, frozenset({Role.LEAD_LAWYER}))
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("INSERT INTO firms (firm_id, display_name) VALUES (%s, %s)", (self.firm_id, "Synthetic Test Firm"))
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            connection.execute(
                "INSERT INTO users (user_id, firm_id, external_subject, display_name, status) VALUES (%s, %s, %s, %s, 'ACTIVE')",
                (self.actor_id, self.firm_id, f"subject-{self.actor_id}", "Synthetic Lead"),
            )
        self.store = PostgresMatterStore(TEST_DSN)
        self.case_ledger_store = PostgresCaseLedgerStore(TEST_DSN)
        self.workflow = MatterWorkflow(self.store)

    def test_command_is_idempotent_and_audited_under_tenant_scope(self) -> None:
        matter_id = str(uuid4())
        first = self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-001",
        )
        repeated = self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-001",
        )
        self.assertEqual(first, repeated)
        found = self.workflow.get_matter(self.actor, matter_id=matter_id)
        self.assertEqual(found.stage, MatterStage.CREATED)
        accessible = self.store.list_accessible(actor=self.actor)
        self.assertEqual([matter_id], [item["matter_id"] for item in accessible])
        self.assertEqual("Synthetic UUID Matter", accessible[0]["title"])
        events = self.store.audit_events(matter_id, firm_id=self.firm_id)
        self.assertEqual(["MATTER_CREATED"], [event.event_type for event in events])

        advanced = self.workflow.advance(
            self.actor,
            matter_id=matter_id,
            expected_version=1,
            idempotency_key="advance-001",
        )
        self.assertEqual(advanced.matter_version, 2)
        self.assertEqual(self.workflow.get_matter(self.actor, matter_id=matter_id).stage, MatterStage.INGESTING)

    def test_row_level_security_hides_another_firm_matter(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic UUID Matter",
            idempotency_key="create-rls-001",
        )
        other_firm_id = str(uuid4())
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (other_firm_id,))
            row = connection.execute("SELECT count(*) AS count FROM matters WHERE matter_id = %s", (matter_id,)).fetchone()
        self.assertEqual(row["count"], 0)

    def test_fact_candidate_and_decision_share_matter_version_audit_and_rls_boundary(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic Persistent Fact Matter",
            idempotency_key="create-ledger-001",
        )
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            connection.execute(
                """
                INSERT INTO matter_actor_roles (matter_id, firm_id, user_id, role)
                VALUES (%s, %s, %s, 'LEAD_LAWYER')
                """,
                (matter_id, self.firm_id, self.actor_id),
            )
        candidate = self.case_ledger_store.create_fact_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=1,
            idempotency_key="persist-fact-001",
            original_text="[合成] 被告主张已支付一笔款项。",
            origin=AssertionOrigin.DEFENDANT_STATEMENT,
            evidence_links=(
                EvidenceLink(
                    evidence_id="synthetic-integration-evidence",
                    original_file_sha256="a" * 64,
                    page_number=1,
                    region_id="synthetic-integration-region",
                    original_label="[合成] 原始账单第1页",
                ),
            ),
        )
        decided = self.case_ledger_store.decide_fact(
            matter_id=matter_id,
            fact_id=candidate.object_id,
            actor=self.actor,
            expected_version=2,
            idempotency_key="persist-fact-decision-001",
            status=FactStatus.CONFIRMED,
            decision_hash="b" * 64,
        )
        facts = self.case_ledger_store.list_facts(matter_id=matter_id, actor=self.actor)
        self.assertEqual(decided.matter_version, 3)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].status, FactStatus.CONFIRMED)

    def test_full_case_ledger_command_chain_preserves_versions_and_normalized_links(self) -> None:
        matter_id = str(uuid4())
        self.workflow.create_matter(
            self.actor,
            matter_id=matter_id,
            title="Synthetic Full Persistent Ledger Matter",
            idempotency_key="create-full-ledger-001",
        )
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            connection.execute(
                """
                INSERT INTO matter_actor_roles (matter_id, firm_id, user_id, role)
                VALUES (%s, %s, %s, 'LEAD_LAWYER')
                """,
                (matter_id, self.firm_id, self.actor_id),
            )
        original = (
            EvidenceLink(
                evidence_id="synthetic-full-ledger-evidence",
                original_file_sha256="c" * 64,
                page_number=1,
                region_id="synthetic-full-ledger-region",
                original_label="[合成] 原始材料第1页",
            ),
        )
        fact = self.case_ledger_store.create_fact_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=1,
            idempotency_key="full-fact-create",
            original_text="[合成] 被告主张已支付一笔款项。",
            origin=AssertionOrigin.DEFENDANT_STATEMENT,
            evidence_links=original,
        )
        self.case_ledger_store.decide_fact(
            matter_id=matter_id,
            fact_id=fact.object_id,
            actor=self.actor,
            expected_version=2,
            idempotency_key="full-fact-confirm",
            status=FactStatus.CONFIRMED,
            decision_hash="d" * 64,
        )
        claim = self.case_ledger_store.create_claim_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=3,
            idempotency_key="full-claim-create",
            original_claim_text="[合成] 原告主张本金1,000.00元。",
            claimed_amount=Decimal("1000.00"),
            currency="CNY",
            evidence_links=original,
        )
        self.case_ledger_store.confirm_claim_scope(
            matter_id=matter_id,
            claim_id=claim.object_id,
            actor=self.actor,
            expected_version=4,
            idempotency_key="full-claim-confirm",
            confirmation_hash="e" * 64,
        )
        self.case_ledger_store.set_claim_response(
            matter_id=matter_id,
            claim_id=claim.object_id,
            actor=self.actor,
            expected_version=5,
            idempotency_key="full-response-set",
            position=ClaimResponsePosition.PARTIALLY_ADMIT,
            confirmed_fact_ids=(fact.object_id,),
            partial_amount=Decimal("800.00"),
            currency="CNY",
            approval_hash="f" * 64,
        )
        issue = self.case_ledger_store.create_dispute_issue_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=6,
            idempotency_key="full-issue-create",
            question="[合成] 已付款项应如何计入？",
            claim_ids=(claim.object_id,),
            confirmed_fact_ids=(fact.object_id,),
        )
        self.case_ledger_store.confirm_dispute_issue(
            matter_id=matter_id,
            issue_id=issue.object_id,
            actor=self.actor,
            expected_version=7,
            idempotency_key="full-issue-confirm",
            approval_hash="1" * 64,
        )
        transaction = self.case_ledger_store.create_transaction_candidate(
            matter_id=matter_id,
            actor=self.actor,
            expected_version=8,
            idempotency_key="full-transaction-create",
            local_date=date(2020, 8, 20),
            date_precision=DatePrecision.EXACT_DATE,
            amount=Decimal("1000.00"),
            currency="CNY",
            direction=TransactionDirection.OUTGOING,
            payer_label="[合成] 被告",
            payee_label="[合成] 原告",
            channel=TransactionChannel.WECHAT,
            transaction_reference="synthetic-full-ledger-reference",
            evidence_links=original,
        )
        self.case_ledger_store.confirm_transaction(
            matter_id=matter_id,
            transaction_id=transaction.object_id,
            actor=self.actor,
            expected_version=9,
            idempotency_key="full-transaction-confirm",
            confirmation_hash="2" * 64,
        )
        classification = self.case_ledger_store.create_payment_classification_candidate(
            matter_id=matter_id,
            transaction_id=transaction.object_id,
            actor=self.actor,
            expected_version=10,
            idempotency_key="full-classification-create",
            origin=ClassificationOrigin.DEFENDANT_STATEMENT,
            nature=PaymentNature.INTEREST_PAYMENT,
            allocations=(ObligationAllocation("synthetic-obligation", Decimal("1000.00"), "CNY"),),
            same_day_sequence=1,
            evidence_links=original,
        )
        final = self.case_ledger_store.approve_payment_classification(
            matter_id=matter_id,
            classification_id=classification.object_id,
            actor=self.actor,
            expected_version=11,
            idempotency_key="full-classification-approve",
            approval_hash="3" * 64,
        )
        self.assertEqual(final.matter_version, 12)
        snapshot = self.case_ledger_store.get_case_snapshot(matter_id=matter_id, actor=self.actor)
        self.assertEqual(snapshot.version, 12)
        self.assertEqual(len(snapshot.facts), 1)
        self.assertEqual(len(snapshot.claims), 1)
        self.assertEqual(len(snapshot.issues), 1)
        self.assertEqual(len(snapshot.transactions), 1)
        self.assertEqual(len(snapshot.payment_classifications), 1)
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (self.firm_id,))
            response_links = connection.execute(
                "SELECT count(*) AS count FROM case_claim_response_facts WHERE matter_id = %s",
                (matter_id,),
            ).fetchone()["count"]
            allocation_links = connection.execute(
                "SELECT count(*) AS count FROM case_payment_allocations WHERE matter_id = %s",
                (matter_id,),
            ).fetchone()["count"]
        self.assertEqual(response_links, 1)
        self.assertEqual(allocation_links, 1)

class PostgresMatterStoreBoundaryTests(unittest.TestCase):
    def test_alpha_identifiers_are_rejected_before_connection(self) -> None:
        store = PostgresMatterStore("postgresql://not-used.invalid/lawcase_workbench_test")
        workflow = MatterWorkflow(store)
        with patch("case_kernel.postgres_store.psycopg.connect") as connect:
            with self.assertRaisesRegex(ValueError, "requires UUID"):
                workflow.create_matter(
                    Actor("alpha_lead_lawyer", "alpha_firm_001", frozenset({Role.LEAD_LAWYER})),
                    matter_id="alpha_matter_001",
                    title="[合成] 不可进入持久化库",
                    idempotency_key="alpha-create-001",
                )
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
