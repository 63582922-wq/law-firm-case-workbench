from pathlib import Path
import unittest


class CaseAgentActiveFollowupGateOwnerMigrationTests(unittest.TestCase):
    def test_gate_uses_the_isolated_followup_reader_owner(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0068_case_agent_active_followup_gate_owner.sql"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "gate_case_agent_snapshot_refresh_insert_for_active_followup()",
            sql,
        )
        self.assertIn("OWNER TO lawcase_ledger_confirmation_owner", sql)


if __name__ == "__main__":
    unittest.main()
