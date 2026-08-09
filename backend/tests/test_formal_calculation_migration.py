from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0005_formal_calculations.sql"


class FormalCalculationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_every_calculation_table_is_tenant_scoped_and_forces_rls(self) -> None:
        tables = (
            "case_legal_bundles",
            "case_legal_bundle_rule_versions",
            "calculation_scenarios",
            "calculation_scenario_events",
            "calculation_rule_segments",
            "calculation_runs",
            "calculation_line_items",
            "calculation_payment_allocations",
        )
        for table in tables:
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_formal_scenario_is_cny_approval_and_bundle_bound(self) -> None:
        self.assertIn("currency char(3) NOT NULL CHECK (currency = 'CNY')", self.sql)
        self.assertIn("legal_bundle_hash", self.sql)
        self.assertIn("transaction_snapshot_hash", self.sql)
        self.assertIn("approval_hash", self.sql)
        self.assertIn("calculation_scenarios_one_approved_per_obligation", self.sql)

    def test_run_requires_independent_check_and_persists_each_period_and_payment(self) -> None:
        self.assertIn("independent_check_hash", self.sql)
        self.assertIn("calculation_runs_one_verified_per_scenario", self.sql)
        self.assertIn("CREATE TABLE calculation_line_items", self.sql)
        self.assertIn("CREATE TABLE calculation_payment_allocations", self.sql)
        self.assertIn("same_day_sequence", self.sql)
        self.assertIn("payment_application", self.sql)


if __name__ == "__main__":
    unittest.main()
