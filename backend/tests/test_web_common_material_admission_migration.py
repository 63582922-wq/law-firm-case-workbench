from __future__ import annotations

from pathlib import Path
import unittest


class WebCommonMaterialAdmissionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0041_web_common_material_admission.sql"
        ).read_text(encoding="utf-8")

    def test_material_object_upload_and_append_only_event_ledgers_are_rls_forced(self) -> None:
        for table in (
            "web_common_material_uploads",
            "case_material_objects",
            "web_common_material_upload_events",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY", self.sql)
            self.assertIn(f"REVOKE ALL ON TABLE {table} FROM PUBLIC", self.sql)
        self.assertIn("case_material_objects_immutable", self.sql)
        self.assertIn("web_common_material_upload_events_append_only", self.sql)

    def test_exact_supported_formats_and_explicit_routes_exclude_legacy_ole_ofd_and_pdf(self) -> None:
        for value in ("'DOCX'", "'XLSX'", "'PPTX'", "'RTF'", "'TXT'", "'CSV'", "'HTML'", "'EML'", "'JPEG'", "'PNG'"):
            self.assertIn(value, self.sql)
        for unsupported in ("'DOC'", "'XLS'", "'PPT'", "'MSG'", "'OFD'", "'PDF'"):
            self.assertNotIn(unsupported, self.sql)
        self.assertIn("'COMMON_DOCUMENT_READER'", self.sql)
        self.assertIn("'VISUAL_OCR'", self.sql)

    def test_review_only_flags_object_integrity_version_idempotency_audit_and_outbox_are_enforced(self) -> None:
        for boundary in (
            "expected_matter_version",
            "reserve_idempotency_key",
            "content_idempotency_key",
            "admitted_content_sha256",
            "admitted_inspection_hash",
            "source_object_version_id",
            "source_reference_hash",
            "original_locked boolean NOT NULL CHECK (original_locked = true)",
            "formal_fact boolean NOT NULL CHECK (formal_fact = false)",
            "formal_transaction boolean NOT NULL CHECK (formal_transaction = false)",
            "legal_conclusion boolean NOT NULL CHECK (legal_conclusion = false)",
            "evidence_decision boolean NOT NULL CHECK (evidence_decision = false)",
            "court_ready boolean NOT NULL CHECK (court_ready = false)",
            "JOIN audit_events",
            "JOIN outbox_events",
        ):
            self.assertIn(boundary, self.sql)
        self.assertIn("RECONCILIATION_REQUIRED", self.sql)
        self.assertIn("ADMISSION_UNAVAILABLE", self.sql)
        self.assertIn("failure_code = 'OBJECT_STATE_UNKNOWN' AND object_stored_at IS NULL", self.sql)
        self.assertIn("source_reference_hash IS NOT NULL", self.sql)
        self.assertIn("terminal common material uploads are immutable", self.sql)
        self.assertIn("source_object_key", self.sql)
        self.assertIn("Private server locator", self.sql)


if __name__ == "__main__":
    unittest.main()
