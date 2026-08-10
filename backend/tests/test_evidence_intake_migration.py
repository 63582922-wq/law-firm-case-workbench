from pathlib import Path
import unittest


class EvidenceIntakeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1] / "migrations" / "0011_evidence_intake_runs.sql"
        ).read_text(encoding="utf-8")

    def test_runs_and_items_are_tenant_scoped_and_rls_forced(self) -> None:
        for table in ("evidence_intake_runs", "evidence_intake_items"):
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE {table}", self.sql)
                self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
                self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)
                self.assertIn(f"CREATE TRIGGER {table}_no_delete", self.sql)

    def test_database_never_persists_folder_grant_or_absolute_root(self) -> None:
        lowered = self.sql.lower()
        self.assertNotIn("folder_grant_id", lowered)
        self.assertNotIn("absolute_path", lowered)
        self.assertNotIn("selected_root", lowered)
        self.assertIn("relative_path", lowered)
        self.assertIn("scan_manifest_hash", lowered)

    def test_items_require_leases_and_explicit_terminal_outcomes(self) -> None:
        self.assertIn("'REGISTERED', 'REVIEW_REQUIRED', 'BLOCKED', 'FAILED', 'STALE'", self.sql)
        self.assertIn("lease_expires_at", self.sql)
        self.assertIn("scanner_definitions_version", self.sql)
        self.assertIn("evidence_intake_runs_one_active_per_scan", self.sql)


if __name__ == "__main__":
    unittest.main()
