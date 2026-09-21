from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0019_document_consistency_reviews.sql"


class DocumentConsistencyMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_review_records_are_tenant_scoped_append_only_and_keep_no_document_text(self) -> None:
        self.assertIn("CREATE TABLE document_consistency_reviews", self.sql)
        self.assertIn("CREATE TABLE document_consistency_review_documents", self.sql)
        self.assertIn("CREATE TABLE document_consistency_review_findings", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("prohibit_document_consistency_review_mutation", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)
        self.assertNotIn("document_text", self.sql)
        self.assertNotIn("canonical_value", self.sql)

    def test_submission_qa_requires_hash_bound_consistency_review_reference(self) -> None:
        self.assertIn("ADD COLUMN consistency_review_id", self.sql)
        self.assertIn("ADD COLUMN consistency_input_hash", self.sql)
        self.assertIn("ADD COLUMN consistency_output_hash", self.sql)
        self.assertIn("submission_compilation_specs_consistency_review_fk", self.sql)
        self.assertIn("status IN ('PASS', 'BLOCKED')", self.sql)
        self.assertIn("status = 'PASS' AND blocking_count = 0", self.sql)


if __name__ == "__main__":
    unittest.main()
