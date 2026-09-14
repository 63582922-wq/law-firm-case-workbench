from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VisualOcrUnknownPhaseMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "migrations" / "0056_case_agent_visual_ocr_unknown_phase.sql"
        ).read_text(encoding="utf-8")

    def test_unknown_outcome_remains_lookup_only_with_controlled_stage(self):
        self.assertIn("status = 'UNKNOWN_SUBMISSION'", self.sql)
        for code in (
            "QWEN_VISUAL_OCR_OUTCOME_UNKNOWN",
            "QWEN_VISUAL_OCR_UNKNOWN_DNS",
            "QWEN_VISUAL_OCR_UNKNOWN_CONNECT",
            "QWEN_VISUAL_OCR_UNKNOWN_SEND",
            "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
            "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY",
        ):
            self.assertIn(code, self.sql)

    def test_unknown_receipt_cannot_store_response_or_provider_fields(self):
        for field in (
            "provider_request_id IS NULL",
            "response_sha256 IS NULL",
            "response_bytes IS NULL",
            "response_body IS NULL",
        ):
            self.assertIn(field, self.sql)
        lowered = self.sql.lower()
        self.assertNotIn("exception_text", lowered)
        self.assertNotIn("api_key", lowered)

    def test_named_constraint_is_replaced_atomically(self):
        self.assertIn("BEGIN;", self.sql)
        self.assertIn("COMMIT;", self.sql)
        self.assertIn(
            "DROP CONSTRAINT case_agent_visual_ocr_outcomes_check",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
