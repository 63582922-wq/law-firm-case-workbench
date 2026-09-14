from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrLocalFailureResolutionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT
            / "migrations"
            / "0058_case_agent_visual_ocr_local_failure_resolution.sql"
        ).read_text(encoding="utf-8")

    def test_resolution_is_narrow_and_preserves_original_unknown(self):
        self.assertIn("case_agent_visual_ocr_local_failure_resolutions", self.sql)
        self.assertIn("QWEN_VISUAL_OCR_LOCAL_RECEIPT_PERSISTENCE_FAILED", self.sql)
        self.assertIn("POSTGRES_ERROR_LOG", self.sql)
        self.assertIn("outcome_row.status IS DISTINCT FROM 'UNKNOWN_SUBMISSION'", self.sql)
        self.assertIn("QWEN_VISUAL_OCR_OUTCOME_UNKNOWN", self.sql)
        self.assertNotIn("UPDATE case_agent_visual_ocr_outcomes", self.sql)
        self.assertNotIn("DELETE FROM case_agent_visual_ocr_outcomes", self.sql)

    def test_resolution_is_append_only_rls_and_worker_read_only(self):
        self.assertIn("append_only", self.sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn(
            "GRANT SELECT ON TABLE case_agent_visual_ocr_local_failure_resolutions",
            self.sql,
        )
        self.assertNotIn(
            "GRANT INSERT ON TABLE case_agent_visual_ocr_local_failure_resolutions",
            self.sql,
        )

    def test_resolution_requires_active_matter_worker_and_evidence_hash(self):
        self.assertIn("worker.status = 'ACTIVE'", self.sql)
        self.assertIn("worker_role.role = 'SYSTEM_WORKER'", self.sql)
        self.assertIn("evidence_sha256 ~ '^[0-9a-f]{64}$'", self.sql)


if __name__ == "__main__":
    unittest.main()
