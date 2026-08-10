from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0014_office_render_verification.sql"


class OfficeRenderVerificationMigrationTests(unittest.TestCase):
    def test_office_render_receipt_is_immutable_and_only_accepts_sha256_or_null(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN render_verification_hash char(64)", sql)
        self.assertIn("render_verification_hash IS NULL", sql)
        self.assertIn("^[0-9a-f]{64}$", sql)


if __name__ == "__main__":
    unittest.main()
