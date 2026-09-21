from pathlib import Path
import unittest


class CaseAgentLedgerExceptionReasonNoteConstraintMigrationTests(unittest.TestCase):
    def test_constraint_uses_real_control_character_ranges(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0063_case_agent_ledger_exception_reason_note_constraint.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("reason_note !~ '[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]'", sql)
        self.assertNotIn("reason_note !~ '[\\\\x00-\\\\x08", sql)
        self.assertIn("length(reason_note) BETWEEN 1 AND 500", sql)
        self.assertIn("octet_length(reason_note) <= 2000", sql)


if __name__ == "__main__":
    unittest.main()
