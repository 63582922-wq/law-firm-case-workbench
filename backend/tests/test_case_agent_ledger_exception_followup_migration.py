from pathlib import Path
import unittest


class LedgerExceptionFollowupMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = (
            Path(__file__).parents[1]
            / "migrations"
            / "0049_case_agent_ledger_exception_followups.sql"
        ).read_text()

    def test_route_terminal_and_followup_terminal_are_separate(self) -> None:
        self.assertIn("0047's RESOLVED batch status", self.sql)
        for kind in ("REEXTRACTION", "MORE_EVIDENCE", "DEFERRED_REVIEW"):
            self.assertIn(f"'{kind}'", self.sql)
        self.assertIn("WHERE current_state = 'ACTIVE'", self.sql)
        self.assertIn("case_agent_ledger_exception_followup_one_active_subject", self.sql)

    def test_subject_uses_exact_group_semantics_not_only_page_ids(self) -> None:
        self.assertIn("case-ledger-exception-subject-v2", self.sql)
        self.assertIn("exception_group.group_key_hash", self.sql)
        self.assertIn("exception_group.candidate_set_hash", self.sql)

    def test_reextraction_requires_prior_exact_task_binding(self) -> None:
        self.assertIn(
            "case_agent_ledger_exception_reextraction_task_bindings",
            self.sql,
        )
        self.assertIn(
            "bind_case_agent_ledger_exception_reextraction_task_from_worker",
            self.sql,
        )
        self.assertIn("input_run_id <> followup_row.control_run_id", self.sql)
        self.assertIn(
            "task_row.graph_version <= followup_row.origin_graph_version",
            self.sql,
        )
        self.assertIn("task_row.tool_id <> 'extract_case_ledger'", self.sql)
        self.assertIn(
            "case_agent_ledger_exception_reextraction_task_binding_heads",
            self.sql,
        )
        self.assertNotIn("batch.run_id <> followup_row.origin_run_id", self.sql)

    def test_reextraction_satisfaction_is_atomic_and_server_discovers_all_heads(self) -> None:
        self.assertIn(
            "satisfy_case_agent_ledger_reextraction_set_from_worker",
            self.sql,
        )
        self.assertIn(
            "ledger re-extraction set is incomplete or contains a duplicate task batch",
            self.sql,
        )
        self.assertIn("GET DIAGNOSTICS active_followup_count = ROW_COUNT", self.sql)
        self.assertIn("verified_followup_count <> active_followup_count", self.sql)
        self.assertIn("current_task_binding_id", self.sql)
        self.assertIn("'followup_count', active_followup_count", self.sql)
        self.assertNotIn(
            "TO lawcase_agent_worker;\nGRANT EXECUTE ON FUNCTION\n"
            "    public.satisfy_case_agent_ledger_reextraction_followup_from_worker",
            self.sql,
        )

    def test_control_run_is_shared_refreshable_and_fanned_out(self) -> None:
        self.assertIn(
            "case_agent_ledger_exception_control_assignments",
            self.sql,
        )
        self.assertIn("case_agent_ledger_exception_control_heads", self.sql)
        self.assertIn("'HEALTHY', 'RECOVERY_REQUIRED'", self.sql)
        self.assertIn("CASE_LEDGER_EXCEPTION_ACTIVE_CONTROL_RUN", self.sql)
        self.assertIn("control run is not safely refreshable", self.sql)
        self.assertIn(
            "CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED",
            self.sql,
        )
        self.assertIn(
            "zz_case_agent_exception_control_run_refresh_fanout",
            self.sql,
        )
        self.assertIn(
            "transfer_case_agent_ledger_exception_control_from_web_session",
            self.sql,
        )
        self.assertIn(
            "case_agent_ledger_exception_control_failure_requires_recovery",
            self.sql,
        )
        self.assertIn(
            "case_agent_active_exception_control_run_cancel_block",
            self.sql,
        )
        self.assertIn("control_assignment_id uuid", self.sql)

    def test_reextraction_capacity_is_bounded_before_activation(self) -> None:
        self.assertIn(
            "exceeds the 64-page governed task window",
            self.sql,
        )
        self.assertIn(
            "exceed the 99-cohort governed graph capacity",
            self.sql,
        )
        self.assertIn("REEXTRACTION_SOURCE_WINDOW_EXCEEDED", self.sql)
        self.assertIn(
            "REEXTRACTION_GRAPH_COHORT_CAPACITY_EXCEEDED",
            self.sql,
        )

    def test_more_evidence_binds_nonempty_new_managed_source_set(self) -> None:
        self.assertIn(
            "case_agent_ledger_exception_evidence_source_bindings",
            self.sql,
        )
        self.assertIn("managed_evidence_source_set_hash", self.sql)
        self.assertIn("original.created_at >", self.sql)
        self.assertIn("successor.supersedes_file_id", self.sql)
        self.assertIn("material.created_at >", self.sql)
        self.assertIn(
            "requires a non-empty exact managed source set",
            self.sql,
        )

    def test_plan_promotion_activation_and_legacy_upgrade_fail_closed(self) -> None:
        self.assertIn(
            "case_agent_work_plan_active_exception_followup_block",
            self.sql,
        )
        self.assertIn(
            "case_work_plan_active_exception_followup_activation_block",
            self.sql,
        )
        self.assertIn(
            "0049 refuses an ACTIVE work plan with an ACTIVE exception follow-up",
            self.sql,
        )
        self.assertIn("IF NEW.status = 'ACTIVE'", self.sql)
        self.assertIn(
            "BEFORE INSERT OR UPDATE ON public.case_work_plans",
            self.sql,
        )

    def test_append_only_force_rls_owner_permissions_and_refresh_bridge(self) -> None:
        for table in (
            "case_agent_ledger_exception_followups",
            "case_agent_ledger_exception_control_assignments",
            "case_agent_ledger_exception_control_heads",
            "case_agent_ledger_exception_evidence_source_bindings",
            "case_agent_ledger_exception_followup_events",
            "case_agent_ledger_exception_followup_heads",
            "case_agent_ledger_exception_reextraction_task_bindings",
            "case_agent_ledger_exception_reextraction_task_binding_heads",
            "case_agent_ledger_exception_reextraction_bindings",
        ):
            self.assertIn(
                f"ALTER TABLE public.{table}\n    FORCE ROW LEVEL SECURITY",
                self.sql,
            )
        for table in (
            "public.case_material_objects",
            "public.case_agent_task_heads",
            "public.case_agent_events",
            "public.outbox_events",
        ):
            self.assertIn(table, self.sql)
        self.assertIn("case_agent_snapshot_refresh_requests", self.sql)
        self.assertIn("enqueue_case_agent_snapshot_refresh_from_exception_followup", self.sql)
        self.assertIn("'PENDING'", self.sql)

    def test_composite_batch_fk_has_a_matching_unique_key(self) -> None:
        self.assertIn(
            "UNIQUE (extraction_batch_id, run_id, firm_id, matter_id)",
            self.sql,
        )
        self.assertIn(
            "FOREIGN KEY (reextraction_batch_id, reextraction_run_id, firm_id, matter_id)",
            self.sql,
        )

    def test_refresh_definers_do_not_row_lock_immutable_lineage(self) -> None:
        outbox_start = self.sql.index(
            "CREATE FUNCTION public.enqueue_case_agent_exception_control_run_refresh_outbox()"
        )
        materialize_start = self.sql.index(
            "CREATE FUNCTION public.enqueue_case_agent_exception_control_run_refresh()"
        )
        materialize_end = self.sql.index(
            "CREATE TRIGGER case_agent_exception_control_run_refresh_materializes",
            materialize_start,
        )
        outbox_body = self.sql[outbox_start:materialize_start]
        materialize_body = self.sql[materialize_start:materialize_end]

        # SECURITY DEFINER row locks require UPDATE privilege in PostgreSQL.
        # Immutable audit/outbox/control-assignment lineage is read-only; only
        # the mutable control head may be locked while choosing the current run.
        self.assertIn("FOR SHARE OF head;", outbox_body)
        self.assertNotIn("FOR SHARE OF head, assignment", outbox_body)
        self.assertNotIn("FOR SHARE", materialize_body)

    def test_upgrade_hardens_old_trigger_helpers_before_lifecycle_backfill(self) -> None:
        backfill = self.sql.index(
            "FOR item IN\n        SELECT decision.exception_decision_id"
        )
        for signature in (
            "public.wake_case_agent_run_for_snapshot_refresh()",
            "public.guard_case_agent_snapshot_refresh_request()",
            "public.normalize_case_agent_snapshot_refresh_review_gate()",
            "public.validate_case_agent_ledger_exception_group_integrity(\n"
            "    uuid, uuid, uuid\n)",
            "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
            "public.case_agent_ledger_extraction_run_review_resolved(\n"
            "    uuid, uuid, uuid\n)",
        ):
            alteration = self.sql.index(f"ALTER FUNCTION {signature}")
            self.assertLess(alteration, backfill)
            self.assertIn(
                "SET search_path = pg_catalog, public, pg_temp;",
                self.sql[alteration:alteration + 300],
            )
        for lock_grant in (
            "GRANT UPDATE (session_id) ON TABLE public.web_sessions",
            "GRANT UPDATE (user_id) ON TABLE public.users",
            "GRANT UPDATE (user_id) ON TABLE public.matter_actor_roles",
            "GRANT UPDATE (run_id) ON TABLE public.case_agent_runs",
        ):
            self.assertLess(self.sql.index(lock_grant), backfill)


if __name__ == "__main__":
    unittest.main()
