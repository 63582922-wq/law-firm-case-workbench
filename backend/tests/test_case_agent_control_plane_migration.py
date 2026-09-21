from pathlib import Path
import unittest


class CaseAgentControlPlaneMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1] / "migrations" / "0031_agent_control_plane.sql"
        ).read_text(encoding="utf-8")

    def test_control_plane_contains_every_durable_domain(self) -> None:
        for table in (
            "case_agent_goals",
            "case_agent_runs",
            "case_agent_events",
            "case_agent_planning_attempts",
            "case_agent_planning_external_events",
            "case_agent_worker_heartbeats",
            "case_agent_task_graphs",
            "case_agent_tasks",
            "case_agent_task_dependencies",
            "case_agent_task_heads",
            "case_agent_task_attempts",
            "case_agent_approvals",
            "case_agent_task_receipts",
            "case_agent_external_submissions",
            "case_agent_artifacts",
            "case_agent_checkpoints",
            "case_agent_command_audits",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertIn(f"'{table}'", self.sql)
        self.assertIn("current_event_version bigint", self.sql)
        self.assertIn("UNIQUE (run_id, event_sequence)", self.sql)

    def test_every_control_plane_table_is_forced_rls(self) -> None:
        self.assertIn("ALTER TABLE %I FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("current_setting(''app.firm_id'', true)", self.sql)
        self.assertIn("_firm_isolation", self.sql)
        self.assertEqual(
            self.sql.count("'case_agent_planning_external_events'"),
            1,
            "the policy loop must not create the same PostgreSQL policy twice",
        )

    def test_history_is_append_only_and_heads_are_version_guarded(self) -> None:
        self.assertIn("case Agent goals, events, graphs, tasks, approvals, receipts", self.sql)
        self.assertIn("case_agent_runs_guard", self.sql)
        self.assertIn("NEW.current_event_version <> OLD.current_event_version + 1", self.sql)
        self.assertIn("case_agent_task_heads_guard", self.sql)
        self.assertIn("case_agent_task_attempts_guard", self.sql)
        self.assertIn("NEW.attempt_version <> OLD.attempt_version + 1", self.sql)
        self.assertIn("case_agent_command_audits", self.sql)
        self.assertIn("version_domain", (
            Path(__file__).parents[1] / "case_kernel" / "case_agent_postgres.py"
        ).read_text(encoding="utf-8"))

    def test_execution_contract_preserves_sandbox_retry_and_unknown_state(self) -> None:
        for fragment in (
            "sandbox_policy_hash",
            "resource_budget",
            "external_request_approval_required",
            "BEFORE_EXTERNAL_SUBMISSION_ONLY",
            "NEVER_AUTOMATIC",
            "external_submission_state",
            "submission_state text NOT NULL",
            "RECONCILIATION_REQUIRED",
            "UNKNOWN",
            "verification_hash",
            "artifact_manifest_hash",
        ):
            self.assertIn(fragment, self.sql)

    def test_legacy_agent_run_table_is_not_reused_as_the_new_aggregate(self) -> None:
        self.assertNotIn("ALTER TABLE agent_runs", self.sql)
        self.assertNotIn("REFERENCES agent_runs", self.sql)
        self.assertIn("CREATE TABLE case_agent_runs", self.sql)


if __name__ == "__main__":
    unittest.main()
