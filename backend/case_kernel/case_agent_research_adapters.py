"""Network-capable, review-only public-research Skill for the case Agent.

The browser and the planner never supply a URL, provider, credential or raw
query.  One server-owned binding port resolves the exact current issue/work
plan references, keeps the confidential lawyer question private and emits only
minimized public terms.  The task context commits its durable external
submission boundary before the configured provider may send a network byte.

Search hits remain untrusted research leads.  They are staged as a canonical
JSON candidate and cannot become a legal source, rule or case conclusion until
the separate official-snapshot and lawyer-review ledgers accept them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Callable, Protocol
from uuid import UUID, uuid5

from .brave_public_search import (
    BRAVE_SEARCH_HOST,
    BRAVE_SEARCH_PROVIDER_ID,
    BRAVE_SEARCH_SERVICE_ID,
    BravePublicSearchProvider,
    BraveSearchTransportRequest,
    BraveSearchTransportResult,
    parse_brave_public_search_results,
)
from .case_agent_skill_adapters import (
    REVIEW_STATUS,
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
from .controlled_web_research import (
    AuthoritySourceRegistry,
    EgressReceiptRef,
    ExternalAttemptRef,
    ExternalOperationStatus,
    ExternalRequestRef,
    MinimizedPublicQuery,
    PublicSearchResultSet,
    ResearchPurpose,
    SearchProviderRegistration,
    SearchProviderRegistry,
    compile_public_search_provider_input,
    prepare_minimized_public_query,
    require_public_search_ready,
)


class CaseAgentResearchAdapterBlocked(RuntimeError):
    """The research task is stale, unsafe or not durably recoverable."""


PUBLIC_RESEARCH_CANDIDATE_SCHEMA = "agent-public-research-leads-candidate-v1"
PUBLIC_RESEARCH_ARTIFACT_KIND = "PUBLIC_RESEARCH_LEADS_CANDIDATE"
_MAX_BINDING_REFS = 100
_MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


def _policy_hash() -> str:
    return sha256(
        json.dumps(
            {
            "schema_version": "case-agent-public-research-adapter-policy-v1",
            "rules": (
                "server-owned-current-research-binding-only",
                "confidential-question-never-egresses",
                "minimized-public-terms-only",
                "exact-brave-host-and-service",
                "durable-boundary-before-network",
                "unknown-result-never-resubmitted",
                "public-hits-are-review-leads-not-law",
                "canonical-private-candidate-only",
                "official-snapshot-requires-separate-task-and-review",
            ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


PUBLIC_WEB_RESEARCH_MANIFEST = RuntimeAdapterManifest(
    tool_id="search_public_web",
    adapter_id="controlled-public-web-research",
    adapter_version="1.0.0",
    execution_mode=AdapterExecutionMode.NETWORK_CONNECTOR,
    supports_idempotency=True,
    supports_reconciliation=True,
    network_capable=True,
    sandbox_policy_version="1.0.0",
    sandbox_policy_hash=_policy_hash(),
)


@dataclass(frozen=True)
class AuthorizedPublicResearchBinding:
    """Exact server projection behind one compiled research task.

    Confidential fields are excluded from repr.  ``binding_hash`` covers their
    hashes, so a restart cannot silently substitute another question or
    private-term deny list.
    """

    run_id: str
    task_id: str
    attempt_id: str
    task_input_hash: str
    matter_id: str
    question_id: str
    input_refs: tuple[str, ...]
    confidential_question: str = field(repr=False, compare=False)
    proposed_public_terms: tuple[str, ...]
    private_terms: tuple[str, ...] = field(repr=False, compare=False)
    purpose: ResearchPurpose
    language: str
    max_results: int
    external_request: ExternalRequestRef
    binding_hash: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        matter_id: str,
        question_id: str,
        input_refs: tuple[str, ...],
        confidential_question: str,
        proposed_public_terms: tuple[str, ...],
        private_terms: tuple[str, ...],
        purpose: ResearchPurpose,
        language: str = "zh-CN",
        max_results: int = 20,
        external_request_id: str,
        egress_grant_id: str,
        expires_at: datetime,
    ) -> "AuthorizedPublicResearchBinding":
        query = _minimized_query(
            matter_id=matter_id,
            question_id=question_id,
            confidential_question=confidential_question,
            proposed_public_terms=proposed_public_terms,
            private_terms=private_terms,
            purpose=purpose,
        )
        source_binding_hash = _source_binding_hash(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            matter_id=matter_id,
            question_id=question_id,
            input_refs=input_refs,
            confidential_question=confidential_question,
            proposed_public_terms=proposed_public_terms,
            private_terms=private_terms,
            purpose=purpose,
            language=language,
            max_results=max_results,
        )
        external_request = _build_external_request(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            query=query,
            source_binding_hash=source_binding_hash,
            external_request_id=external_request_id,
            egress_grant_id=egress_grant_id,
            expires_at=expires_at,
        )
        payload = _binding_payload(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            task_input_hash=task_input_hash,
            matter_id=matter_id,
            question_id=question_id,
            input_refs=input_refs,
            confidential_question=confidential_question,
            proposed_public_terms=proposed_public_terms,
            private_terms=private_terms,
            purpose=purpose,
            language=language,
            max_results=max_results,
            external_request=external_request,
        )
        value = cls(
            run_id,
            task_id,
            attempt_id,
            task_input_hash,
            matter_id,
            question_id,
            input_refs,
            confidential_question,
            proposed_public_terms,
            private_terms,
            purpose,
            language,
            max_results,
            external_request,
            _canonical_hash(payload),
        )
        value.validate()
        return value

    def validate(self) -> None:
        payload = _binding_payload(
            run_id=self.run_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            task_input_hash=self.task_input_hash,
            matter_id=self.matter_id,
            question_id=self.question_id,
            input_refs=self.input_refs,
            confidential_question=self.confidential_question,
            proposed_public_terms=self.proposed_public_terms,
            private_terms=self.private_terms,
            purpose=self.purpose,
            language=self.language,
            max_results=self.max_results,
            external_request=self.external_request,
        )
        _sha256(self.binding_hash, "research binding_hash")
        if self.binding_hash != _canonical_hash(payload):
            raise CaseAgentResearchAdapterBlocked("research binding hash differs")


def authorized_public_research_source_binding_hash(
    binding: AuthorizedPublicResearchBinding,
) -> str:
    """Return the private-source digest persisted by the server binding port."""

    if not isinstance(binding, AuthorizedPublicResearchBinding):
        raise CaseAgentResearchAdapterBlocked("research binding is invalid")
    binding.validate()
    return _source_binding_hash(
        run_id=binding.run_id,
        task_id=binding.task_id,
        attempt_id=binding.attempt_id,
        task_input_hash=binding.task_input_hash,
        matter_id=binding.matter_id,
        question_id=binding.question_id,
        input_refs=binding.input_refs,
        confidential_question=binding.confidential_question,
        proposed_public_terms=binding.proposed_public_terms,
        private_terms=binding.private_terms,
        purpose=binding.purpose,
        language=binding.language,
        max_results=binding.max_results,
    )


def authorized_public_research_query(
    binding: AuthorizedPublicResearchBinding,
) -> MinimizedPublicQuery:
    """Rebuild the exact minimized query without exposing the private question."""

    if not isinstance(binding, AuthorizedPublicResearchBinding):
        raise CaseAgentResearchAdapterBlocked("research binding is invalid")
    binding.validate()
    return _minimized_query_from_binding(binding)


class PublicResearchBindingPort(Protocol):
    """Resolve current governed issues/work-plan items into a private query."""

    def resolve_public_research(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> AuthorizedPublicResearchBinding: ...


class RecoveredSearchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class RecoveredPublicSearch:
    status: RecoveredSearchStatus
    response_body: bytes | None = field(default=None, repr=False, compare=False)
    egress_receipt: EgressReceiptRef | None = None
    error_code: str | None = None

    def validate(self) -> None:
        if self.status is RecoveredSearchStatus.SUCCEEDED:
            if not isinstance(self.response_body, bytes) or not self.response_body:
                raise CaseAgentResearchAdapterBlocked("recovered search body is unavailable")
            if not isinstance(self.egress_receipt, EgressReceiptRef) or self.error_code is not None:
                raise CaseAgentResearchAdapterBlocked("recovered search receipt is invalid")
        elif self.status is RecoveredSearchStatus.FAILED:
            if self.response_body is not None or self.egress_receipt is not None:
                raise CaseAgentResearchAdapterBlocked("failed search cannot contain response bytes")
            _error_code(self.error_code, "recovered search error_code")
        elif self.response_body is not None or self.egress_receipt is not None or self.error_code is not None:
            raise CaseAgentResearchAdapterBlocked("unresolved search cannot claim an outcome")


class DurablePublicSearchExchange(Protocol):
    """Transport plus lookup-only recovery for an already submitted request."""

    def send(
        self,
        *,
        request: BraveSearchTransportRequest,
        external_request: ExternalRequestRef,
    ) -> BraveSearchTransportResult: ...

    def recover(
        self,
        *,
        external_request_id: str,
        request_hash: str,
    ) -> RecoveredPublicSearch: ...


class PublicWebResearchTaskAdapter:
    """Execute one approved public search and stage review-only leads."""

    manifest = PUBLIC_WEB_RESEARCH_MANIFEST

    def __init__(
        self,
        *,
        binding_port: PublicResearchBindingPort,
        provider: BravePublicSearchProvider,
        exchange: DurablePublicSearchExchange,
        staging_port: ReviewCandidateStagingPort,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(getattr(binding_port, "resolve_public_research", None)):
            raise ValueError("public-research binding port is invalid")
        if not isinstance(provider, BravePublicSearchProvider):
            raise ValueError("Brave public-search provider is required")
        if not callable(getattr(exchange, "send", None)) or not callable(
            getattr(exchange, "recover", None)
        ):
            raise ValueError("durable public-search exchange is required")
        if not callable(getattr(staging_port, "stage_review_candidate", None)):
            raise ValueError("research candidate staging port is invalid")
        self._binding_port = binding_port
        self._provider = provider
        self._exchange = exchange
        self._staging = staging_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock

    def __repr__(self) -> str:
        return "PublicWebResearchTaskAdapter(<server-bound>)"

    def execute(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._monotonic()
        binding, query, external_request, prepared = self._prepare(
            context, for_reconciliation=False
        )
        context.begin_external_submission(
            external_request_id=external_request.request_id,
            destination=BRAVE_SEARCH_HOST,
            request_hash=prepared.request_hash,
        )
        raw_result = self._exchange.send(
            request=prepared,
            external_request=external_request,
        )
        raw, receipt = self._provider.validate_prepared_result(
            public_input=compile_public_search_provider_input(
                query, max_results=binding.max_results, language=binding.language
            ),
            external_request=external_request,
            request=prepared,
            result=raw_result,
        )
        result = parse_brave_public_search_results(
            raw,
            query=query,
            request=external_request,
            succeeded_attempt=_successful_attempt(
                attempt_id=context.claim.attempt_id,
                request=external_request,
                raw=raw,
            ),
            egress_receipt=receipt,
            max_results=binding.max_results,
            authority_registry=AuthoritySourceRegistry(),
        )
        return self._stage_success(
            context=context,
            binding=binding,
            query=query,
            result=result,
            egress_receipt=receipt,
            runtime_seconds=_runtime_seconds(started, self._monotonic()),
        )

    def reconcile(self, *, context: TaskExecutionContext) -> TaskAdapterOutcome:
        started = self._monotonic()
        binding, query, external_request, prepared = self._prepare(
            context, for_reconciliation=True
        )
        if not context.claim.reconciliation or context.external_request_id != external_request.request_id:
            raise CaseAgentResearchAdapterBlocked(
                "research reconciliation differs from its durable external request"
            )
        recovered = self._exchange.recover(
            external_request_id=external_request.request_id,
            request_hash=prepared.request_hash,
        )
        if not isinstance(recovered, RecoveredPublicSearch):
            raise CaseAgentResearchAdapterBlocked("search recovery returned an invalid result")
        recovered.validate()
        if recovered.status is RecoveredSearchStatus.UNRESOLVED:
            raise CaseAgentResearchAdapterBlocked(
                "public search remains unresolved; submission must not be repeated"
            )
        if recovered.status is RecoveredSearchStatus.FAILED:
            return TaskAdapterOutcome(
                status=ResultStatus.FAILED,
                external_submission_state=ExternalSubmissionState.SUBMITTED,
                output_hash=None,
                error_code=recovered.error_code,
                external_request_id=external_request.request_id,
                runtime_seconds=_runtime_seconds(started, self._monotonic()),
                cost_minor_units=0,
                external_calls=1,
            )
        assert recovered.response_body is not None
        assert recovered.egress_receipt is not None
        result = parse_brave_public_search_results(
            recovered.response_body,
            query=query,
            request=external_request,
            succeeded_attempt=_successful_attempt(
                attempt_id=context.claim.attempt_id,
                request=external_request,
                raw=recovered.response_body,
            ),
            egress_receipt=recovered.egress_receipt,
            max_results=binding.max_results,
            authority_registry=AuthoritySourceRegistry(),
        )
        return self._stage_success(
            context=context,
            binding=binding,
            query=query,
            result=result,
            egress_receipt=recovered.egress_receipt,
            runtime_seconds=_runtime_seconds(started, self._monotonic()),
        )

    def _prepare(
        self, context: TaskExecutionContext, *, for_reconciliation: bool
    ) -> tuple[
        AuthorizedPublicResearchBinding,
        MinimizedPublicQuery,
        ExternalRequestRef,
        BraveSearchTransportRequest,
    ]:
        claim = getattr(context, "claim", None)
        task = getattr(context, "task", None)
        if claim is None or task is None:
            raise CaseAgentResearchAdapterBlocked("research requires a durable task context")
        if (
            task.skill.tool_id != "search_public_web"
            or task.capability.network_policy is not NetworkPolicy.EXACT_ALLOWLIST
            or task.capability.allowed_domains != (BRAVE_SEARCH_HOST,)
            or task.budget.max_external_calls != 1
        ):
            raise CaseAgentResearchAdapterBlocked("compiled research capability is not exact")
        binding = self._binding_port.resolve_public_research(
            run_id=claim.run_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            task_input_hash=task.input_hash,
            input_refs=task.input_refs,
        )
        if not isinstance(binding, AuthorizedPublicResearchBinding):
            raise CaseAgentResearchAdapterBlocked("research binding is invalid")
        binding.validate()
        if (
            binding.run_id != claim.run_id
            or binding.task_id != claim.task_id
            or binding.attempt_id != claim.attempt_id
            or binding.task_input_hash != task.input_hash
            or binding.input_refs != task.input_refs
        ):
            raise CaseAgentResearchAdapterBlocked("research binding differs from the task")
        query = _minimized_query_from_binding(binding)
        external_request = binding.external_request
        public_input = compile_public_search_provider_input(
            query, max_results=binding.max_results, language=binding.language
        )
        if not for_reconciliation:
            require_public_search_ready(
                query=query,
                request=external_request,
                prior_attempts=(),
                provider_registry=SearchProviderRegistry(
                    (
                        SearchProviderRegistration(
                            BRAVE_SEARCH_PROVIDER_ID,
                            BRAVE_SEARCH_SERVICE_ID,
                            frozenset({BRAVE_SEARCH_HOST}),
                        ),
                    )
                ),
                now=self._clock(),
            )
        prepared = self._provider.prepare_request(
            public_input=public_input,
            external_request=external_request,
        )
        timeout_budget = getattr(task.budget, "timeout_seconds", 0)
        output_budget = getattr(task.budget, "max_output_bytes", -1)
        if (
            type(timeout_budget) is not int
            or prepared.timeout_seconds > timeout_budget
            or type(output_budget) is not int
            or output_budget < 2
        ):
            raise CaseAgentResearchAdapterBlocked(
                "compiled research resource budget is insufficient"
            )
        return binding, query, external_request, prepared

    def _stage_success(
        self,
        *,
        context: TaskExecutionContext,
        binding: AuthorizedPublicResearchBinding,
        query: MinimizedPublicQuery,
        result: PublicSearchResultSet,
        egress_receipt: EgressReceiptRef,
        runtime_seconds: int,
    ) -> TaskAdapterOutcome:
        source_hash = _canonical_hash(
            {
                "schema_version": "agent-public-research-source-set-v1",
                "binding_hash": binding.binding_hash,
                "query_hash": query.query_hash,
                "external_request_id": result.external_request_id,
                "external_attempt_id": result.external_attempt_id,
                "provider_output_hash": result.output_hash,
                "egress_grant_hash": egress_receipt.egress_grant_hash,
                "egress_response_hash": egress_receipt.response_sha256,
            }
        )
        payload = _json_bytes(
            {
                "schema_version": PUBLIC_RESEARCH_CANDIDATE_SCHEMA,
                "task_input_hash": context.task.input_hash,
                "source_hash": source_hash,
                "review_status": REVIEW_STATUS,
                "formal_fact": False,
                "formal_transaction": False,
                "legal_conclusion": False,
                "evidence_decision": False,
                "legal_effect_confirmed": False,
                "query": {
                    "question_id": binding.question_id,
                    "purpose": binding.purpose.value,
                    "query_hash": query.query_hash,
                    "public_terms": query.terms,
                },
                "provider_id": result.provider_id,
                "external_request_id": result.external_request_id,
                "leads": [
                    {
                        "lead_id": item.lead_id,
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "published_on_candidate": (
                            item.published_on_candidate.isoformat()
                            if item.published_on_candidate is not None
                            else None
                        ),
                        "authority_class": item.authority.authority_class.value,
                        "official_source_id": item.authority.official_source_id,
                        "publisher": item.authority.publisher,
                        "official_domain": item.authority.official_domain,
                        "prompt_injection_signals": tuple(
                            signal.value for signal in item.prompt_injection_signals
                        ),
                        "status": item.status.value,
                    }
                    for item in result.leads
                ],
            }
        )
        if len(payload) > _MAX_CANDIDATE_BYTES:
            raise CaseAgentResearchAdapterBlocked("research candidate exceeds its output limit")
        if len(payload) > context.task.budget.max_output_bytes:
            raise CaseAgentResearchAdapterBlocked(
                "research candidate exceeds its compiled output budget"
            )
        content_hash = sha256(payload).hexdigest()
        idempotency_key = _canonical_hash(
            {
                "schema_version": "agent-public-research-staging-v1",
                "run_id": context.claim.run_id,
                "task_id": context.claim.task_id,
                "task_input_hash": context.task.input_hash,
                "source_hash": source_hash,
                "content_sha256": content_hash,
            }
        )
        request = ReviewCandidateStagingRequest(
            schema_version="agent-review-candidate-staging-v1",
            idempotency_key=idempotency_key,
            run_id=context.claim.run_id,
            task_id=context.claim.task_id,
            task_input_hash=context.task.input_hash,
            source_hash=source_hash,
            artifact_kind=PUBLIC_RESEARCH_ARTIFACT_KIND,
            media_type="application/json",
            content_sha256=content_hash,
            byte_size=len(payload),
            review_status=REVIEW_STATUS,
            payload=payload,
        )
        request.validate()
        staged = self._staging.stage_review_candidate(request)
        if not isinstance(staged, StagedReviewCandidate):
            raise CaseAgentResearchAdapterBlocked("research staging receipt is invalid")
        staged.validate_against(request)
        artifact = ArtifactReceipt(
            artifact_id=staged.artifact_id,
            artifact_kind=staged.artifact_kind,
            content_hash=staged.content_sha256,
            byte_size=staged.byte_size,
            source_input_hash=context.task.input_hash,
            managed_derivative=False,
        )
        artifact.validate()
        output_hash = _canonical_hash(
            {
                "schema_version": "agent-public-research-adapter-output-v1",
                "task_input_hash": context.task.input_hash,
                "source_hash": source_hash,
                "staging_receipt_hash": staged.receipt_hash,
                "artifact_id": staged.artifact_id,
                "artifact_hash": staged.content_sha256,
                "review_status": REVIEW_STATUS,
            }
        )
        return TaskAdapterOutcome(
            status=ResultStatus.SUCCEEDED,
            external_submission_state=ExternalSubmissionState.SUBMITTED,
            output_hash=output_hash,
            error_code=None,
            external_request_id=result.external_request_id,
            runtime_seconds=runtime_seconds,
            cost_minor_units=0,
            external_calls=1,
            artifacts=(artifact,),
        )


def _successful_attempt(
    *, attempt_id: str, request: ExternalRequestRef, raw: bytes
) -> ExternalAttemptRef:
    return ExternalAttemptRef(
        attempt_id=str(uuid5(UUID(request.request_id), f"provider-attempt:{attempt_id}")),
        request_id=request.request_id,
        input_hash=request.input_hash,
        status=ExternalOperationStatus.SUCCEEDED,
        provider_request_ref_hash=None,
        output_hash=sha256(raw).hexdigest(),
        error_code=None,
    )


def _binding_payload(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    task_input_hash: str,
    matter_id: str,
    question_id: str,
    input_refs: tuple[str, ...],
    confidential_question: str,
    proposed_public_terms: tuple[str, ...],
    private_terms: tuple[str, ...],
    purpose: ResearchPurpose,
    language: str,
    max_results: int,
    external_request: ExternalRequestRef,
) -> dict[str, object]:
    for value, label in (
        (run_id, "research run_id"),
        (task_id, "research task_id"),
        (attempt_id, "research attempt_id"),
        (matter_id, "research matter_id"),
        (question_id, "research question_id"),
    ):
        _uuid(value, label)
    _sha256(task_input_hash, "research task_input_hash")
    if (
        not isinstance(input_refs, tuple)
        or not 1 <= len(input_refs) <= _MAX_BINDING_REFS
        or len(set(input_refs)) != len(input_refs)
    ):
        raise CaseAgentResearchAdapterBlocked("research input refs are invalid")
    for value in input_refs:
        if not isinstance(value, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value
        ) is None:
            raise CaseAgentResearchAdapterBlocked("research input ref is invalid")
    if not isinstance(confidential_question, str) or not 1 <= len(confidential_question) <= 20_000:
        raise CaseAgentResearchAdapterBlocked("confidential research question is invalid")
    if not isinstance(proposed_public_terms, tuple) or not proposed_public_terms:
        raise CaseAgentResearchAdapterBlocked("public research terms are required")
    if not isinstance(private_terms, tuple):
        raise CaseAgentResearchAdapterBlocked("private research terms are invalid")
    if not isinstance(purpose, ResearchPurpose):
        raise CaseAgentResearchAdapterBlocked("research purpose is invalid")
    if language not in {"zh-CN", "en"} or type(max_results) is not int or not 1 <= max_results <= 20:
        raise CaseAgentResearchAdapterBlocked("research locale or result limit is invalid")
    if not isinstance(external_request, ExternalRequestRef):
        raise CaseAgentResearchAdapterBlocked("research external authorization is invalid")
    query = _minimized_query(
        matter_id=matter_id,
        question_id=question_id,
        confidential_question=confidential_question,
        proposed_public_terms=proposed_public_terms,
        private_terms=private_terms,
        purpose=purpose,
    )
    source_binding_hash = _source_binding_hash(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        task_input_hash=task_input_hash,
        matter_id=matter_id,
        question_id=question_id,
        input_refs=input_refs,
        confidential_question=confidential_question,
        proposed_public_terms=proposed_public_terms,
        private_terms=private_terms,
        purpose=purpose,
        language=language,
        max_results=max_results,
    )
    expected_request = _build_external_request(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        task_input_hash=task_input_hash,
        query=query,
        source_binding_hash=source_binding_hash,
        external_request_id=external_request.request_id,
        egress_grant_id=external_request.egress_grant_id,
        expires_at=external_request.expires_at,
    )
    if external_request != expected_request:
        raise CaseAgentResearchAdapterBlocked(
            "research external authorization differs from its server binding"
        )
    return {
        "schema_version": "authorized-public-research-binding-v1",
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "task_input_hash": task_input_hash,
        "matter_id": matter_id,
        "question_id": question_id,
        "input_refs": input_refs,
        "confidential_question_hash": sha256(confidential_question.encode("utf-8")).hexdigest(),
        "proposed_public_terms": proposed_public_terms,
        "private_terms_hash": _canonical_hash(private_terms),
        "purpose": purpose.value,
        "language": language,
        "max_results": max_results,
        "source_binding_hash": source_binding_hash,
        "external_request": {
            "request_id": external_request.request_id,
            "authorization_hash": external_request.authorization_hash,
            "egress_grant_id": external_request.egress_grant_id,
            "egress_grant_hash": external_request.egress_grant_hash,
            "provider_id": external_request.provider_id,
            "service_id": external_request.service_id,
            "input_hash": external_request.input_hash,
            "allowed_hosts": external_request.allowed_hosts,
            "expires_at": external_request.expires_at.isoformat(),
            "status": external_request.status.value,
        },
    }


def _minimized_query_from_binding(
    binding: AuthorizedPublicResearchBinding,
) -> MinimizedPublicQuery:
    return _minimized_query(
        matter_id=binding.matter_id,
        question_id=binding.question_id,
        confidential_question=binding.confidential_question,
        proposed_public_terms=binding.proposed_public_terms,
        private_terms=binding.private_terms,
        purpose=binding.purpose,
    )


def _minimized_query(
    *,
    matter_id: str,
    question_id: str,
    confidential_question: str,
    proposed_public_terms: tuple[str, ...],
    private_terms: tuple[str, ...],
    purpose: ResearchPurpose,
) -> MinimizedPublicQuery:
    return prepare_minimized_public_query(
        matter_id=matter_id,
        question_id=question_id,
        confidential_question=confidential_question,
        proposed_public_terms=proposed_public_terms,
        private_terms=private_terms,
        purpose=purpose,
    )


def _build_external_request(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    task_input_hash: str,
    query: MinimizedPublicQuery,
    source_binding_hash: str,
    external_request_id: str,
    egress_grant_id: str,
    expires_at: datetime,
) -> ExternalRequestRef:
    for value, label in (
        (run_id, "research run_id"),
        (task_id, "research task_id"),
        (attempt_id, "research attempt_id"),
        (external_request_id, "research external request_id"),
        (egress_grant_id, "research egress grant_id"),
    ):
        _uuid(value, label)
    _sha256(task_input_hash, "research task_input_hash")
    _sha256(source_binding_hash, "research source_binding_hash")
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise CaseAgentResearchAdapterBlocked("research authorization expiry is invalid")
    authorization_hash = _canonical_hash(
        {
            "schema_version": "case-agent-public-research-authorization-v1",
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "task_input_hash": task_input_hash,
            "query_hash": query.query_hash,
            "source_binding_hash": source_binding_hash,
            "provider_id": BRAVE_SEARCH_PROVIDER_ID,
            "service_id": BRAVE_SEARCH_SERVICE_ID,
            "allowed_hosts": (BRAVE_SEARCH_HOST,),
            "expires_at": expires_at.isoformat(),
        }
    )
    grant_hash = _canonical_hash(
        {
            "schema_version": "case-agent-public-research-egress-grant-v1",
            "grant_id": egress_grant_id,
            "external_request_id": external_request_id,
            "allowed_hosts": (BRAVE_SEARCH_HOST,),
            "allowed_methods": ("GET",),
            "max_requests": 1,
            "max_response_bytes": 2 * 1024 * 1024,
            "expires_at": expires_at.isoformat(),
            "query_data_minimized": True,
        }
    )
    return ExternalRequestRef(
        request_id=external_request_id,
        authorization_hash=authorization_hash,
        egress_grant_id=egress_grant_id,
        egress_grant_hash=grant_hash,
        provider_id=BRAVE_SEARCH_PROVIDER_ID,
        service_id=BRAVE_SEARCH_SERVICE_ID,
        input_hash=query.query_hash,
        allowed_hosts=(BRAVE_SEARCH_HOST,),
        expires_at=expires_at,
    )


def _source_binding_hash(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    task_input_hash: str,
    matter_id: str,
    question_id: str,
    input_refs: tuple[str, ...],
    confidential_question: str,
    proposed_public_terms: tuple[str, ...],
    private_terms: tuple[str, ...],
    purpose: ResearchPurpose,
    language: str,
    max_results: int,
) -> str:
    return _canonical_hash(
        {
            "schema_version": "case-agent-public-research-source-binding-v1",
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "task_input_hash": task_input_hash,
            "matter_id": matter_id,
            "question_id": question_id,
            "input_refs": input_refs,
            "confidential_question_hash": sha256(
                confidential_question.encode("utf-8")
            ).hexdigest(),
            "proposed_public_terms": proposed_public_terms,
            "private_terms_hash": _canonical_hash(private_terms),
            "purpose": purpose.value,
            "language": language,
            "max_results": max_results,
        }
    )


def _runtime_seconds(started: float, finished: float) -> int:
    if finished < started:
        raise CaseAgentResearchAdapterBlocked("research monotonic clock moved backwards")
    return max(0, int(finished - started))


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseAgentResearchAdapterBlocked(f"{label} is invalid") from error


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CaseAgentResearchAdapterBlocked(f"{label} is invalid")


def _error_code(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", value) is None:
        raise CaseAgentResearchAdapterBlocked(f"{label} is invalid")


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


__all__ = (
    "AuthorizedPublicResearchBinding",
    "authorized_public_research_query",
    "authorized_public_research_source_binding_hash",
    "CaseAgentResearchAdapterBlocked",
    "DurablePublicSearchExchange",
    "PUBLIC_RESEARCH_ARTIFACT_KIND",
    "PUBLIC_RESEARCH_CANDIDATE_SCHEMA",
    "PUBLIC_WEB_RESEARCH_MANIFEST",
    "PublicResearchBindingPort",
    "PublicWebResearchTaskAdapter",
    "RecoveredPublicSearch",
    "RecoveredSearchStatus",
)
