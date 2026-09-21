from __future__ import annotations

from pathlib import Path
import unittest


class CaseAgentRecoveryGoalBindingMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0052_case_agent_recovery_goal_binding.sql"
        ).read_text(encoding="utf-8")

    def test_binding_is_server_derived_append_only_state(self) -> None:
        self.assertIn(
            "ADD COLUMN recovery_goal_id uuid", self.sql
        )
        self.assertIn(
            "ADD COLUMN recovery_goal_hash char(64)", self.sql
        )
        self.assertIn(
            "NEW.recovery_goal_id := NEW.replacement_run_id", self.sql
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_goal_hash_exact", self.sql
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_intents_append_only", self.sql
        )

    def test_upgrade_rejects_noncanonical_live_recovery_runs(self) -> None:
        self.assertIn(
            "0052 upgrade requires non-canonical recovery runs to be drained",
            self.sql,
        )
        self.assertIn(
            "intent_head.current_outcome IN ('PENDING', 'TRANSFERRED')",
            self.sql,
        )
        self.assertIn("goal.goal_hash IS DISTINCT FROM", self.sql)

    def test_prepare_transfer_and_run_wake_all_fail_closed(self) -> None:
        self.assertIn(
            "ledger exception recovery run goal differs during resume", self.sql
        )
        self.assertIn(
            "ledger exception recovery run goal differs during transfer", self.sql
        )
        self.assertIn(
            "case_agent_runs_recovery_goal_binding_guard", self.sql
        )
        self.assertIn(
            "case Agent recovery run goal differs from its immutable binding",
            self.sql,
        )

    def test_intent_and_generic_run_share_a_pre_visibility_transaction_lock(self) -> None:
        lock_key = "CASE_AGENT_RECOVERY_GOAL_BINDING|"
        self.assertEqual(self.sql.count(lock_key), 2)
        guard_start = self.sql.index(
            "CREATE FUNCTION public.guard_case_agent_recovery_run_goal_binding()"
        )
        guard_lock = self.sql.index(lock_key, guard_start)
        guard_check = self.sql.index("IF EXISTS (", guard_start)
        self.assertLess(guard_lock, guard_check)
        bind_start = self.sql.index(
            "CREATE FUNCTION public.bind_case_agent_ledger_exception_recovery_goal()"
        )
        bind_lock = self.sql.index(lock_key, bind_start)
        bind_assignment = self.sql.index(
            "NEW.recovery_goal_id := NEW.replacement_run_id",
            bind_start,
        )
        self.assertLess(bind_lock, bind_assignment)

    def test_application_roles_cannot_call_v2_inner_or_hash_helpers(self) -> None:
        self.assertIn("case_agent_prepare_recovery_v2_inner", self.sql)
        self.assertIn("case_agent_transfer_recovery_v2_inner", self.sql)
        self.assertIn(
            "FROM PUBLIC, lawcase_web_application, lawcase_agent_worker",
            self.sql,
        )
        self.assertIn(
            "GRANT SELECT (actor_id, recovery_goal_id, recovery_goal_hash)",
            self.sql,
        )


if __name__ == "__main__":
    unittest.main()
