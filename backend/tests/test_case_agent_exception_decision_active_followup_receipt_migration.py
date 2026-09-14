from pathlib import Path
import unittest


class CaseAgentExceptionDecisionActiveFollowupReceiptMigrationTests(
    unittest.TestCase
):
    def test_active_followup_is_a_valid_resolved_but_blocked_receipt(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0066_case_agent_exception_decision_active_followup_receipt.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("head.current_state = 'ACTIVE'", sql)
        self.assertIn("pending_refresh_count <> 0", sql)
        self.assertIn("pending_refresh_count <> 1", sql)
        self.assertIn("occurrence_count <> 1", sql)
        self.assertIn("pg_get_functiondef", sql)


if __name__ == "__main__":
    unittest.main()
