from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0033_case_agent_runtime_execution.sql"


class CaseAgentRuntimeExecutionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_run_insert_and_event_advance_wake_one_durable_inbox(self) -> None:
        self.assertIn("CREATE TABLE case_agent_run_inbox", self.sql)
        self.assertIn("AFTER INSERT ON case_agent_runs", self.sql)
        self.assertIn("AFTER UPDATE OF current_event_version ON case_agent_runs", self.sql)
        self.assertIn("ON CONFLICT (run_id) DO UPDATE", self.sql)
        self.assertIn("pg_notify('case_agent_run_ready'", self.sql)
        self.assertNotIn("document_text", self.sql)
        self.assertNotIn("browser_path", self.sql)

    def test_inbox_is_recoverable_fair_and_rls_scoped(self) -> None:
        self.assertIn("inbox_status IN ('READY', 'LEASED', 'QUIET')", self.sql)
        self.assertIn("case_agent_run_inbox_claim_idx", self.sql)
        self.assertIn("FROM case_agent_runs\nON CONFLICT (run_id) DO NOTHING", self.sql)
        self.assertIn("ALTER TABLE %I FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("REVOKE ALL ON TABLE case_agent_run_inbox FROM PUBLIC", self.sql)

    def test_office_and_candidate_locators_are_private_hash_bound_metadata(self) -> None:
        self.assertIn("CREATE TABLE case_agent_material_objects", self.sql)
        self.assertIn("admitted_format IN ('DOCX', 'XLSX')", self.sql)
        self.assertIn("^case-materials/v1/", self.sql)
        self.assertIn("CREATE TABLE case_agent_review_candidates", self.sql)
        self.assertIn("review_status = 'NEEDS_LAWYER_REVIEW'", self.sql)
        self.assertIn("byte_size BETWEEN 2 AND 67108864", self.sql)
        self.assertIn("^case-agent-candidates/v1/", self.sql)
        self.assertNotIn("payload bytea", self.sql.lower())


if __name__ == "__main__":
    unittest.main()
