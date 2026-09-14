from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import psycopg

from case_kernel.case_agent_ledger_exception_followup import (
    LedgerExceptionControlHealth,
    LedgerExceptionFollowupAction,
    LedgerExceptionFollowupBlocked,
    ManagedEvidenceSourceRef,
    ManagedEvidenceSourceType,
)
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    PostgresCaseLedgerExceptionFollowupStore,
    _read_followup_for_command,
    preflight_case_agent_ledger_exception_followup_schema,
)
from case_kernel.errors import IdempotencyConflict, VersionConflict
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Result:
    def __init__(self, *, row=None, rows=()) -> None:
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT set_config"):
            return _Result()
        if not self.responses:
            raise AssertionError(f"unexpected SQL: {normalized}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _Result(**response)


class _PreflightConnection:
    tables = {
        "case_agent_ledger_exception_followups",
        "case_agent_ledger_exception_control_assignments",
        "case_agent_ledger_exception_control_heads",
        "case_agent_ledger_exception_managed_evidence_requests",
        "case_agent_ledger_exception_evidence_source_bindings",
        "case_agent_ledger_exception_followup_events",
        "case_agent_ledger_exception_followup_heads",
        "case_agent_ledger_exception_reextraction_task_bindings",
        "case_agent_ledger_exception_reextraction_task_binding_heads",
        "case_agent_ledger_exception_reextraction_bindings",
        "case_agent_ledger_exception_duplicate_dispositions",
        "case_agent_ledger_exception_duplicate_heads",
        "case_agent_ledger_exception_recovery_intents",
        "case_agent_ledger_exception_recovery_intent_heads",
        "case_agent_ledger_exception_recovery_quarantines",
    }
    triggers = {
        "case_agent_ledger_exception_decision_initializes_lifecycle",
        "case_agent_exception_followup_enqueues_snapshot_refresh",
        "case_agent_work_plan_active_exception_followup_block",
        "case_work_plan_active_exception_followup_activation_block",
        "case_agent_ledger_exception_followup_heads_guard",
        "case_agent_ledger_exception_reextraction_tasks_append_only",
        "case_agent_ledger_exception_evidence_sources_append_only",
        "case_agent_ledger_exception_followups_append_only",
        "case_agent_ledger_exception_evidence_requests_append_only",
        "case_agent_ledger_exception_followup_events_append_only",
        "case_agent_ledger_exception_reextraction_bindings_append_only",
        "case_agent_ledger_exception_reextraction_task_heads_guard",
        "case_agent_ledger_exception_duplicate_dispositions_append_only",
        "case_agent_ledger_exception_duplicate_heads_guard",
        "case_agent_exception_control_run_refresh_materializes",
        "zz_case_agent_exception_control_run_refresh_fanout",
        "case_agent_active_exception_control_run_cancel_block",
        "case_agent_ledger_exception_control_assignments_append_only",
        "case_agent_ledger_exception_control_heads_guard",
        "case_agent_ledger_exception_control_failure_requires_recovery",
        "case_agent_snapshot_refresh_requests_guard",
        "case_agent_ledger_exception_recovery_intents_append_only",
        "case_agent_ledger_exception_recovery_intent_heads_guard",
        "case_agent_ledger_exception_recovery_quarantines_append_only",
        "case_agent_ledger_exception_recovery_goal_bind",
        "case_agent_runs_recovery_goal_binding_guard",
    }
    restricted_table_columns = {
        "case_agent_ledger_exception_control_assignments": {
            "control_assignment_id", "firm_id", "matter_id",
            "assignment_sequence", "control_run_id", "state_after",
            "transition_type", "supersedes_control_assignment_id",
            "source_exception_decision_id", "source_agent_event_id",
            "actor_id", "web_session_id", "expected_matter_version",
            "reason_code", "idempotency_key", "request_hash",
            "audit_event_id", "assigned_at",
        },
        "case_agent_ledger_exception_followup_events": {
            "followup_event_id", "followup_id", "event_sequence", "firm_id",
            "matter_id", "subject_hash", "event_type", "state_after",
            "actor_id", "web_session_id", "expected_matter_version",
            "managed_evidence_request_id", "managed_evidence_source_set_hash",
            "managed_evidence_source_count", "reextraction_binding_id",
            "reason_note", "idempotency_key", "request_hash", "event_hash",
            "audit_event_id", "occurred_at",
        },
        "case_agent_ledger_exception_recovery_intents": {
            "recovery_intent_id", "firm_id", "matter_id",
            "source_control_assignment_id", "replacement_run_id", "actor_id",
            "prepared_web_session_id", "expected_matter_version",
            "idempotency_key", "request_hash", "prepared_at",
            "recovery_goal_id", "recovery_goal_hash",
        },
        "case_agent_ledger_exception_recovery_intent_heads": {
            "recovery_intent_id", "firm_id", "matter_id", "current_outcome",
            "transfer_control_assignment_id", "outcome_reason_code",
            "outcome_actor_id", "outcome_web_session_id", "outcome_at",
            "outcome_version", "updated_at",
        },
        "case_agent_ledger_exception_recovery_quarantines": {
            "run_id", "firm_id", "matter_id", "source_control_assignment_id",
            "reason_code", "quarantined_at",
        },
    }
    column_read_contract = {
        "lawcase_web_application": {
            "case_agent_ledger_exception_control_assignments": {
                "control_assignment_id", "firm_id", "matter_id",
                "assignment_sequence", "control_run_id", "state_after",
            },
        },
        "lawcase_agent_worker": {
            "case_agent_ledger_exception_control_assignments": {
                "control_assignment_id", "firm_id", "matter_id",
                "assignment_sequence", "control_run_id", "state_after",
            },
            "case_agent_ledger_exception_followup_events": {
                "followup_event_id", "followup_id", "event_sequence",
                "firm_id", "matter_id", "subject_hash", "state_after",
                "expected_matter_version", "event_hash",
            },
            "case_agent_ledger_exception_recovery_intents": {
                "recovery_intent_id", "firm_id", "matter_id",
                "replacement_run_id",
                "actor_id", "recovery_goal_id", "recovery_goal_hash",
            },
            "case_agent_ledger_exception_recovery_intent_heads": {
                "recovery_intent_id", "firm_id", "matter_id",
                "current_outcome", "transfer_control_assignment_id",
            },
            "case_agent_ledger_exception_recovery_quarantines": {
                "run_id", "firm_id", "matter_id",
            },
        },
    }

    def __init__(
        self,
        *,
        public_execute: bool = False,
        hardened_search_path: bool = True,
        definer_search_path: bool = True,
        session_column_leak: bool = False,
        broad_runtime_select: bool = False,
        column_projection_drift: bool = False,
    ) -> None:
        self.public_execute = public_execute
        self.hardened_search_path = hardened_search_path
        self.definer_search_path = definer_search_path
        self.session_column_leak = session_column_leak
        self.broad_runtime_select = broad_runtime_select
        self.column_projection_drift = column_projection_drift

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT set_config"):
            return _Result()
        if "FROM pg_catalog.pg_roles owner" in normalized:
            return _Result(
                row={
                    "no_login": True,
                    "no_inherit": True,
                    "not_super": True,
                    "no_bypass_rls": True,
                    "web_not_member": True,
                    "worker_not_member": True,
                    "web_cannot_shadow": True,
                    "worker_cannot_shadow": True,
                    "owner_cannot_shadow": True,
                    "public_cannot_shadow": True,
                }
            )
        if "AS staging_trigger_bound" in normalized:
            return _Result(
                row={
                    "installed": True,
                    "prosecdef": True,
                    "owner": "lawcase_schema_owner",
                    "owner_no_login": True,
                    "owner_no_inherit": True,
                    "owner_not_super": True,
                    "owner_no_bypass_rls": True,
                    "proconfig": ["search_path=pg_catalog, public, pg_temp"],
                    "web_denied": True,
                    "worker_denied": True,
                    "verifier_denied": True,
                    "public_denied": True,
                    "staging_trigger_bound": True,
                }
            )
        if "relation.relrowsecurity" in normalized:
            return _Result(
                rows=[
                    {
                        "relname": table,
                        "relrowsecurity": True,
                        "relforcerowsecurity": True,
                        "owner_name": "lawcase_ledger_confirmation_owner",
                    }
                    for table in self.tables
                ]
            )
        if (
            "attribute.attname AS column_name" in normalized
            and "data_type.typname AS udt_name" in normalized
        ):
            contracts = {
                "case_agent_ledger_exception_recovery_intents": {
                    "recovery_intent_id": ("uuid", "NO"),
                    "firm_id": ("uuid", "NO"),
                    "matter_id": ("uuid", "NO"),
                    "source_control_assignment_id": ("uuid", "NO"),
                    "replacement_run_id": ("uuid", "NO"),
                    "actor_id": ("uuid", "NO"),
                    "prepared_web_session_id": ("uuid", "NO"),
                    "expected_matter_version": ("int4", "NO"),
                    "idempotency_key": ("text", "NO"),
                    "request_hash": ("bpchar", "NO"),
                    "prepared_at": ("timestamptz", "NO"),
                    "recovery_goal_id": ("uuid", "NO"),
                    "recovery_goal_hash": ("bpchar", "NO"),
                },
                "case_agent_ledger_exception_recovery_intent_heads": {
                    "recovery_intent_id": ("uuid", "NO"),
                    "firm_id": ("uuid", "NO"),
                    "matter_id": ("uuid", "NO"),
                    "current_outcome": ("text", "NO"),
                    "transfer_control_assignment_id": ("uuid", "YES"),
                    "outcome_reason_code": ("text", "YES"),
                    "outcome_actor_id": ("uuid", "YES"),
                    "outcome_web_session_id": ("uuid", "YES"),
                    "outcome_at": ("timestamptz", "YES"),
                    "outcome_version": ("int4", "NO"),
                    "updated_at": ("timestamptz", "NO"),
                },
                "case_agent_ledger_exception_recovery_quarantines": {
                    "run_id": ("uuid", "NO"),
                    "firm_id": ("uuid", "NO"),
                    "matter_id": ("uuid", "NO"),
                    "source_control_assignment_id": ("uuid", "NO"),
                    "reason_code": ("text", "NO"),
                    "quarantined_at": ("timestamptz", "NO"),
                },
            }
            return _Result(
                rows=[
                    {
                        "table_name": table,
                        "column_name": column,
                        "udt_name": contract[0],
                        "is_nullable": contract[1],
                    }
                    for table, columns in contracts.items()
                    for column, contract in columns.items()
                ]
            )
        if "AS head_terminal_contract" in normalized:
            return _Result(
                row={
                    "head_terminal_contract": True,
                    "source_history_allows_terminal_replacement": True,
                    "one_pending_contract": True,
                    "legacy_quarantine_contract": True,
                    "recovery_goal_hash_contract": True,
                }
            )
        if "trigger.tgname" in normalized:
            contracts = {
                "case_agent_ledger_exception_decision_initializes_lifecycle": (
                    "case_agent_ledger_exception_group_decisions",
                    "initialize_case_agent_ledger_exception_lifecycle_trigger",
                    5,
                    False,
                ),
                "case_agent_exception_followup_enqueues_snapshot_refresh": (
                    "outbox_events",
                    "enqueue_case_agent_snapshot_refresh_from_exception_followup",
                    5,
                    "(new.event_type = ANY (ARRAY['CASE_LEDGER_EXCEPTION_REEXTRACTION_VERIFIED_AND_STAGED'::text, 'CASE_LEDGER_EXCEPTION_REEXTRACTION_SET_VERIFIED_AND_STAGED'::text, 'CASE_LEDGER_EXCEPTION_MORE_EVIDENCE_CONFIRMED'::text, 'CASE_LEDGER_EXCEPTION_DEFER_RESUMED'::text, 'CASE_LEDGER_EXCEPTION_FOLLOWUP_WITHDRAWN'::text, 'CASE_LEDGER_EXCEPTION_FOLLOWUP_SUPERSEDED'::text]))",
                ),
                "case_agent_work_plan_active_exception_followup_block": (
                    "case_agent_work_plan_promotions",
                    "block_active_exception_followup_work_plan_promotion",
                    7,
                    False,
                ),
                "case_work_plan_active_exception_followup_activation_block": (
                    "case_work_plans",
                    "block_active_exception_followup_work_plan_activation",
                    23,
                    False,
                ),
                "case_agent_ledger_exception_followup_heads_guard": (
                    "case_agent_ledger_exception_followup_heads",
                    "guard_case_agent_ledger_exception_followup_head",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_reextraction_task_heads_guard": (
                    "case_agent_ledger_exception_reextraction_task_binding_heads",
                    "guard_case_agent_ledger_exception_reextraction_task_binding_hea",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_duplicate_heads_guard": (
                    "case_agent_ledger_exception_duplicate_heads",
                    "guard_case_agent_ledger_exception_duplicate_head",
                    27,
                    False,
                ),
                "case_agent_exception_control_run_refresh_materializes": (
                    "outbox_events",
                    "enqueue_case_agent_exception_control_run_refresh",
                    5,
                    "(new.event_type = 'CASE_LEDGER_EXCEPTION_CONTROL_RUN_REFRESH_REQUESTED'::text)",
                ),
                "zz_case_agent_exception_control_run_refresh_fanout": (
                    "outbox_events",
                    "enqueue_case_agent_exception_control_run_refresh_outbox",
                    5,
                    False,
                ),
                "case_agent_active_exception_control_run_cancel_block": (
                    "case_agent_events",
                    "block_active_exception_control_run_cancellation",
                    7,
                    "(new.event_type = 'RUN_CANCELLED'::text)",
                ),
                "case_agent_ledger_exception_control_heads_guard": (
                    "case_agent_ledger_exception_control_heads",
                    "guard_case_agent_ledger_exception_control_head",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_control_failure_requires_recovery": (
                    "case_agent_events",
                    "mark_case_agent_ledger_exception_control_recovery_required",
                    5,
                    "(new.event_type = 'VERIFICATION_FAILED'::text)",
                ),
                "case_agent_snapshot_refresh_requests_guard": (
                    "case_agent_snapshot_refresh_requests",
                    "guard_case_agent_snapshot_refresh_request",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_recovery_intent_heads_guard": (
                    "case_agent_ledger_exception_recovery_intent_heads",
                    "guard_case_agent_ledger_exception_recovery_intent_head",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_recovery_intents_append_only": (
                    "case_agent_ledger_exception_recovery_intents",
                    "prohibit_case_agent_ledger_exception_recovery_intent_mutation",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_recovery_quarantines_append_only": (
                    "case_agent_ledger_exception_recovery_quarantines",
                    "prohibit_case_agent_ledger_exception_recovery_intent_mutation",
                    27,
                    False,
                ),
                "case_agent_ledger_exception_recovery_goal_bind": (
                    "case_agent_ledger_exception_recovery_intents",
                    "bind_case_agent_ledger_exception_recovery_goal",
                    7,
                    False,
                ),
                "case_agent_runs_recovery_goal_binding_guard": (
                    "case_agent_runs",
                    "guard_case_agent_recovery_run_goal_binding",
                    23,
                    False,
                ),
            }
            append_only_tables = {
                "case_agent_ledger_exception_followups_append_only":
                    "case_agent_ledger_exception_followups",
                "case_agent_ledger_exception_evidence_requests_append_only":
                    "case_agent_ledger_exception_managed_evidence_requests",
                "case_agent_ledger_exception_followup_events_append_only":
                    "case_agent_ledger_exception_followup_events",
                "case_agent_ledger_exception_reextraction_bindings_append_only":
                    "case_agent_ledger_exception_reextraction_bindings",
                "case_agent_ledger_exception_reextraction_tasks_append_only":
                    "case_agent_ledger_exception_reextraction_task_bindings",
                "case_agent_ledger_exception_evidence_sources_append_only":
                    "case_agent_ledger_exception_evidence_source_bindings",
                "case_agent_ledger_exception_duplicate_dispositions_append_only":
                    "case_agent_ledger_exception_duplicate_dispositions",
                "case_agent_ledger_exception_control_assignments_append_only":
                    "case_agent_ledger_exception_control_assignments",
            }
            contracts.update({
                name: (
                    table,
                    "prohibit_case_agent_ledger_exception_lifecycle_mutation",
                    27,
                    False,
                )
                for name, table in append_only_tables.items()
            })
            rows = []
            for trigger in self.triggers:
                relation, function, tgtype, expected_when = contracts[trigger]
                when_clause = (
                    f" WHEN {expected_when}" if expected_when is not False else ""
                )
                rows.append(
                    {
                        "tgname": trigger,
                        "tgenabled": "O",
                        "relation_name": relation,
                        "function_schema": "public",
                        "function_name": function,
                        "tgtype": tgtype,
                        "has_when": expected_when is not False,
                        "trigger_definition": (
                            f"CREATE TRIGGER {trigger} TEST{when_clause} "
                            f"EXECUTE FUNCTION {function}()"
                        ),
                    }
                )
            return _Result(rows=rows)
        if "AS exact_contract" in normalized:
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "installed": True,
                        "exact_contract": True,
                    }
                    for signature in params[0]
                ]
            )
        if (
            "procedure.proconfig" in normalized
            and "AS function_schema" in normalized
        ):
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "installed": True,
                        "function_schema": "public",
                        "proconfig": [
                            (
                                "search_path=pg_catalog, public, pg_temp"
                                if self.hardened_search_path
                                else "search_path=pg_catalog"
                            )
                        ],
                    }
                    for signature in params[0]
                ]
            )
        if "procedure.prosecdef" in normalized and "AS web_execute" in normalized:
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "installed": True,
                        "prosecdef": True,
                        "owner": "lawcase_ledger_confirmation_owner",
                        "proconfig": [
                            (
                                "search_path=pg_catalog, public, pg_temp"
                                if signature in {
                                    "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
                                    "public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()",
                                }
                                else "search_path=pg_catalog"
                            )
                            if self.definer_search_path else "search_path=public"
                        ],
                        "web_execute": False,
                        "worker_execute": False,
                        "public_execute": False,
                    }
                    for signature in params[0]
                ]
            )
        if "procedure.prosecdef" in normalized:
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "installed": True,
                        "prosecdef": True,
                        "owner": "lawcase_ledger_confirmation_owner",
                        "proconfig": [
                            (
                                "search_path=pg_catalog, public, pg_temp"
                                if signature in {
                                    "public.enforce_case_agent_ledger_extraction_confirmation_integrity()",
                                    "public.enforce_case_agent_ledger_extraction_batch_confirmation_completeness()",
                                }
                                else "search_path=pg_catalog"
                            )
                            if self.definer_search_path else "search_path=public"
                        ],
                    }
                    for signature in params[0]
                ]
            )
        if "AS opposite_execute" in normalized:
            if "%%_from_web_session(%%" not in sql:
                raise AssertionError(
                    "psycopg LIKE wildcards must be escaped as %% placeholders"
                )
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "intended_execute": True,
                        "opposite_execute": False,
                        "public_execute": self.public_execute,
                    }
                    for signature in params[0]
                ]
            )
        if "AS web_execute" in normalized and "AS worker_execute" in normalized:
            return _Result(
                rows=[
                    {
                        "signature": signature,
                        "installed": True,
                        "web_execute": False,
                        "worker_execute": False,
                        "public_execute": False,
                    }
                    for signature in params[0]
                ]
            )
        if "AS can_mutate" in normalized:
            return _Result(
                rows=[
                    {
                        "role_name": role,
                        "object_name": table,
                        "can_mutate": False,
                    }
                    for role in ("lawcase_web_application", "lawcase_agent_worker")
                    for table in self.tables
                ]
            )
        if "AS runtime_can_select" in normalized:
            return _Result(
                rows=[
                    {
                        "role_name": role,
                        "object_name": table,
                        "runtime_can_select": True,
                    }
                    for role in ("lawcase_web_application", "lawcase_agent_worker")
                    for table in params[0]
                ]
            )
        if "AS table_select" in normalized and "AS column_select" in normalized:
            rows = []
            for role in ("lawcase_web_application", "lawcase_agent_worker"):
                for table, columns in self.restricted_table_columns.items():
                    allowed = set(
                        self.column_read_contract.get(role, {}).get(table, set())
                    )
                    if self.column_projection_drift and role == "lawcase_web_application" \
                            and table == "case_agent_ledger_exception_control_assignments":
                        allowed.add("actor_id")
                    for column in columns:
                        selected = column in allowed
                        if self.session_column_leak and role == "lawcase_web_application" \
                                and table == "case_agent_ledger_exception_control_assignments" \
                                and column == "web_session_id":
                            selected = True
                        rows.append(
                            {
                                "role_name": role,
                                "object_name": table,
                                "column_name": column,
                                "table_select": self.broad_runtime_select,
                                "column_select": selected,
                            }
                        )
            return _Result(rows=rows)
        if "AS can_select" in normalized:
            return _Result(
                rows=[{"object_name": table, "can_select": True} for table in params[0]]
            )
        if "privilege_name" in normalized and "AS allowed" in normalized:
            return _Result(
                rows=[
                    {
                        "object_name": object_name,
                        "privilege_name": privilege,
                        "allowed": True,
                    }
                    for privilege, objects in (
                        ("INSERT", params[0]),
                        ("UPDATE", params[1]),
                    )
                    for object_name in objects
                ]
            )
        if "AS version_update" in normalized:
            return _Result(
                row={
                    "version_update": True,
                    "updated_at_update": True,
                    "session_lock": True,
                    "user_lock": True,
                    "role_lock": True,
                    "run_lock": True,
                }
            )
        if "AS can_verify_staging" in normalized:
            return _Result(row={"can_verify_staging": True})
        raise AssertionError(f"unexpected preflight SQL: {normalized}")


