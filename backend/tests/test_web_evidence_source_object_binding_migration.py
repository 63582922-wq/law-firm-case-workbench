from __future__ import annotations

from pathlib import Path
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0025_web_evidence_source_objects.sql"
)


class WebEvidenceSourceObjectBindingMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_private_binding_is_exactly_tenant_scoped_and_integrity_bound_to_original(self) -> None:
        self.assertIn("CREATE TABLE web_evidence_original_source_objects", self.sql)
        self.assertIn("FOREIGN KEY (evidence_file_id, firm_id, matter_id)", self.sql)
        self.assertIn("REFERENCES evidence_original_files(evidence_file_id, firm_id, matter_id)", self.sql)
        self.assertIn("source.original_file_sha256 = NEW.source_object_sha256", self.sql)
        self.assertIn("source.byte_size = NEW.source_object_bytes", self.sql)
        self.assertIn("source.media_type = 'application/pdf'", self.sql)
        self.assertIn("source_reference_hash = encode(digest(source_object_key, 'sha256'), 'hex')", self.sql)
        self.assertIn("split_part(source_object_key, '/', 3) = firm_id::text", self.sql)
        self.assertIn("split_part(source_object_key, '/', 4) = matter_id::text", self.sql)

    def test_binding_is_append_only_rls_protected_and_not_a_public_locator(self) -> None:
        self.assertIn("web_evidence_original_source_objects_append_only", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE ON web_evidence_original_source_objects", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("web_evidence_original_source_objects_firm_isolation", self.sql)
        self.assertIn("REVOKE ALL ON TABLE web_evidence_original_source_objects FROM PUBLIC", self.sql)
        self.assertIn("must never be selected into evidence", self.sql)
        self.assertIn("HTTP responses", self.sql)
        self.assertIn("only a proven-unbound object", self.sql)
        self.assertNotIn("CREATE POLICY web_evidence_original_source_objects_public", self.sql)

    def test_migration_is_additive_and_preserves_desktop_folder_semantics(self) -> None:
        self.assertIn("This is deliberately additive", self.sql)
        self.assertIn("Existing desktop local-folder originals keep", self.sql)
        self.assertNotIn("ALTER TABLE evidence_original_files ADD COLUMN", self.sql)
        self.assertNotIn("DELETE FROM evidence_original_files", self.sql)
        self.assertNotIn("UPDATE evidence_original_files", self.sql)


if __name__ == "__main__":
    unittest.main()
