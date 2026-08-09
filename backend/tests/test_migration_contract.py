from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_core.sql"


class CoreMigrationContractTests(unittest.TestCase):
    """Structural guardrails until a dedicated PostgreSQL CI service is added."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_core_tables_and_tenant_keys_are_present(self) -> None:
        for table in (
            "firms",
            "users",
            "matters",
            "matter_actor_roles",
            "approvals",
            "submission_bundles",
            "command_idempotency",
            "audit_events",
            "outbox_events",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
        self.assertGreaterEqual(self.sql.count("firm_id uuid NOT NULL"), 8)

    def test_tenant_row_level_security_is_required_for_every_case_table(self) -> None:
        for table in (
            "users",
            "matters",
            "matter_actor_roles",
            "approvals",
            "submission_bundles",
            "command_idempotency",
            "audit_events",
            "outbox_events",
        ):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)
        self.assertIn("current_setting('app.firm_id', true)", self.sql)
        self.assertGreaterEqual(self.sql.count("WITH CHECK (firm_id::text = current_setting('app.firm_id', true))"), 8)

    def test_submission_approval_audit_and_idempotency_invariants_are_encoded(self) -> None:
        self.assertIn("submission_bundles_one_current_valid_locked_per_matter", self.sql)
        self.assertIn("UNIQUE (matter_id, approval_type, approved_matter_version)", self.sql)
        self.assertIn("UNIQUE (firm_id, matter_id, actor_id, command_name, idempotency_key)", self.sql)
        self.assertIn("CREATE TRIGGER audit_events_append_only", self.sql)
        self.assertIn("RAISE EXCEPTION 'audit_events are append-only'", self.sql)
        self.assertIn("FOREIGN KEY (matter_id, firm_id) REFERENCES matters (matter_id, firm_id)", self.sql)


if __name__ == "__main__":
    unittest.main()
