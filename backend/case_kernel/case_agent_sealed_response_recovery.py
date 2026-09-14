"""Narrow, append-only recovery of one authenticated historical model result.

This module deliberately does *not* repair an Agent run.  It can stage an
otherwise parseable, sealed provider response as a separate lawyer-review
candidate only when the original task remains in its recorded blocked state.
It never writes a task receipt, Agent artifact, verification receipt, event,
document package, approval, or submission record.

The command that invokes this port is fixed to the synthetic M2 acceptance
fixture.  The persistence port nevertheless re-authorises every binding and
re-authenticates the private response archive so a caller cannot turn a
claimed hash or a historical failure into a general replay mechanism.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterator, Protocol
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from .case_agent_lawyer_analysis import LAWYER_DECISION_PACKAGE_ARTIFACT_KIND
from .case_agent_skill_adapters import (
    REVIEW_STATUS,
    ReviewCandidateStagingRequest,
    StagedReviewCandidate,
)
from .models import Actor, Role
from .web_object_store import StoredCaseAgentReviewCandidate


class SealedResponseRecoveryBlocked(RuntimeError):
    """A sealed historical result cannot safely become a review candidate."""


SEALED_RESPONSE_RECOVERY_SCHEMA = "case-agent-sealed-response-recovery-staging-v1"
SEALED_RESPONSE_RECOVERY_KIND = "SEALED_RESPONSE_REPARSE"
SEALED_RESPONSE_RECOVERY_FAILURE_CODE = "LAWYER_ANALYSIS_OUTPUT_REJECTED"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


_POLICY_RULES = (
    "fixed-sealed-acceptance-command-only",
    "authenticated-private-provider-response-only",
    "no-provider-request-or-retry",
    "unchanged-waiting-input-run-only",
    "single-submitted-rejected-analysis-receipt-only",
    "review-candidate-only",
    "no-agent-artifact-event-verification-or-final-review-write",
    "no-document-package-or-court-submission",
)
SEALED_RESPONSE_RECOVERY_POLICY_HASH = _canonical_hash(
    {
        "schema_version": "case-agent-sealed-response-recovery-policy-v1",
        "recovery_kind": SEALED_RESPONSE_RECOVERY_KIND,
        "rules": _POLICY_RULES,
    }
)


@dataclass(frozen=True)
class SealedResponseRecoveryStagingRequest:
    """All immutable evidence needed to stage one review-only recovery row."""

    schema_version: str
    candidate: ReviewCandidateStagingRequest
    source_run_event_version: int
    source_run_snapshot_hash: str
    external_request_id: str
    request_hash: str
    response_sha256: str
    archive_sha256: str
    recovery_kind: str
    recovery_policy_hash: str
    failure_code: str

    def validate(self) -> None:
        if self.schema_version != SEALED_RESPONSE_RECOVERY_SCHEMA:
            raise SealedResponseRecoveryBlocked("sealed-response recovery schema is unsupported")
        try:
            self.candidate.validate()
        except Exception as error:
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery candidate is invalid"
            ) from error
        if self.candidate.artifact_kind != LAWYER_DECISION_PACKAGE_ARTIFACT_KIND:
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery only permits the lawyer decision package"
            )
        if self.candidate.review_status != REVIEW_STATUS:
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery candidate must await lawyer review"
            )
        if (
            not isinstance(self.source_run_event_version, int)
            or isinstance(self.source_run_event_version, bool)
            or self.source_run_event_version < 1
        ):
            raise SealedResponseRecoveryBlocked("source run event version is invalid")
        for label, value in (
            ("source run snapshot hash", self.source_run_snapshot_hash),
            ("request hash", self.request_hash),
            ("response hash", self.response_sha256),
            ("archive hash", self.archive_sha256),
            ("recovery policy hash", self.recovery_policy_hash),
        ):
            _hash(value, label)
        _uuid(self.external_request_id, "external request id")
        if self.recovery_kind != SEALED_RESPONSE_RECOVERY_KIND:
            raise SealedResponseRecoveryBlocked("sealed-response recovery kind is invalid")
        if self.recovery_policy_hash != SEALED_RESPONSE_RECOVERY_POLICY_HASH:
            raise SealedResponseRecoveryBlocked("sealed-response recovery policy differs")
        if self.failure_code != SEALED_RESPONSE_RECOVERY_FAILURE_CODE:
            raise SealedResponseRecoveryBlocked("sealed-response recovery failure code differs")
        if self.candidate.idempotency_key != _idempotency_key(self):
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery idempotency binding differs"
            )


@dataclass(frozen=True)
class SealedResponseRecoveryReceipt:
    """A deterministic, non-promoting receipt for recovery-candidate staging."""

    recovery_id: str
    candidate: StagedReviewCandidate
    recovery_kind: str
    source_run_event_version: int
    source_run_snapshot_hash: str
    response_sha256: str
    archive_sha256: str
    recovery_policy_hash: str
    receipt_hash: str

    @classmethod
    def build(
        cls, request: SealedResponseRecoveryStagingRequest
    ) -> "SealedResponseRecoveryReceipt":
        request.validate()
        candidate = StagedReviewCandidate.build(
            request.candidate,
            artifact_id=str(uuid5(UUID(request.candidate.task_id), request.candidate.idempotency_key)),
        )
        recovery_id = str(
            uuid5(
                UUID(request.candidate.task_id),
                f"sealed-response-recovery:{request.candidate.idempotency_key}",
            )
        )
        payload = {
            "schema_version": "case-agent-sealed-response-recovery-receipt-v1",
            "recovery_id": recovery_id,
            "candidate": {
                "artifact_id": candidate.artifact_id,
                "receipt_hash": candidate.receipt_hash,
            },
            "recovery_kind": request.recovery_kind,
            "source_run_event_version": request.source_run_event_version,
            "source_run_snapshot_hash": request.source_run_snapshot_hash,
            "response_sha256": request.response_sha256,
            "archive_sha256": request.archive_sha256,
            "recovery_policy_hash": request.recovery_policy_hash,
        }
        return cls(
            recovery_id=recovery_id,
            candidate=candidate,
            recovery_kind=request.recovery_kind,
            source_run_event_version=request.source_run_event_version,
            source_run_snapshot_hash=request.source_run_snapshot_hash,
            response_sha256=request.response_sha256,
            archive_sha256=request.archive_sha256,
            recovery_policy_hash=request.recovery_policy_hash,
            receipt_hash=_canonical_hash(payload),
        )

    def validate_against(self, request: SealedResponseRecoveryStagingRequest) -> None:
        _uuid(self.recovery_id, "sealed-response recovery id")
        _hash(self.receipt_hash, "sealed-response recovery receipt hash")
        expected = SealedResponseRecoveryReceipt.build(request)
        if self != expected:
            raise SealedResponseRecoveryBlocked(
                "stored sealed-response recovery receipt differs from its request"
            )


def build_sealed_response_recovery_staging_request(
    *,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    source_hash: str,
    artifact_kind: str,
    candidate_payload: bytes,
    source_run_event_version: int,
    source_run_snapshot_hash: str,
    external_request_id: str,
    request_hash: str,
    response_sha256: str,
    archive_sha256: str,
) -> SealedResponseRecoveryStagingRequest:
    """Create the only supported request shape from authenticated replay output."""

    for label, value in (
        ("run id", run_id),
        ("task id", task_id),
        ("external request id", external_request_id),
    ):
        _uuid(value, label)
    for label, value in (
        ("task input hash", task_input_hash),
        ("source hash", source_hash),
        ("source run snapshot hash", source_run_snapshot_hash),
        ("request hash", request_hash),
        ("response hash", response_sha256),
        ("archive hash", archive_sha256),
    ):
        _hash(value, label)
    if not isinstance(candidate_payload, bytes) or len(candidate_payload) < 2:
        raise SealedResponseRecoveryBlocked("recovery candidate payload is invalid")
    content_sha256 = sha256(candidate_payload).hexdigest()
    candidate = ReviewCandidateStagingRequest(
        schema_version="agent-review-candidate-staging-v1",
        idempotency_key="0" * 64,
        run_id=run_id,
        task_id=task_id,
        task_input_hash=task_input_hash,
        source_hash=source_hash,
        artifact_kind=artifact_kind,
        media_type="application/json",
        content_sha256=content_sha256,
        byte_size=len(candidate_payload),
        review_status=REVIEW_STATUS,
        payload=candidate_payload,
    )
    provisional = SealedResponseRecoveryStagingRequest(
        schema_version=SEALED_RESPONSE_RECOVERY_SCHEMA,
        candidate=candidate,
        source_run_event_version=source_run_event_version,
        source_run_snapshot_hash=source_run_snapshot_hash,
        external_request_id=external_request_id,
        request_hash=request_hash,
        response_sha256=response_sha256,
        archive_sha256=archive_sha256,
        recovery_kind=SEALED_RESPONSE_RECOVERY_KIND,
        recovery_policy_hash=SEALED_RESPONSE_RECOVERY_POLICY_HASH,
        failure_code=SEALED_RESPONSE_RECOVERY_FAILURE_CODE,
    )
    candidate = ReviewCandidateStagingRequest(
        **{**candidate.__dict__, "idempotency_key": _idempotency_key(provisional)}
    )
    result = SealedResponseRecoveryStagingRequest(
        **{**provisional.__dict__, "candidate": candidate}
    )
    result.validate()
    return result


class _SealedRecoveryObjectStore(Protocol):
    def put_case_agent_review_candidate(
        self, content: bytes, **kwargs: Any
    ) -> StoredCaseAgentReviewCandidate: ...

    def verify_case_agent_review_candidate(
        self, stored: StoredCaseAgentReviewCandidate, *, artifact_id: str
    ) -> None: ...

    def recover_case_agent_lawyer_analysis_response(
        self, **kwargs: Any
    ) -> tuple[Any, bytes, dict[str, object]] | None: ...


class PostgresSealedResponseRecoveryStagingPort:
    """Re-authorise and append one sealed response recovery candidate."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: _SealedRecoveryObjectStore,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("sealed-response recovery PostgreSQL DSN is required")
        _worker_actor(worker_actor)
        for method in (
            "put_case_agent_review_candidate",
            "verify_case_agent_review_candidate",
            "recover_case_agent_lawyer_analysis_response",
        ):
            if not callable(getattr(object_store, method, None)):
                raise ValueError("sealed-response recovery object store is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        self._object_store = object_store
        self._connect = connection_factory or psycopg.connect

    def stage(
        self, request: SealedResponseRecoveryStagingRequest
    ) -> SealedResponseRecoveryReceipt:
        request.validate()
        expected = SealedResponseRecoveryReceipt.build(request)
        with self._transaction(read_only=True) as connection:
            binding = self._read_binding(connection, request=request)
            prior = self._read_prior(connection, idempotency_key=request.candidate.idempotency_key)
        self._authenticate_archive(request=request, binding=binding)
        if prior is not None:
            return self._verify_prior(prior, request=request)

        stored = self._object_store.put_case_agent_review_candidate(
            request.candidate.payload,
            firm_id=self._worker.firm_id,
            matter_id=binding["matter_id"],
            artifact_id=expected.candidate.artifact_id,
            content_sha256=request.candidate.content_sha256,
        )
        if (
            not isinstance(stored, StoredCaseAgentReviewCandidate)
            or stored.content_sha256 != request.candidate.content_sha256
            or stored.byte_size != request.candidate.byte_size
        ):
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery object-store receipt differs"
            )
        try:
            with self._transaction(read_only=False) as connection:
                current = self._read_binding(connection, request=request)
                if current != binding:
                    raise SealedResponseRecoveryBlocked(
                        "sealed-response recovery binding changed before staging"
                    )
                connection.execute(
                    """
                    INSERT INTO case_agent_review_candidates (
                        artifact_id, idempotency_key, run_id, graph_id, task_id,
                        firm_id, matter_id, task_input_hash, source_hash,
                        artifact_kind, media_type, content_sha256, byte_size,
                        review_status, source_object_key,
                        source_object_version_id, receipt_hash, staged_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        expected.candidate.artifact_id,
                        request.candidate.idempotency_key,
                        request.candidate.run_id,
                        binding["graph_id"],
                        request.candidate.task_id,
                        self._worker.firm_id,
                        binding["matter_id"],
                        request.candidate.task_input_hash,
                        request.candidate.source_hash,
                        request.candidate.artifact_kind,
                        request.candidate.media_type,
                        request.candidate.content_sha256,
                        request.candidate.byte_size,
                        request.candidate.review_status,
                        stored.object_key,
                        stored.object_version_id,
                        expected.candidate.receipt_hash,
                        self._worker.actor_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO case_agent_sealed_response_recovery_candidates (
                        recovery_id, artifact_id, idempotency_key, run_id, graph_id,
                        task_id, firm_id, matter_id, task_input_hash, source_hash,
                        candidate_content_sha256, source_run_event_version,
                        source_snapshot_hash, recovery_kind, recovery_policy_hash,
                        external_request_id, request_hash, response_sha256,
                        archive_sha256, failure_code, recovered_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        expected.recovery_id,
                        expected.candidate.artifact_id,
                        request.candidate.idempotency_key,
                        request.candidate.run_id,
                        binding["graph_id"],
                        request.candidate.task_id,
                        self._worker.firm_id,
                        binding["matter_id"],
                        request.candidate.task_input_hash,
                        request.candidate.source_hash,
                        request.candidate.content_sha256,
                        request.source_run_event_version,
                        request.source_run_snapshot_hash,
                        request.recovery_kind,
                        request.recovery_policy_hash,
                        request.external_request_id,
                        request.request_hash,
                        request.response_sha256,
                        request.archive_sha256,
                        request.failure_code,
                        self._worker.actor_id,
                    ),
                )
            return expected
        except psycopg.IntegrityError:
            with self._transaction(read_only=True) as connection:
                prior = self._read_prior(
                    connection, idempotency_key=request.candidate.idempotency_key
                )
            if prior is None:
                raise
            return self._verify_prior(prior, request=request)

    def _authenticate_archive(
        self,
        *,
        request: SealedResponseRecoveryStagingRequest,
        binding: dict[str, str],
    ) -> None:
        recovered = self._object_store.recover_case_agent_lawyer_analysis_response(
            firm_id=binding["firm_id"],
            matter_id=binding["matter_id"],
            external_request_id=request.external_request_id,
            request_hash=request.request_hash,
        )
        if recovered is None:
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery archive is unavailable"
            )
        stored, response, archive_receipt = recovered
        if (
            not isinstance(response, bytes)
            or sha256(response).hexdigest() != request.response_sha256
            or getattr(stored, "response_sha256", None) != request.response_sha256
            or getattr(stored, "archive_sha256", None) != request.archive_sha256
            or not isinstance(archive_receipt, dict)
            or archive_receipt.get("response_sha256") != request.response_sha256
        ):
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery archive integrity differs"
            )

    def _read_binding(
        self,
        connection: Any,
        *,
        request: SealedResponseRecoveryStagingRequest,
    ) -> dict[str, str]:
        row = connection.execute(
            """
            SELECT run.run_id::text AS run_id,
                   run.firm_id::text AS firm_id,
                   run.matter_id::text AS matter_id,
                   run.current_graph_id::text AS graph_id,
                   run.status AS run_status,
                   run.current_event_version::text AS event_version,
                   run.snapshot_hash,
                   task.task_id::text AS task_id,
                   task.input_hash,
                   task.tool_id,
                   head.status AS task_status,
                   submission.external_request_id::text AS external_request_id,
                   submission.request_hash,
                   submission.recorded_by::text AS recorded_by,
                   receipt.result_status,
                   receipt.external_submission_state,
                   receipt.error_code,
                   receipt.external_request_id AS receipt_external_request_id,
                   receipt.external_calls::text AS external_calls,
                   (
                       SELECT COUNT(*)::text
                         FROM case_agent_external_submissions counted
                        WHERE counted.run_id = run.run_id
                          AND counted.firm_id = run.firm_id
                          AND counted.matter_id = run.matter_id
                   ) AS external_submission_count
              FROM case_agent_runs run
              JOIN case_agent_tasks task
                ON task.run_id = run.run_id
               AND task.graph_id = run.current_graph_id
               AND task.firm_id = run.firm_id
               AND task.matter_id = run.matter_id
              JOIN case_agent_task_heads head
                ON head.run_id = task.run_id
               AND head.graph_id = task.graph_id
               AND head.task_id = task.task_id
               AND head.firm_id = task.firm_id
               AND head.matter_id = task.matter_id
               AND head.is_current
              JOIN case_agent_external_submissions submission
                ON submission.run_id = task.run_id
               AND submission.task_id = task.task_id
               AND submission.firm_id = task.firm_id
               AND submission.matter_id = task.matter_id
              JOIN case_agent_task_receipts receipt
                ON receipt.attempt_id = submission.attempt_id
               AND receipt.run_id = submission.run_id
               AND receipt.task_id = submission.task_id
               AND receipt.firm_id = submission.firm_id
               AND receipt.matter_id = submission.matter_id
              JOIN matter_actor_roles worker_role
                ON worker_role.matter_id = run.matter_id
               AND worker_role.firm_id = run.firm_id
               AND worker_role.user_id = %s::uuid
               AND worker_role.role = 'SYSTEM_WORKER'
               AND worker_role.revoked_at IS NULL
              JOIN users worker_user
                ON worker_user.user_id = worker_role.user_id
               AND worker_user.firm_id = worker_role.firm_id
               AND worker_user.status = 'ACTIVE'
             WHERE run.run_id = %s::uuid
               AND run.firm_id = %s::uuid
               AND task.task_id = %s::uuid
            """,
            (
                self._worker.actor_id,
                request.candidate.run_id,
                self._worker.firm_id,
                request.candidate.task_id,
            ),
        ).fetchall()
        if len(row) != 1:
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery binding is unavailable or ambiguous"
            )
        value = {key: str(item) for key, item in dict(row[0]).items()}
        expected = {
            "run_id": request.candidate.run_id,
            "firm_id": self._worker.firm_id,
            "task_id": request.candidate.task_id,
            "run_status": "WAITING_INPUT",
            "event_version": str(request.source_run_event_version),
            "snapshot_hash": request.source_run_snapshot_hash,
            "input_hash": request.candidate.task_input_hash,
            "tool_id": "analyze_lawyer_decision_package",
            "task_status": "FAILED",
            "external_request_id": request.external_request_id,
            "request_hash": request.request_hash,
            "recorded_by": self._worker.actor_id,
            "result_status": "FAILED",
            "external_submission_state": "SUBMITTED",
            "error_code": request.failure_code,
            "receipt_external_request_id": request.external_request_id,
            "external_calls": "1",
            "external_submission_count": "1",
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise SealedResponseRecoveryBlocked(
                "sealed-response recovery does not match the recorded blocked result"
            )
        for label, identifier in (
            ("binding run id", value.get("run_id")),
            ("binding firm id", value.get("firm_id")),
            ("binding matter id", value.get("matter_id")),
            ("binding graph id", value.get("graph_id")),
            ("binding task id", value.get("task_id")),
        ):
            _uuid(identifier, label)
        return {
            "firm_id": value["firm_id"],
            "matter_id": value["matter_id"],
            "graph_id": value["graph_id"],
        }

    def _read_prior(
        self, connection: Any, *, idempotency_key: str
    ) -> dict[str, Any] | None:
        return connection.execute(
            """
            SELECT recovery.recovery_id, recovery.recovery_kind,
                   recovery.source_run_event_version,
                   recovery.source_snapshot_hash, recovery.response_sha256,
                   recovery.archive_sha256, recovery.recovery_policy_hash,
                   candidate.artifact_id, candidate.idempotency_key,
                   candidate.artifact_kind, candidate.content_sha256,
                   candidate.byte_size, candidate.source_hash,
                   candidate.task_input_hash, candidate.review_status,
                   candidate.receipt_hash, candidate.source_object_key,
                   candidate.source_object_version_id
              FROM case_agent_sealed_response_recovery_candidates recovery
              JOIN case_agent_review_candidates candidate
                ON candidate.artifact_id = recovery.artifact_id
               AND candidate.run_id = recovery.run_id
               AND candidate.firm_id = recovery.firm_id
               AND candidate.matter_id = recovery.matter_id
             WHERE recovery.firm_id = %s::uuid
               AND recovery.idempotency_key = %s
            """,
            (self._worker.firm_id, idempotency_key),
        ).fetchone()

    def _verify_prior(
        self,
        row: dict[str, Any],
        *,
        request: SealedResponseRecoveryStagingRequest,
    ) -> SealedResponseRecoveryReceipt:
        candidate = StagedReviewCandidate(
            artifact_id=str(row["artifact_id"]),
            idempotency_key=str(row["idempotency_key"]),
            artifact_kind=str(row["artifact_kind"]),
            content_sha256=str(row["content_sha256"]),
            byte_size=int(row["byte_size"]),
            source_hash=str(row["source_hash"]),
            task_input_hash=str(row["task_input_hash"]),
            review_status=str(row["review_status"]),
            receipt_hash=str(row["receipt_hash"]),
        )
        receipt = SealedResponseRecoveryReceipt.build(request)
        prior = SealedResponseRecoveryReceipt(
            recovery_id=str(row["recovery_id"]),
            candidate=candidate,
            recovery_kind=str(row["recovery_kind"]),
            source_run_event_version=int(row["source_run_event_version"]),
            source_run_snapshot_hash=str(row["source_snapshot_hash"]),
            response_sha256=str(row["response_sha256"]),
            archive_sha256=str(row["archive_sha256"]),
            recovery_policy_hash=str(row["recovery_policy_hash"]),
            receipt_hash=receipt.receipt_hash,
        )
        prior.validate_against(request)
        stored = StoredCaseAgentReviewCandidate(
            object_key=str(row["source_object_key"]),
            content_sha256=str(row["content_sha256"]),
            byte_size=int(row["byte_size"]),
            object_version_id=row.get("source_object_version_id"),
        )
        self._object_store.verify_case_agent_review_candidate(
            stored, artifact_id=prior.candidate.artifact_id
        )
        return prior

    @contextmanager
    def _transaction(self, *, read_only: bool) -> Iterator[Any]:
        with self._connect(self._dsn, row_factory=dict_row) as connection:
            if read_only:
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
            connection.execute(
                "SELECT set_config('app.firm_id', %s, true)",
                (self._worker.firm_id,),
            )
            yield connection


def _idempotency_key(request: SealedResponseRecoveryStagingRequest) -> str:
    candidate = request.candidate
    return _canonical_hash(
        {
            "schema_version": SEALED_RESPONSE_RECOVERY_SCHEMA,
            "recovery_kind": request.recovery_kind,
            "recovery_policy_hash": request.recovery_policy_hash,
            "run_id": candidate.run_id,
            "task_id": candidate.task_id,
            "task_input_hash": candidate.task_input_hash,
            "source_hash": candidate.source_hash,
            "artifact_kind": candidate.artifact_kind,
            "content_sha256": candidate.content_sha256,
            "byte_size": candidate.byte_size,
            "source_run_event_version": request.source_run_event_version,
            "source_run_snapshot_hash": request.source_run_snapshot_hash,
            "external_request_id": request.external_request_id,
            "request_hash": request.request_hash,
            "response_sha256": request.response_sha256,
            "archive_sha256": request.archive_sha256,
            "failure_code": request.failure_code,
        }
    )


def _worker_actor(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("sealed-response recovery requires a dedicated SYSTEM_WORKER")
    _uuid(actor.actor_id, "worker actor id")
    _uuid(actor.firm_id, "worker firm id")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise SealedResponseRecoveryBlocked(f"{label} must be a UUID") from error


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SealedResponseRecoveryBlocked(f"{label} must be a SHA-256 digest")
