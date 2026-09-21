from __future__ import annotations

from pathlib import Path
import unittest


class ActivePlanPreTaskReissueMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0108_active_plan_pre_task_reissue.sql"
        ).read_text(encoding="utf-8")

    def test_reissue_requires_a_proven_zero_external_pre_task_failure(self) -> None:
        for fragment in (
            "run.failure_code = 'PLANNER_PROPOSAL_REJECTED'",
            "run.current_graph_id IS NULL",
            "attempt.status = 'FAILED'",
            "case_agent_tasks",
            "case_agent_planning_external_events",
            "case_agent_external_submissions",
        ):
            self.assertIn(fragment, self.sql)

    def test_current_subset_guard_retains_the_safe_reissue_predicate(self) -> None:
        self.assertIn("pg_get_functiondef('public.guard_case_agent_active_plan_execution_run()'", self.sql)
        self.assertIn("case_agent_local_plan_reissue_allowed(predecessor.run_id", self.sql)
        self.assertIn("case_agent_planning_external_events", self.sql)
        self.assertIn("REVOKE ALL ON FUNCTION public.guard_case_agent_active_plan_execution_run()", self.sql)


class ActivePlanCompileFailureReissueMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0109_active_plan_compile_failure_reissue.sql"
        ).read_text(encoding="utf-8")

    def test_local_compilation_recovery_allows_only_one_zero_external_attempt(self) -> None:
        self.assertIn("attempt.status IN ('FAILED', 'SUCCEEDED')", self.sql)
        self.assertIn("run.current_graph_id IS NULL", self.sql)
        self.assertIn("case_agent_tasks", self.sql)
        self.assertIn("case_agent_planning_external_events", self.sql)
        self.assertIn("case_agent_external_submissions", self.sql)


class ActivePlanPreapprovalPolicyReissueMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0110_active_plan_preapproval_policy_reissue.sql"
        ).read_text(encoding="utf-8")

    def test_only_one_untouched_local_document_graph_can_be_reissued(self) -> None:
        for fragment in (
            "run.status = 'WAITING_APPROVAL'",
            "task.skill_id NOT IN",
            "task.network_policy <> 'DENY'",
            "task.external_request_approval_required",
            "NOT task.writes_managed_derivatives",
            "prior_execution.run_id <> p_run",
            "case_agent_task_receipts",
            "case_agent_planning_external_events",
            "case_agent_external_submissions",
            "predecessor_run.status IN (''WAITING_INPUT'', ''WAITING_APPROVAL'')",
        ):
            self.assertIn(fragment, self.sql)


class ActivePlanLocalDocumentPackageGateMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0111_active_plan_local_document_package_gate.sql"
        ).read_text(encoding="utf-8")

    def test_package_insert_remains_limited_to_local_review_candidates(self) -> None:
        for fragment in (
            "task_row.execution_mode <> 'IN_PROCESS'",
            "task_row.network_policy <> 'DENY'",
            "task_row.allowed_domains <> '[]'::jsonb",
            "task_row.risk_level <> 'MEDIUM'",
            "task_row.autonomy_level <> 'A2_INTERNAL_REVERSIBLE'",
            "task_row.approval_gate <> 'NONE'",
            "task_row.external_request_approval_required",
            "task_row.retry_mode <> 'NEVER_AUTOMATIC'",
            "max_external_calls')::integer, -1) <> 0",
            "dynamic_document_delivery",
            "dynamic_spreadsheet_delivery",
        ):
            self.assertIn(fragment, self.sql)


if __name__ == "__main__":
    unittest.main()
