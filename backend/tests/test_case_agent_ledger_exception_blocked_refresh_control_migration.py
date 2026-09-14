from pathlib import Path
import unittest


class CaseAgentLedgerExceptionBlockedRefreshControlMigrationTests(unittest.TestCase):
    def test_blocked_open_exception_refresh_remains_eligible_for_control(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0064_case_agent_ledger_exception_blocked_refresh_control.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("BLOCKED_BY_OPEN_EXCEPTIONS", sql)
        self.assertIn("lawcase_ledger_confirmation_owner", sql)
        self.assertIn("occurrence_count <> 1", sql)
        self.assertIn("pg_get_functiondef", sql)


if __name__ == "__main__":
    unittest.main()
