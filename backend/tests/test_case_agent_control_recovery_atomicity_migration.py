from pathlib import Path
import unittest


class CaseAgentControlRecoveryAtomicityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0050_case_agent_control_recovery_atomicity.sql"
        ).read_text(encoding="utf-8")

    def test_intent_is_append_only_and_has_one_pending_matter_head(self) -> None:
        self.assertIn(
            "case_agent_ledger_exception_recovery_intents", self.sql
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_intent_heads", self.sql
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_quarantines", self.sql
        )
        self.assertNotIn("UNIQUE (source_control_assignment_id)", self.sql)
        self.assertIn(
            "case_agent_ledger_exception_one_pending_recovery", self.sql
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_intents_append_only",
            self.sql,
        )
        self.assertIn("current_outcome = 'TRANSFERRED'", self.sql)
        self.assertIn("current_outcome = 'ABANDONED'", self.sql)
        self.assertIn("SUPERSEDED_BY_NEW_RECOVERY_INTENT", self.sql)

    def test_prepare_precedes_run_and_browser_never_supplies_authority_hash(self) -> None:
        self.assertIn(
            "prepare_case_agent_ledger_exception_control_recovery_from_web_session",
            self.sql,
        )
        self.assertIn("input_replacement_run_id uuid", self.sql)
        self.assertIn("input_request_hash text", self.sql)
        self.assertIn("current_row.current_state <> 'RECOVERY_REQUIRED'", self.sql)
        self.assertIn("FOR UPDATE OF intent_head", self.sql)
        self.assertIn("pending_row.replacement_run_id", self.sql)
        self.assertIn("CASE_LEDGER_EXCEPTION_CONTROL_CLAIM", self.sql)

    def test_new_prepare_can_abandon_and_park_a_lost_key_intent(self) -> None:
        prepare_start = self.sql.index(
            "CREATE FUNCTION public.prepare_case_agent_ledger_exception_control_recovery_from_web_session"
        )
        transfer_start = self.sql.index(
            "CREATE OR REPLACE FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session"
        )
        prepare = self.sql[prepare_start:transfer_start]
        self.assertIn("current_outcome = 'ABANDONED'", prepare)
        self.assertIn("outcome_web_session_id = input_session_id", prepare)
        self.assertIn("inbox_status = 'QUIET'", prepare)
        self.assertLess(
            prepare.index("CASE_LEDGER_EXCEPTION_CONTROL_CLAIM"),
            prepare.index("FROM public.matters matter"),
        )

    def test_transfer_and_claim_share_lock_and_inboxes_move_atomically(self) -> None:
        transfer_start = self.sql.index(
            "CREATE OR REPLACE FUNCTION public.transfer_case_agent_ledger_exception_control_from_web_session"
        )
        wake_start = self.sql.index(
            "CREATE OR REPLACE FUNCTION public.wake_case_agent_run()"
        )
        transfer = self.sql[transfer_start:wake_start]
        self.assertIn("CASE_LEDGER_EXCEPTION_CONTROL_CLAIM", transfer)
        self.assertIn("ORDER BY run.run_id", transfer)
        self.assertIn("ORDER BY inbox.run_id", transfer)
        self.assertIn("inbox_status = 'QUIET'", transfer)
        self.assertIn("inbox_status = 'READY'", transfer)
        self.assertIn("control run is still executing", transfer)
        self.assertLess(
            transfer.index("ORDER BY run.run_id"),
            transfer.index("FROM public.matters matter"),
        )

    def test_wake_keeps_pending_and_superseded_control_runs_quiet(self) -> None:
        wake_start = self.sql.index(
            "CREATE OR REPLACE FUNCTION public.wake_case_agent_run()"
        )
        wake = self.sql[wake_start:]
        self.assertIn("intent_head.current_outcome <> 'TRANSFERRED'", wake)
        self.assertIn("historical.control_run_id = NEW.run_id", wake)
        self.assertIn(
            "CASE WHEN run_is_claimable THEN 'READY' ELSE 'QUIET' END",
            wake,
        )

    def test_new_authority_is_force_rls_and_least_privilege(self) -> None:
        for table in (
            "case_agent_ledger_exception_recovery_intents",
            "case_agent_ledger_exception_recovery_intent_heads",
            "case_agent_ledger_exception_recovery_quarantines",
        ):
            self.assertIn(
                f"ALTER TABLE public.{table}\n    FORCE ROW LEVEL SECURITY",
                self.sql,
            )
        self.assertIn(
            "FROM PUBLIC, lawcase_agent_worker", self.sql
        )
        self.assertIn("TO lawcase_web_application", self.sql)

    def test_upgrade_quarantines_the_exact_legacy_recovery_shape(self) -> None:
        self.assertIn("LEGACY_PRE_INTENT_RECOVERY_RUN", self.sql)
        self.assertIn("LOCK TABLE", self.sql)
        self.assertIn("IN SHARE ROW EXCLUSIVE MODE", self.sql)
        self.assertIn("FOR UPDATE OF inbox", self.sql)
        self.assertIn(
            "0050 upgrade requires legacy recovery-run leases to be drained",
            self.sql,
        )
        self.assertIn("goal.success_criteria =", self.sql)
        self.assertIn("goal.constraints =", self.sql)
        self.assertIn(
            "recovery run requires a durable intent", self.sql
        )


if __name__ == "__main__":
    unittest.main()
