from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import unittest
from uuid import uuid4

from case_kernel.case_agent_sealed_response_recovery import (
    PostgresSealedResponseRecoveryStagingPort,
    SEALED_RESPONSE_RECOVERY_POLICY_HASH,
    SealedResponseRecoveryBlocked,
    SealedResponseRecoveryReceipt,
    build_sealed_response_recovery_staging_request,
)
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import StoredCaseAgentReviewCandidate


def _id() -> str:
    return str(uuid4())


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class _Cursor:
    def __init__(self, *, row=None, rows=None) -> None:
        self._row = row
        self._rows = [] if rows is None else rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, binding: dict[str, object]) -> None:
        self.binding = binding
        self.sql: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=None):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, parameters))
        if "FROM case_agent_runs run" in normalized:
            return _Cursor(rows=[self.binding])
        if "FROM case_agent_sealed_response_recovery_candidates recovery" in normalized:
            return _Cursor(row=None)
        return _Cursor()


class _Archive:
    def __init__(self, response_sha256: str, archive_sha256: str) -> None:
        self.response_sha256 = response_sha256
        self.archive_sha256 = archive_sha256


class _ObjectStore:
    def __init__(self, *, response: bytes, archive_sha256: str, firm_id: str, matter_id: str) -> None:
        self.response = response
        self.archive_sha256 = archive_sha256
        self.firm_id = firm_id
        self.matter_id = matter_id
        self.put_calls: list[dict[str, object]] = []
        self.verify_calls: list[tuple[object, str]] = []

    def recover_case_agent_lawyer_analysis_response(self, **kwargs):
        if kwargs["firm_id"] != self.firm_id or kwargs["matter_id"] != self.matter_id:
            return None
        digest = sha256(self.response).hexdigest()
        return _Archive(digest, self.archive_sha256), self.response, {"response_sha256": digest}

    def put_case_agent_review_candidate(self, content: bytes, **kwargs):
        self.put_calls.append({"content": content, **kwargs})
        return StoredCaseAgentReviewCandidate(
            object_key=(
                f"case-agent-candidates/v1/{kwargs['firm_id']}/{kwargs['matter_id']}/"
                f"{kwargs['artifact_id']}/{kwargs['content_sha256']}.json"
            ),
            content_sha256=kwargs["content_sha256"],
            byte_size=len(content),
            object_version_id="sealed-recovery-test-version",
        )

    def verify_case_agent_review_candidate(self, stored, *, artifact_id):
        self.verify_calls.append((stored, artifact_id))


class SealedResponseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firm_id = _id()
        self.matter_id = _id()
        self.run_id = _id()
        self.graph_id = _id()
        self.task_id = _id()
        self.worker_id = _id()
        self.request_hash = _hash("request")
        self.response = b'{"choices":[]}'
        self.response_hash = sha256(self.response).hexdigest()
        self.archive_hash = _hash("archive")
        self.payload = json.dumps(
            {
                "court_ready": False,
                "formal_fact": False,
                "formal_transaction": False,
                "legal_conclusion": False,
                "review_status": "NEEDS_LAWYER_REVIEW",
                "schema_version": "agent-lawyer-decision-package-candidate-v1",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.request = build_sealed_response_recovery_staging_request(
            run_id=self.run_id,
            task_id=self.task_id,
            task_input_hash=_hash("task-input"),
            source_hash=_hash("sources"),
            artifact_kind="LAWYER_DECISION_PACKAGE_CANDIDATE",
            candidate_payload=self.payload,
            source_run_event_version=8,
            source_run_snapshot_hash=_hash("snapshot"),
            external_request_id=_id(),
            request_hash=self.request_hash,
            response_sha256=self.response_hash,
            archive_sha256=self.archive_hash,
        )
        self.binding = {
            "run_id": self.run_id,
            "firm_id": self.firm_id,
            "matter_id": self.matter_id,
            "graph_id": self.graph_id,
            "run_status": "WAITING_INPUT",
            "event_version": "8",
            "snapshot_hash": self.request.source_run_snapshot_hash,
            "task_id": self.task_id,
            "input_hash": self.request.candidate.task_input_hash,
            "tool_id": "analyze_lawyer_decision_package",
            "task_status": "FAILED",
            "external_request_id": self.request.external_request_id,
            "request_hash": self.request_hash,
            "recorded_by": self.worker_id,
            "result_status": "FAILED",
            "external_submission_state": "SUBMITTED",
            "error_code": "LAWYER_ANALYSIS_OUTPUT_REJECTED",
            "receipt_external_request_id": self.request.external_request_id,
            "external_calls": "1",
            "external_submission_count": "1",
        }

    def test_request_and_receipt_are_deterministic_and_review_only(self) -> None:
        self.request.validate()
        receipt = SealedResponseRecoveryReceipt.build(self.request)
        self.assertEqual(receipt.candidate.review_status, "NEEDS_LAWYER_REVIEW")
        self.assertEqual(receipt.recovery_policy_hash, SEALED_RESPONSE_RECOVERY_POLICY_HASH)
        self.assertEqual(receipt, SealedResponseRecoveryReceipt.build(self.request))

    def test_request_rejects_a_non_lawyer_decision_candidate(self) -> None:
        invalid_candidate = replace(
            self.request.candidate,
            artifact_kind="CASE_CONTEXT_REVIEW_CANDIDATE",
        )
        invalid = replace(self.request, candidate=invalid_candidate)
        with self.assertRaisesRegex(SealedResponseRecoveryBlocked, "only permits"):
            invalid.validate()

    def test_stage_writes_only_candidate_and_sealed_recovery_metadata(self) -> None:
        first, second = _Connection(self.binding), _Connection(self.binding)
        connections = iter((first, second))
        store = _ObjectStore(
            response=self.response,
            archive_sha256=self.archive_hash,
            firm_id=self.firm_id,
            matter_id=self.matter_id,
        )
        port = PostgresSealedResponseRecoveryStagingPort(
            dsn="postgresql://not-used.invalid/test",
            worker_actor=Actor(
                self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER})
            ),
            object_store=store,
            connection_factory=lambda *_args, **_kwargs: next(connections),
        )

        receipt = port.stage(self.request)

        self.assertEqual(len(store.put_calls), 1)
        self.assertEqual(receipt.candidate.review_status, "NEEDS_LAWYER_REVIEW")
        writes = [sql for connection in (first, second) for sql, _ in connection.sql if sql.startswith("INSERT")]
        self.assertEqual(len(writes), 2)
        self.assertTrue(any("INSERT INTO case_agent_review_candidates" in sql for sql in writes))
        self.assertTrue(any("INSERT INTO case_agent_sealed_response_recovery_candidates" in sql for sql in writes))
        self.assertFalse(any("case_agent_artifacts" in sql for sql in writes))
        self.assertFalse(any("case_agent_events" in sql for sql in writes))

    def test_archive_integrity_failure_stops_before_candidate_write(self) -> None:
        connection = _Connection(self.binding)
        store = _ObjectStore(
            response=self.response,
            archive_sha256=_hash("different-archive"),
            firm_id=self.firm_id,
            matter_id=self.matter_id,
        )
        port = PostgresSealedResponseRecoveryStagingPort(
            dsn="postgresql://not-used.invalid/test",
            worker_actor=Actor(
                self.worker_id, self.firm_id, frozenset({Role.SYSTEM_WORKER})
            ),
            object_store=store,
            connection_factory=lambda *_args, **_kwargs: connection,
        )

        with self.assertRaisesRegex(SealedResponseRecoveryBlocked, "archive integrity"):
            port.stage(self.request)
        self.assertEqual(store.put_calls, [])


if __name__ == "__main__":
    unittest.main()
