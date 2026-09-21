from pathlib import Path
import unittest


class LegalSourceLicenseMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0009_legal_source_license_review.sql"
        ).read_text(encoding="utf-8")

    def test_license_basis_and_hash_are_added_as_an_atomic_pair(self) -> None:
        self.assertIn("ADD COLUMN license_basis text", self.sql)
        self.assertIn("ADD COLUMN license_review_hash char(64)", self.sql)
        self.assertIn("official_legal_source_license_review_pair", self.sql)
        self.assertIn("license_basis IS NULL AND license_review_hash IS NULL", self.sql)
        self.assertIn("length(trim(license_basis)) > 0", self.sql)

    def test_legacy_rows_are_not_silently_fabricated_with_a_license_basis(self) -> None:
        self.assertNotIn("UPDATE official_legal_source_snapshots", self.sql)
        self.assertIn("legacy rows without this pair cannot support new rule approvals", self.sql)


if __name__ == "__main__":
    unittest.main()
