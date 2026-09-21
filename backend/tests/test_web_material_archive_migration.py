from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0027_web_material_archive_uploads.sql"


class WebMaterialArchiveMigrationTests(unittest.TestCase):
    def test_archive_state_machine_is_rls_scoped_and_never_claims_pdf_completion(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE web_material_archive_uploads", sql)
        self.assertIn("CREATE TABLE web_material_archive_upload_events", sql)
        self.assertIn("ALTER TABLE web_material_archive_uploads FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("'OBJECT_STORED'", sql)
        self.assertIn("'RECONCILIATION_REQUIRED'", sql)
        self.assertIn("STORED_PENDING_PROCESSING", sql)  # documentation/contract marker for the API boundary
        self.assertNotIn("evidence_original_files", sql)

