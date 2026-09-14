from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrKnownFailureMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "migrations" / "0055_case_agent_visual_ocr_known_failure.sql"
        ).read_text(encoding="utf-8")

    def test_terminal_failure_is_added_without_weakening_unknown(self):
        self.assertIn(
            "status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN_SUBMISSION')",
            self.sql,
        )
        self.assertIn("status = 'FAILED'", self.sql)
        self.assertIn("status = 'UNKNOWN_SUBMISSION'", self.sql)
        self.assertIn("QWEN_VISUAL_OCR_OUTCOME_UNKNOWN", self.sql)

    def test_failed_row_keeps_only_controlled_code_and_request_binding(self):
        self.assertIn("^QWEN_VISUAL_OCR_[A-Z0-9_]{3,57}$", self.sql)
        for field in (
            "provider_request_id IS NULL",
            "response_sha256 IS NULL",
            "response_bytes IS NULL",
            "response_body IS NULL",
        ):
            self.assertIn(field, self.sql)
        self.assertNotIn("api_key", self.sql.lower())

    def test_existing_named_constraints_are_replaced_atomically(self):
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn(
            "DROP CONSTRAINT case_agent_visual_ocr_outcomes_status_check",
            self.sql,
        )
        self.assertIn(
            "DROP CONSTRAINT case_agent_visual_ocr_outcomes_check",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
