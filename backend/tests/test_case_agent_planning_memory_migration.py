from pathlib import Path
import unittest


SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0036_case_agent_planning_memory_enrichments.sql"
).read_text(encoding="utf-8")


class PlanningMemoryMigrationTests(unittest.TestCase):
    def test_receipt_is_minimal_append_only_rls_and_run_bound(self) -> None:
        self.assertIn("CREATE TABLE case_agent_planning_memory_enrichments", SQL)
        self.assertIn("REFERENCES case_agent_memory_retrieval_audits", SQL)
        self.assertIn("bound_run.created_by <> NEW.owner_actor_id", SQL)
        self.assertIn("goal.requested_by = NEW.owner_actor_id", SQL)
        self.assertIn("retrieval.actor_id <> NEW.owner_actor_id", SQL)
        self.assertIn("retrieval.query_hash <> NEW.query_hash", SQL)
        self.assertIn("query_contract_hash char(64) NOT NULL", SQL)
        self.assertIn("retrieval.scope_hash <> NEW.retrieval_scope_hash", SQL)
        self.assertIn("retrieval.matter_version <> NEW.case_snapshot_version", SQL)
        self.assertIn("matter.version = NEW.case_snapshot_version", SQL)
        self.assertIn("worker_role.role = 'SYSTEM_WORKER'", SQL)
        self.assertIn("current_owner_roles <> NEW.owner_roles", SQL)
        self.assertIn(
            "current_permission_group_ids <> NEW.permission_group_ids", SQL
        )
        self.assertIn("final_verified_at >= verified_at", SQL)
        self.assertIn("jsonb_array_length(items) BETWEEN 1 AND 20", SQL)
        self.assertIn("octet_length(item->>'summary') NOT BETWEEN 1 AND 300", SQL)
        self.assertIn("append-only", SQL)
        self.assertIn("FORCE ROW LEVEL SECURITY", SQL)
        self.assertIn("source->>'exposure' <> 'CASE_PRIVATE'", SQL)
        table_body = SQL.split(
            "CREATE TABLE case_agent_planning_memory_enrichments (", 1
        )[1].split(");", 1)[0]
        for forbidden in (
            "query_text",
            "search_document",
            "object_key",
            "source_url",
        ):
            self.assertNotIn(forbidden, table_body)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        self.assertTrue(parse_sql(SQL))


if __name__ == "__main__":
    unittest.main()