class _Context:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class _VersionError(psycopg.DatabaseError):
    sqlstate = "P4091"


class _IntentError(psycopg.DatabaseError):
    sqlstate = "P4092"


class _UnknownError(psycopg.DatabaseError):
    sqlstate = "P0001"


class PostgresLedgerExceptionFollowupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.followup_id = _id()
        self.decision_id = _id()
        self.worker = Actor(
            _id(), self.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.lead = Actor(
            _id(), self.firm_id, frozenset({Role.LEAD_LAWYER})
        )
        self.store = PostgresCaseLedgerExceptionFollowupStore(
            "postgresql://not-used.invalid/lawcase"
        )

    def _run(self, connection, callback):
        with patch(
            "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            return callback()

    def test_worker_binds_exact_task_through_one_definer_function(self) -> None:
        binding_id = _id()
        receipt = {
            "command_name": "BIND_LEDGER_EXCEPTION_REEXTRACTION_TASK",
            "idempotency_key": "reextract-bind-0001",
            "matter_id": self.matter_id,
            "matter_version": 9,
            "object_type": "CASE_LEDGER_EXCEPTION_REEXTRACTION_TASK_BINDING",
            "object_id": binding_id,
        }
        connection = _Connection(({"row": {"receipt": receipt}},))
        result = self._run(
            connection,
            lambda: self.store.bind_reextraction_task(
                matter_id=self.matter_id,
                actor=self.worker,
                expected_version=9,
                idempotency_key="reextract-bind-0001",
                followup_id=self.followup_id,
                run_id=_id(),
                graph_id=_id(),
                task_id=_id(),
            ),
        )
        self.assertEqual(result.task_binding_id, binding_id)
        self.assertEqual(len(connection.executed), 2)
        self.assertIn(
            "bind_case_agent_ledger_exception_reextraction_task_from_worker",
            connection.executed[-1][0],
        )

    def test_list_is_tenant_role_authorized_and_only_queries_active_heads(self) -> None:
        active = {
            "followup_id": self.followup_id,
            "origin_exception_decision_id": self.decision_id,
            "origin_exception_group_id": _id(),
            "origin_extraction_batch_id": _id(),
            "followup_kind": "REEXTRACTION",
            "created_matter_version": 9,
            "created_at": datetime.now(timezone.utc),
            "current_state": "ACTIVE",
            "head_sequence": 1,
            "reason_code": "SOURCE_QUALITY_INSUFFICIENT",
            "reason_note": None,
            "candidate_count": 2,
            "canonical_reason_codes": ["OCR_DERIVED"],
            "evidence_page_ids": [_id(), _id()],
            "evidence_page_count": 2,
            "active_total_count": 1,
            "evidence_request_id": None,
            "acceptance_criteria": None,
            "automation_status": "WAITING_FOR_PLAN",
            "control_health": "HEALTHY",
        }
        connection = _Connection(
            ({"row": {"permitted": True}}, {"rows": (active,)})
        )
        followups = self._run(
            connection,
            lambda: self.store.list_active_followups(
                matter_id=self.matter_id,
                actor=self.lead,
                offset=0,
                limit=50,
            ),
        )
        self.assertEqual(followups.total_count, 1)
        self.assertEqual(followups.followups[0].followup_id, self.followup_id)
        self.assertIs(
            followups.followups[0].control_health,
            LedgerExceptionControlHealth.HEALTHY,
        )
        query, params = connection.executed[-1]
        self.assertIn("head.current_state = 'ACTIVE'", query)
        self.assertNotIn("subject_hash", query)
        self.assertNotIn("followup.origin_run_id", query)
        self.assertEqual(params, (self.firm_id, self.matter_id, 50, 0))

    def test_satisfy_reextraction_graph_replays_exact_atomic_receipt(self) -> None:
        run_id = _id()
        graph_id = _id()
        receipt = {
            "command_name": "SATISFY_LEDGER_EXCEPTION_REEXTRACTION_SET",
            "idempotency_key": "reextract-finish-0001",
            "matter_id": self.matter_id,
            "matter_version": 10,
            "audit_event_id": _id(),
            "object_type": "CASE_LEDGER_EXCEPTION_REEXTRACTION_SET",
            "object_id": graph_id,
            "followup_count": 2,
        }
        connection = _Connection(
            (
                {"row": {"receipt": receipt}},
                {"row": {"receipt": receipt}},
            )
        )
        callback = lambda: self.store.satisfy_reextraction_graph(
            matter_id=self.matter_id,
            actor=self.worker,
            run_id=run_id,
            graph_id=graph_id,
            expected_version=9,
            idempotency_key="reextract-finish-0001",
        )
        first = self._run(connection, callback)
        second = self._run(connection, callback)
        self.assertEqual(first, second)
        self.assertEqual(first.followup_count, 2)
        signature = inspect.signature(
            PostgresCaseLedgerExceptionFollowupStore.satisfy_reextraction_graph
        ).parameters
        self.assertNotIn("followup_id", signature)
        self.assertNotIn("reextraction_batch_id", signature)

    def test_more_evidence_passes_exact_source_set_after_database_role_check(self) -> None:
        request_id = _id()
        source_id = _id()
        active = {
            "followup_id": self.followup_id,
            "origin_exception_decision_id": self.decision_id,
            "matter_id": self.matter_id,
            "followup_kind": "MORE_EVIDENCE",
            "subject_hash": "a" * 64,
            "current_state": "ACTIVE",
            "head_sequence": 1,
            "evidence_request_id": request_id,
        }
        receipt = {
            "command_name": "RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP",
            "idempotency_key": "more-evidence-0001",
            "matter_id": self.matter_id,
            "matter_version": 10,
            "audit_event_id": _id(),
            "object_type": "CASE_LEDGER_EXCEPTION_FOLLOWUP",
            "object_id": self.followup_id,
        }
        connection = _Connection(
            (
                {"row": {"permitted": True}},
                {"row": active},
                {"row": {"receipt": receipt}},
            )
        )
        result = self._run(
            connection,
            lambda: self.store.resolve_followup(
                matter_id=self.matter_id,
                actor=self.lead,
                server_session_id=_id(),
                expected_version=9,
                idempotency_key="more-evidence-0001",
                followup_id=self.followup_id,
                action=LedgerExceptionFollowupAction.CONFIRM_MORE_EVIDENCE,
                managed_evidence_sources=(
                    ManagedEvidenceSourceRef(
                        ManagedEvidenceSourceType.EVIDENCE_FILE,
                        source_id,
                    ),
                ),
                reason_note="已核验新增来源",
            ),
        )
        self.assertEqual(result.matter_version, 10)
        command = connection.executed[-1]
        self.assertIn(
            "resolve_case_agent_ledger_exception_followup_from_web_session",
            command[0],
        )
        self.assertEqual(command[1][6], request_id)
        self.assertEqual(command[1][7].obj[0]["object_id"], source_id)
        self.assertNotIn(
            "managed_evidence_request_id",
            inspect.signature(
                PostgresCaseLedgerExceptionFollowupStore.resolve_followup
            ).parameters,
        )

    def test_server_selected_control_transfer_is_session_bound_and_replayable(self) -> None:
        replacement_run_id = _id()
        assignment_id = _id()
        audit_id = _id()
        receipt = {
            "command_name": "TRANSFER_CASE_LEDGER_EXCEPTION_CONTROL",
            "idempotency_key": "recover-control-0001",
            "matter_id": self.matter_id,
            "matter_version": 9,
            "audit_event_id": audit_id,
            "object_type": "CASE_LEDGER_EXCEPTION_CONTROL_ASSIGNMENT",
            "object_id": assignment_id,
            "control_health": "HEALTHY",
        }
        connection = _Connection(
            ({"row": {"receipt": receipt}}, {"row": {"receipt": receipt}})
        )
        session_id = _id()
        callback = lambda: self.store.transfer_control_to_recovery_run(
            matter_id=self.matter_id,
            actor=self.lead,
            server_session_id=session_id,
            replacement_run_id=replacement_run_id,
            expected_version=9,
            idempotency_key="recover-control-0001",
        )
        first = self._run(connection, callback)
        second = self._run(connection, callback)
        self.assertEqual(first, second)
        self.assertEqual(first.control_assignment_id, assignment_id)
        self.assertIs(first.control_health, LedgerExceptionControlHealth.HEALTHY)
        sql, params = connection.executed[-1]
        self.assertIn(
            "transfer_case_agent_ledger_exception_control_from_web_session",
            sql,
        )
        self.assertEqual(params[:3], (session_id, self.matter_id, replacement_run_id))

    def test_recovery_intent_is_persisted_before_server_selected_run_creation(self) -> None:
        replacement_run_id = _id()
        intent_id = _id()
        receipt = {
            "command_name": "PREPARE_CASE_LEDGER_EXCEPTION_CONTROL_RECOVERY",
            "idempotency_key": "recover-control-prepare-0001",
            "matter_id": self.matter_id,
            "matter_version": 9,
            "object_type": "CASE_LEDGER_EXCEPTION_RECOVERY_INTENT",
            "object_id": intent_id,
            "recovery_state": "PENDING",
            "transfer_idempotency_key": "recover-control-prepare-0001",
            "replacement_run_id": replacement_run_id,
            "run_exists": False,
        }
        connection = _Connection(({"row": {"receipt": receipt}},))
        session_id = _id()
        prepared = self._run(
            connection,
            lambda: self.store.prepare_control_recovery(
                matter_id=self.matter_id,
                actor=self.lead,
                server_session_id=session_id,
                replacement_run_id=replacement_run_id,
                expected_version=9,
                idempotency_key="recover-control-prepare-0001",
            ),
        )
        self.assertEqual(prepared.recovery_intent_id, intent_id)
        self.assertEqual(prepared.recovery_state, "PENDING")
        self.assertEqual(prepared.replacement_run_id, replacement_run_id)
        self.assertFalse(prepared.run_exists)
        sql, params = connection.executed[-1]
        self.assertIn(
            "prepare_case_agent_ledger_exception_control_recovery_from_web_session",
            sql,
        )
        self.assertEqual(
            params[:3], (session_id, self.matter_id, replacement_run_id)
        )

    def test_current_control_state_is_server_only_and_tenant_authorized(self) -> None:
        assignment_id = _id()
        run_id = _id()
        connection = _Connection(
            (
                {"row": {"permitted": True}},
                {
                    "row": {
                        "current_control_assignment_id": assignment_id,
                        "current_state": "RECOVERY_REQUIRED",
                        "head_sequence": 2,
                        "control_run_id": run_id,
                    }
                },
            )
        )
        state = self._run(
            connection,
            lambda: self.store.read_current_control_state(
                matter_id=self.matter_id,
                actor=self.lead,
            ),
        )
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.control_assignment_id, assignment_id)
        self.assertEqual(state.control_run_id, run_id)
        self.assertIs(
            state.control_health,
            LedgerExceptionControlHealth.RECOVERY_REQUIRED,
        )

    def test_eligible_more_evidence_sources_are_opaque_and_request_scoped(self) -> None:
        request_id = _id()
        source_id = _id()
        active = {
            "followup_id": self.followup_id,
            "origin_exception_decision_id": self.decision_id,
            "matter_id": self.matter_id,
            "followup_kind": "MORE_EVIDENCE",
            "subject_hash": "a" * 64,
            "current_state": "ACTIVE",
            "head_sequence": 1,
            "evidence_request_id": request_id,
        }
        created_at = datetime.now(timezone.utc)
        connection = _Connection(
            (
                {"row": {"permitted": True}},
                {"row": active},
                {
                    "rows": (
                        {
                            "object_type": "EVIDENCE_FILE",
                            "object_id": source_id,
                            "display_label": "新增银行流水.pdf",
                            "created_at": created_at,
                            "eligible_total_count": 1,
                        },
                    )
                },
            )
        )
        sources = self._run(
            connection,
            lambda: self.store.list_eligible_managed_evidence_sources(
                matter_id=self.matter_id,
                actor=self.lead,
                followup_id=self.followup_id,
                offset=0,
                limit=50,
            ),
        )
        self.assertEqual(sources.total_count, 1)
        self.assertEqual(sources.sources[0].object_id, source_id)
        query = connection.executed[-1][0]
        self.assertIn("original.created_at > request.created_at", query)
        self.assertIn("successor.supersedes_file_id", query)
        self.assertNotIn("source_object_key", query)
        self.assertNotIn("content_sha256", query)

    def test_lawyer_commit_lost_replay_reaches_definer_after_head_is_closed(self) -> None:
        self.assertNotIn(
            "current_state = 'ACTIVE'",
            inspect.getsource(_read_followup_for_command),
        )
        active = {
            "followup_id": self.followup_id,
            "origin_exception_decision_id": self.decision_id,
            "matter_id": self.matter_id,
            "followup_kind": "DEFERRED_REVIEW",
            "subject_hash": "b" * 64,
            "current_state": "ACTIVE",
            "head_sequence": 1,
            "evidence_request_id": None,
        }
        closed = {**active, "current_state": "RESUMED", "head_sequence": 2}
        receipt = {
            "command_name": "RESOLVE_CASE_LEDGER_EXCEPTION_FOLLOWUP",
            "idempotency_key": "resume-followup-0001",
            "matter_id": self.matter_id,
            "matter_version": 10,
            "audit_event_id": _id(),
            "object_type": "CASE_LEDGER_EXCEPTION_FOLLOWUP",
            "object_id": self.followup_id,
        }
        connection = _Connection(
            (
                {"row": {"permitted": True}},
                {"row": active},
                {"row": {"receipt": receipt}},
                {"row": {"permitted": True}},
                {"row": closed},
                {"row": {"receipt": receipt}},
            )
        )
        session_id = _id()
        callback = lambda: self.store.resolve_followup(
            matter_id=self.matter_id,
            actor=self.lead,
            server_session_id=session_id,
            expected_version=9,
            idempotency_key="resume-followup-0001",
            followup_id=self.followup_id,
            action=LedgerExceptionFollowupAction.RESUME,
            reason_note="恢复办理",
        )
        first = self._run(connection, callback)
        second = self._run(connection, callback)
        self.assertEqual(first, second)
        self.assertEqual(
            sum(
                "resolve_case_agent_ledger_exception_followup_from_web_session"
                in sql
                for sql, _ in connection.executed
            ),
            2,
        )

    def test_only_explicit_database_conflicts_are_mapped(self) -> None:
        run_id = _id()
        graph_id = _id()
        for error, expected in (
            (_VersionError("stale"), VersionConflict),
            (_IntentError("intent"), IdempotencyConflict),
        ):
            connection = _Connection((error,))
            with self.subTest(sqlstate=error.sqlstate), self.assertRaises(expected):
                self._run(
                    connection,
                    lambda: self.store.satisfy_reextraction_graph(
                        matter_id=self.matter_id,
                        actor=self.worker,
                        run_id=run_id,
                        graph_id=graph_id,
                        expected_version=9,
                        idempotency_key="reextract-finish-0001",
                    ),
                )
        connection = _Connection((_UnknownError("unknown"),))
        with self.assertRaises(_UnknownError):
            self._run(
                connection,
                lambda: self.store.satisfy_reextraction_graph(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    run_id=run_id,
                    graph_id=graph_id,
                    expected_version=9,
                    idempotency_key="reextract-finish-0001",
                ),
            )

    def test_worker_must_be_dedicated_and_public_signature_has_no_freeform_ids(self) -> None:
        mixed = Actor(
            self.worker.actor_id,
            self.firm_id,
            frozenset({Role.SYSTEM_WORKER, Role.LEAD_LAWYER}),
        )
        with self.assertRaises(PermissionError):
            self.store.bind_reextraction_task(
                matter_id=self.matter_id,
                actor=mixed,
                expected_version=9,
                idempotency_key="reextract-bind-0001",
                followup_id=self.followup_id,
                run_id=_id(),
                graph_id=_id(),
                task_id=_id(),
            )
        parameters = inspect.signature(
            PostgresCaseLedgerExceptionFollowupStore.resolve_followup
        ).parameters
        self.assertNotIn("url", parameters)
        self.assertNotIn("file_path", parameters)
        self.assertNotIn("prompt", parameters)

    def test_malformed_receipt_fails_closed(self) -> None:
        connection = _Connection(({"row": {"receipt": {"matter_id": self.matter_id}}},))
        with self.assertRaises(LedgerExceptionFollowupBlocked):
            self._run(
                connection,
                lambda: self.store.satisfy_reextraction_graph(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    run_id=_id(),
                    graph_id=_id(),
                    expected_version=9,
                    idempotency_key="reextract-finish-0001",
                ),
            )

    def test_preflight_checks_complete_authority_and_rejects_public_execute(self) -> None:
        for public_execute, expected_error in ((False, None), (True, LedgerExceptionFollowupBlocked)):
            connection = _PreflightConnection(public_execute=public_execute)
            with patch(
                "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
                return_value=_Context(connection),
            ):
                if expected_error is None:
                    preflight_case_agent_ledger_exception_followup_schema(
                        dsn="postgresql://not-used.invalid/lawcase",
                        firm_id=self.firm_id,
                    )
                else:
                    with self.assertRaises(expected_error):
                        preflight_case_agent_ledger_exception_followup_schema(
                            dsn="postgresql://not-used.invalid/lawcase",
                            firm_id=self.firm_id,
                        )

    def test_preflight_rejects_unhardened_upgrade_trigger_search_path(self) -> None:
        connection = _PreflightConnection(hardened_search_path=False)
        with patch(
            "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaises(LedgerExceptionFollowupBlocked):
                preflight_case_agent_ledger_exception_followup_schema(
                    dsn="postgresql://not-used.invalid/lawcase",
                    firm_id=self.firm_id,
                )

    def test_preflight_rejects_untrusted_definer_search_path(self) -> None:
        connection = _PreflightConnection(definer_search_path=False)
        with patch(
            "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaises(LedgerExceptionFollowupBlocked):
                preflight_case_agent_ledger_exception_followup_schema(
                    dsn="postgresql://not-used.invalid/lawcase",
                    firm_id=self.firm_id,
                )

    def test_preflight_rejects_server_session_column_read_leak(self) -> None:
        connection = _PreflightConnection(session_column_leak=True)
        with patch(
            "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(
                LedgerExceptionFollowupBlocked,
                "server session identifier",
            ):
                preflight_case_agent_ledger_exception_followup_schema(
                    dsn="postgresql://not-used.invalid/lawcase",
                    firm_id=self.firm_id,
                )

    def test_preflight_rejects_broad_or_extra_runtime_read_grants(self) -> None:
        for connection, message in (
            (
                _PreflightConnection(broad_runtime_select=True),
                "broad lifecycle read grant",
            ),
            (
                _PreflightConnection(column_projection_drift=True),
                "column projection grants are not exact",
            ),
        ):
            with self.subTest(message=message), patch(
                "case_kernel.case_agent_ledger_exception_followup_postgres.psycopg.connect",
                return_value=_Context(connection),
            ):
                with self.assertRaisesRegex(
                    LedgerExceptionFollowupBlocked,
                    message,
                ):
                    preflight_case_agent_ledger_exception_followup_schema(
                        dsn="postgresql://not-used.invalid/lawcase",
                        firm_id=self.firm_id,
                    )


if __name__ == "__main__":
    unittest.main()
