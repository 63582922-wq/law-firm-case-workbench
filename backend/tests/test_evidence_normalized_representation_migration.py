from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0012_evidence_normalized_representations.sql"


class EvidenceNormalizedRepresentationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_representation_is_tenant_scoped_immutable_and_bound_to_the_original(self) -> None:
        self.assertIn("CREATE TABLE evidence_normalized_representations", self.sql)
        self.assertIn("REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)", self.sql)
        self.assertIn("UNIQUE (evidence_file_id)", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("evidence_normalized_representations_firm_isolation", self.sql)
        self.assertIn("evidence_normalized_representations_no_update", self.sql)

    def test_only_content_addressed_encrypted_pdf_representations_are_allowed(self) -> None:
        self.assertIn("normalized_media_type = 'application/pdf'", self.sql)
        self.assertIn("source_media_type <> 'application/pdf'", self.sql)
        self.assertIn("storage_object_key ~ '^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}", self.sql)
        self.assertIn("transform_hash", self.sql)
        self.assertIn("artifact_sha256", self.sql)


if __name__ == "__main__":
    unittest.main()
