from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest
from uuid import uuid4

from case_kernel.case_agent_runtime_postgres import (
    CaseAgentRuntimePersistenceBlocked,
    PostgresCaseAgentMatterPrincipalReadiness,
    PostgresCaseAgentRunnerIncidentSink,
    PostgresEvidenceProjectionAuthorizationPort,
    PostgresManagedArtifactAccessPort,
    preflight_case_agent_runtime_contract,
)
from case_kernel.case_agent_runtime_identity import case_agent_worker_id
from case_kernel.case_agent_verifier import (
    FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS,
)
from case_kernel.case_agent_runtime_postgres import PostgresCaseAgentRunInbox
from case_kernel.case_agent_supervisor import ArtifactReceipt
from case_kernel.models import Actor, Role


class _Cursor:
    def __init__(self, *, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.sql = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, params))
        if normalized.startswith("SELECT inbox.run_id, inbox.matter_id"):
            return _Cursor(
                row={
                    "run_id": self.row["run_id"],
                    "matter_id": self.row["matter_id"],
                }
            )
        if "UPDATE case_agent_run_inbox inbox" in normalized:
            return _Cursor(row=self.row)
        return _Cursor()


class _IncidentConnection:
    def __init__(self, *, insert_rowcount=1, already_recorded=False):
        self.insert_rowcount = insert_rowcount
        self.already_recorded = already_recorded
        self.sql = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, params))
        if "INSERT INTO case_agent_command_audits" in normalized:
            return _Cursor(rowcount=self.insert_rowcount)
        if "AS already_recorded" in normalized:
            return _Cursor(row={"already_recorded": self.already_recorded})
        return _Cursor()


class _ArtifactConnection:
    def __init__(self, row):
        self.row = row
        self.sql = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, params))
        if "FROM case_agent_review_candidates candidate" in normalized:
            return _Cursor(row=self.row)
        return _Cursor()


