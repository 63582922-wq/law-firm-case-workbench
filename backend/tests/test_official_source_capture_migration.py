from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0008_official_source_capture_runs.sql"


class OfficialSourceCaptureMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_capture_runs_and_reviews_force_tenant_rls(self) -> None:
        for table in ("official_source_capture_runs", "official_source_capture_reviews"):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"CREATE POLICY {table}_firm_isolation", self.sql)

    def test_run_is_one_attempt_and_requires_encrypted_hash_bound_result(self) -> None:
        self.assertIn("attempt_count IN (0, 1)", self.sql)
        self.assertIn("status = 'REVIEW_REQUIRED'", self.sql)
        self.assertIn("storage_object_key = substring(content_sha256", self.sql)
        self.assertIn("capture_verification_hash", self.sql)
        self.assertIn("parsed_output_hash", self.sql)

    def test_review_is_append_only_and_separate_from_capture_success(self) -> None:
        self.assertIn("APPROVE_FOR_REGISTRATION", self.sql)
        self.assertIn("official_source_capture_reviews_append_only", self.sql)
        self.assertIn("UNIQUE (run_id)", self.sql)

    def test_formal_source_can_bind_one_reviewed_capture_run(self) -> None:
        self.assertIn("ADD COLUMN capture_run_id uuid", self.sql)
        self.assertIn("official_legal_source_snapshots_capture_run_fk", self.sql)
        self.assertIn("official_legal_source_snapshots_capture_run_idx", self.sql)


if __name__ == "__main__":
    unittest.main()
