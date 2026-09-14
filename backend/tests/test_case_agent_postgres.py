from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from hashlib import sha256
import unittest
from uuid import uuid4

from case_kernel.case_agent_postgres import (
    PostgresCaseAgentStore,
    _active_plan_execution_can_be_reissued,
    _assert_new_case_agent_run_allowed,
    _classify_run_input_snapshot_status,
    _event_from_row,
    _event_json,
    _assert_final_document_versions,
    _payload_hash,
    _payload_json,
)
from case_kernel.case_agent_supervisor import (
    AgentTaskStatus,
    AgentRunStatus,
    ExternalSubmissionState,
    ResultStatus,
    TaskResultPayload,
    decide_next_commands,
    reduce_agent_event,
)
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked, VersionConflict
from case_kernel.case_agent_supervisor import (
    AgentEventType,
    AgentDeliverableFormat,
    AgentDeliverableKind,
    AgentGoal,
    AgentSupervisorBlocked,
    AgentSupervisorEvent,
    CaseSnapshotRef,
    RunCreatedPayload,
    RunResourceBudget,
)
from case_kernel.models import Actor, Role


class FinalDocumentVersionTransactionTests(unittest.TestCase):
    def test_locks_all_roots_before_rechecking_and_rejects_stale_or_pending(self) -> None:
        actor = SimpleNamespace(actor_id=str(uuid4()), firm_id=str(uuid4()))
        ids = [str(uuid4()), str(uuid4())]
        roots = [{"package_id": str(uuid4()), "candidate_artifact_id": id} for id in ids]
        state = SimpleNamespace(matter_id=str(uuid4()), run_id=str(uuid4()), artifacts=tuple(
            SimpleNamespace(artifact_id=id, artifact_kind="REVIEWABLE_DOCUMENT_CANDIDATE_JSON") for id in ids))
        rows = [{"package_id": root["package_id"], "package_receipt_hash": "a" * 64,
                 "pending_revision": False} for root in roots]
        versions = tuple((id, sha256(f"lawyer-document-review-v1:{row['package_id']}:{row['package_receipt_hash']}".encode()).hexdigest())
                         for id, row in zip(ids, rows))
        approval = SimpleNamespace(document_review_versions=versions)
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = roots
        connection.execute.return_value.fetchone.side_effect = [{"recovery_results_available": True}, *rows]
        _assert_final_document_versions(connection, actor=actor, state=state, approval=approval)
        calls = connection.execute.call_args_list
        lock_positions = [index for index, call in enumerate(calls) if "pg_advisory_xact_lock" in call.args[0]]
        read_positions = [index for index, call in enumerate(calls) if "AS pending_revision" in call.args[0]]
        self.assertTrue(all("case_agent_document_revision_current_results" in calls[index].args[0] for index in read_positions))
        self.assertLess(max(lock_positions), min(read_positions))
        lock_keys = [calls[index].args[1][0] for index in lock_positions]
        self.assertEqual(lock_keys, sorted(lock_keys))
        for changed in ({**rows[0], "package_receipt_hash": "b" * 64},
                        {**rows[0], "pending_revision": True}, None):
            connection.execute.return_value.fetchone.side_effect = [{"recovery_results_available": True}, changed]
            with self.subTest(changed=changed), self.assertRaises((CaseLedgerPersistenceBlocked, VersionConflict)):
                _assert_final_document_versions(connection, actor=actor, state=state, approval=approval)
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT") for call in connection.execute.call_args_list))

    def test_missing_document_binding_blocks_before_any_database_access(self) -> None:
        connection = MagicMock()
        state = SimpleNamespace(artifacts=(SimpleNamespace(artifact_id=str(uuid4()),
            artifact_kind="REVIEWABLE_DOCUMENT_CANDIDATE_JSON"),))
        with self.assertRaises(CaseLedgerPersistenceBlocked):
            _assert_final_document_versions(connection, actor=SimpleNamespace(), state=state,
                approval=SimpleNamespace(document_review_versions=()))
        connection.execute.assert_not_called()


def _id() -> str:
    return str(uuid4())


def _created_event(*, actor: Actor, matter_id: str) -> AgentSupervisorEvent:
    goal = AgentGoal.build(
        goal_id=_id(),
        objective="审阅合成案卷并形成来源可追溯的内部成果。",
        success_criteria=("材料范围清晰",),
        constraints=("不得自动提交法院",),
        requested_by=actor.actor_id,
    )
    return AgentSupervisorEvent(
        event_id=_id(),
        run_id=_id(),
        firm_id=actor.firm_id,
        matter_id=matter_id,
        sequence=1,
        event_type=AgentEventType.RUN_CREATED,
        occurred_at=datetime.now(timezone.utc),
        actor_id=actor.actor_id,
        payload=RunCreatedPayload(
            goal=goal,
            snapshot=CaseSnapshotRef(
                matter_id=matter_id,
                matter_version=7,
                snapshot_hash="a" * 64,
                schema_version="case-snapshot-v1",
            ),
            budget=RunResourceBudget(20, 40, 4, 1800, 1000, 10_000_000),
        ),
    )


