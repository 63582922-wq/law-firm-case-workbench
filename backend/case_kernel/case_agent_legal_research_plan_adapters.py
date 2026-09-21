"""Managed Worker adapter for deterministic legal-research planning."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from time import monotonic
from typing import Callable, Protocol

from .case_agent_case_context import BoundCaseContextProjection
from .case_agent_legal_research_plan import (
    LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
    LEGAL_RESEARCH_PLANNING_SKILL_ID,
    LEGAL_RESEARCH_PLANNING_TOOL_ID,
    LegalResearchPlanBlocked,
    build_legal_research_plan_candidate,
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
    NetworkPolicy,
    ResultStatus,
    RuntimeAdapterManifest,
)
from .case_agent_worker import TaskAdapterOutcome, TaskExecutionContext


class LegalResearchPlanningAdapterBlocked(RuntimeError):
    """The compiled planning task is stale, overbroad or structurally invalid."""


class LegalResearchProjectionPort(Protocol):
    def project_case_context(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> BoundCaseContextProjection: ...


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


_POLICY = {
    "schema_version": "case-agent-legal-research-planning-policy-v1",
    "rules": (
        "server-compiled-current-structured-refs-only",
        "current-case-snapshot-fence",
        "network-denied",
        "fixed-public-legal-vocabulary-only",
        "research-plan-candidate-only",
        "search-requires-separate-lawyer-approval",
        "official-capture-is-separate",
        "no-legal-effect-or-case-application-decision",
    ),
}

LEGAL_RESEARCH_PLANNING_MANIFEST = RuntimeAdapterManifest(
    tool_id=LEGAL_RESEARCH_PLANNING_TOOL_ID,
    adapter_id="deterministic-legal-research-planning",
    adapter_version="1.0.0",
    execution_mode=AdapterExecutionMode.IN_PROCESS,
    supports_idempotency=True,
    supports_reconciliation=False,
    network_capable=False,
    sandbox_policy_version="1.0.0",
    sandbox_policy_hash=_canonical_hash(_POLICY),
)


class DeterministicLegalResearchPlanningTaskAdapter:
    manifest = LEGAL_RESEARCH_PLANNING_MANIFEST

    def __init__(
        self,
        *,
        projection_port: LegalResearchProjectionPort,
        staging_port: ReviewCandidateStagingPort,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(projection_port, "project_case_context", None)):
            raise ValueError("legal-research projection port is invalid")
        if not callable(getattr(staging_port, "stage_review_candidate", None)):
            raise ValueError("legal-research staging port is invalid")
        self._projection = projection_port
        self._staging = staging_port
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "DeterministicLegalResearchPlanningTaskAdapter(<server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._clock()
        task = context.task
        claim = context.claim
        if (
            task.skill.skill_id != LEGAL_RESEARCH_PLANNING_SKILL_ID
            or task.skill.tool_id != LEGAL_RESEARCH_PLANNING_TOOL_ID
            or task.skill.adapter_id != self.manifest.adapter_id
            or task.skill.adapter_version != self.manifest.adapter_version
            or task.capability.network_policy is not NetworkPolicy.DENY
            or task.capability.allowed_domains
            or task.budget.max_external_calls != 0
            or task.budget.max_cost_minor_units != 0
            or not task.input_refs
            or task.input_refs != context.input_refs
            or claim.task_id != task.task_id
        ):
            raise LegalResearchPlanningAdapterBlocked(
                "compiled legal-research planning capability is not exact"
            )
        _budget_guard(task.budget.timeout_seconds, task.budget.max_output_bytes)
        projection = self._projection.project_case_context(
            run_id=claim.run_id,
            task_id=claim.task_id,
            task_input_hash=task.input_hash,
            input_refs=task.input_refs,
        )
        if not isinstance(projection, BoundCaseContextProjection):
            raise LegalResearchPlanningAdapterBlocked(
                "legal-research projection is invalid"
            )
        projection.validate()
        if (
            projection.run_id != claim.run_id
            or projection.task_id != claim.task_id
            or projection.task_input_hash != task.input_hash
            or projection.input_refs != task.input_refs
        ):
            raise LegalResearchPlanningAdapterBlocked(
                "legal-research projection differs from the compiled task"
            )
        try:
            payload, source_hash = build_legal_research_plan_candidate(projection)
        except LegalResearchPlanBlocked as error:
            raise LegalResearchPlanningAdapterBlocked(
                "legal-research plan candidate was blocked"
            ) from error
        elapsed = _elapsed(started, self._clock)
        if elapsed > task.budget.timeout_seconds or len(payload) > task.budget.max_output_bytes:
            raise LegalResearchPlanningAdapterBlocked(
                "legal-research planning exceeded its compiled budget"
            )
        content_hash = sha256(payload).hexdigest()
        request = ReviewCandidateStagingRequest(
            schema_version=STAGING_REQUEST_SCHEMA,
            idempotency_key=_canonical_hash(
                {
                    "schema_version": STAGING_REQUEST_SCHEMA,
                    "run_id": claim.run_id,
                    "task_id": claim.task_id,
                    "task_input_hash": task.input_hash,
                    "source_hash": source_hash,
                    "artifact_kind": LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
                    "content_sha256": content_hash,
                }
            ),
            run_id=claim.run_id,
            task_id=claim.task_id,
            task_input_hash=task.input_hash,
            source_hash=source_hash,
            artifact_kind=LEGAL_RESEARCH_PLAN_ARTIFACT_KIND,
            media_type="application/json",
            content_sha256=content_hash,
            byte_size=len(payload),
            review_status=REVIEW_STATUS,
            payload=payload,
        )
        request.validate()
        staged = self._staging.stage_review_candidate(request)
        if not isinstance(staged, StagedReviewCandidate):
            raise LegalResearchPlanningAdapterBlocked(
                "legal-research staging returned an invalid receipt"
            )
        staged.validate_against(request)
        receipt = ArtifactReceipt(
            artifact_id=staged.artifact_id,
            artifact_kind=staged.artifact_kind,
            content_hash=staged.content_sha256,
            byte_size=staged.byte_size,
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        receipt.validate()
        return TaskAdapterOutcome(
            status=ResultStatus.SUCCEEDED,
            external_submission_state=ExternalSubmissionState.NOT_APPLICABLE,
            output_hash=_canonical_hash(
                {
                    "schema_version": "agent-legal-research-planning-output-v1",
                    "task_input_hash": task.input_hash,
                    "source_hash": source_hash,
                    "staging_receipt_hash": staged.receipt_hash,
                    "artifact_id": receipt.artifact_id,
                    "content_hash": receipt.content_hash,
                    "review_status": REVIEW_STATUS,
                }
            ),
            error_code=None,
            external_request_id=None,
            runtime_seconds=min(
                task.budget.timeout_seconds, int(math.ceil(_elapsed(started, self._clock)))
            ),
            cost_minor_units=0,
            external_calls=0,
            artifacts=(receipt,),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        _ = context
        raise LegalResearchPlanningAdapterBlocked(
            "deterministic legal-research planning is not reconcilable"
        )


def _budget_guard(timeout_seconds: int, max_output_bytes: int) -> None:
    if not 1 <= timeout_seconds <= 24 * 60 * 60:
        raise LegalResearchPlanningAdapterBlocked(
            "legal-research timeout budget is invalid"
        )
    if not 1 <= max_output_bytes <= 10 * 1024**3:
        raise LegalResearchPlanningAdapterBlocked(
            "legal-research output budget is invalid"
        )


def _elapsed(started: float, clock: Callable[[], float]) -> float:
    elapsed = clock() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        raise LegalResearchPlanningAdapterBlocked("legal-research clock is invalid")
    return elapsed


__all__ = (
    "LEGAL_RESEARCH_PLANNING_MANIFEST",
    "LEGAL_RESEARCH_PLANNING_SKILL_ID",
    "LEGAL_RESEARCH_PLANNING_TOOL_ID",
    "DeterministicLegalResearchPlanningTaskAdapter",
    "LegalResearchPlanningAdapterBlocked",
    "LegalResearchProjectionPort",
)
