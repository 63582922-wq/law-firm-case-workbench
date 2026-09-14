"""Bounded read-only Skill adapters for the lawyer case Agent.

The planner compiles opaque, server-owned ``input_refs``.  These adapters do
not turn those references into paths, URLs, commands or arbitrary payloads.
Instead, an injected projection/materialization port resolves the exact
immutable objects for the current run and task.  Parser/model output is then
written only to an injected review-candidate staging port and returned as an
``ArtifactReceipt``.  It is never written to a formal fact, transaction or
legal-decision ledger.

No production persistence implementation lives here.  A deployment must bind
the ports to its authorised case store, private object store and encrypted
candidate store.  The adapters therefore cannot make a missing database or
object-store path look successful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import re
from time import monotonic
from typing import Callable, Protocol
from uuid import UUID, uuid5

from .case_agent_supervisor import (
    AdapterExecutionMode,
    ArtifactReceipt,
    ExternalSubmissionState,
    ResultStatus,
    RuntimeAdapterManifest,
)
from .case_agent_worker import TaskAdapterOutcome, TaskExecutionContext
from .common_document_reader import (
    CommonDocumentFormat,
    CommonDocumentReadResult,
    DocumentReadBudget,
    MaterializedDocumentSource,
    read_materialized_common_document,
)
from .models import Actor, Role
from .visual_page_understanding import (
    VisualPageCandidate,
    VisualPageProjection,
    VisualPageProvider,
    VisualSourceKind,
    build_visual_page_projection,
    parse_visual_page_candidate,
    visual_page_request_hash,
)
from .web_agent_evidence_projection import WebAgentEvidenceProjectionSource
from .web_agent_material_review import AgentEvidencePageProjection


class CaseAgentSkillAdapterBlocked(RuntimeError):
    """A compiled task cannot cross one of the read-only adapter boundaries."""


REVIEW_STATUS = "NEEDS_LAWYER_REVIEW"
COMMON_DOCUMENT_CANDIDATE_SCHEMA = "agent-common-document-candidate-v1"
PDF_TEXT_CANDIDATE_SCHEMA = "agent-pdf-text-candidate-v1"
VISUAL_CANDIDATE_SCHEMA = "agent-visual-page-candidate-bundle-v1"
STAGING_REQUEST_SCHEMA = "agent-review-candidate-staging-v1"


def _policy_hash(policy_id: str, rules: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema_version": "case-agent-read-adapter-policy-v1",
            "policy_id": policy_id,
            "rules": rules,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


_READ_ONLY_RULES = (
    "compiled-input-refs-only",
    "server-owned-projection-or-materialization",
    "network-denied",
    "no-browser-path-url-or-command",
    "review-candidate-staging-only",
    "no-formal-ledger-write",
    "bounded-output-and-runtime",
)

COMMON_DOCUMENT_READER_MANIFEST = RuntimeAdapterManifest(
    tool_id="parse_office_document",
    adapter_id="common-document-review-reader",
    adapter_version="1.0.0",
    execution_mode=AdapterExecutionMode.IN_PROCESS,
    supports_idempotency=True,
    supports_reconciliation=False,
    network_capable=False,
    sandbox_policy_version="1.0.0",
    sandbox_policy_hash=_policy_hash(
        "common-document-review-reader-v1",
        _READ_ONLY_RULES
        + (
            "active-content-blocked",
            "literal-text-only",
            "source-hash-reverified",
        ),
    ),
)

PDF_TEXT_READER_MANIFEST = RuntimeAdapterManifest(
    tool_id="extract_pdf_text",
    adapter_id="registered-pdf-page-review-reader",
    adapter_version="1.0.0",
    execution_mode=AdapterExecutionMode.IN_PROCESS,
    supports_idempotency=True,
    supports_reconciliation=False,
    network_capable=False,
    sandbox_policy_version="1.0.0",
    sandbox_policy_hash=_policy_hash(
        "registered-pdf-page-review-reader-v1",
        _READ_ONLY_RULES
        + (
            "registered-pages-only",
            "static-pdf-text-only",
            "private-materialization-erased",
        ),
    ),
)


@dataclass(frozen=True)
class BoundCommonDocument:
    """One server-resolved immutable source; its private path is never input."""

    input_ref: str
    source: MaterializedDocumentSource = field(repr=False, compare=False)


class CommonDocumentInputPort(Protocol):
    """Open an auto-cleaned private materialization for exact task refs."""

    def open_common_documents(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> "CommonDocumentMaterializationLease": ...


class CommonDocumentMaterializationLease(Protocol):
    """The server port must erase its private sources on context exit."""

    def __enter__(self) -> tuple[BoundCommonDocument, ...]: ...

    def __exit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> bool | None: ...


class PdfPageProjectionPort(Protocol):
    """Return only registered, re-authorised PDF page projections."""

    def project_pdf_pages(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[AgentEvidencePageProjection, ...]: ...


@dataclass(frozen=True)
class EvidenceProjectionAuthorization:
    """A server-issued binding used to bridge the existing Web projection."""

    run_id: str
    task_id: str
    task_input_hash: str
    input_refs: tuple[str, ...]
    matter_id: str
    authorized_actor: Actor
    evidence_page_ids: tuple[str, ...]
    binding_hash: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
        matter_id: str,
        authorized_actor: Actor,
        evidence_page_ids: tuple[str, ...],
    ) -> "EvidenceProjectionAuthorization":
        payload = _evidence_authorization_payload(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            matter_id=matter_id,
            actor=authorized_actor,
            evidence_page_ids=evidence_page_ids,
        )
        return cls(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
            matter_id=matter_id,
            authorized_actor=authorized_actor,
            evidence_page_ids=evidence_page_ids,
            binding_hash=_canonical_hash(payload),
        )

    def validate(self) -> None:
        _uuid(self.run_id, "authorization run_id")
        _uuid(self.task_id, "authorization task_id")
        _sha256(self.task_input_hash, "authorization task_input_hash")
        _uuid(self.matter_id, "authorization matter_id")
        _validate_input_refs(self.input_refs)
        if len(self.evidence_page_ids) != len(self.input_refs):
            raise CaseAgentSkillAdapterBlocked(
                "evidence projection must bind every compiled input ref exactly once"
            )
        for page_id in self.evidence_page_ids:
            _uuid(page_id, "authorization evidence_page_id")
        if len(set(self.evidence_page_ids)) != len(self.evidence_page_ids):
            raise CaseAgentSkillAdapterBlocked("evidence projection page ids are duplicated")
        actor = self.authorized_actor
        if (
            not isinstance(actor, Actor)
            or Role.SYSTEM_WORKER in actor.roles
            or not actor.roles.intersection(
                {
                    Role.ASSISTANT,
                    Role.COLLABORATING_LAWYER,
                    Role.LEAD_LAWYER,
                    Role.REVIEWER,
                }
            )
        ):
            raise CaseAgentSkillAdapterBlocked(
                "evidence projection requires an authorised human case reader"
            )
        _uuid(actor.actor_id, "authorization actor_id")
        _uuid(actor.firm_id, "authorization firm_id")
        _sha256(self.binding_hash, "authorization binding_hash")
        expected = _canonical_hash(
            _evidence_authorization_payload(
                run_id=self.run_id,
                task_id=self.task_id,
                task_input_hash=self.task_input_hash,
                input_refs=self.input_refs,
                matter_id=self.matter_id,
                actor=actor,
                evidence_page_ids=self.evidence_page_ids,
            )
        )
        if self.binding_hash != expected:
            raise CaseAgentSkillAdapterBlocked("evidence projection binding hash differs")


class EvidenceProjectionAuthorizationPort(Protocol):
    def resolve_evidence_projection(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> EvidenceProjectionAuthorization: ...


class WebEvidencePageTaskProjectionPort:
    """Bridge task refs to :class:`WebAgentEvidenceProjectionSource` safely.

    The binding port is server owned.  A browser cannot provide the actor,
    matter id or evidence page ids used here.
    """

    def __init__(
        self,
        *,
        authorization_port: EvidenceProjectionAuthorizationPort,
        projection_source: WebAgentEvidenceProjectionSource,
    ) -> None:
        if not callable(getattr(authorization_port, "resolve_evidence_projection", None)):
            raise ValueError("evidence projection authorization port is invalid")
        if not callable(getattr(projection_source, "load_pages", None)):
            raise ValueError("Web evidence projection source is invalid")
        self._authorization_port = authorization_port
        self._projection_source = projection_source

    def __repr__(self) -> str:
        return "WebEvidencePageTaskProjectionPort(<server-authorized>)"

    def project_pdf_pages(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[AgentEvidencePageProjection, ...]:
        binding = self._authorization_port.resolve_evidence_projection(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
        )
        if not isinstance(binding, EvidenceProjectionAuthorization):
            raise CaseAgentSkillAdapterBlocked("evidence projection binding is invalid")
        binding.validate()
        if (
            binding.run_id != run_id
            or binding.task_id != task_id
            or binding.task_input_hash != task_input_hash
            or binding.input_refs != input_refs
        ):
            raise CaseAgentSkillAdapterBlocked(
                "evidence projection binding differs from the compiled task"
            )
        pages = self._projection_source.load_pages(
            actor=binding.authorized_actor,
            matter_id=binding.matter_id,
            evidence_page_ids=binding.evidence_page_ids,
        )
        _validate_pdf_projections(pages, expected_page_ids=binding.evidence_page_ids)
        return pages


@dataclass(frozen=True)
class ServerBoundVisualSource:
    """Decoded-content input returned by a server-owned materialization port."""

    input_ref: str
    matter_id: str
    evidence_page_id: str
    page_number: int
    source_kind: VisualSourceKind
    source_file_sha256: str
    source_page_sha256: str
    source_media_type: str
    source_bytes: bytes = field(repr=False, compare=False)


class VisualSourceBindingPort(Protocol):
    def resolve_visual_sources(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[ServerBoundVisualSource, ...]: ...


class VisualPageProjectionPort(Protocol):
    def project_visual_pages(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[VisualPageProjection, ...]: ...


class DeterministicVisualPageProjectionPort:
    """Normalize server-bound image bytes with the existing visual kernel."""

    def __init__(self, source_port: VisualSourceBindingPort) -> None:
        if not callable(getattr(source_port, "resolve_visual_sources", None)):
            raise ValueError("visual source binding port is invalid")
        self._source_port = source_port

    def __repr__(self) -> str:
        return "DeterministicVisualPageProjectionPort(<server-bound>)"

    def project_visual_pages(
        self,
        *,
        run_id: str,
        task_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> tuple[VisualPageProjection, ...]:
        sources = self._source_port.resolve_visual_sources(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=task_input_hash,
            input_refs=input_refs,
        )
        if not isinstance(sources, tuple) or tuple(item.input_ref for item in sources) != input_refs:
            raise CaseAgentSkillAdapterBlocked(
                "visual sources differ from the exact compiled input refs"
            )
        pages: list[VisualPageProjection] = []
        for source in sources:
            if not isinstance(source, ServerBoundVisualSource):
                raise CaseAgentSkillAdapterBlocked("visual source binding is invalid")
            pages.append(
                build_visual_page_projection(
                    matter_id=source.matter_id,
                    evidence_page_id=source.evidence_page_id,
                    page_number=source.page_number,
                    source_kind=source.source_kind,
                    source_file_sha256=source.source_file_sha256,
                    source_page_sha256=source.source_page_sha256,
                    source_media_type=source.source_media_type,
                    source_bytes=source.source_bytes,
                )
            )
        return tuple(pages)


@dataclass(frozen=True)
class ReviewCandidateStagingRequest:
    schema_version: str
    idempotency_key: str
    run_id: str
    task_id: str
    task_input_hash: str
    source_hash: str
    artifact_kind: str
    media_type: str
    content_sha256: str
    byte_size: int
    review_status: str
    payload: bytes = field(repr=False, compare=False)

    def validate(self) -> None:
        if self.schema_version != STAGING_REQUEST_SCHEMA:
            raise CaseAgentSkillAdapterBlocked("candidate staging schema is unsupported")
        _sha256(self.idempotency_key, "candidate idempotency_key")
        _uuid(self.run_id, "candidate run_id")
        _uuid(self.task_id, "candidate task_id")
        _sha256(self.task_input_hash, "candidate task_input_hash")
        _sha256(self.source_hash, "candidate source_hash")
        _code(self.artifact_kind, "candidate artifact_kind")
        if self.media_type != "application/json":
            raise CaseAgentSkillAdapterBlocked("candidate artifact must be canonical JSON")
        _sha256(self.content_sha256, "candidate content_sha256")
        if not isinstance(self.payload, bytes) or len(self.payload) != self.byte_size or self.byte_size < 2:
            raise CaseAgentSkillAdapterBlocked("candidate payload size differs")
        if sha256(self.payload).hexdigest() != self.content_sha256:
            raise CaseAgentSkillAdapterBlocked("candidate payload hash differs")
        if self.review_status != REVIEW_STATUS:
            raise CaseAgentSkillAdapterBlocked("candidate must await lawyer review")


@dataclass(frozen=True)
class StagedReviewCandidate:
    artifact_id: str
    idempotency_key: str
    artifact_kind: str
    content_sha256: str
    byte_size: int
    source_hash: str
    task_input_hash: str
    review_status: str
    receipt_hash: str

    @classmethod
    def build(
        cls, request: ReviewCandidateStagingRequest, *, artifact_id: str
    ) -> "StagedReviewCandidate":
        request.validate()
        _uuid(artifact_id, "staged artifact_id")
        expected_artifact_id = str(
            uuid5(UUID(request.task_id), request.idempotency_key)
        )
        if artifact_id != expected_artifact_id:
            raise CaseAgentSkillAdapterBlocked(
                "candidate artifact id differs from its deterministic idempotency binding"
            )
        payload = {
            "schema_version": "agent-review-candidate-staged-receipt-v1",
            "artifact_id": artifact_id,
            "idempotency_key": request.idempotency_key,
            "artifact_kind": request.artifact_kind,
            "content_sha256": request.content_sha256,
            "byte_size": request.byte_size,
            "source_hash": request.source_hash,
            "task_input_hash": request.task_input_hash,
            "review_status": request.review_status,
        }
        return cls(**{key: value for key, value in payload.items() if key != "schema_version"}, receipt_hash=_canonical_hash(payload))

    def validate_against(self, request: ReviewCandidateStagingRequest) -> None:
        _uuid(self.artifact_id, "staged artifact_id")
        _sha256(self.receipt_hash, "staged receipt_hash")
        expected = StagedReviewCandidate.build(request, artifact_id=self.artifact_id)
        if self != expected:
            raise CaseAgentSkillAdapterBlocked(
                "candidate staging receipt differs from the exact request"
            )


class ReviewCandidateStagingPort(Protocol):
    """Persist encrypted review-only output with durable idempotency."""

    def stage_review_candidate(
        self, request: ReviewCandidateStagingRequest
    ) -> StagedReviewCandidate: ...


class CommonDocumentTaskAdapter:
    manifest = COMMON_DOCUMENT_READER_MANIFEST

    def __init__(
        self,
        *,
        input_port: CommonDocumentInputPort,
        staging_port: ReviewCandidateStagingPort,
        read_budget: DocumentReadBudget | None = None,
        max_documents: int = 100,
        max_total_source_bytes: int = 512 * 1024 * 1024,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(input_port, "open_common_documents", None)):
            raise ValueError("common-document input port is invalid")
        _validate_staging_port(staging_port)
        self._input_port = input_port
        self._staging_port = staging_port
        self._read_budget = read_budget or DocumentReadBudget()
        self._read_budget.validate()
        if not 1 <= max_documents <= 100 or not 1 <= max_total_source_bytes <= 2 * 1024**3:
            raise ValueError("common-document adapter budget is invalid")
        self._max_documents = max_documents
        self._max_total_source_bytes = max_total_source_bytes
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "CommonDocumentTaskAdapter(<server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        run_id, task_id, input_hash, input_refs, guard = _task_binding(context, self._clock)
        if len(input_refs) > self._max_documents:
            raise CaseAgentSkillAdapterBlocked("common-document task exceeds the document limit")
        lease = self._input_port.open_common_documents(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            input_refs=input_refs,
        )
        if not callable(getattr(lease, "__enter__", None)) or not callable(
            getattr(lease, "__exit__", None)
        ):
            raise CaseAgentSkillAdapterBlocked(
                "common-document materialization lacks an auto-cleanup lease"
            )
        total_source_bytes = 0
        results: list[tuple[str, CommonDocumentReadResult]] = []
        with lease as sources:
            if not isinstance(sources, tuple) or tuple(item.input_ref for item in sources) != input_refs:
                raise CaseAgentSkillAdapterBlocked(
                    "common-document materialization differs from the compiled input refs"
                )
            for item in sources:
                guard.check()
                if not isinstance(item, BoundCommonDocument):
                    raise CaseAgentSkillAdapterBlocked("common-document binding is invalid")
                if item.source.admitted_format not in {
                    CommonDocumentFormat.DOCX,
                    CommonDocumentFormat.XLSX,
                }:
                    raise CaseAgentSkillAdapterBlocked(
                        "parse_office_document accepts only admitted DOCX or XLSX sources"
                    )
                total_source_bytes += item.source.byte_size
                if total_source_bytes > self._max_total_source_bytes:
                    raise CaseAgentSkillAdapterBlocked("common-document sources exceed the aggregate limit")
                results.append(
                    (
                        item.input_ref,
                        read_materialized_common_document(item.source, budget=self._read_budget),
                    )
                )
        if any(item.source.path.exists() or item.source.path.is_symlink() for item in sources):
            raise CaseAgentSkillAdapterBlocked(
                "common-document materialization was not erased on lease exit"
            )
        source_hash = _canonical_hash(
            {
                "schema_version": "agent-common-document-source-set-v1",
                "sources": [
                    {
                        "input_ref": input_ref,
                        "source_object_id": result.source_object_id,
                        "source_sha256": result.source_sha256,
                        "source_reference_hash": result.source_reference_hash,
                    }
                    for input_ref, result in results
                ],
            }
        )
        payload = _json_bytes(
            {
                "schema_version": COMMON_DOCUMENT_CANDIDATE_SCHEMA,
                "task_input_hash": input_hash,
                "source_hash": source_hash,
                **_review_only_declarations(),
                "documents": [
                    _common_document_payload(input_ref, result)
                    for input_ref, result in results
                ],
            }
        )
        guard.check_output(len(payload))
        guard.check()
        artifact = _stage_candidate(
            staging_port=self._staging_port,
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact_kind="COMMON_DOCUMENT_REVIEW_CANDIDATE",
            payload=payload,
        )
        guard.check()
        return _successful_outcome(
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact=artifact,
            runtime_seconds=guard.runtime_seconds(),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        raise CaseAgentSkillAdapterBlocked("local common-document reading is not reconcilable")


class PdfTextTaskAdapter:
    manifest = PDF_TEXT_READER_MANIFEST

    def __init__(
        self,
        *,
        projection_port: PdfPageProjectionPort,
        staging_port: ReviewCandidateStagingPort,
        max_pages: int = 50,
        max_total_text_bytes: int = 512 * 1024,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(projection_port, "project_pdf_pages", None)):
            raise ValueError("PDF page projection port is invalid")
        _validate_staging_port(staging_port)
        if not 1 <= max_pages <= 200 or not 1 <= max_total_text_bytes <= 8 * 1024 * 1024:
            raise ValueError("PDF text adapter budget is invalid")
        self._projection_port = projection_port
        self._staging_port = staging_port
        self._max_pages = max_pages
        self._max_total_text_bytes = max_total_text_bytes
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "PdfTextTaskAdapter(<server-ports>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        run_id, task_id, input_hash, input_refs, guard = _task_binding(context, self._clock)
        if len(input_refs) > self._max_pages:
            raise CaseAgentSkillAdapterBlocked("PDF task exceeds the page limit")
        pages = self._projection_port.project_pdf_pages(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            input_refs=input_refs,
        )
        _validate_pdf_projections(pages)
        if len(pages) != len(input_refs):
            raise CaseAgentSkillAdapterBlocked("PDF projection count differs from input refs")
        total_text_bytes = sum(len(item.extracted_text.encode("utf-8")) for item in pages)
        if total_text_bytes > self._max_total_text_bytes:
            raise CaseAgentSkillAdapterBlocked("PDF projected text exceeds the aggregate limit")
        guard.check()
        source_hash = _canonical_hash(
            {
                "schema_version": "agent-pdf-page-source-set-v1",
                "pages": [
                    {
                        "input_ref": input_ref,
                        "evidence_page_id": page.evidence_page_id,
                        "source_file_sha256": page.source_file_sha256,
                        "page_number": page.page_number,
                        "extracted_text_sha256": page.extracted_text_sha256,
                    }
                    for input_ref, page in zip(input_refs, pages, strict=True)
                ],
            }
        )
        payload = _json_bytes(
            {
                "schema_version": PDF_TEXT_CANDIDATE_SCHEMA,
                "task_input_hash": input_hash,
                "source_hash": source_hash,
                **_review_only_declarations(),
                "pages": [
                    {
                        "input_ref": input_ref,
                        "evidence_page_id": page.evidence_page_id,
                        "source_file_sha256": page.source_file_sha256,
                        "page_number": page.page_number,
                        "extracted_text": page.extracted_text,
                        "extracted_text_sha256": page.extracted_text_sha256,
                    }
                    for input_ref, page in zip(input_refs, pages, strict=True)
                ],
            }
        )
        guard.check_output(len(payload))
        guard.check()
        artifact = _stage_candidate(
            staging_port=self._staging_port,
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            payload=payload,
        )
        guard.check()
        return _successful_outcome(
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact=artifact,
            runtime_seconds=guard.runtime_seconds(),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        raise CaseAgentSkillAdapterBlocked("local PDF reading is not reconcilable")


class LocalVisualPageProviderPort(VisualPageProvider, Protocol):
    """A configured local provider; external HTTP providers do not satisfy it."""

    provider_id: str
    model_id: str
    provider_version: str
    network_capable: bool


class LocalVisualPageTaskAdapter:
    """Review-only visual/OCR adapter for a configured *local* provider.

    External Qwen or other hosted providers require the worker's durable
    external-submission ledger and a network-capable Tool.  They must not be
    passed to this local adapter.
    """

    def __init__(
        self,
        *,
        projection_port: VisualPageProjectionPort,
        provider: LocalVisualPageProviderPort,
        staging_port: ReviewCandidateStagingPort,
        max_pages: int = 20,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(projection_port, "project_visual_pages", None)):
            raise ValueError("visual page projection port is invalid")
        if not callable(getattr(provider, "analyze_page", None)):
            raise ValueError("visual provider is invalid")
        if getattr(provider, "network_capable", None) is not False:
            raise ValueError(
                "network visual providers require a durable external-submission adapter"
            )
        provider_id = _code_value(provider.provider_id, "visual provider_id")
        model_id = _code_value(provider.model_id, "visual model_id")
        provider_version = _semver_value(
            provider.provider_version, "visual provider_version"
        )
        _validate_staging_port(staging_port)
        if not 1 <= max_pages <= 100:
            raise ValueError("visual adapter page budget is invalid")
        self.manifest = RuntimeAdapterManifest(
            tool_id="understand_visual_page",
            adapter_id=f"local-visual-{provider_id}-{model_id}"[:200],
            adapter_version=provider_version,
            execution_mode=AdapterExecutionMode.IN_PROCESS,
            supports_idempotency=True,
            supports_reconciliation=False,
            network_capable=False,
            sandbox_policy_version="1.0.0",
            sandbox_policy_hash=_policy_hash(
                "local-visual-page-review-v1",
                _READ_ONLY_RULES
                + (
                    f"provider:{provider_id}",
                    f"model:{model_id}",
                    f"provider-version:{provider_version}",
                    "one-normalized-raster-per-call",
                    "no-authenticity-or-tamper-conclusion",
                ),
            ),
        )
        self.manifest.validate()
        self._projection_port = projection_port
        self._provider = provider
        self._staging_port = staging_port
        self._max_pages = max_pages
        self._clock = monotonic_clock

    def __repr__(self) -> str:
        return "LocalVisualPageTaskAdapter(<configured-local-provider>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        run_id, task_id, input_hash, input_refs, guard = _task_binding(context, self._clock)
        if len(input_refs) > self._max_pages:
            raise CaseAgentSkillAdapterBlocked("visual task exceeds the page limit")
        projections = self._projection_port.project_visual_pages(
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            input_refs=input_refs,
        )
        if not isinstance(projections, tuple) or len(projections) != len(input_refs):
            raise CaseAgentSkillAdapterBlocked("visual projections differ from input refs")
        candidates: list[VisualPageCandidate] = []
        for projection in projections:
            if not isinstance(projection, VisualPageProjection):
                raise CaseAgentSkillAdapterBlocked("visual projection is invalid")
            guard.check()
            request_id = str(
                uuid5(
                    UUID(context.claim.attempt_id),
                    _canonical_hash(
                        {
                            "task_input_hash": input_hash,
                            "projection_hash": projection.projection_hash,
                            "provider_id": self._provider.provider_id,
                            "model_id": self._provider.model_id,
                        }
                    ),
                )
            )
            raw = self._provider.analyze_page(
                projection=projection,
                external_request_id=request_id,
            )
            provider_request_ref_hash = _canonical_hash(
                {
                    "schema_version": "local-visual-provider-request-ref-v1",
                    "request_id": request_id,
                    "request_hash": visual_page_request_hash(projection),
                    "provider_id": self._provider.provider_id,
                    "model_id": self._provider.model_id,
                    "provider_version": self._provider.provider_version,
                }
            )
            candidates.append(
                parse_visual_page_candidate(
                    raw,
                    projection=projection,
                    expected_provider_id=self._provider.provider_id,
                    expected_model_id=self._provider.model_id,
                    provider_request_ref_hash=provider_request_ref_hash,
                )
            )
        source_hash = _canonical_hash(
            {
                "schema_version": "agent-visual-page-source-set-v1",
                "pages": [
                    {
                        "input_ref": input_ref,
                        "evidence_page_id": page.evidence_page_id,
                        "source_file_sha256": page.source_file_sha256,
                        "source_page_sha256": page.source_page_sha256,
                        "rendered_page_sha256": page.rendered_page_sha256,
                        "projection_hash": page.projection_hash,
                    }
                    for input_ref, page in zip(input_refs, projections, strict=True)
                ],
            }
        )
        payload = _json_bytes(
            {
                "schema_version": VISUAL_CANDIDATE_SCHEMA,
                "task_input_hash": input_hash,
                "source_hash": source_hash,
                **_review_only_declarations(),
                "provider": {
                    "provider_id": self._provider.provider_id,
                    "model_id": self._provider.model_id,
                    "provider_version": self._provider.provider_version,
                    "network_capable": False,
                },
                "pages": [
                    _visual_candidate_payload(input_ref, item)
                    for input_ref, item in zip(input_refs, candidates, strict=True)
                ],
            }
        )
        guard.check_output(len(payload))
        guard.check()
        artifact = _stage_candidate(
            staging_port=self._staging_port,
            run_id=run_id,
            task_id=task_id,
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact_kind="VISUAL_PAGE_REVIEW_CANDIDATE",
            payload=payload,
        )
        guard.check()
        return _successful_outcome(
            task_input_hash=input_hash,
            source_hash=source_hash,
            artifact=artifact,
            runtime_seconds=guard.runtime_seconds(),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        raise CaseAgentSkillAdapterBlocked("local visual understanding is not reconcilable")


def configured_local_visual_page_adapter(
    *,
    projection_port: VisualPageProjectionPort | None,
    provider: LocalVisualPageProviderPort | None,
    staging_port: ReviewCandidateStagingPort | None,
) -> LocalVisualPageTaskAdapter | None:
    """Return no adapter unless every local visual runtime dependency exists."""

    if projection_port is None or provider is None or staging_port is None:
        return None
    return LocalVisualPageTaskAdapter(
        projection_port=projection_port,
        provider=provider,
        staging_port=staging_port,
    )


class _TaskBudgetGuard:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_output_bytes: int,
        clock: Callable[[], float],
    ) -> None:
        if not 1 <= timeout_seconds <= 24 * 60 * 60:
            raise CaseAgentSkillAdapterBlocked("task timeout budget is invalid")
        if not 0 <= max_output_bytes <= 10 * 1024**3:
            raise CaseAgentSkillAdapterBlocked("task output budget is invalid")
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._clock = clock
        self._started = clock()
        if not isinstance(self._started, (int, float)) or not math.isfinite(self._started):
            raise CaseAgentSkillAdapterBlocked("monotonic clock is invalid")

    def check(self) -> None:
        elapsed = self._clock() - self._started
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed > self._timeout_seconds:
            raise CaseAgentSkillAdapterBlocked("task exceeded its compiled timeout budget")

    def check_output(self, byte_size: int) -> None:
        if not isinstance(byte_size, int) or byte_size < 0 or byte_size > self._max_output_bytes:
            raise CaseAgentSkillAdapterBlocked("candidate exceeds the compiled output budget")

    def runtime_seconds(self) -> int:
        elapsed = self._clock() - self._started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise CaseAgentSkillAdapterBlocked("monotonic runtime is invalid")
        return min(self._timeout_seconds, int(math.ceil(elapsed)))


def _task_binding(
    context: TaskExecutionContext, clock: Callable[[], float]
) -> tuple[str, str, str, tuple[str, ...], _TaskBudgetGuard]:
    claim = getattr(context, "claim", None)
    task = getattr(context, "task", None)
    input_refs = getattr(context, "input_refs", None)
    if claim is None or task is None:
        raise CaseAgentSkillAdapterBlocked("adapter requires a lease-bound task context")
    run_id = getattr(claim, "run_id", None)
    task_id = getattr(claim, "task_id", None)
    attempt_id = getattr(claim, "attempt_id", None)
    _uuid(run_id, "task run_id")
    _uuid(task_id, "task task_id")
    _uuid(attempt_id, "task attempt_id")
    if getattr(task, "task_id", None) != task_id:
        raise CaseAgentSkillAdapterBlocked("task context differs from its durable claim")
    input_hash = getattr(task, "input_hash", None)
    _sha256(input_hash, "task input_hash")
    if not isinstance(input_refs, tuple) or input_refs != getattr(task, "input_refs", None):
        raise CaseAgentSkillAdapterBlocked("task input refs differ from their compiled binding")
    _validate_input_refs(input_refs)
    budget = getattr(task, "budget", None)
    return (
        run_id,
        task_id,
        input_hash,
        input_refs,
        _TaskBudgetGuard(
            timeout_seconds=getattr(budget, "timeout_seconds", 0),
            max_output_bytes=getattr(budget, "max_output_bytes", -1),
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
    artifact_kind: str,
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
            "artifact_kind": artifact_kind,
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
        artifact_kind=artifact_kind,
        media_type="application/json",
        content_sha256=content_hash,
        byte_size=len(payload),
        review_status=REVIEW_STATUS,
        payload=payload,
    )
    request.validate()
    staged = staging_port.stage_review_candidate(request)
    if not isinstance(staged, StagedReviewCandidate):
        raise CaseAgentSkillAdapterBlocked("candidate staging returned an invalid receipt")
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
    output_hash = _canonical_hash(
        {
            "schema_version": "agent-read-adapter-output-v1",
            "task_input_hash": task_input_hash,
            "source_hash": source_hash,
            "staging_receipt_hash": artifact.receipt_hash,
            "artifact": {
                "artifact_id": receipt.artifact_id,
                "artifact_kind": receipt.artifact_kind,
                "content_hash": receipt.content_hash,
                "byte_size": receipt.byte_size,
                "managed_derivative": receipt.managed_derivative,
            },
            "review_status": REVIEW_STATUS,
        }
    )
    return TaskAdapterOutcome(
        status=ResultStatus.SUCCEEDED,
        external_submission_state=ExternalSubmissionState.NOT_APPLICABLE,
        output_hash=output_hash,
        error_code=None,
        external_request_id=None,
        runtime_seconds=runtime_seconds,
        cost_minor_units=0,
        external_calls=0,
        artifacts=(receipt,),
    )


def _common_document_payload(
    input_ref: str, result: CommonDocumentReadResult
) -> dict[str, object]:
    return {
        "input_ref": input_ref,
        "source_object_id": result.source_object_id,
        "source_sha256": result.source_sha256,
        "source_reference_hash": result.source_reference_hash,
        "detected_format": result.detected_format.value,
        "parser_version": result.parser_version,
        "document_risk_flags": result.document_risk_flags,
        "review_status": REVIEW_STATUS,
        "result_hash": result.result_hash,
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "kind": item.kind.value,
                "text": item.text,
                "content_hash": item.content_hash,
                "location": {
                    "container_part": item.location.container_part,
                    "section": item.location.section,
                    "ordinal": item.location.ordinal,
                    "page_or_slide": item.location.page_or_slide,
                    "row": item.location.row,
                    "column": item.location.column,
                    "coordinate": item.location.coordinate,
                    "line_start": item.location.line_start,
                    "line_end": item.location.line_end,
                },
                "risk_flags": item.risk_flags,
                "attributes": item.attributes,
                "literal_text_only": item.literal_text_only,
                "review_status": REVIEW_STATUS,
            }
            for item in result.candidates
        ],
    }


def _visual_candidate_payload(
    input_ref: str, candidate: VisualPageCandidate
) -> dict[str, object]:
    return {
        "input_ref": input_ref,
        "matter_id": candidate.matter_id,
        "evidence_page_id": candidate.evidence_page_id,
        "source_file_sha256": candidate.source_file_sha256,
        "source_page_sha256": candidate.source_page_sha256,
        "rendered_page_sha256": candidate.rendered_page_sha256,
        "projection_hash": candidate.projection_hash,
        "provider_id": candidate.provider_id,
        "model_id": candidate.model_id,
        "provider_request_ref_hash": candidate.provider_request_ref_hash,
        "candidate_hash": candidate.candidate_hash,
        "review_status": REVIEW_STATUS,
        "text_blocks": [
            {
                "block_id": item.block_id,
                "kind": item.kind.value,
                "text": item.text,
                "region": _region_payload(item.region),
                "confidence": item.confidence,
            }
            for item in candidate.text_blocks
        ],
        "tables": [
            {
                "table_id": item.table_id,
                "region": _region_payload(item.region),
                "row_count": item.row_count,
                "column_count": item.column_count,
                "cells": item.cells,
                "confidence": item.confidence,
            }
            for item in candidate.tables
        ],
        "fields": [
            {
                "field_id": item.field_id,
                "kind": item.kind.value,
                "value": item.value,
                "region": _region_payload(item.region),
                "confidence": item.confidence,
                "currency": item.currency,
            }
            for item in candidate.fields
        ],
        "quality_risks": [
            {
                "code": item.code.value,
                "severity": item.severity,
                "region": _region_payload(item.region) if item.region is not None else None,
                "confidence": item.confidence,
                "note": item.note,
            }
            for item in candidate.quality_risks
        ],
    }


def _region_payload(region) -> dict[str, float]:
    return {
        "x": region.x,
        "y": region.y,
        "width": region.width,
        "height": region.height,
    }


def _review_only_declarations() -> dict[str, object]:
    return {
        "review_status": REVIEW_STATUS,
        "formal_fact": False,
        "formal_transaction": False,
        "legal_conclusion": False,
        "evidence_decision": False,
    }


def _validate_pdf_projections(
    pages: object, *, expected_page_ids: tuple[str, ...] | None = None
) -> None:
    if not isinstance(pages, tuple) or not pages:
        raise CaseAgentSkillAdapterBlocked("PDF projection requires registered pages")
    seen: set[str] = set()
    for page in pages:
        if not isinstance(page, AgentEvidencePageProjection):
            raise CaseAgentSkillAdapterBlocked("PDF page projection is invalid")
        _uuid(page.evidence_page_id, "PDF evidence_page_id")
        _sha256(page.source_file_sha256, "PDF source_file_sha256")
        _sha256(page.extracted_text_sha256, "PDF extracted_text_sha256")
        if page.page_number < 1 or sha256(page.extracted_text.encode("utf-8")).hexdigest() != page.extracted_text_sha256:
            raise CaseAgentSkillAdapterBlocked("PDF page projection hash differs")
        if page.evidence_page_id in seen:
            raise CaseAgentSkillAdapterBlocked("PDF page projection is duplicated")
        seen.add(page.evidence_page_id)
    if expected_page_ids is not None and tuple(item.evidence_page_id for item in pages) != expected_page_ids:
        raise CaseAgentSkillAdapterBlocked("PDF projection differs from its authorized pages")


def _validate_input_refs(input_refs: tuple[str, ...]) -> None:
    if not 1 <= len(input_refs) <= 500:
        raise CaseAgentSkillAdapterBlocked("compiled task requires bounded input refs")
    if len(set(input_refs)) != len(input_refs):
        raise CaseAgentSkillAdapterBlocked("compiled input refs are duplicated")
    for value in input_refs:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is None:
            raise CaseAgentSkillAdapterBlocked("compiled input ref is invalid")
        lowered = value.lower()
        if lowered.startswith(
            ("http:", "https:", "file:", "path:", "cmd:", "shell:", "powershell:")
        ):
            raise CaseAgentSkillAdapterBlocked(
                "compiled input ref cannot be a URL, path or command"
            )


def _evidence_authorization_payload(
    *,
    run_id: str,
    task_id: str,
    task_input_hash: str,
    input_refs: tuple[str, ...],
    matter_id: str,
    actor: Actor,
    evidence_page_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": "agent-evidence-projection-authorization-v1",
        "run_id": run_id,
        "task_id": task_id,
        "task_input_hash": task_input_hash,
        "input_refs": input_refs,
        "matter_id": matter_id,
        "actor_id": actor.actor_id,
        "firm_id": actor.firm_id,
        "roles": sorted(role.value for role in actor.roles),
        "evidence_page_ids": evidence_page_ids,
    }


def _validate_staging_port(port: ReviewCandidateStagingPort) -> None:
    if not callable(getattr(port, "stage_review_candidate", None)):
        raise ValueError("review-candidate staging port is invalid")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseAgentSkillAdapterBlocked(f"{label} must be a UUID") from error


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CaseAgentSkillAdapterBlocked(f"{label} must be a SHA-256 digest")


def _code(value: object, label: str) -> None:
    try:
        _code_value(value, label)
    except ValueError as error:
        raise CaseAgentSkillAdapterBlocked(str(error)) from error


def _code_value(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{0,199}", value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _semver_value(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", value) is None:
        raise ValueError(f"{label} must be semantic")
    return value


__all__ = (
    "BoundCommonDocument",
    "COMMON_DOCUMENT_READER_MANIFEST",
    "CaseAgentSkillAdapterBlocked",
    "CommonDocumentInputPort",
    "CommonDocumentMaterializationLease",
    "CommonDocumentTaskAdapter",
    "DeterministicVisualPageProjectionPort",
    "EvidenceProjectionAuthorization",
    "EvidenceProjectionAuthorizationPort",
    "LocalVisualPageProviderPort",
    "LocalVisualPageTaskAdapter",
    "PDF_TEXT_READER_MANIFEST",
    "PdfPageProjectionPort",
    "PdfTextTaskAdapter",
    "REVIEW_STATUS",
    "ReviewCandidateStagingPort",
    "ReviewCandidateStagingRequest",
    "ServerBoundVisualSource",
    "StagedReviewCandidate",
    "VisualPageProjectionPort",
    "VisualSourceBindingPort",
    "WebEvidencePageTaskProjectionPort",
    "configured_local_visual_page_adapter",
)
