"""Runtime adapter for deterministic whole-case context review."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from time import monotonic
from typing import Callable, Protocol

from .case_agent_case_context import (
    BoundCaseContextProjection,
    CASE_CONTEXT_ARTIFACT_KIND,
    CaseContextReviewBlocked,
    build_case_context_review_candidate,
)
from .case_agent_skill_adapters import (
    REVIEW_STATUS,
    STAGING_REQUEST_SCHEMA,
    ReviewCandidateStagingPort,
    ReviewCandidateStagingRequest,
    StagedReviewCandidate,
)
from .case_agent_supervisor import (
    AdapterExecutionMode,
    ArtifactReceipt,
    ExternalSubmissionState,
    ResultStatus,
    RuntimeAdapterManifest,
)
from .case_agent_worker import TaskAdapterOutcome, TaskExecutionContext


class CaseContextProjectionPort(Protocol):
    """Resolve only the exact structured refs selected by a compiled task."""

    def project_case_context(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> BoundCaseContextProjection: ...


_POLICY_RULES = (
    "compiled-structured-input-refs-only",
    "current-run-current-graph-only",
    "current-case-snapshot-fence",
    "server-owned-ledger-projection",
    "network-denied",
    "deterministic-rules-only",
    "review-candidate-staging-only",
    "no-formal-ledger-write",
    "no-legal-conclusion",
    "no-court-ready-output",
)


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


def _policy_hash() -> str:
    return _canonical_hash(
        {
            "schema_version": "case-context-review-adapter-policy-v1",
            "policy_id": "deterministic-case-context-review-v1",
            "rules": _POLICY_RULES,
        }
    )


CASE_CONTEXT_REVIEW_MANIFEST = RuntimeAdapterManifest(
    tool_id="review_case_context",
    adapter_id="deterministic-case-context-review",
    adapter_version="1.0.0",
    execution_mode=AdapterExecutionMode.IN_PROCESS,
    supports_idempotency=True,
    supports_reconciliation=False,
    network_capable=False,
    sandbox_policy_version="1.0.0",
    sandbox_policy_hash=_policy_hash(),
)


class DeterministicCaseContextTaskAdapter:
    """Organise current case ledgers without asking a model to invent facts."""

    manifest = CASE_CONTEXT_REVIEW_MANIFEST

    def __init__(
        self,
        *,
        projection_port: CaseContextProjectionPort,
        staging_port: ReviewCandidateStagingPort,
        max_sources: int = 500,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(projection_port, "project_case_context", None)):
            raise ValueError("case-context projection port is invalid")
        if not callable(getattr(staging_port, "stage_review_candidate", None)):
            raise ValueError("case-context staging port is invalid")
        if not 1 <= max_sources <= 500:
            raise ValueError("case-context source limit is invalid")
        self._projection_port = projection_port
        self._staging_port = staging_port
        self._max_sources = max_sources
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "DeterministicCaseContextTaskAdapter(<server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        run_id, task_id, input_hash, input_refs, guard = _task_binding(
            context, self._clock
        )
        if len(input_refs) > self._max_sources:
            raise CaseContextReviewBlocked(
                "case-context task exceeds the source limit"
            )
        projection = self._projection_port.project_case_context(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            input_refs=input_refs,
        )
        if not isinstance(projection, BoundCaseContextProjection):
            raise CaseContextReviewBlocked(
                "case-context projection port returned an invalid projection"
            )
        projection.validate()
        if (
            projection.run_id != run_id
            or projection.task_id != task_id
            or projection.task_input_hash != input_hash
            or projection.input_refs != input_refs
        ):
            raise CaseContextReviewBlocked(
                "case-context projection differs from the compiled task"
            )
        guard.check()
        payload, source_hash = build_case_context_review_candidate(projection)
        guard.check_output(len(payload))
        guard.check()
        staged = _stage_candidate(
            staging_port=self._staging_port,
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            source_hash=source_hash,
            payload=payload,
        )
        guard.check()
        return _successful_outcome(
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact=staged,
            runtime_seconds=guard.runtime_seconds(),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        raise CaseContextReviewBlocked(
            "local deterministic case-context review is not reconcilable"
        )


class _BudgetGuard:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        clock: Callable[[], float],
    ) -> None:
        if not 1 <= timeout_seconds <= 24 * 60 * 60:
            raise CaseContextReviewBlocked("case-context timeout budget is invalid")
        if not 1 <= max_output_bytes <= 10 * 1024**3:
            raise CaseContextReviewBlocked("case-context output budget is invalid")
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes
        self._clock = clock
        self._started = clock()
        if not isinstance(self._started, (int, float)) or not math.isfinite(
            self._started
        ):
            raise CaseContextReviewBlocked("case-context clock is invalid")

    def check(self) -> None:
        elapsed = self._clock() - self._started
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed > self._timeout:
            raise CaseContextReviewBlocked(
                "case-context task exceeded its compiled timeout"
            )

    def check_output(self, byte_size: int) -> None:
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 2
            or byte_size > self._max_output
        ):
            raise CaseContextReviewBlocked(
                "case-context candidate exceeds its compiled output budget"
            )

    def runtime_seconds(self) -> int:
        elapsed = self._clock() - self._started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise CaseContextReviewBlocked("case-context runtime is invalid")
        return min(self._timeout, int(math.ceil(elapsed)))


def _task_binding(
    context: TaskExecutionContext, clock: Callable[[], float]
) -> tuple[str, str, str, tuple[str, ...], _BudgetGuard]:
    claim = getattr(context, "claim", None)
    task = getattr(context, "task", None)
    input_refs = getattr(context, "input_refs", None)
    if claim is None or task is None:
        raise CaseContextReviewBlocked(
            "case-context adapter requires a lease-bound task"
        )
    run_id = getattr(claim, "run_id", None)
    task_id = getattr(claim, "task_id", None)
    if getattr(task, "task_id", None) != task_id:
        raise CaseContextReviewBlocked(
            "case-context task differs from its durable claim"
        )
    input_hash = getattr(task, "input_hash", None)
    if not isinstance(input_hash, str) or len(input_hash) != 64:
        raise CaseContextReviewBlocked("case-context task input hash is invalid")
    if (
        not isinstance(input_refs, tuple)
        or input_refs != getattr(task, "input_refs", None)
        or not input_refs
        or len(input_refs) != len(set(input_refs))
    ):
        raise CaseContextReviewBlocked(
            "case-context input refs differ from their compiled binding"
        )
    budget = getattr(task, "budget", None)
    return (
        run_id,
        task_id,
        input_hash,
        input_refs,
        _BudgetGuard(
            timeout_seconds=getattr(budget, "timeout_seconds", 0),
            max_output_bytes=getattr(budget, "max_output_bytes", 0),
            clock=clock,
        ),
    )


def _stage_candidate(
    *,
    staging_port: ReviewCandidateStagingPort,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    source_hash: str,
    payload: bytes,
) -> StagedReviewCandidate:
    content_hash = sha256(payload).hexdigest()
    idempotency_key = _canonical_hash(
        {
            "schema_version": STAGING_REQUEST_SCHEMA,
            "run_id": run_id,
            "task_id": task_id,
            "task_input_hash": task_input_hash,
            "source_hash": source_hash,
            "artifact_kind": CASE_CONTEXT_ARTIFACT_KIND,
            "content_sha256": content_hash,
        }
    )
    request = ReviewCandidateStagingRequest(
        schema_version=STAGING_REQUEST_SCHEMA,
        idempotency_key=idempotency_key,
        run_id=run_id,
        task_id=task_id,
        task_input_hash=task_input_hash,
        source_hash=source_hash,
        artifact_kind=CASE_CONTEXT_ARTIFACT_KIND,
        media_type="application/json",
        content_sha256=content_hash,
        byte_size=len(payload),
        review_status=REVIEW_STATUS,
        payload=payload,
    )
    request.validate()
    staged = staging_port.stage_review_candidate(request)
    if not isinstance(staged, StagedReviewCandidate):
        raise CaseContextReviewBlocked(
            "case-context staging returned an invalid receipt"
        )
    staged.validate_against(request)
    return staged


def _successful_outcome(
    *,
    task_input_hash: str,
    source_hash: str,
    artifact: StagedReviewCandidate,
    runtime_seconds: int,
) -> TaskAdapterOutcome:
    receipt = ArtifactReceipt(
        artifact_id=artifact.artifact_id,
        artifact_kind=artifact.artifact_kind,
        content_hash=artifact.content_sha256,
        byte_size=artifact.byte_size,
        source_input_hash=task_input_hash,
        managed_derivative=False,
    )
    receipt.validate()
    return TaskAdapterOutcome(
        status=ResultStatus.SUCCEEDED,
        external_submission_state=ExternalSubmissionState.NOT_APPLICABLE,
        output_hash=_canonical_hash(
            {
                "schema_version": "agent-case-context-adapter-output-v1",
                "task_input_hash": task_input_hash,
                "source_hash": source_hash,
                "staging_receipt_hash": artifact.receipt_hash,
                "artifact_id": receipt.artifact_id,
                "content_hash": receipt.content_hash,
                "review_status": REVIEW_STATUS,
            }
        ),
        error_code=None,
        external_request_id=None,
        runtime_seconds=runtime_seconds,
        cost_minor_units=0,
        external_calls=0,
        artifacts=(receipt,),
    )


__all__ = (
    "CASE_CONTEXT_REVIEW_MANIFEST",
    "CaseContextProjectionPort",
    "DeterministicCaseContextTaskAdapter",
)
