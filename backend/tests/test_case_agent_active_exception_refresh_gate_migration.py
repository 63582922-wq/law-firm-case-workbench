from pathlib import Path
import unittest


class CaseAgentActiveExceptionRefreshGateMigrationTests(unittest.TestCase):
    def test_active_followups_keep_snapshot_refresh_blocked(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0065_case_agent_active_exception_refresh_gate.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("NEW.decision <> 'REJECT_AS_DUPLICATE'", sql)
        self.assertGreaterEqual(sql.count("head.current_state = 'ACTIVE'"), 4)
        self.assertIn(
            "NEW.request_status := 'BLOCKED_BY_OPEN_EXCEPTIONS'",
            sql,
        )
        self.assertIn("NEW.request_status = 'PENDING'", sql)
        self.assertIn(
            "aa_case_agent_snapshot_refresh_active_followup_gate",
            sql,
        )


if __name__ == "__main__":
    unittest.main()
