"""Unified Worker adapters for dynamic, review-only Office deliverables.

The adapter owns no database query, object key, template text, provider key or
LibreOffice command.  Those are supplied by server-only ports.  It verifies
the compiled task, asks the binding port for one current dynamic work-plan
projection, commits an external boundary before DeepSeek, and stages the
structured candidate together with editable Office bytes and its PDF preview
as one immutable package. The first-release case memo and payment ledger are
both deterministic projections of lawyer-confirmed sources; drafting models
cannot add facts, amounts, law or strategy to either deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from time import monotonic
from typing import Callable, Protocol
from uuid import UUID, uuid5

from .case_agent_document_delivery import (
    DynamicDocumentTaskBinding,
    ReviewableDocumentCandidate,
    ReviewableDocumentFormat,
    build_document_draft_request,
    build_deterministic_case_review_memo_candidate,
    build_deterministic_defence_statement_candidate,
    build_deterministic_evidence_catalogue_candidate,
    build_deterministic_payment_ledger_candidate,
    build_deterministic_supplementary_evidence_checklist_candidate,
    canonical_document_candidate_bytes,
    visible_document_source_labels,
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
from .deepseek_case_agent_planner import DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT
from .deepseek_document_drafting import (
    DeepSeekDocumentDraftNotSubmitted,
    DeepSeekDocumentDraftProvider,
    DeepSeekDocumentDraftRejected,
    DeepSeekDocumentDraftUnknownSubmission,
    PreparedDeepSeekDocumentRequest,
)
from .reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
    ReviewOfficeConverter,
    ReviewableOfficeDraft,
    create_reviewable_docx_draft,
    create_reviewable_xlsx_ledger,
)


DEEPSEEK_DOCUMENT_HOST = "api.deepseek.com"
DOCUMENT_CANDIDATE_ARTIFACT_KIND = "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"
DOCUMENT_EDITABLE_ARTIFACT_KIND = "REVIEWABLE_DOCUMENT_EDITABLE"
DOCUMENT_PDF_ARTIFACT_KIND = "REVIEWABLE_DOCUMENT_PDF_PREVIEW"


class CaseAgentDocumentAdapterBlocked(RuntimeError):
    """The document task is stale, unsupported or not recoverable."""


def _manifest(tool_id: str, output_format: ReviewableDocumentFormat) -> RuntimeAdapterManifest:
    deterministic_projection = (
        "deterministic-source-bound-case-risk-analysis"
        if output_format is ReviewableDocumentFormat.DOCX
        else "deterministic-confirmed-transaction-row-projection"
    )
    policy = {
        "schema_version": "case-agent-dynamic-document-adapter-policy-v1",
        "tool_id": tool_id,
        "output_format": output_format.value,
        "rules": (
            "active-dynamic-work-plan-item-only",
            "server-owned-template-and-authoritative-sources-only",
            deterministic_projection,
            "no-model-or-provider-call",
            "known-or-unknown-render-result-never-auto-retried",
            "strict-source-bound-review-candidate",
            "isolated-office-to-pdf-conversion",
            "immutable-candidate-office-pdf-package",
            "independent-package-verification-required",
            "no-formal-fact-legal-conclusion-or-court-release",
        ),
    }
    return RuntimeAdapterManifest(
        tool_id=tool_id,
        adapter_id=f"dynamic-reviewable-{output_format.value.casefold()}-delivery",
        adapter_version="1.0.0",
        execution_mode=AdapterExecutionMode.IN_PROCESS,
        supports_idempotency=True,
        supports_reconciliation=False,
        network_capable=False,
        sandbox_policy_version="1.0.0",
        sandbox_policy_hash=sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


DOCX_DOCUMENT_DELIVERY_MANIFEST = _manifest(
    "draft_reviewable_docx_package", ReviewableDocumentFormat.DOCX
)
XLSX_DOCUMENT_DELIVERY_MANIFEST = _manifest(
    "draft_reviewable_xlsx_package", ReviewableDocumentFormat.XLSX
)


class DynamicDocumentBindingPort(Protocol):
    """Read current case content and exact plan/item refs in one transaction."""

    def resolve_document_task(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
        expected_format: ReviewableDocumentFormat,
    ) -> DynamicDocumentTaskBinding: ...


@dataclass(frozen=True)
class StagedDocumentPackage:
    package_id: str
    receipt_hash: str
    artifact_receipts: tuple[ArtifactReceipt, ...]

    def validate(self, *, task_id: str, task_input_hash: str) -> None:
        _uuid(self.package_id, "document package_id")
        _sha256(self.receipt_hash, "document package receipt_hash")
        if len(self.artifact_receipts) != 3:
            raise CaseAgentDocumentAdapterBlocked("document package must contain three artifacts")
        kinds = {item.artifact_kind for item in self.artifact_receipts}
        if kinds != {
            DOCUMENT_CANDIDATE_ARTIFACT_KIND,
            DOCUMENT_EDITABLE_ARTIFACT_KIND,
            DOCUMENT_PDF_ARTIFACT_KIND,
        }:
            raise CaseAgentDocumentAdapterBlocked("document package artifact kinds differ")
        for item in self.artifact_receipts:
            item.validate()
            if item.source_input_hash != task_input_hash or not item.managed_derivative:
                raise CaseAgentDocumentAdapterBlocked("document artifact lineage differs")
            expected_id = str(uuid5(UUID(task_id), f"{self.package_id}:{item.artifact_kind}"))
            if item.artifact_id != expected_id:
                raise CaseAgentDocumentAdapterBlocked("document artifact id is not deterministic")


@dataclass(frozen=True)
class ReviewableDocumentPackageStaging:
    run_id: str
    task_id: str
    attempt_id: str
    task_input_hash: str
    binding: DynamicDocumentTaskBinding
    candidate: ReviewableDocumentCandidate
    candidate_content: bytes = field(repr=False, compare=False)
    generated: ReviewableOfficeDraft = field(repr=False, compare=False)


class ReviewableDocumentPackageStagingPort(Protocol):
    def stage_document_package(
        self, request: ReviewableDocumentPackageStaging
    ) -> StagedDocumentPackage: ...


@dataclass(frozen=True)
class RecoveredDocumentDraft:
    status: str
    candidate: ReviewableDocumentCandidate | None = None
    error_code: str | None = None

    def validate(self) -> None:
        if self.status == "SUCCEEDED":
            if not isinstance(self.candidate, ReviewableDocumentCandidate) or self.error_code is not None:
                raise CaseAgentDocumentAdapterBlocked("recovered document candidate is invalid")
        elif self.status == "FAILED":
            if self.candidate is not None or not _error_code(self.error_code):
                raise CaseAgentDocumentAdapterBlocked("recovered document failure is invalid")
        elif self.status == "UNRESOLVED":
            if self.candidate is not None or self.error_code is not None:
                raise CaseAgentDocumentAdapterBlocked("unresolved document result is invalid")
        else:
            raise CaseAgentDocumentAdapterBlocked("recovered document status is invalid")


class RecoverableDocumentDraftExchange(Protocol):
    """Durable provider exchange; recovery is lookup-only."""

    def send(
        self,
        *,
        prepared: PreparedDeepSeekDocumentRequest,
        binding: DynamicDocumentTaskBinding,
    ) -> ReviewableDocumentCandidate: ...

    def recover(
        self,
        *,
        external_request_id: str,
        request_hash: str,
        binding: DynamicDocumentTaskBinding,
    ) -> RecoveredDocumentDraft: ...


class DynamicDocumentTaskAdapter:
    def __init__(
        self,
        *,
        output_format: ReviewableDocumentFormat,
        binding_port: DynamicDocumentBindingPort,
        provider: DeepSeekDocumentDraftProvider,
        exchange: RecoverableDocumentDraftExchange,
        converter: ReviewOfficeConverter,
        staging_port: ReviewableDocumentPackageStagingPort,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if output_format not in {ReviewableDocumentFormat.DOCX, ReviewableDocumentFormat.XLSX}:
            raise ValueError("dynamic document output format is invalid")
        if not callable(getattr(binding_port, "resolve_document_task", None)):
            raise ValueError("dynamic document binding port is invalid")
        if not isinstance(provider, DeepSeekDocumentDraftProvider):
            raise ValueError("DeepSeek document provider is invalid")
        if not callable(getattr(exchange, "send", None)) or not callable(getattr(exchange, "recover", None)):
            raise ValueError("recoverable document exchange is invalid")
        if not callable(getattr(converter, "convert_generated_document", None)):
            raise ValueError("isolated Office converter is invalid")
        if not callable(getattr(staging_port, "stage_document_package", None)):
            raise ValueError("document package staging port is invalid")
        self.output_format = output_format
        self.manifest = (
            DOCX_DOCUMENT_DELIVERY_MANIFEST
            if output_format is ReviewableDocumentFormat.DOCX
            else XLSX_DOCUMENT_DELIVERY_MANIFEST
        )
        self._binding_port = binding_port
        self._provider = provider
        self._exchange = exchange
        self._converter = converter
        self._staging = staging_port
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return f"DynamicDocumentTaskAdapter(output_format={self.output_format.value!r}, <server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._clock()
        binding = self._resolve_binding(context)
        if binding.template.deliverable_kind == "CASE_REVIEW_MEMO":
            candidate = build_deterministic_case_review_memo_candidate(binding)
        elif binding.template.deliverable_kind == "SUPPLEMENTARY_EVIDENCE_CHECKLIST":
            candidate = build_deterministic_supplementary_evidence_checklist_candidate(binding)
        elif binding.template.deliverable_kind == "DEFENCE_STATEMENT":
            candidate = build_deterministic_defence_statement_candidate(binding)
        elif binding.template.deliverable_kind == "PAYMENT_LEDGER":
            candidate = build_deterministic_payment_ledger_candidate(binding)
        elif binding.template.deliverable_kind == "EVIDENCE_CATALOGUE":
            candidate = build_deterministic_evidence_catalogue_candidate(binding)
        else:
            raise CaseAgentDocumentAdapterBlocked(
                "compiled document deliverable has no deterministic compiler"
            )
        return self._generate_stage_success(
            context=context,
            binding=binding,
            candidate=candidate,
            external_request_id=None,
            external_submission_state=ExternalSubmissionState.NOT_APPLICABLE,
            external_calls=0,
            started=started,
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        raise CaseAgentDocumentAdapterBlocked(
            "deterministic document delivery has no external result to reconcile"
        )

    def _resolve_binding(
        self, context: TaskExecutionContext
    ) -> DynamicDocumentTaskBinding:
        claim = getattr(context, "claim", None)
        task = getattr(context, "task", None)
        if claim is None or task is None:
            raise CaseAgentDocumentAdapterBlocked("document task requires a durable context")
        if (
            task.skill.tool_id != self.manifest.tool_id
            or task.capability.execution_mode is not AdapterExecutionMode.IN_PROCESS
            or task.capability.network_policy is not NetworkPolicy.DENY
            or task.capability.allowed_domains
            or task.budget.max_external_calls != 0
            or not task.capability.writes_managed_derivatives
        ):
            raise CaseAgentDocumentAdapterBlocked("compiled document capability is not exact")
        binding = self._binding_port.resolve_document_task(
            run_id=claim.run_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            task_input_hash=task.input_hash,
            input_refs=task.input_refs,
            expected_format=self.output_format,
        )
        if not isinstance(binding, DynamicDocumentTaskBinding):
            raise CaseAgentDocumentAdapterBlocked("document binding is invalid")
        binding.validate()
        if (
            binding.run_id != claim.run_id
            or binding.task_id != claim.task_id
            or binding.task_input_hash != task.input_hash
            or binding.template.output_format is not self.output_format
        ):
            raise CaseAgentDocumentAdapterBlocked("document binding differs from the compiled task")
        return binding

    def _prepare(
        self, context: TaskExecutionContext
    ) -> tuple[DynamicDocumentTaskBinding, PreparedDeepSeekDocumentRequest, str]:
        if self.output_format is not ReviewableDocumentFormat.DOCX:
            raise CaseAgentDocumentAdapterBlocked(
                "deterministic payment ledger does not prepare a model request"
            )
        claim = context.claim
        binding = self._resolve_binding(context)
        request = build_document_draft_request(binding)
        prepared = self._provider.prepare(request=request, binding=binding)
        external_request_id = str(uuid5(UUID(claim.attempt_id), prepared.request_hash))
        return binding, prepared, external_request_id

    def _generate_stage_success(
        self,
        *,
        context: TaskExecutionContext,
        binding: DynamicDocumentTaskBinding,
        candidate: ReviewableDocumentCandidate,
        external_request_id: str | None,
        external_submission_state: ExternalSubmissionState,
        external_calls: int,
        started: float,
    ) -> TaskAdapterOutcome:
        if (
            not isinstance(candidate, ReviewableDocumentCandidate)
            or candidate.output_format is not self.output_format
            or candidate.binding_hash != binding.binding_hash
            or candidate.task_input_hash != context.task.input_hash
        ):
            raise CaseAgentDocumentAdapterBlocked("document candidate differs from the task binding")
        context.heartbeat()
        source_labels = visible_document_source_labels(binding)
        try:
            if self.output_format is ReviewableDocumentFormat.DOCX:
                generated = create_reviewable_docx_draft(
                    candidate.to_docx_input(source_labels), converter=self._converter
                )
            else:
                sheet, columns, rows = candidate.to_xlsx_input(source_labels)
                generated = create_reviewable_xlsx_ledger(
                    approval_hash=candidate.candidate_hash,
                    sheet_name=sheet,
                    columns=columns,
                    rows=rows,
                    converter=self._converter,
                )
        except ReviewOfficeConversionUnknown:
            # The renderer has no durable result lookup API.  Turning this
            # into the generic task UNKNOWN state would make the supervisor's
            # provider reconciliation path invoke the renderer a second time.
            # Keep the already-submitted provider call terminal and require a
            # new, explicit lawyer-controlled task instead.
            return self._failed(
                external_request_id=external_request_id,
                external_submission_state=external_submission_state,
                external_calls=external_calls,
                code="DOCUMENT_RENDERER_RESULT_UNKNOWN",
                started=started,
            )
        except ReviewOfficeConversionBlocked:
            return self._failed(
                external_request_id=external_request_id,
                external_submission_state=external_submission_state,
                external_calls=external_calls,
                code="DOCUMENT_RENDERER_REJECTED",
                started=started,
            )
        candidate_content = canonical_document_candidate_bytes(candidate)
        context.heartbeat()
        staged = self._staging.stage_document_package(
            ReviewableDocumentPackageStaging(
                run_id=context.claim.run_id,
                task_id=context.claim.task_id,
                attempt_id=context.claim.attempt_id,
                task_input_hash=context.task.input_hash,
                binding=binding,
                candidate=candidate,
                candidate_content=candidate_content,
                generated=generated,
            )
        )
        if not isinstance(staged, StagedDocumentPackage):
            raise CaseAgentDocumentAdapterBlocked("document package staging receipt is invalid")
        staged.validate(task_id=context.claim.task_id, task_input_hash=context.task.input_hash)
        output_hash = sha256(
            json.dumps(
                {
                    "schema_version": "case-agent-document-package-output-v1",
                    "package_id": staged.package_id,
                    "receipt_hash": staged.receipt_hash,
                    "artifacts": [
                        {
                            "artifact_id": item.artifact_id,
                            "artifact_kind": item.artifact_kind,
                            "content_hash": item.content_hash,
                            "byte_size": item.byte_size,
                        }
                        for item in sorted(staged.artifact_receipts, key=lambda value: value.artifact_id)
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return TaskAdapterOutcome(
            status=ResultStatus.SUCCEEDED,
            external_submission_state=external_submission_state,
            output_hash=output_hash,
            error_code=None,
            external_request_id=external_request_id,
            runtime_seconds=max(0, int(self._clock() - started)),
            cost_minor_units=0,
            external_calls=external_calls,
            artifacts=staged.artifact_receipts,
        )

    def _failed(
        self,
        *,
        external_request_id: str | None,
        code: str,
        started: float,
        external_submission_state: ExternalSubmissionState = ExternalSubmissionState.SUBMITTED,
        external_calls: int = 1,
    ) -> TaskAdapterOutcome:
        return TaskAdapterOutcome(
            status=ResultStatus.FAILED,
            external_submission_state=external_submission_state,
            output_hash=None,
            error_code=code,
            external_request_id=external_request_id,
            runtime_seconds=max(0, int(self._clock() - started)),
            cost_minor_units=0,
            external_calls=external_calls,
        )


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise CaseAgentDocumentAdapterBlocked(f"{label} is invalid") from None


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CaseAgentDocumentAdapterBlocked(f"{label} is invalid")


def _error_code(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and 3 <= len(value) <= 80
        and value[0].isalpha()
        and all(character.isupper() or character.isdigit() or character == "_" for character in value)
    )


__all__ = [
    "CaseAgentDocumentAdapterBlocked",
    "DOCX_DOCUMENT_DELIVERY_MANIFEST",
    "DOCUMENT_CANDIDATE_ARTIFACT_KIND",
    "DOCUMENT_EDITABLE_ARTIFACT_KIND",
    "DOCUMENT_PDF_ARTIFACT_KIND",
    "DynamicDocumentTaskAdapter",
    "RecoveredDocumentDraft",
    "ReviewableDocumentPackageStaging",
    "StagedDocumentPackage",
    "XLSX_DOCUMENT_DELIVERY_MANIFEST",
]