class _CandidateStore:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def verify_case_agent_review_candidate(self, *_args, **_kwargs):
        return None

    def read_case_agent_review_candidate(self, stored, **kwargs):
        self.calls.append((stored, kwargs))
        return self.content


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class _PreflightConnection:
    def __init__(
        self,
        *,
        firm_id,
        actor_ids,
        missing_column=None,
        missing_trigger=None,
        missing_function=None,
        unbound_matter_count=0,
        bad_0053_owner=False,
        bad_0053_acl=False,
        bad_0060_acl=False,
    ):
        from case_kernel import case_agent_runtime_postgres as runtime_postgres

        self.firm_id = firm_id
        self.actor_ids = set(actor_ids)
        self.missing_column = missing_column
        self.missing_trigger = missing_trigger
        self.missing_function = missing_function
        self.unbound_matter_count = unbound_matter_count
        self.bad_0053_owner = bad_0053_owner
        self.bad_0053_acl = bad_0053_acl
        self.bad_0060_acl = bad_0060_acl
        self.sql = []
        self.required_columns = runtime_postgres._CASE_AGENT_RUNTIME_REQUIRED_COLUMNS
        self.required_triggers = runtime_postgres._CASE_AGENT_RUNTIME_REQUIRED_TRIGGERS
        self.required_functions = runtime_postgres._CASE_AGENT_RUNTIME_REQUIRED_FUNCTIONS

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, params))
        if "FROM information_schema.columns" in normalized:
            rows = []
            for table_name, columns in self.required_columns.items():
                for column in columns:
                    if (table_name, column) != self.missing_column:
                        rows.append({"table_name": table_name, "column_name": column})
            return _RowsCursor(rows)
        if "FROM pg_catalog.pg_trigger" in normalized:
            return _RowsCursor(
                [
                    {"trigger_name": trigger}
                    for trigger in self.required_triggers
                    if trigger != self.missing_trigger
                ]
            )
        if "FROM information_schema.routines" in normalized:
            return _RowsCursor(
                [
                    {"routine_name": function}
                    for function in self.required_functions
                    if function != self.missing_function
                ]
            )
        if "AS execution_table_owner" in normalized:
            owner = "lawcase_agent_worker" if self.bad_0053_owner else "lawcase_schema_owner"
            return _RowsCursor(
                [
                    {
                        "execution_table_owner": owner,
                        "goal_table_owner": "lawcase_schema_owner",
                    }
                ]
            )
        if (
            "FROM pg_catalog.pg_proc procedure" in normalized
            and "AS verifier_execute" not in normalized
        ):
            owner = "lawcase_agent_worker" if self.bad_0053_owner else "lawcase_schema_owner"
            return _RowsCursor(
                [
                    {
                        "proname": "case_agent_active_plan_execution_valid",
                        "owner_name": owner,
                        "prosecdef": False,
                    },
                    {
                        "proname": "case_agent_requested_deliverables_valid",
                        "owner_name": owner,
                        "prosecdef": False,
                    },
                    {
                        "proname": "guard_case_agent_active_plan_execution_run",
                        "owner_name": owner,
                        "prosecdef": True,
                    },
                ]
            )
        if "AS web_execution_select" in normalized:
            return _RowsCursor(
                [
                    {
                        "web_execution_select": True,
                        "web_execution_insert": True,
                        "web_execution_mutate": self.bad_0053_acl,
                        "worker_execution_select": True,
                        "worker_execution_mutate": False,
                        "web_active_plan_read": not self.bad_0053_acl,
                        "web_goal_identity_read": True,
                        "web_lock_entitlements": True,
                        "web_requested_deliverables_write": True,
                        "web_active_execution_write": True,
                        "web_goal_extension_update": False,
                        "worker_goal_extension_select": True,
                        "worker_goal_extension_write": False,
                        "web_validation_execute": True,
                        "web_guard_execute": False,
                        "worker_0053_function_execute": False,
                    }
                ]
            )
        if "AS verifier_execute" in normalized:
            return _RowsCursor(
                [
                    {
                        "owner_name": "lawcase_schema_owner",
                        "security_definer": True,
                        "verifier_execute": not self.bad_0060_acl,
                        "worker_execute": False,
                        "web_execute": False,
                        "verifier_outbox_insert": not self.bad_0060_acl,
                        "verifier_idempotency_insert": not self.bad_0060_acl,
                        "verifier_outbox_mutate": False,
                        "verifier_idempotency_mutate": False,
                    }
                ]
            )
        if "AS verifier_snapshot_execute" in normalized:
            return _RowsCursor(
                [{
                    "verifier_snapshot_execute": not self.bad_0060_acl,
                    "verifier_outbox_insert": not self.bad_0060_acl,
                    "verifier_idempotency_insert": not self.bad_0060_acl,
                }]
            )
        if "FROM pg_catalog.pg_class" in normalized:
            return _RowsCursor(
                [
                    {
                        "relname": table_name,
                        "relrowsecurity": True,
                        "relforcerowsecurity": True,
                    }
                    for table_name in self.required_columns
                ]
            )
        if "FROM users principal" in normalized:
            actor_id, firm_id = params
            row = None
            if actor_id in self.actor_ids and firm_id == self.firm_id:
                row = {
                    "user_id": actor_id,
                    "firm_id": firm_id,
                    "status": "ACTIVE",
                    "has_active_non_worker_role": False,
                }
            return _RowsCursor([] if row is None else [row])
        if "unbound_matter_count" in normalized:
            return _RowsCursor(
                [{"unbound_matter_count": self.unbound_matter_count}]
            )
        if "JOIN users principal" in normalized:
            execution_id, verifier_id, firm_id, joined_firm_id = params
            rows = []
            if firm_id == joined_firm_id == self.firm_id:
                for actor_id in (execution_id, verifier_id):
                    if actor_id in self.actor_ids:
                        rows.append(
                            {
                                "actor_id": actor_id,
                                "status": "ACTIVE",
                                "has_active_non_worker_role": False,
                            }
                        )
            return _RowsCursor(rows)
        return _RowsCursor([])


class _RowsCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return None if not self.rows else self.rows[0]

    def fetchall(self):
        return list(self.rows)


