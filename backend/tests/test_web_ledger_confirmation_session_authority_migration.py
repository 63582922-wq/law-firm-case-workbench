from __future__ import annotations

from pathlib import Path
import re
import unittest


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "0048_web_ledger_confirmation_session_authority.sql"
)


class WebLedgerConfirmationSessionAuthorityMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_definer_owner_is_narrow_no_login_role_not_inherited_by_web(self) -> None:
        for fragment in (
            "lawcase_ledger_confirmation_owner",
            "NOT rolcanlogin AND NOT rolinherit",
            "NOT rolsuper AND NOT rolbypassrls",
            "lawcase_web_application must not inherit",
            "lawcase_web_application cannot own protected ledger confirmation tables",
        ):
            self.assertIn(fragment, self.sql)

    def test_exact_session_selector_is_available_only_to_definer_owner(self) -> None:
        policy = re.search(
            r"CREATE POLICY web_sessions_ledger_confirmation_exact_session"
            r"(?P<body>.*?)\);",
            self.sql,
            re.DOTALL,
        )
        self.assertIsNotNone(policy)
        body = policy.group("body")
        self.assertIn("FOR SELECT", body)
        self.assertIn("TO lawcase_ledger_confirmation_owner", body)
        self.assertIn("app.web_session_id", body)
        self.assertNotIn("lawcase_web_application", body)

    def test_approval_is_short_lived_append_only_and_force_rls(self) -> None:
        for fragment in (
            "CREATE TABLE public.case_agent_ledger_extraction_session_approvals",
            "web_session_id uuid NOT NULL",
            "extraction_batch_id uuid NOT NULL",
            "expected_matter_version integer NOT NULL",
            "candidate_set_hash char(64) NOT NULL",
            "source_binding_hash char(64) NOT NULL",
            "approval_attempt integer NOT NULL CHECK (approval_attempt > 0)",
            "expires_at <= authorized_at + interval '5 minutes'",
            "case_agent_ledger_extraction_session_approvals\n    FORCE ROW LEVEL SECURITY",
            "case_agent_ledger_extraction_session_approvals_append_only",
        ):
            self.assertIn(fragment, self.sql)

    def test_expired_same_intent_creates_a_new_append_only_approval_attempt(self) -> None:
        for fragment in (
            "ORDER BY approval.approval_attempt DESC",
            "IF prior_approval.expires_at > pg_catalog.clock_timestamp() THEN",
            "next_approval_attempt := prior_approval.approval_attempt + 1",
            "approval_attempt, expires_at",
        ):
            self.assertIn(fragment, self.sql)
        self.assertNotIn(
            "OR prior_approval.expires_at <= pg_catalog.clock_timestamp() THEN",
            self.sql,
        )

    def test_low_risk_commands_rederive_live_oidc_mfa_lead_authority(self) -> None:
        for function_name in (
            "authorize_case_agent_ledger_extraction_low_risk_confirmation",
            "finalize_case_agent_ledger_extraction_low_risk_confirmation",
        ):
            self.assertIn(f"CREATE FUNCTION public.{function_name}", self.sql)
        for fragment in (
            "session.revoked_at IS NULL",
            "session.expires_at > pg_catalog.clock_timestamp()",
            "session.authenticated_at <= pg_catalog.clock_timestamp()",
            "session.issuer LIKE 'https://%'",
            "actor.status = 'ACTIVE'",
            "role_binding.role = 'LEAD_LAWYER'",
            "role_binding.revoked_at IS NULL",
        ):
            self.assertGreaterEqual(self.sql.count(fragment), 3, fragment)
        self.assertNotIn("app.actor_id", self.sql)
        self.assertEqual(self.sql.count("FOR SHARE OF role_binding, lead;"), 3)

    def test_live_lead_is_locked_before_idempotent_replay_is_returned(self) -> None:
        function_starts = (
            "CREATE FUNCTION public.authorize_case_agent_ledger_extraction_low_risk_confirmation",
            "CREATE FUNCTION public.finalize_case_agent_ledger_extraction_low_risk_confirmation",
            "CREATE FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session",
        )
        for index, marker in enumerate(function_starts):
            start = self.sql.index(marker)
            end = (
                self.sql.index(function_starts[index + 1], start)
                if index + 1 < len(function_starts)
                else self.sql.index(
                    "-- Once a 0042 target has an immutable promotion mapping",
                    start,
                )
            )
            command = self.sql[start:end]
            self.assertLess(
                command.index("FROM public.matters matter"),
                command.index("FROM public.matter_actor_roles role_binding"),
            )
            self.assertLess(
                command.index("FOR SHARE OF role_binding, lead;"),
                command.index("FROM public.command_idempotency"),
            )

    def test_two_phase_finalizer_rejects_expiry_session_revocation_and_revoked_lead(self) -> None:
        start = self.sql.index(
            "CREATE FUNCTION public.finalize_case_agent_ledger_extraction_low_risk_confirmation"
        )
        end = self.sql.index(
            "-- Exception routing has no external source-read phase", start
        )
        finalizer = self.sql[start:end]
        for fragment in (
            "approval.expires_at <= pg_catalog.clock_timestamp()",
            "session.revoked_at IS NULL",
            "session.expires_at > pg_catalog.clock_timestamp()",
            "session.user_id = approval.actor_id",
            "actor.status = 'ACTIVE'",
            "role_binding.role = 'LEAD_LAWYER'",
            "role_binding.revoked_at IS NULL",
            "role_binding.user_id = approval.actor_id",
        ):
            self.assertIn(fragment, finalizer)

    def test_cross_firm_actor_or_batch_cannot_be_supplied_to_definer_commands(self) -> None:
        signatures = self.sql[
            self.sql.index(
                "CREATE FUNCTION public.authorize_case_agent_ledger_extraction_low_risk_confirmation"
            ) : self.sql.index(")\nRETURNS jsonb", self.sql.index(
                "CREATE FUNCTION public.authorize_case_agent_ledger_extraction_low_risk_confirmation"
            ))
        ]
        self.assertNotIn("input_firm_id", signatures)
        self.assertNotIn("input_actor_id", signatures)
        for fragment in (
            "batch.firm_id = session_row.firm_id",
            "batch.matter_id = input_matter_id",
            "matter.firm_id = session_row.firm_id",
            "approval.firm_id",
            "session.user_id = approval.actor_id",
        ):
            self.assertIn(fragment, self.sql)

    def test_authorization_and_finalization_bind_same_batch_and_full_run_barrier(self) -> None:
        self.assertIn("input_batch_id uuid", self.sql)
        self.assertIn("extraction_batch_id uuid NOT NULL", self.sql)
        self.assertIn(
            "web_session_id, firm_id, matter_id, actor_id, command_name",
            self.sql,
        )
        self.assertGreaterEqual(
            self.sql.count("case_agent_ledger_extraction_run_staging_complete("),
            4,
        )
        self.assertGreaterEqual(
            self.sql.count("case_agent_ledger_extraction_current_review_version("),
            3,
        )
        self.assertGreaterEqual(
            self.sql.count("case_agent_ledger_extraction_batch_review_status("),
            3,
        )
        self.assertIn("candidate.review_lane = 'BULK_PROMOTION_ELIGIBLE'", self.sql)
        self.assertIn("candidate.eligible_for_bulk_promotion = true", self.sql)
        self.assertIn("exception_candidates_included', false", self.sql)

    def test_server_source_reverification_and_request_hashes_are_rechecked(self) -> None:
        for fragment in (
            "case-agent-ledger-source-verification-v1|",
            "source_binding_hash IS DISTINCT FROM input_source_verification_hash",
            "case-agent-ledger-candidate-set-v1|",
            "case-ledger-extraction-batch-decision-v2|",
            "ledger confirmation request hash differs",
            "ledger exception decision request hash differs",
            "case-ledger-exception-group-request-v1",
        ):
            self.assertIn(fragment, self.sql)

    def test_exception_decision_is_atomic_session_bound_and_staging_complete(self) -> None:
        start = self.sql.index(
            "CREATE FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session"
        )
        end = self.sql.index(
            "-- Once a 0042 target has an immutable promotion mapping", start
        )
        command = self.sql[start:end]
        for fragment in (
            "SECURITY DEFINER",
            "SET search_path = pg_catalog",
            "session.revoked_at IS NULL",
            "actor.status = 'ACTIVE'",
            "role_binding.role = 'LEAD_LAWYER'",
            "validate_case_agent_ledger_exception_group_integrity",
            "case_agent_ledger_extraction_run_staging_complete",
            "case_agent_ledger_extraction_current_review_version",
            "case_agent_ledger_extraction_run_review_resolved",
            "INSERT INTO public.case_agent_ledger_exception_group_decisions",
        ):
            self.assertIn(fragment, command)

    def test_exception_replay_accepts_only_group_or_bound_run_receipt_shape(self) -> None:
        start = self.sql.index(
            "CREATE FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session"
        )
        end = self.sql.index(
            "-- Once a 0042 target has an immutable promotion mapping", start
        )
        command = self.sql[start:end]
        for fragment in (
            "SELECT exception_group.run_id",
            "INTO prior_group_run_id",
            "CASE_LEDGER_EXCEPTION_GROUP",
            "CASE_LEDGER_EXTRACTION_RUN_REVIEW",
            "prior_group_run_id::text",
            "(input_expected_version + 1)::text",
        ):
            self.assertIn(fragment, command)

    def test_exception_concurrency_has_two_narrow_stable_conflict_codes(self) -> None:
        start = self.sql.index(
            "CREATE FUNCTION public.decide_case_agent_ledger_exception_group_from_web_session"
        )
        end = self.sql.index(
            "-- Once a 0042 target has an immutable promotion mapping", start
        )
        command = self.sql[start:end]
        self.assertEqual(command.count("ERRCODE = 'P4091'"), 2)
        self.assertEqual(command.count("ERRCODE = 'P4092'"), 1)
        self.assertIn("matter version is stale", command)
        self.assertIn("review version is stale", command)
        self.assertIn("terminally routed by another intent", command)
        self.assertNotIn("ERRCODE = 'P0001'", command)

    def test_web_role_cannot_directly_write_authoritative_review_tables(self) -> None:
        revoke = re.search(
            r"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE(?P<body>.*?)"
            r"FROM lawcase_web_application;",
            self.sql,
            re.DOTALL,
        )
        self.assertIsNotNone(revoke)
        for table in (
            "case_agent_ledger_extraction_batches",
            "case_agent_ledger_extraction_staging_events",
            "case_agent_ledger_extraction_candidates",
            "case_agent_ledger_extraction_candidate_pages",
            "case_agent_ledger_extraction_promotions",
            "case_agent_ledger_extraction_batch_confirmations",
            "case_agent_ledger_exception_groups",
            "case_agent_ledger_exception_group_members",
            "case_agent_ledger_exception_group_decisions",
            "case_agent_ledger_exception_decision_events",
        ):
            self.assertIn(table, revoke.group("body"))
        self.assertIn(
            "REVOKE ALL ON TABLE\n"
            "    public.case_agent_ledger_extraction_session_approvals\n"
            "    FROM PUBLIC, lawcase_web_application;",
            self.sql,
        )

    def test_security_definer_functions_have_fixed_path_owner_and_execute_grants(self) -> None:
        signatures = (
            "authorize_case_agent_ledger_extraction_low_risk_confirmation",
            "finalize_case_agent_ledger_extraction_low_risk_confirmation",
            "decide_case_agent_ledger_exception_group_from_web_session",
        )
        self.assertGreaterEqual(self.sql.count("SECURITY DEFINER"), 4)
        self.assertGreaterEqual(self.sql.count("SET search_path = pg_catalog"), 5)
        for name in signatures:
            self.assertIn(f"ALTER FUNCTION public.{name}", self.sql)
            self.assertIn(f"public.{name}", self.sql)
        self.assertGreaterEqual(
            self.sql.count(") TO lawcase_web_application;"), len(signatures)
        )

    def test_session_bound_targets_cannot_be_changed_through_legacy_dml(self) -> None:
        for fragment in (
            "case_facts_session_bound_extraction_target_immutable",
            "case_transactions_session_bound_extraction_target_immutable",
            "confirmation.session_approval_id IS NOT NULL",
            "pg_catalog.set_config('app.firm_id', OLD.firm_id::text, true)",
            "IF TG_OP = 'UPDATE' THEN\n        RETURN NEW;",
        ):
            self.assertIn(fragment, self.sql)

    def test_sql_parses_with_pglast_when_available(self) -> None:
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        parse_sql(self.sql)

    def test_authority_command_bodies_parse_as_plpgsql_when_available(self) -> None:
        try:
            from pglast import parse_plpgsql
        except ImportError:
            self.skipTest("pglast is not installed in the workspace runtime")
        command_names = (
            "authorize_case_agent_ledger_extraction_low_risk_confirmation",
            "finalize_case_agent_ledger_extraction_low_risk_confirmation",
            "decide_case_agent_ledger_exception_group_from_web_session",
        )
        for command_name in command_names:
            match = re.search(
                rf"CREATE FUNCTION public\.{command_name}\(.*?\$\$\s*;",
                self.sql,
                re.DOTALL,
            )
            self.assertIsNotNone(match, command_name)
            parse_plpgsql(match.group(0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
