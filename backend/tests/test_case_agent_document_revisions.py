from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from case_kernel import case_agent_document_revisions as revision_module
from case_kernel.case_agent_document_revisions import (
    DocumentRevisionClaim,
    PostgresDocumentRevisionWorker,
)
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_worker import ReviewOfficeConversionUnknown


def _id() -> str:
    return str(uuid4())


class CaseAgentDocumentRevisionWorkerTests(unittest.TestCase):
    def test_recovery_preflight_requires_all_readonly_boundaries(self) -> None:
        fields = ("correct_role", "append_access", "immutable_access", "result_access", "lock_access",
                  "protected_table", "isolated_lock_owner", "recovery_triggers")
        with patch.object(revision_module, "_transaction") as transaction:
            cursor = transaction.return_value.__enter__.return_value.execute.return_value
            cursor.fetchone.return_value = dict.fromkeys(fields, True)
            revision_module.preflight_content_recovery_runtime(verifier_dsn="verifier-test", verifier_actor=self.verifier_actor)
            transaction.assert_called_once_with("verifier-test", self.verifier_actor, read_only=True)
            for field in fields:
                cursor.fetchone.return_value = {**dict.fromkeys(fields, True), field: False}
                with self.subTest(field=field), self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                    revision_module.preflight_content_recovery_runtime(verifier_dsn="verifier-test", verifier_actor=self.verifier_actor)

    def test_content_recovery_writer_is_opt_in_independent_and_idempotent(self) -> None:
        from backend.tests.test_web_case_agent_documents import WebCaseAgentDocumentReviewTests
        fixture = WebCaseAgentDocumentReviewTests()
        fixture.setUp()
        package, _ = fixture._content_successor()
        worker = Actor(_id(), fixture.firm_id, frozenset({Role.SYSTEM_WORKER}))
        verifier = Actor(_id(), fixture.firm_id, frozenset({Role.SYSTEM_WORKER}))
        access = SimpleNamespace(_verifier=verifier, _execution_actor_id=worker.actor_id,
                                 read_package=Mock(return_value=package))
        args = dict(matter_id=fixture.matter_id, run_id=fixture.run_id, request_id=package.revision_request_id)
        row = dict(request_hash="a" * 64, review_id=_id(), root_package_id=package.root_package_id,
            predecessor_package_id=package.supersedes_package_id, expected_revision_number=1,
            package_id=package.package_id, package_receipt_hash=package.receipt_hash,
            candidate_artifact_id=package.candidate.artifact_id, candidate_hash=package.candidate_hash,
            binding_hash=package.binding_hash, claim_version=2, original_receipt=None)
        reader, writer = MagicMock(), MagicMock()
        reader.execute.return_value.fetchone.return_value = row
        persisted = None
        def execute(sql, params):
            nonlocal persisted
            if "INSERT INTO case_agent_document_content_recoveries" in sql:
                keys = ("recovery_id", "request_id", "review_id", "firm_id", "matter_id", "run_id",
                    "original_receipt_id", "original_receipt_hash", "successor_package_id", "successor_package_receipt_hash",
                    "claim_version", "executed_by", "verified_by", "verification_hash", "recovery_hash")
                persisted = {**dict(zip(keys, params)), "job_state": "SUCCEEDED"}
            return Mock(fetchone=Mock(return_value=persisted if "SELECT recovery.*" in sql else None))
        writer.execute.side_effect = execute
        def transaction(_dsn, actor, *, read_only):
            self.assertEqual(actor, verifier)
            context = MagicMock()
            context.__enter__.return_value = reader if read_only else writer
            return context
        with patch.object(revision_module, "_transaction", side_effect=transaction) as transactions:
            store = revision_module.PostgresContentRevisionRecoveryStore(verifier_dsn="verifier-test",
                worker_actor=worker, verifier_actor=verifier, package_access=access)
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                store.recover(**args)
            transactions.assert_not_called()
            store = revision_module.PostgresContentRevisionRecoveryStore(verifier_dsn="verifier-test",
                worker_actor=worker, verifier_actor=verifier, package_access=access, enabled=True)
            receipt = store.recover(**args)
            self.assertEqual(store.recover(**args), receipt)
            self.assertEqual(sum("INSERT INTO" in call.args[0] for call in writer.execute.call_args_list), 1)
            self.assertEqual(access.read_package.call_count, 2)
            self.assertIsNone(persisted["original_receipt_id"])
            persisted["job_state"] = "UNKNOWN"
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionConflict):
                store.recover(**args)
            persisted["job_state"] = "SUCCEEDED"
            persisted["recovery_hash"] = "0" * 64
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionConflict):
                store.recover(**args)
            row["original_receipt"] = {"outcome": "UNKNOWN", "receipt_hash": "0" * 64}
            access.read_package.reset_mock()
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                store.recover(**args)
            access.read_package.assert_not_called()
            self.assertEqual(sum("INSERT INTO" in call.args[0] for call in writer.execute.call_args_list), 1)

    def test_generation_status_does_not_invent_success_or_retry_unknown(self) -> None:
        connection = MagicMock()
        cases = [
            (None, "NOT_AUTHORIZED"),
            ({"state": None}, "UNKNOWN"),
            ({"state": "READY"}, "QUEUED"),
            ({"state": "LEASED", "lease_live": True}, "GENERATING"),
            ({"state": "LEASED", "lease_live": False}, "RECOVERING"),
            ({"state": "RENDERING", "lease_live": False}, "UNKNOWN"),
            ({"state": "UNKNOWN"}, "UNKNOWN"),
            ({"state": "UNKNOWN", "registered_result": True}, "UNKNOWN_REGISTERED"),
            ({"state": "UNKNOWN", "outcome": "UNKNOWN", "registered_result": True}, "UNKNOWN_REGISTERED"),
            ({"state": "UNKNOWN", "registered_result": False}, "UNKNOWN"),
            ({"state": "UNKNOWN", "registered_result": "true"}, "UNKNOWN"),
            ({"state": "UNKNOWN", "outcome": "PASSED", "registered_result": True}, "UNKNOWN"),
            ({"state": "RENDERING", "lease_live": False, "registered_result": True}, "UNKNOWN_REGISTERED"),
            ({"state": "RENDERING", "lease_live": True, "registered_result": True}, "GENERATING"),
            ({"state": "FAILED", "outcome": "FAILED", "registered_result": True}, "FAILED"),
            ({"state": "FAILED"}, "FAILED"),
            ({"state": "SUCCEEDED"}, "UNKNOWN"),
            ({"state": "SUCCEEDED", "outcome": "PASSED", "successor_package_id": _id()}, "GENERATED_REVIEW_COPY"),
            ({"state": "RENDERING", "outcome": "PASSED", "successor_package_id": _id()}, "UNKNOWN"),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                connection.execute.return_value.fetchone.side_effect = [{"recovery_results_available": True}, row]
                result = revision_module.PostgresDocumentRevisionCommandStore._read_content_generation_status(
                    connection, actor=self.worker_actor, matter_id=self.claim.matter_id,
                    run_id=self.claim.run_id, proposal_id=self.claim.request_id)
                self.assertEqual(result, expected)
                self.assertIn("LEFT JOIN case_agent_document_revision_current_results", connection.execute.call_args.args[0])
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT") for call in connection.execute.call_args_list))

    def test_current_result_relation_only_falls_back_for_absent_schema(self) -> None:
        connection = MagicMock()
        for available, expected in ((False, "case_agent_document_revision_receipts"),
                                    (True, "case_agent_document_revision_current_results")):
            connection.execute.return_value.fetchone.return_value = {"recovery_results_available": available}
            self.assertEqual(revision_module.current_document_result_relation(connection), expected)
        for invalid in (None, {}, {"recovery_results_available": "false"}):
            connection.execute.return_value.fetchone.return_value = invalid
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                revision_module.current_document_result_relation(connection)
        connection.execute.side_effect = PermissionError("no result access")
        with self.assertRaises(PermissionError):
            revision_module.current_document_result_relation(connection)

    def test_registered_content_lookup_is_scoped_readonly_and_rejects_ambiguity(self) -> None:
        store = revision_module.PostgresDocumentRevisionCommandStore.__new__(revision_module.PostgresDocumentRevisionCommandStore)
        store._dsn = "read-only-test"
        root = _id()
        store._read_state = Mock(return_value=SimpleNamespace(root_package_id=root))
        actor = Actor(_id(), self.worker_actor.firm_id, frozenset({Role.LEAD_LAWYER}))
        row = dict(package_id=_id(), candidate_artifact_id=_id(), package_receipt_hash="a" * 64,
            request_id=_id(), root_package_id=root, supersedes_package_id=_id(), revision_number=2,
            candidate_hash="b" * 64, binding_hash="c" * 64, content_generation_claim_version=1)
        args = dict(actor=actor, matter_id=self.claim.matter_id, run_id=self.claim.run_id,
                    artifact_id=_id(), proposal_id=_id())
        with patch.object(revision_module, "_transaction") as transaction:
            connection = transaction.return_value.__enter__.return_value
            connection.execute.return_value.fetchall.return_value = [row]
            result = store.read_registered_content_result(**args)
            self.assertEqual(result.candidate_artifact_id, row["candidate_artifact_id"])
            transaction.assert_called_once_with("read-only-test", actor, read_only=True)
            sql, params = connection.execute.call_args.args
            self.assertTrue(sql.lstrip().startswith("SELECT"))
            self.assertEqual(params, (args["proposal_id"], actor.firm_id, args["matter_id"], args["run_id"], root))
            connection.execute.return_value.fetchall.return_value = []
            self.assertIsNone(store.read_registered_content_result(**args))
            connection.execute.return_value.fetchall.return_value = [row, row]
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                store.read_registered_content_result(**args)
            row["request_hash"] = "d" * 64
            row["staged_by"] = self.worker_actor.actor_id
            original_id = str(revision_module.uuid5(revision_module.UUID(row["request_id"]), "document-revision:UNKNOWN"))
            receipt = dict(receipt_id=original_id, request_id=row["request_id"], firm_id=actor.firm_id,
                matter_id=args["matter_id"], outcome="UNKNOWN", successor_package_id=None,
                successor_package_receipt_hash=None, failure_code="DOCUMENT_CONTENT_RESULT_UNKNOWN",
                external_calls=0, executed_by=self.worker_actor.actor_id, verified_by=None)
            receipt["receipt_hash"] = revision_module._canonical_hash({**receipt,
                "schema_version": "case-agent-document-revision-receipt-v1", "request_hash": row["request_hash"]})
            row["original_receipt"] = receipt
            connection.execute.return_value.fetchall.return_value = [row]
            recovered = store.read_registered_content_result(**args)
            self.assertEqual(recovered.original_receipt_id, original_id)
            self.assertEqual(recovered.original_receipt_hash, receipt["receipt_hash"])
            for field, value in (("receipt_hash", "0" * 64), ("executed_by", _id()),
                                 ("outcome", "PASSED"), ("request_id", _id()), ("external_calls", 1)):
                row["original_receipt"] = {**receipt, field: value}
                with self.subTest(field=field), self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                    store.read_registered_content_result(**args)

    def test_revision_group_rotates_priority_and_isolates_outage(self) -> None:
        first, second = Mock(), Mock()
        first.run_cycle.return_value = second.run_cycle.return_value = True
        group = revision_module.DocumentRevisionWorkerGroup(first, second)
        self.assertTrue(group.run_cycle())
        first.run_cycle.assert_called_once()
        second.run_cycle.assert_not_called()
        self.assertTrue(group.run_cycle())
        second.run_cycle.assert_called_once()
        first.run_cycle.side_effect = RuntimeError("private details")
        with self.assertLogs(revision_module.__name__, level="ERROR") as logs:
            self.assertTrue(group.run_cycle())
        self.assertNotIn("private details", " ".join(logs.output))
        self.assertEqual(second.run_cycle.call_count, 2)

    def test_content_preflight_requires_both_readonly_catalogue_checks(self) -> None:
        keys = ("correct_role", "protected_tables", "active_triggers", "claim_column", "can_read", "cannot_insert")
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = dict.fromkeys(keys, True)
        with patch.object(revision_module, "_transaction") as transaction:
            transaction.return_value.__enter__.return_value = connection
            revision_module.preflight_content_revision_runtime(
                execution_dsn="execution", verifier_dsn="verification",
                worker_actor=self.worker_actor, verifier_actor=self.verifier_actor)
            self.assertEqual(transaction.call_count, 2)
            self.assertTrue(all(call.kwargs["read_only"] for call in transaction.call_args_list))
            for key in keys:
                with self.subTest(key=key):
                    connection.execute.return_value.fetchone.return_value = {**dict.fromkeys(keys, True), key: False}
                    with self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                        revision_module.preflight_content_revision_runtime(
                            execution_dsn="execution", verifier_dsn="verification",
                            worker_actor=self.worker_actor, verifier_actor=self.verifier_actor)

    def setUp(self) -> None:
        self.worker_actor = Actor(
            _id(), _id(), frozenset({Role.SYSTEM_WORKER})
        )
        self.verifier_actor = Actor(
            _id(), self.worker_actor.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.claim = DocumentRevisionClaim(
            request_id=_id(),
            request_hash="a" * 64,
            root_package_id=_id(),
            predecessor_package_id=_id(),
            expected_revision_number=1,
            requested_by=_id(),
            run_id=_id(),
            matter_id=_id(),
            graph_id=_id(),
            task_id=_id(),
            attempt_id=_id(),
            task_input_hash="b" * 64,
            attempt_count=1,
        )

    def _worker(self, *, package=None) -> PostgresDocumentRevisionWorker:
        instance = PostgresDocumentRevisionWorker.__new__(
            PostgresDocumentRevisionWorker
        )
        instance._worker = self.worker_actor
        instance._verifier = self.verifier_actor
        instance._access = SimpleNamespace(
            read_package=Mock(return_value=package)
        )
        return instance

    def _verified_package(self):
        return SimpleNamespace(
            package_id=_id(),
            receipt_hash="c" * 64,
            generation_mode="DETERMINISTIC_TEMPLATE_REVISION",
            revision_request_id=self.claim.request_id,
            supersedes_package_id=self.claim.predecessor_package_id,
            root_package_id=self.claim.root_package_id,
            revision_number=2,
        )

    def test_success_requires_exact_independently_reread_successor(self) -> None:
        package = self._verified_package()
        worker = self._worker(package=package)
        worker._claim = Mock(return_value=self.claim)
        worker._execute = Mock(
            return_value=SimpleNamespace(
                package_id=package.package_id,
                receipt_hash=package.receipt_hash,
                candidate_artifact=SimpleNamespace(artifact_id=_id()),
            )
        )
        worker._record_receipt = Mock()

        self.assertTrue(worker.run_cycle())

        worker._record_receipt.assert_called_once_with(
            self.claim,
            outcome="PASSED",
            successor_package_id=package.package_id,
            successor_package_receipt_hash=package.receipt_hash,
            failure_code=None,
        )

    def test_unknown_renderer_result_is_never_guessed_or_retried(self) -> None:
        worker = self._worker()
        worker._claim = Mock(return_value=self.claim)
        worker._execute = Mock(side_effect=ReviewOfficeConversionUnknown("unknown"))
        worker._record_receipt = Mock()

        self.assertTrue(worker.run_cycle())

        worker._record_receipt.assert_called_once_with(
            self.claim,
            outcome="UNKNOWN",
            successor_package_id=None,
            successor_package_receipt_hash=None,
            failure_code="DOCUMENT_RENDERER_RESULT_UNKNOWN",
        )

    def test_unexpected_runtime_failure_has_three_attempt_cap(self) -> None:
        worker = self._worker()
        worker._claim = Mock(return_value=self.claim)
        worker._execute = Mock(side_effect=RuntimeError("private"))
        worker._release = Mock()
        worker._record_receipt = Mock()

        self.assertTrue(worker.run_cycle())
        worker._release.assert_called_once_with(
            self.claim, retry_after_seconds=30
        )
        worker._record_receipt.assert_not_called()

        final_claim = DocumentRevisionClaim(
            **{**self.claim.__dict__, "attempt_count": 3}
        )
        worker._claim = Mock(return_value=final_claim)
        worker._release.reset_mock()
        self.assertTrue(worker.run_cycle())
        worker._release.assert_not_called()
        worker._record_receipt.assert_called_once_with(
            final_claim,
            outcome="FAILED",
            successor_package_id=None,
            successor_package_receipt_hash=None,
            failure_code="DOCUMENT_REVISION_RUNTIME_UNAVAILABLE",
        )

    def test_runtime_stage_is_sanitized_in_the_terminal_receipt(self) -> None:
        final_claim = DocumentRevisionClaim(
            **{**self.claim.__dict__, "attempt_count": 3}
        )
        worker = self._worker()
        worker._claim = Mock(return_value=final_claim)
        worker._execute = Mock(
            side_effect=revision_module._DocumentRevisionRuntimeUnavailable(
                "PACKAGE_STAGING"
            )
        )
        worker._record_receipt = Mock()

        self.assertTrue(worker.run_cycle())

        worker._record_receipt.assert_called_once_with(
            final_claim,
            outcome="FAILED",
            successor_package_id=None,
            successor_package_receipt_hash=None,
            failure_code="DOCUMENT_REVISION_RUNTIME_PACKAGE_STAGING",
        )

    def test_shared_receipt_store_reads_committed_result_before_any_reinsert(self):
        store = revision_module.PostgresDocumentRevisionReceiptStore(
            execution_dsn="execution-test", verifier_dsn="verification-test",
            worker_actor=self.worker_actor, verifier_actor=self.verifier_actor)
        context, connection = MagicMock(), MagicMock()
        context.__enter__.return_value = connection
        recorded = None
        def execute(sql, params):
            nonlocal recorded
            if "INSERT INTO case_agent_document_revision_receipts" in sql:
                recorded = dict(zip(("receipt_id", "request_id", "firm_id", "matter_id", "outcome",
                    "successor_package_id", "successor_package_receipt_hash", "failure_code", "executed_by",
                    "verified_by", "receipt_hash"), params))
            return Mock(fetchone=Mock(return_value=recorded if "SELECT receipt_id" in sql else None))
        connection.execute.side_effect = execute
        args = dict(request_id=self.claim.request_id, request_hash=self.claim.request_hash,
            matter_id=self.claim.matter_id, outcome="PASSED", successor_package_id=_id(),
            successor_package_receipt_hash="c" * 64, failure_code=None)
        with patch.object(revision_module, "_transaction", return_value=context) as transaction:
            store.record(**args)
            transaction.assert_called_with("verification-test", self.verifier_actor, read_only=False)
            self.assertEqual(recorded["executed_by"], self.worker_actor.actor_id)
            self.assertEqual(recorded["verified_by"], self.verifier_actor.actor_id)
            statements = [call.args[0] for call in connection.execute.call_args_list]
            self.assertIn("pg_advisory_xact_lock", statements[0])
            self.assertIn("SELECT receipt_id", statements[1])
            self.assertIn("INSERT INTO", statements[2])
            connection.reset_mock()
            store.record(**args)
            self.assertFalse(any("INSERT" in call.args[0] for call in connection.execute.call_args_list))
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionConflict):
                store.record(**{**args, "successor_package_receipt_hash": "d" * 64})
            with self.assertRaises(revision_module.CaseAgentDocumentRevisionConflict):
                store.record(**{**args, "outcome": "UNKNOWN", "successor_package_id": None,
                    "successor_package_receipt_hash": None, "failure_code": "DOCUMENT_RENDERER_RESULT_UNKNOWN"})
            self.assertFalse(any("INSERT" in call.args[0] for call in connection.execute.call_args_list))
            recorded = None
            store.record(**{**args, "outcome": "UNKNOWN", "successor_package_id": None,
                "successor_package_receipt_hash": None, "failure_code": "DOCUMENT_RENDERER_RESULT_UNKNOWN"})
            transaction.assert_called_with("execution-test", self.worker_actor, read_only=False)
            self.assertIsNone(recorded["verified_by"])
            self.assertIsNone(recorded["successor_package_id"])

    def test_receipt_store_rejects_mixed_outcome_and_nonindependent_identity(self):
        with self.assertRaises(ValueError):
            revision_module.PostgresDocumentRevisionReceiptStore(execution_dsn="x", verifier_dsn="y",
                worker_actor=self.worker_actor, verifier_actor=self.worker_actor)
        store = revision_module.PostgresDocumentRevisionReceiptStore(execution_dsn="x", verifier_dsn="y",
            worker_actor=self.worker_actor, verifier_actor=self.verifier_actor)
        args = dict(request_id=self.claim.request_id, request_hash=self.claim.request_hash, matter_id=self.claim.matter_id,
            outcome="PASSED", successor_package_id=_id(), successor_package_receipt_hash="c" * 64, failure_code=None)
        with patch.object(revision_module, "_transaction") as transaction:
            for change in ({"outcome": "SUCCESS"}, {"request_hash": "bad"}, {"failure_code": "FAILED"},
                           {"outcome": "FAILED"}, {"successor_package_receipt_hash": None}):
                with self.subTest(change=change), self.assertRaises(revision_module.CaseAgentDocumentRevisionBlocked):
                    store.record(**{**args, **change})
            transaction.assert_not_called()

    def test_template_worker_delegates_to_shared_receipt_writer(self):
        worker = self._worker()
        worker._execution_dsn, worker._verifier_dsn = "execution-test", "verification-test"
        with patch.object(revision_module, "PostgresDocumentRevisionReceiptStore") as factory:
            worker._record_receipt(self.claim, outcome="UNKNOWN", successor_package_id=None,
                successor_package_receipt_hash=None, failure_code="DOCUMENT_RENDERER_RESULT_UNKNOWN")
            factory.assert_called_once_with(execution_dsn="execution-test", verifier_dsn="verification-test",
                worker_actor=self.worker_actor, verifier_actor=self.verifier_actor)
            factory.return_value.record.assert_called_once_with(request_id=self.claim.request_id,
                request_hash=self.claim.request_hash, matter_id=self.claim.matter_id, outcome="UNKNOWN",
                successor_package_id=None, successor_package_receipt_hash=None, failure_code="DOCUMENT_RENDERER_RESULT_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