class CaseAgentRuntimePostgresTests(unittest.TestCase):
    def test_evidence_projection_tool_is_fixed_by_server_composition(self) -> None:
        firm_id, actor_id, matter_id = str(uuid4()), str(uuid4()), str(uuid4())
        run_id, task_id = str(uuid4()), str(uuid4())
        page_id = str(uuid4())
        worker = Actor(actor_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        binding = {
            "matter_id": matter_id,
            "requested_by": str(uuid4()),
            "requested_by_roles": [Role.LEAD_LAWYER.value],
        }
        ledger = PostgresEvidenceProjectionAuthorizationPort(
            dsn="postgresql://not-used.invalid/lawcase",
            worker_actor=worker,
            required_tool="extract_case_ledger",
        )
        with patch.object(ledger, "_resolve_task", return_value=binding) as resolve:
            authorization = ledger.resolve_evidence_projection(
                run_id=run_id,
                task_id=task_id,
                task_input_hash="a" * 64,
                input_refs=(f"evidence-page:{page_id}",),
            )
        self.assertEqual(authorization.evidence_page_ids, (page_id,))
        self.assertEqual(
            resolve.call_args.kwargs["required_tool"], "extract_case_ledger"
        )

        pdf = PostgresEvidenceProjectionAuthorizationPort(
            dsn="postgresql://not-used.invalid/lawcase",
            worker_actor=worker,
        )
        with patch.object(
            pdf,
            "_resolve_task",
            side_effect=CaseAgentRuntimePersistenceBlocked(
                "compiled task tool differs from the runtime port"
            ),
        ) as wrong_tool:
            with self.assertRaisesRegex(
                CaseAgentRuntimePersistenceBlocked, "tool differs"
            ):
                pdf.resolve_evidence_projection(
                    run_id=run_id,
                    task_id=task_id,
                    task_input_hash="a" * 64,
                    input_refs=(f"evidence-page:{page_id}",),
                )
        self.assertEqual(
            wrong_tool.call_args.kwargs["required_tool"], "extract_pdf_text"
        )

    def test_verifier_artifact_reader_tracks_exact_first_release_registry(self) -> None:
        self.assertEqual(
            PostgresManagedArtifactAccessPort._FIRST_RELEASE_KINDS,
            frozenset(
                kind
                for kind, _schema
                in FIRST_RELEASE_EXECUTABLE_REVIEW_CANDIDATE_SCHEMAS
            ),
        )
    def test_runner_incident_sink_persists_only_sanitized_code_and_opaque_ids(self) -> None:
        firm_id, actor_id, matter_id, run_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        actor = Actor(actor_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        connection = _IncidentConnection()
        sink = PostgresCaseAgentRunnerIncidentSink(
            dsn="postgresql://not-used.invalid/lawcase",
            actor=actor,
        )
        occurred_at = datetime.now(timezone.utc)
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            sink.record(
                firm_id=firm_id,
                worker_id=case_agent_worker_id(firm_id),
                run_id=run_id,
                matter_id=matter_id,
                code="CASE_AGENT_MEMORY_CHECKPOINT_BLOCKED",
                occurred_at=occurred_at,
            )
        insert_sql, params = next(
            item
            for item in connection.sql
            if "INSERT INTO case_agent_command_audits" in item[0]
        )
        self.assertIn("worker_role.role = 'SYSTEM_WORKER'", insert_sql)
        self.assertIn('"code":"CASE_AGENT_MEMORY_CHECKPOINT_BLOCKED"', params[2])
        self.assertNotIn("path", params[2].lower())
        self.assertNotIn("object", params[2].lower())

    def test_runner_incident_sink_fails_closed_when_run_or_binding_is_missing(self) -> None:
        firm_id, actor_id = str(uuid4()), str(uuid4())
        actor = Actor(actor_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        connection = _IncidentConnection(insert_rowcount=0, already_recorded=False)
        sink = PostgresCaseAgentRunnerIncidentSink(
            dsn="postgresql://not-used.invalid/lawcase",
            actor=actor,
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(
                CaseAgentRuntimePersistenceBlocked, "binding is unavailable"
            ):
                sink.record(
                    firm_id=firm_id,
                    worker_id=case_agent_worker_id(firm_id),
                    run_id=str(uuid4()),
                    matter_id=str(uuid4()),
                    code="CASE_AGENT_RUN_STEP_BLOCKED",
                    occurred_at=datetime.now(timezone.utc),
                )

    def test_web_readiness_principal_probe_requires_both_active_pure_identities(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        connection = _PreflightConnection(
            firm_id=firm_id,
            actor_ids={execution_id, verifier_id},
        )
        probe = PostgresCaseAgentMatterPrincipalReadiness(
            "postgresql://not-used.invalid/lawcase"
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            self.assertTrue(
                probe(
                    firm_id=firm_id,
                    execution_actor_id=execution_id,
                    verifier_actor_id=verifier_id,
                )
            )

    def test_startup_preflight_requires_0033_0034_0035_on_both_principals(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        execution = _PreflightConnection(
            firm_id=firm_id, actor_ids={execution_id, verifier_id}
        )
        verifier = _PreflightConnection(
            firm_id=firm_id, actor_ids={verifier_id}
        )
        contexts = iter((_Context(execution), _Context(verifier)))
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            side_effect=lambda *_args, **_kwargs: next(contexts),
        ) as connect:
            preflight_case_agent_runtime_contract(
                execution_dsn="postgresql://execution.invalid/lawcase",
                verifier_dsn="postgresql://verifier.invalid/lawcase",
                firm_id=firm_id,
                execution_actor_id=execution_id,
                verifier_actor_id=verifier_id,
            )
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(
            any("FROM case_agent_run_inbox" in sql for sql, _ in execution.sql)
        )
        self.assertTrue(
            any("unbound_matter_count" in sql for sql, _ in execution.sql)
        )
        self.assertTrue(
            any(
                "FROM case_agent_verification_receipts" in sql
                for sql, _ in verifier.sql
            )
        )

    def test_startup_preflight_blocks_missing_0035_contract_before_heartbeat(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        connection = _PreflightConnection(
            firm_id=firm_id,
            actor_ids={execution_id, verifier_id},
            missing_column=(
                "case_agent_lawyer_decision_signals",
                "decision_hash",
            ),
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ) as connect:
            with self.assertRaisesRegex(
                CaseAgentRuntimePersistenceBlocked, "through 0035"
            ):
                preflight_case_agent_runtime_contract(
                    execution_dsn="postgresql://execution.invalid/lawcase",
                    verifier_dsn="postgresql://verifier.invalid/lawcase",
                    firm_id=firm_id,
                    execution_actor_id=execution_id,
                    verifier_actor_id=verifier_id,
                )
        self.assertEqual(connect.call_count, 1)

    def test_startup_preflight_requires_verifier_snapshot_authority(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        connection = _PreflightConnection(
            firm_id=firm_id,
            actor_ids={execution_id, verifier_id},
            bad_0060_acl=True,
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(
                CaseAgentRuntimePersistenceBlocked,
                "0060 verifier snapshot authority",
            ):
                preflight_case_agent_runtime_contract(
                    execution_dsn="postgresql://execution.invalid/lawcase",
                    verifier_dsn="postgresql://verifier.invalid/lawcase",
                    firm_id=firm_id,
                    execution_actor_id=execution_id,
                    verifier_actor_id=verifier_id,
                )

    def test_startup_preflight_blocks_missing_0043_plan_promotion_contract(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        for missing_column, missing_trigger in (
            (("case_work_plan_items", "purpose"), None),
            (
                None,
                "case_agent_work_plan_promotion_valid",
            ),
        ):
            with self.subTest(
                missing_column=missing_column, missing_trigger=missing_trigger
            ):
                connection = _PreflightConnection(
                    firm_id=firm_id,
                    actor_ids={execution_id, verifier_id},
                    missing_column=missing_column,
                    missing_trigger=missing_trigger,
                )
                with patch(
                    "case_kernel.case_agent_runtime_postgres.psycopg.connect",
                    return_value=_Context(connection),
                ) as connect:
                    with self.assertRaisesRegex(
                        CaseAgentRuntimePersistenceBlocked, "through 0053"
                    ):
                        preflight_case_agent_runtime_contract(
                            execution_dsn="postgresql://execution.invalid/lawcase",
                            verifier_dsn="postgresql://verifier.invalid/lawcase",
                            firm_id=firm_id,
                            execution_actor_id=execution_id,
                            verifier_actor_id=verifier_id,
                        )
                self.assertEqual(connect.call_count, 1)

    def test_startup_preflight_blocks_unsafe_0053_owner_or_acl(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        for flag, expected in (
            ({"bad_0053_owner": True}, "managed schema owner"),
            ({"bad_0053_acl": True}, "ACL"),
        ):
            with self.subTest(flag=flag):
                connection = _PreflightConnection(
                    firm_id=firm_id,
                    actor_ids={execution_id, verifier_id},
                    **flag,
                )
                with patch(
                    "case_kernel.case_agent_runtime_postgres.psycopg.connect",
                    return_value=_Context(connection),
                ) as connect:
                    with self.assertRaisesRegex(
                        CaseAgentRuntimePersistenceBlocked, expected
                    ):
                        preflight_case_agent_runtime_contract(
                            execution_dsn="postgresql://execution.invalid/lawcase",
                            verifier_dsn="postgresql://verifier.invalid/lawcase",
                            firm_id=firm_id,
                            execution_actor_id=execution_id,
                            verifier_actor_id=verifier_id,
                        )
                self.assertEqual(connect.call_count, 1)

    def test_startup_preflight_blocks_missing_0053_active_plan_execution(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        for missing_column, missing_trigger in (
            (("case_agent_goals", "requested_deliverables"), None),
            (("case_agent_active_plan_execution_runs", "plan_hash"), None),
            (None, "case_agent_active_plan_execution_runs_guard"),
        ):
            with self.subTest(
                missing_column=missing_column,
                missing_trigger=missing_trigger,
            ):
                connection = _PreflightConnection(
                    firm_id=firm_id,
                    actor_ids={execution_id, verifier_id},
                    missing_column=missing_column,
                    missing_trigger=missing_trigger,
                )
                with patch(
                    "case_kernel.case_agent_runtime_postgres.psycopg.connect",
                    return_value=_Context(connection),
                ) as connect:
                    with self.assertRaisesRegex(
                        CaseAgentRuntimePersistenceBlocked, "through 0053"
                    ):
                        preflight_case_agent_runtime_contract(
                            execution_dsn="postgresql://execution.invalid/lawcase",
                            verifier_dsn="postgresql://verifier.invalid/lawcase",
                            firm_id=firm_id,
                            execution_actor_id=execution_id,
                            verifier_actor_id=verifier_id,
                        )
                self.assertEqual(connect.call_count, 1)

    def test_startup_preflight_blocks_missing_0047_review_resolution_contract(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        for missing_column, missing_trigger, missing_function in (
            (("case_agent_ledger_exception_groups", "risk_policy"), None, None),
            (None, "case_agent_exception_decision_unblocks_snapshot_refresh", None),
            (None, None, "case_agent_ledger_extraction_run_review_resolved"),
        ):
            with self.subTest(
                missing_column=missing_column,
                missing_trigger=missing_trigger,
                missing_function=missing_function,
            ):
                connection = _PreflightConnection(
                    firm_id=firm_id,
                    actor_ids={execution_id, verifier_id},
                    missing_column=missing_column,
                    missing_trigger=missing_trigger,
                    missing_function=missing_function,
                )
                with patch(
                    "case_kernel.case_agent_runtime_postgres.psycopg.connect",
                    return_value=_Context(connection),
                ) as connect:
                    with self.assertRaisesRegex(
                        CaseAgentRuntimePersistenceBlocked,
                        "0047" if missing_function else "0053",
                    ):
                        preflight_case_agent_runtime_contract(
                            execution_dsn="postgresql://execution.invalid/lawcase",
                            verifier_dsn="postgresql://verifier.invalid/lawcase",
                            firm_id=firm_id,
                            execution_actor_id=execution_id,
                            verifier_actor_id=verifier_id,
                        )
                self.assertEqual(connect.call_count, 1)

    def test_startup_preflight_blocks_legacy_matter_without_exact_pair(self) -> None:
        firm_id, execution_id, verifier_id = (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        connection = _PreflightConnection(
            firm_id=firm_id,
            actor_ids={execution_id, verifier_id},
            unbound_matter_count=1,
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            with self.assertRaisesRegex(
                CaseAgentRuntimePersistenceBlocked, "not bound"
            ):
                preflight_case_agent_runtime_contract(
                    execution_dsn="postgresql://execution.invalid/lawcase",
                    verifier_dsn="postgresql://verifier.invalid/lawcase",
                    firm_id=firm_id,
                    execution_actor_id=execution_id,
                    verifier_actor_id=verifier_id,
                )

    def test_claim_query_recovers_expired_lease_with_skip_locked_fair_order(self) -> None:
        firm_id, actor_id, matter_id, run_id = (
            str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        )
        actor = Actor(actor_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        row = {
            "run_id": run_id,
            "firm_id": firm_id,
            "matter_id": matter_id,
            "observed_event_version": 8,
            "inbox_version": 4,
            "lease_owner": case_agent_worker_id(firm_id),
            "lease_token": uuid4(),
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=2),
        }
        connection = _Connection(row)
        inbox = PostgresCaseAgentRunInbox(
            dsn="postgresql://not-used.invalid/lawcase", actor=actor
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            claim = inbox.claim_next_run(
                lease_owner=case_agent_worker_id(firm_id), lease_seconds=120
            )
        self.assertIsNotNone(claim)
        queries = "\n".join(sql for sql, _ in connection.sql)
        update_query = next(
            sql
            for sql, _ in connection.sql
            if "UPDATE case_agent_run_inbox inbox" in sql
        )
        self.assertIn("inbox.inbox_status = 'LEASED'", queries)
        self.assertIn("inbox.lease_expires_at <= now()", queries)
        self.assertIn(
            "ORDER BY inbox.available_at, inbox.updated_at, inbox.run_id",
            queries,
        )
        self.assertIn("FOR UPDATE OF inbox SKIP LOCKED", update_query)
        self.assertIn("role.role = 'SYSTEM_WORKER'", queries)
        self.assertIn(
            "case_agent_ledger_exception_followup_heads followup_head",
            queries,
        )
        self.assertIn(
            "case_agent_ledger_exception_followups followup", queries
        )
        self.assertIn(
            "followup.followup_kind IN ('REEXTRACTION', 'MORE_EVIDENCE')",
            queries,
        )
        self.assertIn("control_head.current_state <> 'HEALTHY'", queries)
        self.assertIn(
            "control_assignment.control_run_id <> inbox.run_id",
            queries,
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_intents intent", queries
        )
        self.assertIn(
            "case_agent_ledger_exception_recovery_quarantines quarantine",
            queries,
        )
        self.assertIn("legacy_recovery_goal.objective", queries)
        self.assertIn("legacy_recovery_goal.success_criteria", queries)
        self.assertIn("legacy_recovery_goal.constraints", queries)
        self.assertIn("intent_head.current_outcome <> 'TRANSFERRED'", queries)
        self.assertIn(
            "historical_assignment.control_run_id = inbox.run_id", queries
        )
        self.assertGreaterEqual(queries.count("intent.recovery_goal_id"), 2)
        self.assertGreaterEqual(queries.count("intent.recovery_goal_hash"), 2)
        self.assertGreaterEqual(queries.count("recovery_goal.objective"), 2)
        self.assertGreaterEqual(queries.count("recovery_goal.requested_by"), 2)
        self.assertIn("CASE_LEDGER_EXCEPTION_CONTROL_CLAIM", queries)

    def test_independent_verifier_reauthorizes_and_rereads_candidate(self) -> None:
        from hashlib import sha256

        firm_id, matter_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
        execution_id, verifier_id, artifact_id = (
            str(uuid4()), str(uuid4()), str(uuid4())
        )
        content = b'{"schema_version":"agent-pdf-text-candidate-v1"}'
        content_hash = sha256(content).hexdigest()
        input_hash = sha256(b"input").hexdigest()
        receipt_hash = sha256(b"receipt").hexdigest()
        row = {
            "artifact_id": artifact_id,
            "artifact_kind": "PDF_TEXT_REVIEW_CANDIDATE",
            "content_sha256": content_hash,
            "byte_size": len(content),
            "task_input_hash": input_hash,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "receipt_hash": receipt_hash,
            "source_object_key": (
                f"case-agent-candidates/v1/{firm_id}/{matter_id}/"
                f"{artifact_id}/{content_hash}.json"
            ),
            "source_object_version_id": "version-1",
        }
        connection = _ArtifactConnection(row)
        object_store = _CandidateStore(content)
        actor = Actor(verifier_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        port = PostgresManagedArtifactAccessPort(
            dsn="postgresql://not-used.invalid/lawcase",
            verifier_actor=actor,
            execution_actor_id=execution_id,
            object_store=object_store,
        )
        artifact = ArtifactReceipt(
            artifact_id=artifact_id,
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            content_hash=content_hash,
            byte_size=len(content),
            source_input_hash=input_hash,
            managed_derivative=False,
        )
        with patch(
            "case_kernel.case_agent_runtime_postgres.psycopg.connect",
            return_value=_Context(connection),
        ):
            managed = port.read_managed_artifact(
                firm_id=firm_id,
                matter_id=matter_id,
                run_id=run_id,
                artifact=artifact,
            )
        self.assertEqual(managed.content, content)
        self.assertEqual(managed.object_receipt_hash, receipt_hash)
        query = next(
            sql
            for sql, _ in connection.sql
            if "FROM case_agent_review_candidates candidate" in sql
        )
        self.assertIn("run.current_graph_id = candidate.graph_id", query)
        self.assertIn("verifier_role.role = 'SYSTEM_WORKER'", query)
        self.assertIn("execution_role.role = 'SYSTEM_WORKER'", query)
        self.assertEqual(object_store.calls[0][1], {"artifact_id": artifact_id})


if __name__ == "__main__":
    unittest.main()
