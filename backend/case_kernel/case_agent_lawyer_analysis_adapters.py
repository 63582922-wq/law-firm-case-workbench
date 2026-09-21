"""Managed Worker adapter for the review-only lawyer decision package.

The adapter reuses the authoritative whole-case projection boundary, commits
the generic external-submission marker before any provider byte, and delegates
the only call plus lookup-only recovery to an injected exchange.  Provider
output is parsed and compiled by ``case_agent_lawyer_analysis`` before it can
enter ordinary encrypted review-candidate staging.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Callable, Protocol
from uuid import UUID

from .case_agent_case_context import BoundCaseContextProjection
from .case_agent_lawyer_analysis import (
    LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
    LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
    LawyerAnalysisBlocked,
    LawyerAnalysisContract,
    PreparedLawyerAnalysisRequest,
    compile_lawyer_decision_package_candidate,
    known_lawyer_analysis_response_cost_minor_units,
    parse_lawyer_analysis_provider_response,
    prepare_lawyer_analysis_request,
    valid_qwen_lawyer_analysis_host,
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
from .case_agent_worker import (
    CaseAgentReconciliationUnavailable,
    TaskAdapterOutcome,
    TaskExecutionContext,
)


LAWYER_ANALYSIS_TOOL_ID = "analyze_lawyer_decision_package"
LAWYER_ANALYSIS_SKILL_ID = "lawyer_decision_package"
LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED = "LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED"
LAWYER_ANALYSIS_DNS_FAILED = "LAWYER_ANALYSIS_DNS_FAILED"
LAWYER_ANALYSIS_CONNECT_FAILED = "LAWYER_ANALYSIS_CONNECT_FAILED"
LAWYER_ANALYSIS_BINDING_REJECTED = "LAWYER_ANALYSIS_BINDING_REJECTED"
LAWYER_ANALYSIS_BINDING_UNAVAILABLE = "LAWYER_ANALYSIS_BINDING_UNAVAILABLE"
LAWYER_ANALYSIS_PRE_DISPATCH_FAILURES = frozenset(
    {
        LAWYER_ANALYSIS_BINDING_REJECTED,
        LAWYER_ANALYSIS_BINDING_UNAVAILABLE,
        LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED,
        LAWYER_ANALYSIS_DNS_FAILED,
        LAWYER_ANALYSIS_CONNECT_FAILED,
    }
)
LAWYER_ANALYSIS_UNKNOWN_ERROR_CODES = frozenset(
    {
        "LAWYER_ANALYSIS_UNKNOWN_SEND",
        "LAWYER_ANALYSIS_UNKNOWN_RESPONSE_HEAD",
        "LAWYER_ANALYSIS_UNKNOWN_RESPONSE_BODY",
        "LAWYER_ANALYSIS_UNKNOWN_ARCHIVE",
    }
)


class CaseAgentLawyerAnalysisAdapterBlocked(RuntimeError):
    """The task is stale, unsafe, structurally invalid or unrecoverable."""


class LawyerAnalysisKnownFailure(RuntimeError):
    """A complete known provider/storage rejection, never an unknown send."""

    def __init__(self, error_code: str) -> None:
        if (
            not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 80
        ):
            raise ValueError("lawyer analysis error code is invalid")
        self.error_code = error_code
        super().__init__("lawyer analysis request failed")


@dataclass(frozen=True)
class RecoveredLawyerAnalysis:
    status: str
    response_body: bytes | None = None
    error_code: str | None = None
    # The durable boundary supplies this only when it found the immutable
    # submission record.  It lets a recovered response prove it belongs to
    # the exact original request even if current prompt construction has
    # changed since the network call.
    request_hash: str | None = None

    def validate(self) -> None:
        if self.status == "SUCCEEDED":
            if not isinstance(self.response_body, bytes) or self.error_code is not None:
                raise CaseAgentLawyerAnalysisAdapterBlocked(
                    "recovered lawyer analysis success is invalid"
                )
        elif self.status == "FAILED":
            if self.response_body is not None or not isinstance(self.error_code, str):
                raise CaseAgentLawyerAnalysisAdapterBlocked(
                    "recovered lawyer analysis failure is invalid"
                )
        elif self.status == "UNRESOLVED":
            if self.response_body is not None or self.error_code is not None:
                raise CaseAgentLawyerAnalysisAdapterBlocked(
                    "unresolved lawyer analysis result is invalid"
                )
        else:
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "recovered lawyer analysis status is invalid"
            )
        if self.request_hash is not None and (
            not isinstance(self.request_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.request_hash) is None
        ):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "recovered lawyer analysis request hash is invalid"
            )


@dataclass(frozen=True)
class LawyerAnalysisRecoveryBinding:
    """The immutable identity needed for lookup-only recovery.

    Deliberately excludes a regenerated prompt/body and its hash.  A recovery
    must authenticate against the durable submission row, not against current
    prompt-building code, so a later code change cannot turn an old unknown
    request into a fake new provider result.
    """

    run_id: str
    task_id: str
    attempt_id: str
    task_input_hash: str
    input_refs: tuple[str, ...]
    external_request_id: str

    def validate(self) -> None:
        for value, label in (
            (self.run_id, "recovery run_id"),
            (self.task_id, "recovery task_id"),
            (self.attempt_id, "recovery attempt_id"),
            (self.external_request_id, "recovery external_request_id"),
        ):
            try:
                UUID(str(value))
            except (TypeError, ValueError, AttributeError):
                raise CaseAgentLawyerAnalysisAdapterBlocked(
                    f"{label} is invalid"
                ) from None
        if (
            not isinstance(self.task_input_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.task_input_hash) is None
            or not isinstance(self.input_refs, tuple)
            or not self.input_refs
            or len(self.input_refs) != len(set(self.input_refs))
            or any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 500
                for value in self.input_refs
            )
        ):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis recovery binding is invalid"
            )


class LawyerAnalysisProjectionPort(Protocol):
    def project_case_context(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> BoundCaseContextProjection: ...


class RecoverableLawyerAnalysisExchange(Protocol):
    """One durable send; ``recover`` is lookup-only and never resubmits."""

    @property
    def endpoint_host(self) -> str: ...

    def send(self, *, request: PreparedLawyerAnalysisRequest) -> bytes: ...

    def recover(
        self, *, external_request_id: str, request_hash: str
    ) -> RecoveredLawyerAnalysis: ...

    def recover_bound(
        self, *, binding: LawyerAnalysisRecoveryBinding
    ) -> RecoveredLawyerAnalysis: ...


def _manifest() -> RuntimeAdapterManifest:
    policy = {
        "schema_version": "case-agent-lawyer-analysis-adapter-policy-v3",
        "rules": (
            "compiled-current-structured-case-refs-only",
            "current-case-snapshot-fence",
            "fixed-qwen37-strict-json-schema",
            "no-max-tokens-for-structured-output",
            "model-free-text-numeric-and-authority-quarantine",
            "durable-external-boundary-before-network",
            "unknown-is-lookup-only-never-resend",
            "pre-send-connect-failure-is-known-zero-call",
            "unknown-transport-phase-is-preserved",
            "controlled-output-rejection-is-terminal-not-unknown",
            "strict-review-candidate-parser-before-staging",
            "unverified-legal-paths-are-research-hypotheses-only",
            "model-cannot-select-external-legal-action",
            "no-formal-ledger-calculation-approval-or-release-write",
        ),
    }
    return RuntimeAdapterManifest(
        tool_id=LAWYER_ANALYSIS_TOOL_ID,
        adapter_id="qwen-strict-lawyer-decision-analysis",
        adapter_version="1.2.0",
        execution_mode=AdapterExecutionMode.NETWORK_CONNECTOR,
        supports_idempotency=True,
        supports_reconciliation=True,
        network_capable=True,
        sandbox_policy_version="1.2.0",
        sandbox_policy_hash=sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


QWEN_LAWYER_ANALYSIS_MANIFEST = _manifest()


class QwenLawyerAnalysisTaskAdapter:
    manifest = QWEN_LAWYER_ANALYSIS_MANIFEST

    def __init__(
        self,
        *,
        projection_port: LawyerAnalysisProjectionPort,
        exchange: RecoverableLawyerAnalysisExchange,
        staging_port: ReviewCandidateStagingPort,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(projection_port, "project_case_context", None)):
            raise ValueError("lawyer analysis projection port is invalid")
        if (
            not callable(getattr(exchange, "send", None))
            or not callable(getattr(exchange, "recover", None))
            or not callable(getattr(exchange, "recover_bound", None))
            or not valid_qwen_lawyer_analysis_host(
                getattr(exchange, "endpoint_host", None)
            )
        ):
            raise ValueError("recoverable lawyer analysis exchange is invalid")
        if not callable(getattr(staging_port, "stage_review_candidate", None)):
            raise ValueError("lawyer analysis candidate staging port is invalid")
        self._projection = projection_port
        self._exchange = exchange
        self._staging = staging_port
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "QwenLawyerAnalysisTaskAdapter(<server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._clock()
        projection, contract, request = self._prepare(context)
        context.begin_external_submission(
            external_request_id=request.external_request_id,
            destination=request.endpoint_host,
            request_hash=request.request_hash,
        )
        try:
            response = self._exchange.send(request=request)
        except LawyerAnalysisKnownFailure as failure:
            pre_dispatch = failure.error_code in LAWYER_ANALYSIS_PRE_DISPATCH_FAILURES
            return TaskAdapterOutcome(
                status=ResultStatus.FAILED,
                external_submission_state=(
                    ExternalSubmissionState.NOT_SUBMITTED
                    if pre_dispatch
                    else ExternalSubmissionState.SUBMITTED
                ),
                output_hash=None,
                error_code=failure.error_code,
                external_request_id=request.external_request_id,
                runtime_seconds=_runtime_seconds(started, self._clock),
                cost_minor_units=0,
                external_calls=0 if pre_dispatch else 1,
            )
        except CaseAgentLawyerAnalysisAdapterBlocked as failure:
            error_code = getattr(failure, "error_code", None)
            external_request_id = getattr(failure, "external_request_id", None)
            if (
                error_code not in LAWYER_ANALYSIS_UNKNOWN_ERROR_CODES
                or external_request_id != request.external_request_id
            ):
                raise
            return TaskAdapterOutcome(
                status=ResultStatus.UNKNOWN,
                external_submission_state=ExternalSubmissionState.UNKNOWN,
                output_hash=None,
                error_code=error_code,
                external_request_id=external_request_id,
                runtime_seconds=_runtime_seconds(started, self._clock),
                cost_minor_units=0,
                external_calls=1,
            )
        return self._compile_and_stage(
            context=context,
            projection=projection,
            contract=contract,
            request=request,
            response=response,
            started=started,
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._clock()
        self._validate_task_capability(context)
        binding = LawyerAnalysisRecoveryBinding(
            run_id=context.claim.run_id,
            task_id=context.claim.task_id,
            attempt_id=context.claim.attempt_id,
            task_input_hash=context.task.input_hash,
            input_refs=context.task.input_refs,
            external_request_id=context.external_request_id or "",
        )
        binding.validate()
        if (
            not context.claim.reconciliation
            or context.external_request_id != binding.external_request_id
        ):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis reconciliation binding differs"
            )
        # Reconciliation is intentionally independent of current prompt
        # construction.  The exchange reads the original immutable request
        # hash from the durable external-submission row; no regenerated body
        # is allowed to choose a lookup target.
        recovered = self._exchange.recover_bound(binding=binding)
        if not isinstance(recovered, RecoveredLawyerAnalysis):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis recovery result is invalid"
            )
        recovered.validate()
        if recovered.status == "UNRESOLVED":
            raise CaseAgentReconciliationUnavailable(
                "LAWYER_ANALYSIS_RECOVERY_UNRESOLVED"
            )
        if recovered.status == "FAILED":
            return TaskAdapterOutcome(
                status=ResultStatus.FAILED,
                external_submission_state=ExternalSubmissionState.SUBMITTED,
                output_hash=None,
                error_code=recovered.error_code,
                external_request_id=binding.external_request_id,
                runtime_seconds=_runtime_seconds(started, self._clock),
                cost_minor_units=0,
                external_calls=1,
            )
        projection, contract, request = self._prepare(context)
        if (
            recovered.request_hash is None
            or request.external_request_id != binding.external_request_id
            or request.request_hash != recovered.request_hash
        ):
            # A provider response may be real, but it cannot be interpreted
            # under a newly rebuilt request contract.  Preserve the raw
            # archive and put the run behind an explicit lawyer/operator
            # recovery gate rather than recording a fabricated receipt.
            raise CaseAgentReconciliationUnavailable(
                "LAWYER_ANALYSIS_RECOVERY_RECONSTRUCTION_DRIFT"
            )
        assert recovered.response_body is not None
        return self._compile_and_stage(
            context=context,
            projection=projection,
            contract=contract,
            request=request,
            response=recovered.response_body,
            started=started,
        )

    def _prepare(self, context: TaskExecutionContext):
        self._validate_task_capability(context)
        claim = context.claim
        task = context.task
        projection = self._projection.project_case_context(
            run_id=claim.run_id,
            task_id=claim.task_id,
            task_input_hash=task.input_hash,
            input_refs=task.input_refs,
        )
        if not isinstance(projection, BoundCaseContextProjection):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis projection is invalid"
            )
        projection.validate()
        if (
            projection.run_id != claim.run_id
            or projection.task_id != claim.task_id
            or projection.task_input_hash != task.input_hash
            or projection.input_refs != task.input_refs
        ):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis projection differs from the compiled task"
            )
        try:
            prepare = prepare_lawyer_analysis_request
            # A first substantive review can legitimately have confirmed facts
            # but no lawyer-confirmed dispute issue yet.  The legacy contract
            # turns that gap into a generic fact anchor, so the browser gets
            # no concrete question to confirm and the case never advances.
            # Use the source-bound discovery contract in that narrow state.
            # It still only proposes issues and never promotes a fact, claim,
            # rule, amount, or legal position.
            has_formal_issue = any(
                source.source_type.value == "DISPUTE_ISSUE"
                for source in projection.sources
            )
            has_unconfirmed_ledger_item = any(
                source.source_type.value in {"FACT_CANDIDATE", "TRANSACTION_CANDIDATE"}
                for source in projection.sources
            )
            if has_unconfirmed_ledger_item or not has_formal_issue:
                from .case_agent_discovered_analysis import prepare_discovered_analysis_request
                prepare = prepare_discovered_analysis_request
            contract, request = prepare(
                projection=projection,
                task_id=claim.task_id,
                attempt_id=claim.attempt_id,
                endpoint_host=self._exchange.endpoint_host,
            )
        except LawyerAnalysisBlocked as error:
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis request cannot be prepared"
            ) from error
        if request.worst_case_cost_minor_units > task.budget.max_cost_minor_units:
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "lawyer analysis request exceeds the compiled cost cap"
            )
        check_repair = getattr(self._projection, "validate_request_repair_binding", None)
        if check_repair is not None:
            check_repair(request)
        return projection, contract, request

    def _validate_task_capability(self, context: TaskExecutionContext) -> None:
        claim = context.claim
        task = context.task
        if (
            task.skill.skill_id != LAWYER_ANALYSIS_SKILL_ID
            or task.skill.tool_id != LAWYER_ANALYSIS_TOOL_ID
            or task.skill.adapter_id != self.manifest.adapter_id
            or task.skill.adapter_version != self.manifest.adapter_version
            or task.capability.network_policy is not NetworkPolicy.EXACT_ALLOWLIST
            or task.capability.allowed_domains != (self._exchange.endpoint_host,)
            or task.budget.max_attempts != 1
            or task.budget.max_external_calls != 1
            or task.budget.max_cost_minor_units
            != LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS
            or not task.input_refs
            or task.input_refs != context.input_refs
        ):
            raise CaseAgentLawyerAnalysisAdapterBlocked(
                "compiled lawyer analysis capability is not exact"
            )

    def _compile_and_stage(
        self,
        *,
        context: TaskExecutionContext,
        projection: BoundCaseContextProjection,
        contract: LawyerAnalysisContract,
        request: PreparedLawyerAnalysisRequest,
        response: bytes,
        started: float,
    ) -> TaskAdapterOutcome:
        try:
            parse = parse_lawyer_analysis_provider_response
            compile_candidate = compile_lawyer_decision_package_candidate
            from .case_agent_discovered_analysis import DiscoveredAnalysisContract
            if isinstance(contract, DiscoveredAnalysisContract):
                from .case_agent_discovered_analysis import parse_discovered_analysis_provider_response
                from .case_agent_discovered_candidate import compile_discovered_candidate
                parse, compile_candidate = parse_discovered_analysis_provider_response, compile_discovered_candidate
            parsed = parse(
                response, contract=contract
            )
            payload = compile_candidate(
                projection=projection,
                contract=contract,
                parsed=parsed,
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
            )
        except LawyerAnalysisBlocked as error:
            _ = error
            return _failed_after_submission(
                request=request,
                started=started,
                clock=self._clock,
                error_code="LAWYER_ANALYSIS_OUTPUT_REJECTED",
                cost_minor_units=(
                    known_lawyer_analysis_response_cost_minor_units(response) or 0
                ),
            )
        if (
            parsed.cost_minor_units > context.task.budget.max_cost_minor_units
            or len(payload) > context.task.budget.max_output_bytes
        ):
            return _failed_after_submission(
                request=request,
                started=started,
                clock=self._clock,
                error_code="LAWYER_ANALYSIS_OUTPUT_BUDGET_EXCEEDED",
                cost_minor_units=parsed.cost_minor_units,
            )
        content_hash = sha256(payload).hexdigest()
        staging = ReviewCandidateStagingRequest(
            schema_version=STAGING_REQUEST_SCHEMA,
            idempotency_key=sha256(
                (
                    context.claim.run_id
                    + context.claim.task_id
                    + request.external_request_id
                    + content_hash
                ).encode("utf-8")
            ).hexdigest(),
            run_id=context.claim.run_id,
            task_id=context.claim.task_id,
            task_input_hash=context.task.input_hash,
            source_hash=request.source_hash,
            artifact_kind=LAWYER_DECISION_PACKAGE_ARTIFACT_KIND,
            media_type="application/json",
            content_sha256=content_hash,
            byte_size=len(payload),
            review_status=REVIEW_STATUS,
            payload=payload,
        )
        try:
            staging.validate()
            staged = self._staging.stage_review_candidate(staging)
            if not isinstance(staged, StagedReviewCandidate):
                raise CaseAgentLawyerAnalysisAdapterBlocked(
                    "lawyer analysis staging receipt is invalid"
                )
            staged.validate_against(staging)
            artifact = ArtifactReceipt(
                artifact_id=staged.artifact_id,
                artifact_kind=staged.artifact_kind,
                content_hash=staged.content_sha256,
                byte_size=staged.byte_size,
                source_input_hash=context.task.input_hash,
                managed_derivative=False,
            )
            artifact.validate()
        except Exception as error:
            _ = error
            return _failed_after_submission(
                request=request,
                started=started,
                clock=self._clock,
                error_code="LAWYER_ANALYSIS_STAGING_FAILED",
                cost_minor_units=parsed.cost_minor_units,
            )
        return TaskAdapterOutcome(
            status=ResultStatus.SUCCEEDED,
            external_submission_state=ExternalSubmissionState.SUBMITTED,
            output_hash=sha256(
                (
                    staged.receipt_hash
                    + request.external_request_id
                    + request.request_hash
                ).encode("utf-8")
            ).hexdigest(),
            error_code=None,
            external_request_id=request.external_request_id,
            runtime_seconds=_runtime_seconds(started, self._clock),
            cost_minor_units=parsed.cost_minor_units,
            external_calls=1,
            artifacts=(artifact,),
        )


def _failed_after_submission(
    *,
    request: PreparedLawyerAnalysisRequest,
    started: float,
    clock: Callable[[], float],
    error_code: str,
    cost_minor_units: int = 0,
) -> TaskAdapterOutcome:
    return TaskAdapterOutcome(
        status=ResultStatus.FAILED,
        external_submission_state=ExternalSubmissionState.SUBMITTED,
        output_hash=None,
        error_code=error_code,
        external_request_id=request.external_request_id,
        runtime_seconds=_runtime_seconds(started, clock),
        cost_minor_units=cost_minor_units,
        external_calls=1,
    )


def _runtime_seconds(started: float, clock: Callable[[], float]) -> int:
    elapsed = clock() - started
    if not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise CaseAgentLawyerAnalysisAdapterBlocked(
            "lawyer analysis runtime clock is invalid"
        )
    return int(elapsed) + (1 if elapsed % 1 else 0)


__all__ = (
    "CaseAgentLawyerAnalysisAdapterBlocked",
    "LAWYER_ANALYSIS_BINDING_REJECTED",
    "LAWYER_ANALYSIS_BINDING_UNAVAILABLE",
    "LAWYER_ANALYSIS_CONNECT_FAILED",
    "LAWYER_ANALYSIS_DNS_FAILED",
    "LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED",
    "LAWYER_ANALYSIS_PRE_DISPATCH_FAILURES",
    "LAWYER_ANALYSIS_SKILL_ID",
    "LAWYER_ANALYSIS_TOOL_ID",
    "LawyerAnalysisRecoveryBinding",
    "LAWYER_ANALYSIS_UNKNOWN_ERROR_CODES",
    "LawyerAnalysisKnownFailure",
    "LawyerAnalysisProjectionPort",
    "QWEN_LAWYER_ANALYSIS_MANIFEST",
    "QwenLawyerAnalysisTaskAdapter",
    "RecoverableLawyerAnalysisExchange",
    "RecoveredLawyerAnalysis",
)
