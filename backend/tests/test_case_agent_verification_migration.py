from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0034_case_agent_verification_receipts.sql"


class CaseAgentVerificationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_attempt_and_full_receipt_are_append_only_and_rls_scoped(self) -> None:
        self.assertIn("CREATE TABLE case_agent_verification_attempts", self.sql)
        self.assertIn("CREATE TABLE case_agent_verification_receipts", self.sql)
        self.assertEqual(self.sql.count("FORCE ROW LEVEL SECURITY"), 2)
        self.assertEqual(
            self.sql.count("prohibit_case_agent_verification_history_mutation"), 3
        )
        self.assertIn("REVOKE ALL ON TABLE case_agent_verification_receipts", self.sql)
        self.assertIn("ALTER TABLE case_agent_worker_heartbeats", self.sql)
        self.assertIn(
            "case_agent_worker_heartbeat_verifier_binding_check CHECK",
            self.sql,
        )
        self.assertIn(") NOT VALID;", self.sql)
        for field in (
            "verifier_actor_id",
            "verifier_id",
            "verifier_version",
            "verifier_policy_hash",
        ):
            self.assertIn(field, self.sql)

    def test_database_rejects_self_verification_and_partial_failure_lineage(self) -> None:
        self.assertEqual(
            self.sql.count("CHECK (verifier_actor_id <> execution_actor_id)"), 2
        )
        self.assertIn(
            "outcome = 'FAILED' AND error_code IS NOT NULL AND artifact_lineage = '[]'::jsonb",
            self.sql,
        )
        self.assertIn(
            "require_independent_case_agent_verifier_principals", self.sql
        )
        self.assertEqual(
            self.sql.count("role.role = 'SYSTEM_WORKER'"), 1
        )
        self.assertIn("principal.status = 'ACTIVE'", self.sql)
        self.assertIn("role.role <> 'SYSTEM_WORKER'", self.sql)

    def test_receipt_binds_graph_snapshot_tasks_artifacts_and_terminal_event(self) -> None:
        for field in (
            "policy_hash",
            "graph_hash",
            "snapshot_hash",
            "task_receipts_hash",
            "artifact_manifest_hash",
            "artifact_lineage",
            "verification_hash",
            "terminal_event_sequence",
        ):
            self.assertIn(field, self.sql)
        self.assertIn("REFERENCES case_agent_events", self.sql)
        self.assertNotIn("object_key", self.sql)
        self.assertNotIn("document_text", self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":
    unittest.main()
