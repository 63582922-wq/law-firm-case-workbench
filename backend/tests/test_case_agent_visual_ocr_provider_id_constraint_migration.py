from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrProviderIdConstraintMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT
            / "migrations"
            / "0057_case_agent_visual_ocr_provider_id_constraint.sql"
        ).read_text(encoding="utf-8")

    def test_provider_id_uses_postgresql_safe_character_and_length_guards(self):
        self.assertIn(
            "provider_request_id ~ '^[A-Za-z0-9._:-]+$'",
            self.sql,
        )
        self.assertIn(
            "char_length(provider_request_id) BETWEEN 1 AND 500",
            self.sql,
        )
        self.assertNotIn("provider_request_id ~ '^[A-Za-z0-9._:-]{1,500}$'", self.sql)

    def test_existing_terminal_and_unknown_outcome_guards_are_preserved(self):
        for status in ("SUCCEEDED", "FAILED", "UNKNOWN_SUBMISSION"):
            self.assertIn(f"status = '{status}'", self.sql)
        for code in (
            "QWEN_VISUAL_OCR_OUTCOME_UNKNOWN",
            "QWEN_VISUAL_OCR_UNKNOWN_DNS",
            "QWEN_VISUAL_OCR_UNKNOWN_CONNECT",
            "QWEN_VISUAL_OCR_UNKNOWN_SEND",
            "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
            "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY",
        ):
            self.assertIn(code, self.sql)

    def test_constraint_replacement_is_atomic(self):
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn(
            "DROP CONSTRAINT case_agent_visual_ocr_outcomes_check",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
