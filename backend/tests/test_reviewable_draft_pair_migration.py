from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0015_reviewable_office_draft_pairs.sql"


class ReviewableDraftPairMigrationTests(unittest.TestCase):
    def test_pair_requires_both_content_addressed_objects_and_exact_approval_transition(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE reviewable_office_draft_pairs", sql)
        self.assertIn("editable_object_key", sql)
        self.assertIn("review_pdf_object_key", sql)
        self.assertIn("render_verification_hash", sql)
        self.assertIn("UNIQUE (matter_id, review_input_hash)", sql)
        self.assertIn("approval_hash IS NOT NULL", sql)
        self.assertIn("IS DISTINCT FROM OLD.review_input_hash", sql)
        self.assertIn("permit only exact lawyer approval", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("TG_OP = 'DELETE'", sql)


if __name__ == "__main__":
    unittest.main()
