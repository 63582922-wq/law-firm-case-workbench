from pathlib import Path
import unittest


class CaseAgentSessionColumnLeastPrivilegeMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        cls.followup_sql = (
            migrations / "0049_case_agent_ledger_exception_followups.sql"
        ).read_text(encoding="utf-8")
        cls.recovery_sql = (
            migrations / "0050_case_agent_control_recovery_atomicity.sql"
        ).read_text(encoding="utf-8")
        cls.repair_sql = (
            migrations / "0051_case_agent_session_column_least_privilege.sql"
        ).read_text(encoding="utf-8")

    def test_fresh_followup_install_uses_column_projections(self) -> None:
        runtime_grants = self.followup_sql[
            self.followup_sql.index("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE"):
            self.followup_sql.index("REVOKE ALL ON FUNCTION", self.followup_sql.index(
                "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE"
            ))
        ]
        broad_grant = runtime_grants[
            runtime_grants.index("GRANT SELECT ON TABLE"):
            runtime_grants.index("-- Session UUIDs")
        ]
        self.assertNotIn(
            "public.case_agent_ledger_exception_control_assignments,",
            broad_grant,
        )
        self.assertNotIn(
            "public.case_agent_ledger_exception_followup_events,",
            broad_grant,
        )
        self.assertIn(
            "GRANT SELECT (\n    control_assignment_id, firm_id, matter_id, "
            "assignment_sequence,\n    control_run_id, state_after\n)",
            runtime_grants,
        )
        self.assertIn("TO lawcase_agent_worker;", runtime_grants)
        self.assertNotIn("web_session_id\n) ON TABLE", runtime_grants)

    def test_fresh_recovery_install_is_worker_only_and_column_scoped(self) -> None:
        grants = self.recovery_sql[
            self.recovery_sql.index("REVOKE ALL ON TABLE"):
            self.recovery_sql.index("REVOKE ALL ON FUNCTION", self.recovery_sql.index(
                "REVOKE ALL ON TABLE"
            ))
        ]
        self.assertNotIn("GRANT SELECT ON TABLE", grants)
        self.assertNotIn("TO lawcase_web_application", grants)
        self.assertIn(
            "recovery_intent_id, firm_id, matter_id, replacement_run_id",
            grants,
        )
        self.assertIn("transfer_control_assignment_id", grants)
        self.assertNotIn("prepared_web_session_id\n)", grants)
        self.assertNotIn("outcome_web_session_id\n)", grants)

    def test_forward_repair_revokes_before_restoring_exact_columns(self) -> None:
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM lawcase_agent_worker;",
            self.repair_sql,
        )
        self.assertIn(
            "GRANT USAGE ON SCHEMA public TO lawcase_agent_worker;",
            self.repair_sql,
        )
        self.assertNotIn(
            "GRANT CREATE ON SCHEMA public TO lawcase_agent_worker;",
            self.repair_sql,
        )
        revoke_at = self.repair_sql.index("REVOKE SELECT ON TABLE")
        grant_at = self.repair_sql.index("GRANT SELECT (")
        self.assertLess(revoke_at, grant_at)
        for sensitive_column in (
            "web_session_id",
            "prepared_web_session_id",
            "outcome_web_session_id",
        ):
            self.assertIn(sensitive_column, self.repair_sql[revoke_at:grant_at])
            self.assertNotIn(sensitive_column, self.repair_sql[grant_at:])
        self.assertIn(
            "TO lawcase_web_application, lawcase_agent_worker;",
            self.repair_sql[grant_at:],
        )
        worker_only = self.repair_sql[
            self.repair_sql.index("-- Worker-only immutable projections"):
        ]
        self.assertNotIn("lawcase_web_application", worker_only)


if __name__ == "__main__":
    unittest.main()
