from pathlib import Path
import unittest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0016_agent_execution_ledger.sql"


class AgentExecutionMigrationTests(unittest.TestCase):
    def test_agent_plans_and_receipts_are_case_scoped_and_append_only(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE agent_runs", sql)
        self.assertIn("CREATE TABLE agent_action_proposals", sql)
        self.assertIn("CREATE TABLE agent_tool_execution_receipts", sql)
        self.assertIn("policy_manifest_hash", sql)
        self.assertIn("input_matter_version", sql)
        self.assertIn("required_scopes jsonb", sql)
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("agent execution ledger is append-only", sql)


if __name__ == "__main__":
    unittest.main()
