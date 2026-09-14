from __future__ import annotations

from pathlib import Path
import re
import unittest


class CaseAgentActivePlanExecutionMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0053_active_work_plan_agent_execution.sql"
        ).read_text(encoding="utf-8")

    def test_check_constraints_call_helpers_created_first(self) -> None:
        requested_helper = self.sql.index(
            "CREATE FUNCTION public.case_agent_requested_deliverables_valid"
        )
        execution_helper = self.sql.index(
            "CREATE FUNCTION public.case_agent_active_plan_execution_valid"
        )
        constraint = self.sql.index(
            "ADD CONSTRAINT case_agent_goals_requested_deliverables_shape"
        )
        self.assertLess(requested_helper, constraint)
        self.assertLess(execution_helper, constraint)
        constraint_block = self.sql[constraint : self.sql.index("CREATE TABLE", constraint)]
        self.assertNotRegex(constraint_block, re.compile(r"CHECK\s*\([^;]*SELECT", re.S))
        self.assertNotIn("DROP CONSTRAINT", self.sql)

    def test_goal_shape_is_exact_canonical_and_first_release_only(self) -> None:
        self.assertIn(
            "ARRAY['deliverable_kind','item_hash','item_id','output_format']::text[]",
            self.sql,
        )
        self.assertIn("CASE_REVIEW_MEMO", self.sql)
        self.assertIn("PAYMENT_LEDGER", self.sql)
        self.assertIn("value->>'deliverable_kind', value->>'item_id'", self.sql)
        self.assertIn("count(DISTINCT value->>'item_id')", self.sql)

    def test_execution_binding_is_one_per_plan_and_append_only(self) -> None:
        self.assertIn("UNIQUE (plan_id, firm_id, matter_id)", self.sql)
        self.assertIn("UNIQUE (run_id, firm_id, matter_id)", self.sql)
        self.assertIn("CHECK (source_run_id <> run_id)", self.sql)
        self.assertIn(
            "activated_matter_version integer NOT NULL CHECK (activated_matter_version > 0)",
            self.sql,
        )
        self.assertIn(
            "case_agent_active_plan_execution_runs_append_only", self.sql
        )
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)

    def test_guard_binds_execution_goal_to_current_source_goal_and_active_plan(self) -> None:
        self.assertIn("JOIN public.case_agent_goals source_goal", self.sql)
        self.assertIn("source_goal.active_plan_execution IS NULL", self.sql)
        self.assertIn(
            "goal.requested_deliverables = source_goal.requested_deliverables",
            self.sql,
        )
        self.assertIn("plan.status = 'ACTIVE'", self.sql)
        self.assertIn("plan.activated_matter_version = matter.version", self.sql)
        self.assertIn(
            "plan.activated_matter_version = NEW.activated_matter_version", self.sql
        )
        self.assertIn("plan_item.readiness = 'ACTIONABLE'", self.sql)

    def test_security_definer_guard_never_trusts_row_tenant_context(self) -> None:
        guard = self.sql[
            self.sql.index("CREATE FUNCTION public.guard_case_agent_active_plan_execution_run") :
            self.sql.index("CREATE TRIGGER", self.sql.index("CREATE FUNCTION public.guard_case_agent_active_plan_execution_run"))
        ]
        self.assertIn(
            "session_firm_id := pg_catalog.current_setting('app.firm_id', true)",
            guard,
        )
        self.assertIn("session_firm_id <> NEW.firm_id::text", guard)
        self.assertNotIn("set_config", guard)
        self.assertLess(
            guard.index("session_firm_id <> NEW.firm_id::text"),
            guard.index("SELECT EXISTS"),
        )

    def test_runtime_acl_is_least_privilege_and_check_helpers_remain_usable(self) -> None:
        self.assertIn(
            "FROM PUBLIC, lawcase_web_application, lawcase_agent_worker;",
            self.sql,
        )
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION\n"
            "    public.case_agent_requested_deliverables_valid(jsonb),\n"
            "    public.case_agent_active_plan_execution_valid(jsonb, jsonb)\n"
            "    TO lawcase_web_application;",
            self.sql,
        )
        self.assertIn(
            "REVOKE INSERT ON TABLE public.case_agent_goals\n"
            "    FROM lawcase_agent_worker;",
            self.sql,
        )
        self.assertIn(
            "GRANT SELECT, INSERT ON TABLE public.case_agent_active_plan_execution_runs\n"
            "    TO lawcase_web_application;",
            self.sql,
        )
        self.assertIn(
            "GRANT UPDATE (version) ON TABLE public.matters TO lawcase_web_application;",
            self.sql,
        )
        self.assertIn(
            "GRANT UPDATE (run_id) ON TABLE public.case_agent_runs TO lawcase_web_application;",
            self.sql,
        )
        self.assertNotIn("GRANT UPDATE ON TABLE public.case_work_plan", self.sql)
        self.assertNotIn("GRANT DELETE", self.sql)


if __name__ == "__main__":
    unittest.main()