class CaseAgentPostgresTests(unittest.TestCase):
    def test_active_plan_reissue_accepts_only_database_proven_safe_stops(self) -> None:
        base = {
            "is_stale": False,
            "is_cancelled": False,
            "has_safe_local_failure": True,
            "has_only_local_results": True,
        }
        for status in ("WAITING_INPUT", "WAITING_APPROVAL"):
            with self.subTest(status=status):
                self.assertTrue(
                    _active_plan_execution_can_be_reissued(
                        {**base, "run_status": status}
                    )
                )
        for changed in (
            {"run_status": "EXECUTING"},
            {"is_stale": True},
            {"is_cancelled": True},
            {"has_safe_local_failure": False},
            {"has_only_local_results": False},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(
                    _active_plan_execution_can_be_reissued(
                        {**base, "run_status": "WAITING_APPROVAL", **changed}
                    )
                )

    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.lead = Actor(_id(), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.assistant = Actor(_id(), self.firm_id, frozenset({Role.ASSISTANT}))
        self.worker = Actor(_id(), self.firm_id, frozenset({Role.SYSTEM_WORKER}))
        self.store = PostgresCaseAgentStore("postgresql://not-used.invalid/lawcase_test")

    def test_system_worker_cannot_create_a_goal(self) -> None:
        event = _created_event(actor=self.worker, matter_id=self.matter_id)
        with self.assertRaisesRegex(PermissionError, "SYSTEM_WORKER"):
            self.store.create_run(
                matter_id=self.matter_id,
                actor=self.worker,
                expected_matter_version=7,
                idempotency_key="worker-cannot-create-goal",
                event=event,
            )

    def test_new_run_guard_allows_only_terminal_or_newer_snapshot_round(self) -> None:
        _assert_new_case_agent_run_allowed(None, expected_matter_version=8)
        for status in ("COMPLETED", "FAILED", "STALE"):
            _assert_new_case_agent_run_allowed(
                {"status": status, "snapshot_matter_version": 8},
                expected_matter_version=8,
            )
        _assert_new_case_agent_run_allowed(
            {"status": "READY_FOR_REVIEW", "snapshot_matter_version": 7},
            expected_matter_version=8,
        )
        with self.assertRaisesRegex(AgentSupervisorBlocked, "awaits lawyer review"):
            _assert_new_case_agent_run_allowed(
                {"status": "READY_FOR_REVIEW", "snapshot_matter_version": 8},
                expected_matter_version=8,
            )
        with self.assertRaisesRegex(AgentSupervisorBlocked, "another Agent run"):
            _assert_new_case_agent_run_allowed(
                {"status": "EXECUTING", "snapshot_matter_version": 7},
                expected_matter_version=8,
            )

    def test_explicit_new_run_after_known_analysis_rejection_preserves_unknown_gate(self) -> None:
        receipt = SimpleNamespace(status=ResultStatus.FAILED,
            external_submission_state=ExternalSubmissionState.SUBMITTED,
            error_code="LAWYER_ANALYSIS_OUTPUT_REJECTED", attempt_id="attempt")
        task = SimpleNamespace(status=AgentTaskStatus.FAILED, receipts=(receipt,),
            attempt_count=1, active_attempt_id="attempt")
        state = SimpleNamespace(status=AgentRunStatus.WAITING_INPUT, tasks=(task,))
        latest = {"status": "WAITING_INPUT", "snapshot_matter_version": 8}
        _assert_new_case_agent_run_allowed(latest, expected_matter_version=8, prior_state=state)
        for field, value in (("external_submission_state", ExternalSubmissionState.UNKNOWN),
                             ("status", ResultStatus.UNKNOWN), ("error_code", "TRANSPORT_FAILED")):
            previous = getattr(receipt, field)
            setattr(receipt, field, value)
            with self.assertRaises(AgentSupervisorBlocked):
                _assert_new_case_agent_run_allowed(latest, expected_matter_version=8, prior_state=state)
            setattr(receipt, field, previous)
        task.status = AgentTaskStatus.RUNNING
        with self.assertRaises(AgentSupervisorBlocked):
            _assert_new_case_agent_run_allowed(latest, expected_matter_version=8, prior_state=state)

    def test_input_snapshot_status_distinguishes_plan_lineage_from_new_case_input(self) -> None:
        self.assertEqual(
            _classify_run_input_snapshot_status(
                current_matter_version=36,
                snapshot_matter_version=36,
                run_is_stale=False,
                current_plan_lineage=None,
            ),
            "CURRENT",
        )
        self.assertEqual(
            _classify_run_input_snapshot_status(
                current_matter_version=37,
                snapshot_matter_version=36,
                run_is_stale=False,
                current_plan_lineage="PLAN_CANDIDATE_REGISTERED",
            ),
            "PLAN_CANDIDATE_REGISTERED",
        )
        self.assertEqual(
            _classify_run_input_snapshot_status(
                current_matter_version=38,
                snapshot_matter_version=36,
                run_is_stale=False,
                current_plan_lineage="PLAN_ACTIVE",
            ),
            "PLAN_ACTIVE",
        )
        self.assertEqual(
            _classify_run_input_snapshot_status(
                current_matter_version=37,
                snapshot_matter_version=36,
                run_is_stale=False,
                current_plan_lineage=None,
            ),
            "INPUTS_CHANGED",
        )
        self.assertEqual(
            _classify_run_input_snapshot_status(
                current_matter_version=37,
                snapshot_matter_version=36,
                run_is_stale=True,
                current_plan_lineage="PLAN_CANDIDATE_REGISTERED",
            ),
            "INPUTS_CHANGED",
        )

    def test_active_plan_prepare_is_read_only_but_create_path_keeps_row_locks(self) -> None:
        class _EmptyResult:
            @staticmethod
            def fetchone():
                return None

        class _Connection:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, statement, _params):
                self.statements.append(str(statement))
                return _EmptyResult()

        for lock_rows in (False, True):
            connection = _Connection()
            with self.assertRaises(CaseLedgerPersistenceBlocked):
                self.store._active_plan_execution_ref_in_transaction(
                    connection,
                    actor=self.lead,
                    matter_id=self.matter_id,
                    expected_matter_version=7,
                    lock_rows=lock_rows,
                )
            self.assertEqual(
                "FOR SHARE OF matter, source_run" in connection.statements[0],
                lock_rows,
            )

    @patch(
        "case_kernel.case_agent_postgres.planning_work_plan_item_content_hash",
        return_value="a" * 64,
    )
    def test_active_plan_execution_selects_only_source_ready_optional_outputs(self, _hash) -> None:
        plan_id, source_run_id = _id(), _id()

        class _Result:
            def __init__(self, *, row=None, rows=()):
                self.row = row
                self.rows = rows

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        def document_row(kind: str, readiness: str, sequence: int) -> dict[str, object]:
            return {
                "item_id": _id(),
                "sequence": sequence,
                "item_kind": "DOCUMENT_CANDIDATE",
                "readiness": readiness,
                "title": f"生成 {kind} 候选",
                "purpose": "形成可供律师审阅的内部成果。",
                "rationale": "仅使用当前已确认来源。",
                "risk_if_omitted": "无法完成对应审阅工作。",
                "confidence": 0.7,
                "review_gate": "LEAD_LAWYER_CONFIRMATION",
                "delivery_target": "INTERNAL_WORK_PRODUCT",
                "deliverable_kind": kind,
                "required_for_delivery": False,
                "is_primary_document": False,
            }

        rows = (
            document_row("CASE_REVIEW_MEMO", "ACTIONABLE", 1),
            document_row("PAYMENT_LEDGER", "NEEDS_INFORMATION", 2),
            document_row("SUPPLEMENTARY_EVIDENCE_CHECKLIST", "ACTIONABLE", 3),
        )
        plan = {
            "plan_id": plan_id,
            "plan_hash": "b" * 64,
            "status": "ACTIVE",
            "activated_matter_version": 8,
            "matter_version": 8,
            "source_run_id": source_run_id,
            "source_run_status": "READY_FOR_REVIEW",
            "source_run_is_stale": False,
            "source_run_is_cancelled": False,
            "requested_deliverables": [
                AgentDeliverableKind.CASE_REVIEW_MEMO.value,
                AgentDeliverableKind.PAYMENT_LEDGER.value,
                AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST.value,
            ],
            "source_active_plan_execution": None,
        }

        class _Connection:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, _statement, _params):
                self.calls += 1
                return _Result(row=plan) if self.calls == 1 else _Result(rows=rows)

        execution = self.store._active_plan_execution_ref_in_transaction(
            _Connection(),
            actor=self.lead,
            matter_id=self.matter_id,
            expected_matter_version=8,
            lock_rows=False,
        )
        self.assertEqual(
            tuple(item.deliverable_kind for item in execution.items),
            (
                AgentDeliverableKind.CASE_REVIEW_MEMO,
                AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST,
            ),
        )
        self.assertEqual(
            tuple(item.output_format for item in execution.items),
            (AgentDeliverableFormat.DOCX, AgentDeliverableFormat.DOCX),
        )

    def test_final_review_intent_uses_exact_key_audit_and_immutable_event_prefix(self) -> None:
        from backend.tests.test_web_case_agent_control import (
            WebCaseAgentControlTests,
        )

        helper = WebCaseAgentControlTests(methodName="runTest")
        helper.setUp()
        run_id, reviewed_version, _ = helper.ready_for_review()
        key = "case-agent-final-review-intent-0001"
        completed = helper.service.complete_run(
            identity=helper.identity,
            matter_id=helper.matter_id,
            run_id=run_id,
            expected_run_version=reviewed_version,
            idempotency_key=key,
            now=helper.now + timedelta(minutes=1),
        )
        events = tuple(helper.store.events[run_id])
        receipt = completed.receipt
        approval = events[-1].payload.final_review
        projection = {
            "approval_id": approval.approval_id, "run_id": run_id, "firm_id": helper.actor.firm_id,
            "matter_id": helper.matter_id, "graph_id": helper.store.replay_run(run_id=run_id).graph.graph_id,
            "approval_kind": "FINAL_REVIEW", "task_id": None, "task_input_hash": None, "gate": None,
            "graph_hash": approval.graph_hash, "verification_hash": approval.verification_hash,
            "artifact_manifest_hash": approval.artifact_manifest_hash, "approved_by": approval.approved_by,
            "approval_hash": approval.approval_hash, "event_sequence": reviewed_version + 1,
        }
        request_hash = _payload_hash({
            "matter_id": helper.matter_id, "expected_event_version": reviewed_version,
            "run_id": run_id, "event_type": "RUN_COMPLETED",
            "payload": _payload_json(events[-1].payload),
        })
        row = {
            "request_hash": request_hash,
            "response_json": {
                "command_name": "APPEND_CASE_AGENT_RUN_COMPLETED",
                "idempotency_key": key,
                "matter_id": helper.matter_id,
                "run_id": run_id,
                "event_version": reviewed_version + 1,
                "event_id": receipt.completion_id,
                "status": "COMPLETED",
            },
            "audit_request_hash": request_hash,
            "input_event_version": reviewed_version,
            "output_event_version": reviewed_version + 1,
            "audit_event_id": receipt.completion_id,
            "event_sequence": reviewed_version + 1,
            "event_type": "RUN_COMPLETED",
            "actor_id": helper.actor.actor_id,
            "payload": {},
            "approval_projection": projection,
        }
        connection = _Connection(rows=[{"authorized": True}, row])
        with (
            patch.object(
                self.store,
                "_read_transaction",
                return_value=_Context(connection),
            ),
            patch.object(self.store, "_load_events", return_value=events),
        ):
            intent = self.store.final_review_completion_intent(
                matter_id=helper.matter_id,
                actor=helper.actor,
                run_id=run_id,
                expected_event_version=reviewed_version,
                idempotency_key=key,
            )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.receipt.event_id, receipt.completion_id)
        self.assertEqual(intent.reviewed_artifact_count, 1)
        sql, params = connection.executed[1]
        normalized = " ".join(sql.split())
        self.assertIn("command.idempotency_key = %s", normalized)
        self.assertIn("audit.input_event_version = %s", normalized)
        self.assertIn("audit.event_id = %s", normalized)
        self.assertEqual(params[1], receipt.completion_id)
        self.assertIn("LEFT JOIN case_agent_approvals", normalized)
        for invalid in (None, {**projection, "approval_hash": "0" * 64},
                        {**projection, "graph_id": _id()}, {**projection, "approved_by": _id()},
                        {**projection, "event_sequence": reviewed_version}):
            connection = _Connection(rows=[{"authorized": True}, {**row, "approval_projection": invalid}])
            with self.subTest(projection=invalid), \
                 patch.object(self.store, "_read_transaction", return_value=_Context(connection)), \
                 patch.object(self.store, "_load_events", return_value=events), \
                 self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "approval projection"):
                self.store.final_review_completion_intent(
                    matter_id=helper.matter_id, actor=helper.actor, run_id=run_id,
                    expected_event_version=reviewed_version, idempotency_key=key)
        # Matching ledger/audit hashes alone do not prove the event's payload.
        wrong = {**row, "request_hash": "f" * 64, "audit_request_hash": "f" * 64}
        connection = _Connection(rows=[{"authorized": True}, wrong])
        with patch.object(self.store, "_read_transaction", return_value=_Context(connection)), \
             patch.object(self.store, "_load_events", return_value=events), \
             self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "immutable event payload"):
            self.store.final_review_completion_intent(
                matter_id=helper.matter_id, actor=helper.actor, run_id=run_id,
                expected_event_version=reviewed_version, idempotency_key=key)

    def test_mixed_worker_identity_cannot_enter_human_control_path(self) -> None:
        mixed = Actor(
            _id(), self.firm_id, frozenset({Role.SYSTEM_WORKER, Role.LEAD_LAWYER})
        )
        event = _created_event(actor=mixed, matter_id=self.matter_id)
        with self.assertRaisesRegex(PermissionError, "SYSTEM_WORKER"):
            self.store.create_run(
                matter_id=self.matter_id,
                actor=mixed,
                expected_matter_version=7,
                idempotency_key="mixed-worker-cannot-create-goal",
                event=event,
            )

    def test_assistant_cannot_grant_task_or_final_approval(self) -> None:
        for event_type in (
            AgentEventType.APPROVAL_GRANTED,
            AgentEventType.RUN_COMPLETED,
        ):
            with self.assertRaises(PermissionError):
                self.store.append_event(
                    matter_id=self.matter_id,
                    actor=self.assistant,
                    expected_event_version=1,
                    idempotency_key=f"assistant-cannot-{event_type.value.lower()}",
                    event=AgentSupervisorEvent(
                        event_id=_id(), run_id=_id(), firm_id=self.firm_id,
                        matter_id=self.matter_id, sequence=2,
                        event_type=event_type, occurred_at=datetime.now(timezone.utc),
                        actor_id=self.assistant.actor_id, payload=None,
                    ),
                )

    def test_cancellation_authorizes_current_membership_without_reusing_stale_snapshot(self) -> None:
        created = _created_event(actor=self.lead, matter_id=self.matter_id)
        state = reduce_agent_event(None, created)
        cancel = AgentSupervisorEvent(
            event_id=_id(),
            run_id=created.run_id,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            sequence=2,
            event_type=AgentEventType.RUN_CANCELLED,
            occurred_at=datetime.now(timezone.utc),
            actor_id=self.lead.actor_id,
            payload=None,
        )
        connection = object()
        with (
            patch(
                "case_kernel.case_agent_postgres._authorize_matter_read"
            ) as authorize_membership,
            patch(
                "case_kernel.case_agent_postgres._authorize_and_lock_matter"
            ) as authorize_snapshot,
        ):
            self.store._authorize_current_snapshot(
                connection,
                actor=self.lead,
                matter_id=self.matter_id,
                state=state,
                allowed_roles=frozenset({Role.LEAD_LAWYER}),
                proposed_event=cancel,
            )

        authorize_membership.assert_called_once_with(
            connection,
            actor=self.lead,
            matter_id=self.matter_id,
            allowed_roles=frozenset({Role.LEAD_LAWYER}),
        )
        authorize_snapshot.assert_not_called()

    def test_generic_append_cannot_bypass_governed_lawyer_correction(self) -> None:
        with self.assertRaisesRegex(PermissionError, "cannot be appended"):
            self.store.append_event(
                matter_id=self.matter_id,
                actor=self.lead,
                expected_event_version=1,
                idempotency_key="cannot-bypass-governed-correction",
                event=AgentSupervisorEvent(
                    event_id=_id(), run_id=_id(), firm_id=self.firm_id,
                    matter_id=self.matter_id, sequence=2,
                    event_type=AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id=self.lead.actor_id, payload=None,
                ),
            )

    def test_event_serialization_round_trips_through_replay_shape(self) -> None:
        event = _created_event(actor=self.lead, matter_id=self.matter_id)
        encoded = _event_json(event)
        row = {
            "event_id": event.event_id,
            "run_id": event.run_id,
            "firm_id": event.firm_id,
            "matter_id": event.matter_id,
            "event_sequence": event.sequence,
            "event_type": event.event_type.value,
            "actor_id": event.actor_id,
            "payload": encoded["payload"],
            "occurred_at": event.occurred_at,
        }
        self.assertEqual(_event_from_row(row), event)
        original_hash = _payload_hash(encoded)
        for hours in (8, -5):
            with self.subTest(session_offset=hours):
                row["occurred_at"] = event.occurred_at.astimezone(timezone(timedelta(hours=hours)))
                self.assertEqual(_payload_hash(_event_json(_event_from_row(row))), original_hash)
        row["occurred_at"] += timedelta(microseconds=1)
        self.assertNotEqual(_payload_hash(_event_json(_event_from_row(row))), original_hash)

    def test_event_deserialization_rejects_unknown_schema_fields(self) -> None:
        event = _created_event(actor=self.lead, matter_id=self.matter_id)
        encoded = _event_json(event)
        encoded["payload"]["goal"]["unexpected"] = "must fail closed"
        row = {
            "event_id": event.event_id, "run_id": event.run_id,
            "firm_id": event.firm_id, "matter_id": event.matter_id,
            "event_sequence": event.sequence, "event_type": event.event_type.value,
            "actor_id": event.actor_id, "payload": encoded["payload"],
            "occurred_at": event.occurred_at,
        }
        with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "schema"):
            _event_from_row(row)

    def test_create_run_reduces_before_database_access(self) -> None:
        event = _created_event(actor=self.lead, matter_id=self.matter_id)
        invalid = AgentSupervisorEvent(**{**event.__dict__, "sequence": 2})
        with patch("case_kernel.case_agent_postgres.psycopg.connect") as connect:
            with self.assertRaisesRegex(Exception, "first lawyer event"):
                self.store.create_run(
                    matter_id=self.matter_id,
                    actor=self.lead,
                    expected_matter_version=7,
                    idempotency_key="invalid-first-event",
                    event=invalid,
                )
        connect.assert_not_called()

    def test_reaper_and_dispatch_delegate_to_supervisor_reducer(self) -> None:
        source = __import__(
            "inspect"
        ).getsource(PostgresCaseAgentStore)
        self.assertIn("decide_next_commands(state)", source)
        self.assertIn("reduce_agent_event(state, event)", source)
        self.assertIn("replay_agent_events", source)
        self.assertNotIn("status = 'RETRYABLE'", source)
        self.assertIn("ResultStatus.UNKNOWN if crossed_boundary", source)
        self.assertIn("case_agent_external_submissions", source)
        self.assertIn("record_external_submission_started", source)
        self.assertIn("actor lacks an active database role for this Agent run", source)

    def test_reaper_ignores_stale_waiting_run_without_expired_task_lease(self) -> None:
        connection = _Connection(rows=[{"present": False}])
        run_id = _id()
        with patch.object(
            self.store,
            "_transaction",
            return_value=_Context(connection),
        ), patch.object(self.store, "_replay_locked") as replay:
            result = self.store.reap_expired_attempt(
                matter_id=self.matter_id,
                actor=self.worker,
                run_id=run_id,
                expected_event_version=18,
                idempotency_key=f"agent-reap:{run_id}:18",
                now=datetime.now(timezone.utc),
            )

        self.assertIsNone(result)
        replay.assert_not_called()
        self.assertEqual(len(connection.executed), 1)
        self.assertIn(
            "FROM case_agent_task_attempts",
            " ".join(connection.executed[0][0].split()),
        )

    def test_worker_run_lock_is_bound_to_an_active_role_in_the_same_matter(self) -> None:
        connection = _Connection(
            rows=[{"current_event_version": 1, "permitted": False}]
        )
        with self.assertRaisesRegex(PermissionError, "active database role"):
            self.store._replay_locked(
                connection, self.worker, self.matter_id, _id()
            )
        sql, params = connection.executed[0]
        self.assertIn("role.matter_id = run.matter_id", " ".join(sql.split()))
        self.assertEqual(params[0], self.worker.actor_id)
        self.assertIn(Role.SYSTEM_WORKER.value, params[1])

    def test_analysis_stage_input_preflight_uses_a_read_only_authorized_replay(self) -> None:
        event = _created_event(actor=self.lead, matter_id=self.matter_id)
        connection = _Connection(rows=[{"authorized": True}])
        bindings = ((f"fact-candidate:{_id()}", "a" * 64),)
        with (
            patch.object(self.store, "_read_transaction", return_value=_Context(connection)),
            patch.object(self.store, "_load_events", return_value=(event,)),
            patch(
                "case_kernel.case_agent_analysis_stage.current_analysis_candidate_bindings",
                return_value=bindings,
            ),
        ):
            state, actual_bindings = self.store.current_case_analysis_stage_inputs(
                matter_id=self.matter_id,
                actor=self.lead,
                run_id=event.run_id,
            )

        self.assertEqual(state.run_id, event.run_id)
        self.assertEqual(actual_bindings, bindings)
        self.assertEqual(len(connection.executed), 1)
        statement = " ".join(connection.executed[0][0].split()).upper()
        self.assertIn("FROM MATTERS", statement)
        self.assertNotIn("FOR UPDATE", statement)

    def test_external_submission_marker_requires_a_network_task_and_exact_approval(self) -> None:
        attempt_id, run_id, approval_id = _id(), _id(), _id()
        connection = _Connection(
            rows=[
                {"ok": 1},
                {
                    "attempt_version": 1,
                    "task_id": _id(),
                    "status": "RUNNING",
                    "network_policy": "DENY",
                    "external_request_approval_required": False,
                    "input_hash": "a" * 64,
                    "approval_id": None,
                },
            ]
        )
        with patch.object(self.store, "_transaction", return_value=_Context(connection)):
            with self.assertRaisesRegex(CaseLedgerPersistenceBlocked, "exact network task"):
                self.store.record_external_submission_started(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    expected_attempt_version=1,
                    external_request_id="provider-request-1",
                    destination="api.example.invalid",
                    request_hash="b" * 64,
                    approval_id=approval_id,
                )
        statements = [" ".join(sql.split()) for sql, _ in connection.executed]
        self.assertFalse(any("INSERT INTO case_agent_external_submissions" in sql for sql in statements))

    def test_unknown_external_result_exposes_reconciliation_not_dispatch(self) -> None:
        # This is a behavioral reducer contract, not a source-string check.
        from backend.tests.test_case_agent_supervisor import CaseAgentSupervisorTests

        helper = CaseAgentSupervisorTests(methodName="runTest")
        helper.setUp()
        state = helper.state_with_graph(helper.graph((helper.external_task(),)))
        task = state.tasks[0]
        approved = helper.approve(state, task.spec.task_id, sequence=3)
        started = helper.start_ready_task(approved, sequence=4)
        receipt = helper.result_receipt(
            started,
            task_id=task.spec.task_id,
            status=ResultStatus.UNKNOWN,
            external_state=ExternalSubmissionState.UNKNOWN,
            external_request_id="provider-request-unknown",
            external_calls=1,
        )
        unknown = reduce_agent_event(
            started,
            helper.event(
                5,
                AgentEventType.TASK_RESULT_RECORDED,
                payload=TaskResultPayload(receipt),
            ),
        )
        self.assertEqual(unknown.tasks[0].status, AgentTaskStatus.UNKNOWN)
        commands = decide_next_commands(unknown)
        self.assertTrue(commands)
        self.assertTrue(all(command.kind.value == "RECONCILE_EXTERNAL_RESULT" for command in commands))
        self.assertFalse(any(command.kind.value == "DISPATCH_TASK" for command in commands))

    def test_expired_reconciliation_lease_is_reclaimed_without_dispatch(self) -> None:
        import inspect

        source = inspect.getsource(self.store.claim_next_reconciliation)
        self.assertIn("status = 'RECONCILING'", source)
        self.assertIn("lease_expires_at <= %s", source)
        self.assertIn(
            "Agent reconciliation was reclaimed elsewhere",
            source,
        )
        self.assertNotIn("claim_next_dispatchable_task", source)

    def test_current_run_record_selects_newest_run_and_preserves_server_clock(self) -> None:
        run_id = _id()
        created = datetime(2026, 8, 13, 3, tzinfo=timezone.utc)
        updated = created + timedelta(minutes=5)
        connection = _Connection(
            rows=[
                {"ok": 1},
                {"run_id": run_id, "created_at": created, "updated_at": updated},
                {"ok": 1},
                {
                    "created_at": created,
                    "updated_at": updated,
                    "snapshot_matter_version": 7,
                    "is_stale": False,
                    "current_matter_version": 7,
                    "current_plan_lineage": None,
                },
            ]
        )
        projection = object()
        with patch.object(self.store, "_read_transaction", return_value=_Context(connection)), patch.object(
            self.store,
            "read_projection",
            return_value=projection,
        ):
            record = self.store.current_run_record(
                matter_id=self.matter_id, actor=self.lead
            )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIs(record.projection, projection)
        self.assertEqual(record.created_at, created)
        self.assertEqual(record.updated_at, updated)
        self.assertEqual(record.input_snapshot_status, "CURRENT")
        statements = [" ".join(sql.split()) for sql, _ in connection.executed]
        self.assertTrue(any("ORDER BY updated_at DESC" in sql for sql in statements))
        self.assertTrue(any("is_cancelled = false" in sql for sql in statements))

    def test_heartbeat_checks_the_actual_update_rowcount(self) -> None:
        connection = _Connection(rows=[{"ok": 1}, None], rowcounts=[1, 0])
        with patch.object(self.store, "_transaction", return_value=_Context(connection)):
            with self.assertRaisesRegex(Exception, "lease changed"):
                self.store.heartbeat_attempt(
                    matter_id=self.matter_id,
                    actor=self.worker,
                    attempt_id=_id(),
                    expected_attempt_version=1,
                    lease_owner="worker-1",
                )

    def test_snapshot_refresh_appends_one_worker_event_and_coalesces_pending_versions(self) -> None:
        created = _created_event(actor=self.lead, matter_id=self.matter_id)
        state = reduce_agent_event(None, created)
        refresh_id = _id()
        current = CaseSnapshotRef(
            matter_id=self.matter_id,
            matter_version=9,
            snapshot_hash="9" * 64,
            schema_version="case-ledger-snapshot-v1",
        )

        class RefreshConnection:
            def __init__(inner_self):
                inner_self.executed = []

            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                inner_self.executed.append((normalized, tuple(params)))
                if normalized.startswith(
                    "SELECT refresh_request_id, source_matter_version"
                ):
                    return _Result(
                        row={
                            "refresh_request_id": refresh_id,
                            "source_matter_version": 7,
                            "target_matter_version": 8,
                            "extraction_batch_id": _id(),
                            "control_assignment_id": None,
                        }
                    )
                if "case_agent_ledger_extraction_run_review_resolved" in normalized:
                    return _Result(row={"review_resolved": True})
                if normalized.startswith("SELECT version FROM matters"):
                    return _Result(row={"version": 9})
                if normalized.startswith(
                    "UPDATE case_agent_snapshot_refresh_requests"
                ):
                    return _Result(rowcount=2)
                raise AssertionError(f"unexpected SQL: {normalized}")

        connection = RefreshConnection()
        with (
            patch.object(
                self.store, "_transaction", return_value=_Context(connection)
            ),
            patch.object(self.store, "_replay_locked", return_value=state),
            patch(
                "case_kernel.case_agent_postgres._authorize_and_lock_matter"
            ) as authorize,
            patch(
                "case_kernel.case_agent_planning_snapshot_postgres."
                "read_current_case_snapshot_in_transaction",
                return_value=current,
            ),
            patch.object(self.store, "_insert_event") as insert_event,
            patch.object(self.store, "_persist_event_details"),
            patch.object(self.store, "_update_projection"),
            patch.object(self.store, "_insert_checkpoint"),
            patch.object(self.store, "_finish_control_command") as finish,
        ):
            applied = self.store.apply_pending_snapshot_refresh(
                matter_id=self.matter_id,
                actor=self.worker,
                run_id=created.run_id,
            )

        self.assertIsNotNone(applied)
        assert applied is not None
        self.assertEqual(applied.target_matter_version, 8)
        self.assertEqual(applied.event_version, 2)
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, AgentEventType.CASE_SNAPSHOT_CHANGED)
        self.assertEqual(event.payload.snapshot, current)
        authorize.assert_called_once()
        finish.assert_called_once()
        claim_sql = next(
            sql
            for sql, _ in connection.executed
            if sql.startswith("SELECT refresh_request_id, source_matter_version")
        )
        self.assertIn("ORDER BY target_matter_version DESC", claim_sql)
        self.assertIn("FOR UPDATE", claim_sql)
        update_sql = next(
            sql
            for sql, _ in connection.executed
            if sql.startswith("UPDATE case_agent_snapshot_refresh_requests")
        )
        self.assertIn("target_matter_version <= %s", update_sql)
        self.assertNotIn("refresh_request_id = %s", update_sql)

    def test_snapshot_refresh_commit_lost_replay_observes_no_pending_request(self) -> None:
        created = _created_event(actor=self.lead, matter_id=self.matter_id)
        state = reduce_agent_event(None, created)
        connection = _Connection(rows=[None])
        with (
            patch.object(
                self.store, "_transaction", return_value=_Context(connection)
            ),
            patch.object(self.store, "_replay_locked", return_value=state),
            patch.object(self.store, "_insert_event") as insert_event,
        ):
            applied = self.store.apply_pending_snapshot_refresh(
                matter_id=self.matter_id,
                actor=self.worker,
                run_id=created.run_id,
            )
        self.assertIsNone(applied)
        insert_event.assert_not_called()

    def test_snapshot_refresh_never_appends_when_run_review_proof_is_false(self) -> None:
        created = _created_event(actor=self.lead, matter_id=self.matter_id)
        state = reduce_agent_event(None, created)

        class OpenReviewConnection:
            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if normalized.startswith(
                    "SELECT refresh_request_id, source_matter_version"
                ):
                    return _Result(
                        row={
                            "refresh_request_id": _id(),
                            "source_matter_version": 7,
                            "target_matter_version": 8,
                            "extraction_batch_id": _id(),
                            "control_assignment_id": None,
                        }
                    )
                if "case_agent_ledger_extraction_run_review_resolved" in normalized:
                    return _Result(row={"review_resolved": False})
                raise AssertionError(f"unexpected SQL: {normalized}")

        connection = OpenReviewConnection()
        with (
            patch.object(
                self.store, "_transaction", return_value=_Context(connection)
            ),
            patch.object(self.store, "_replay_locked", return_value=state),
            patch.object(self.store, "_insert_event") as insert_event,
            self.assertRaisesRegex(
                CaseLedgerPersistenceBlocked, "open ledger review"
            ),
        ):
            self.store.apply_pending_snapshot_refresh(
                matter_id=self.matter_id,
                actor=self.worker,
                run_id=created.run_id,
            )

    def test_control_assignment_refresh_requires_the_current_healthy_head(self) -> None:
        created = _created_event(actor=self.lead, matter_id=self.matter_id)
        state = reduce_agent_event(None, created)

        class SupersededControlConnection:
            authority_sql = ""

            def execute(inner_self, sql, params=()):
                normalized = " ".join(sql.split())
                if normalized.startswith(
                    "SELECT refresh_request_id, source_matter_version"
                ):
                    return _Result(
                        row={
                            "refresh_request_id": _id(),
                            "source_matter_version": 7,
                            "target_matter_version": 8,
                            "extraction_batch_id": None,
                            "control_assignment_id": _id(),
                        }
                    )
                if "AS current_authority" in normalized:
                    inner_self.authority_sql = normalized
                    return _Result(row={"current_authority": False})
                if "case_agent_ledger_extraction_run_review_resolved" in normalized:
                    raise AssertionError("control refresh must not fake a batch review")
                raise AssertionError(f"unexpected SQL: {normalized}")

        connection = SupersededControlConnection()
        with (
            patch.object(
                self.store, "_transaction", return_value=_Context(connection)
            ),
            patch.object(self.store, "_replay_locked", return_value=state),
            patch.object(self.store, "_insert_event") as insert_event,
            self.assertRaisesRegex(
                CaseLedgerPersistenceBlocked, "no longer current"
            ),
        ):
            self.store.apply_pending_snapshot_refresh(
                matter_id=self.matter_id,
                actor=self.worker,
                run_id=created.run_id,
            )
        insert_event.assert_not_called()
        self.assertIn(
            "AND NOT EXISTS ( SELECT 1 FROM "
            "case_agent_ledger_exception_followup_heads",
            connection.authority_sql,
        )
        insert_event.assert_not_called()


class _Result:
    def __init__(self, row=None, rows=None, *, rowcount=1):
        self._row = row
        self._rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, *, rows, rowcounts=None):
        self.rows = list(rows)
        self.rowcounts = list(rowcounts or ())
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None):
        self.executed.append((sql, params))
        row = self.rows.pop(0) if self.rows else None
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return _Result(row=row, rowcount=rowcount)


class _Context:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


if __name__ == "__main__":
    unittest.main()
