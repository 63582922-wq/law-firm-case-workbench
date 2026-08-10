from __future__ import annotations

from pathlib import Path
import unittest


class SubmissionWorkProductReviewBindingMigrationTests(unittest.TestCase):
    def test_review_hash_is_added_without_rewriting_existing_immutable_records(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0013_submission_work_product_review_binding.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN review_input_hash", migration)
        self.assertIn("review_input_hash ~ '^[0-9a-f]{64}$'", migration)
        self.assertIn("WHERE review_input_hash IS NOT NULL", migration)
        self.assertNotIn("DELETE FROM submission_work_products", migration)


if __name__ == "__main__":
    unittest.main()
