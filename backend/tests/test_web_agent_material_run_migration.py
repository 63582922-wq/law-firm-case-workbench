from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0028_web_agent_material_runs.sql"


class WebAgentMaterialRunMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_all_agent_state_is_forced_rls_and_candidate_data_is_append_only(self) -> None:
        tables = (
            "web_agent_material_runs",
            "web_agent_material_tasks",
            "web_agent_material_page_bindings",
            "web_agent_material_candidates",
            "web_agent_material_commands",
            "web_agent_material_events",
        )
        for table in tables:
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE {table}", self.sql)
                self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)
        for table in (
            "web_agent_material_page_bindings",
            "web_agent_material_candidates",
            "web_agent_material_commands",
            "web_agent_material_events",
        ):
            self.assertIn(f"{table}_append_only", self.sql)

    def test_external_execution_is_exactly_authorized_and_recoverably_claimed(self) -> None:
        self.assertIn("external_request_id uuid", self.sql)
        self.assertIn("UNIQUE (external_request_id)", self.sql)
        self.assertIn("REFERENCES external_request_authorizations", self.sql)
        self.assertIn("status text NOT NULL CHECK (status IN ('QUEUED', 'CLAIMED', 'RUNNING'", self.sql)
        self.assertIn("attempt_count integer NOT NULL DEFAULT 0", self.sql)
        self.assertIn("lease_id uuid", self.sql)
        self.assertIn("lease_expires_at timestamptz", self.sql)
        self.assertIn("provider_request_hash", self.sql)
        self.assertIn("status = 'CLAIMED' AND attempt_count > 0 AND external_request_id IS NOT NULL", self.sql)
        self.assertIn("status = 'RUNNING' AND attempt_count > 0 AND external_request_id IS NOT NULL", self.sql)

    def test_representation_context_is_bound_but_no_fixed_work_product_mapping_exists(self) -> None:
        self.assertIn("representation_profile_version bigint", self.sql)
        self.assertIn("representation_profile_hash char(64)", self.sql)
        self.assertIn("MATERIAL_NEUTRAL_REVIEW", self.sql)
        self.assertIn("CHECK (agent_intent = 'MATERIAL_NEUTRAL_REVIEW')", self.sql)
        self.assertNotIn("work_product", self.sql.lower())
        self.assertNotIn("document_template", self.sql.lower())

    def test_exact_page_bindings_cover_current_run_boundary(self) -> None:
        self.assertIn("sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 64)", self.sql)
        self.assertIn("FOREIGN KEY (evidence_page_id, firm_id, matter_id)", self.sql)
        self.assertIn("status text NOT NULL CHECK (status = 'NEEDS_REVIEW')", self.sql)


if __name__ == "__main__":
    unittest.main()
