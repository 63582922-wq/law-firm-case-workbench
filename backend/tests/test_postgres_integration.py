"""Opt-in destructive integration tests for an explicitly dedicated PostgreSQL test database."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict

from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.evidence_refs import EvidenceLink
from case_kernel.fact_claim_ledger import AssertionOrigin, FactStatus
from case_kernel.models import Actor, MatterStage, Role
from case_kernel.postgres_store import PostgresMatterStore
from case_kernel.workflow import MatterWorkflow


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
