from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ActivePlanExecutionReissueMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            ROOT
            / "backend/migrations/0071_safe_local_active_plan_execution_reissue.sql"
        ).read_text(encoding="utf-8")

    def test_reissue_is_append_only_and_local_failure_only(self) -> None:
        self.assertIn("execution_attempt", self.sql)
        self.assertIn("supersedes_execution_id", self.sql)
        self.assertIn("SAFE_LOCAL_FAILURE_REISSUE", self.sql)
        self.assertIn("predecessor_run.status = 'WAITING_INPUT'", self.sql)
        self.assertIn("receipt.external_submission_state = 'NOT_APPLICABLE'", self.sql)
        self.assertIn("receipt.external_calls <> 0", self.sql)
        self.assertIn("receipt.result_status = 'UNKNOWN'", self.sql)
        self.assertIn("case_agent_active_plan_execution_runs_append_only", (
            ROOT / "backend/migrations/0053_active_work_plan_agent_execution.sql"
        ).read_text(encoding="utf-8"))

    def test_one_successor_per_failed_attempt_and_no_broad_runtime_grant(self) -> None:
        self.assertIn("UNIQUE (supersedes_execution_id)", self.sql)
        self.assertIn(
            "UNIQUE (plan_id, firm_id, matter_id, execution_attempt)", self.sql
        )
        self.assertNotIn("GRANT UPDATE", self.sql)
        self.assertNotIn("GRANT DELETE", self.sql)


if __name__ == "__main__":
    unittest.main()
