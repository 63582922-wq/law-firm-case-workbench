from __future__ import annotations

from pathlib import Path
import unittest


class SealedResponseRecoveryMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "0077_sealed_response_recovery_candidates.sql"
        ).read_text(encoding="utf-8")

    def test_recovery_has_its_own_append_only_table_and_not_an_agent_artifact_write(self) -> None:
        self.assertIn("CREATE TABLE public.case_agent_sealed_response_recovery_candidates", self.sql)
        self.assertIn("REFERENCES public.case_agent_review_candidates", self.sql)
        self.assertNotIn("INSERT INTO public.case_agent_artifacts", self.sql)
        self.assertIn("recovery_kind = 'SEALED_RESPONSE_REPARSE'", self.sql)
        self.assertIn("failure_code = 'LAWYER_ANALYSIS_OUTPUT_REJECTED'", self.sql)

    def test_database_rechecks_the_original_blocked_run_and_failed_provider_receipt(self) -> None:
        self.assertIn("run_row.status <> 'WAITING_INPUT'", self.sql)
        self.assertIn("run_row.current_event_version <> NEW.source_run_event_version", self.sql)
        self.assertIn("run_row.snapshot_hash <> NEW.source_snapshot_hash", self.sql)
        self.assertIn("task_row.tool_id <> 'analyze_lawyer_decision_package'", self.sql)
        self.assertIn("head_status <> 'FAILED'", self.sql)
        self.assertIn("submission_count <> 1", self.sql)
        self.assertIn("receipt.result_status = 'FAILED'", self.sql)
        self.assertIn("receipt.external_submission_state = 'SUBMITTED'", self.sql)
        self.assertIn("receipt.external_calls = 1", self.sql)

    def test_tenant_isolation_append_only_and_least_privilege_are_present(self) -> None:
        self.assertIn("ENABLE ROW LEVEL SECURITY", self.sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", self.sql)
        self.assertIn("case_agent_sealed_response_recovery_candidates_append_only", self.sql)
        self.assertIn("BEFORE UPDATE OR DELETE", self.sql)
        self.assertIn("GRANT SELECT, INSERT ON TABLE public.case_agent_sealed_response_recovery_candidates", self.sql)
        self.assertIn("GRANT SELECT ON TABLE public.case_agent_sealed_response_recovery_candidates", self.sql)


if __name__ == "__main__":
    unittest.main()
