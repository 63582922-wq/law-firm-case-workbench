from pathlib import Path
import unittest


class CaseAgentFollowupReasonNoteControlCharsMigrationTests(unittest.TestCase):
    def test_followup_command_and_event_constraint_use_single_escape(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0067_case_agent_followup_reason_note_control_chars.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("old_clause constant text", sql)
        self.assertIn("new_clause constant text", sql)
        self.assertIn("occurrence_count <> 1", sql)
        self.assertIn(
            "case_agent_ledger_exception_followup_events_reason_note_check",
            sql,
        )
        self.assertIn("reason_note !~ '[\\x00-\\x08", sql)


if __name__ == "__main__":
    unittest.main()
