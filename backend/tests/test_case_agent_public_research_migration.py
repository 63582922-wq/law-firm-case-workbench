from pathlib import Path
import re
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0037_case_agent_public_research.sql"
)


class CaseAgentPublicResearchMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_has_server_binding_grant_exchange_and_append_only_outcome(self) -> None:
        for table in (
            "case_agent_public_research_bindings",
            "case_agent_public_research_egress_grants",
            "case_agent_public_research_exchanges",
            "case_agent_public_research_outcomes",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE %I ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE %I FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("prohibit_case_agent_public_research_history_mutation", self.sql)
        self.assertIn("REVOKE ALL ON TABLE %I FROM PUBLIC", self.sql)

    def test_exact_network_and_lawyer_policy_are_database_guarded(self) -> None:
        for value in (
            "controlled_web_search",
            "search_public_web",
            "NETWORK_CONNECTOR",
            "EXACT_ALLOWLIST",
            "A3_LAWYER_APPROVAL",
            "LAWYER_REVIEW",
            "NEVER_AUTOMATIC",
            "api.search.brave.com",
            "brave_web_search",
            "web_search_v1",
            "max_external_calls",
            "max_attempts",
        ):
            self.assertIn(value, self.sql)
        self.assertRegex(self.sql, r"max_requests\s+integer NOT NULL CHECK \(max_requests = 1\)")
        self.assertRegex(self.sql, r"redirects_allowed\s+boolean NOT NULL CHECK \(redirects_allowed = false\)")

    def test_only_current_governed_same_matter_references_can_bind(self) -> None:
        for value in (
            "case_dispute_issues",
            "case_work_plan_items",
            "case_work_plans",
            "official_legal_source_snapshots",
            "case_agent_lawyer_decision_signals",
            "LEGAL_GAP",
            "current_graph_hash",
            "confidential_question",
            "public_terms",
        ):
            self.assertIn(value, self.sql)
        self.assertNotIn("browser", " ".join(
            line for line in self.sql.splitlines() if line.lstrip().startswith("CREATE TABLE")
        ))

    def test_unknown_can_only_be_followed_by_recovered_success(self) -> None:
        self.assertIn("outcome_sequence integer NOT NULL CHECK (outcome_sequence IN (1, 2))", self.sql)
        self.assertIn("'UNKNOWN_SUBMISSION'", self.sql)
        self.assertIn("outcome_sequence = 2 AND status = 'SUCCEEDED'", self.sql)
        self.assertIn("prior.status <> 'UNKNOWN_SUBMISSION'", self.sql)
        self.assertIn("submission.recorded_by <> NEW.started_by_worker", self.sql)
        self.assertIn("case_agent_public_research_egress_grant_guard", self.sql)
        self.assertIn("COALESCE((task_row.resource_budget->>'max_external_calls')::integer, -1) <> 1", self.sql)
        self.assertIn("run_row.current_graph_id IS DISTINCT FROM NEW.graph_id", self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)


if __name__ == "__main__":
    unittest.main()
