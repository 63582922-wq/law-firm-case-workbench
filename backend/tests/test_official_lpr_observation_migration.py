from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0020_official_lpr_observations.sql"


class OfficialLprObservationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_observations_are_source_hash_bound_tenant_scoped_and_append_only(self) -> None:
        self.assertIn("CREATE TABLE official_lpr_observations", self.sql)
        self.assertIn("FOREIGN KEY (snapshot_id, firm_id, content_sha256)", self.sql)
        self.assertIn("REFERENCES official_legal_source_snapshots(snapshot_id, firm_id, content_sha256)", self.sql)
        self.assertIn("UNIQUE (snapshot_id, source_locator)", self.sql)
        self.assertIn("ALTER TABLE official_lpr_observations FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("CREATE POLICY official_lpr_observations_firm_isolation", self.sql)
        self.assertIn("prohibit_official_lpr_observation_mutation", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)

    def test_observation_rate_and_interval_constraints_are_explicit(self) -> None:
        self.assertIn("one_year_rate > 0 AND one_year_rate < 1", self.sql)
        self.assertIn("five_year_plus_rate > 0 AND five_year_plus_rate < 1", self.sql)
        self.assertIn("CHECK (publication_date = effective_from)", self.sql)
        self.assertIn("CHECK (effective_until IS NULL OR effective_from < effective_until)", self.sql)


if __name__ == "__main__":
    unittest.main()
