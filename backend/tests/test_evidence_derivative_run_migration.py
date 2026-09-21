from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0004_evidence_derivative_runs.sql"


class EvidenceDerivativeRunMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_run_table_is_tenant_scoped_and_forces_rls(self) -> None:
        self.assertIn("CREATE TABLE evidence_derivative_runs", self.sql)
        self.assertIn("FOREIGN KEY (matter_id, firm_id)", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("evidence_derivative_runs_firm_isolation", self.sql)

    def test_recovery_states_are_lease_bound_and_attempt_limited(self) -> None:
        self.assertIn("'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'STALE'", self.sql)
        self.assertIn("attempt_count <= 3", self.sql)
        self.assertIn("status = 'RUNNING' AND attempt_count > 0 AND lease_id IS NOT NULL", self.sql)
        self.assertIn("evidence_derivative_runs_one_active_per_manifest", self.sql)
        self.assertIn("evidence_derivative_runs_one_success_per_manifest", self.sql)

    def test_success_requires_both_hash_verified_artifact_references(self) -> None:
        self.assertIn("related_derivative_id IS NOT NULL AND annotated_derivative_id IS NOT NULL", self.sql)
        self.assertIn("REFERENCES evidence_derivative_artifacts(derivative_id, firm_id, matter_id)", self.sql)
        self.assertIn("manifest_content_hash", self.sql)
        self.assertIn("approval_hash", self.sql)


if __name__ == "__main__":
    unittest.main()
