from __future__ import annotations

from pathlib import Path
import unittest


class SupplementaryEvidenceChecklistMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0112_supplementary_evidence_checklist_delivery.sql"
        ).read_text(encoding="utf-8")

    def test_extends_the_closed_catalogue_without_granting_external_authority(self) -> None:
        self.assertIn("SUPPLEMENTARY_EVIDENCE_CHECKLIST", self.sql)
        self.assertIn("jsonb_array_length(value) > 5", self.sql)
        self.assertIn("NOT BETWEEN 1 AND 5", self.sql)
        self.assertIn("item->>'output_format' <> 'DOCX'", self.sql)
        self.assertIn("requested = execution_kinds", self.sql)
        self.assertIn("TO lawcase_web_application;", self.sql)
        self.assertNotIn("GRANT INSERT", self.sql)
        self.assertNotIn("GRANT UPDATE", self.sql)
        self.assertNotIn("GRANT DELETE", self.sql)


if __name__ == "__main__":
    unittest.main()
