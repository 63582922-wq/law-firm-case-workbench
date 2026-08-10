from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0017_external_request_ledger.sql"


class ExternalRequestMigrationTests(unittest.TestCase):
    def test_preflight_and_attempts_are_isolated_append_only_and_unknown_safe(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE external_request_authorizations", sql)
        self.assertIn("CREATE TABLE external_request_attempts", sql)
        self.assertIn("processor_region", sql)
        self.assertIn("retention_policy", sql)
        self.assertIn("training_policy", sql)
        self.assertIn("selected_field_ids jsonb", sql)
        self.assertIn("UNKNOWN_SUBMISSION", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("external request ledger is append-only", sql)


if __name__ == "__main__":
    unittest.main()
