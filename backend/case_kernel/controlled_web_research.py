"""Controlled public-Web research contracts for the lawyer Agent.

This module separates four things that a generic Web search often conflates:

* a confidential lawyer question;
* a minimized, non-confidential public query;
* an untrusted public research lead;
* an exact official-page snapshot candidate.

``SearchProvider`` and ``SnapshotFetcher`` are server-side protocols which
must be composed with the external-request ledger and the egress broker.  A
Brave Web Search provider adapter now exists in ``brave_public_search``, but it
remains gated until a production broker, durable attempt ledger and server
credential are wired.  Search hits are never legal authority.  Even a
byte-exact snapshot from a registered official domain is only a source
candidate until the legal-source ledger and a lawyer confirm its identity,
version, effect and application to the case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from ipaddress import ip_address
import json
import re
from typing import Iterable, Mapping, Protocol
from urllib.parse import SplitResult, parse_qsl, unquote, urlsplit, urlunsplit
from uuid import UUID


CONTROLLED_WEB_RESEARCH_SKILL_ID = "controlled_web_research"
CONTROLLED_WEB_RESEARCH_MATURITY = "GATED"
SEARCH_RESULT_SCHEMA_VERSION = "controlled-public-search-results-v1"
MAX_QUERY_TERMS = 24
MAX_QUERY_CHARACTERS = 240
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_LEADS = 100
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
OFFICIAL_SNAPSHOT_FETCH_SERVICE_ID = "official_snapshot_fetch"


class ControlledResearchBlocked(ValueError):
    """A query, URL, receipt or untrusted response violates the research policy."""


class ResearchPurpose(StrEnum):
    LEGAL_AUTHORITY_DISCOVERY = "LEGAL_AUTHORITY_DISCOVERY"
    CASE_RESEARCH = "CASE_RESEARCH"
    GENERAL_PUBLIC_RESEARCH = "GENERAL_PUBLIC_RESEARCH"


class ResearchLeadStatus(StrEnum):
    PUBLIC_RESEARCH_LEAD = "PUBLIC_RESEARCH_LEAD"


class AuthorityClass(StrEnum):
    PRIMARY_LEGISLATION = "PRIMARY_LEGISLATION"
    JUDICIAL_INTERPRETATION = "JUDICIAL_INTERPRETATION"
    OFFICIAL_CASE_DATABASE = "OFFICIAL_CASE_DATABASE"
    OFFICIAL_COURT_PUBLICATION = "OFFICIAL_COURT_PUBLICATION"
    OFFICIAL_RATE_DATA = "OFFICIAL_RATE_DATA"
    CENTRAL_GOVERNMENT = "CENTRAL_GOVERNMENT"
    NON_OFFICIAL = "NON_OFFICIAL"


class ExternalOperationStatus(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PromptInjectionSignal(StrEnum):
    IGNORE_PRIOR_INSTRUCTIONS = "IGNORE_PRIOR_INSTRUCTIONS"
    SYSTEM_OR_DEVELOPER_ROLE_TEXT = "SYSTEM_OR_DEVELOPER_ROLE_TEXT"
    TOOL_OR_COMMAND_REQUEST = "TOOL_OR_COMMAND_REQUEST"
    CREDENTIAL_OR_SECRET_REQUEST = "CREDENTIAL_OR_SECRET_REQUEST"
    MODEL_CONTROL_TOKEN = "MODEL_CONTROL_TOKEN"
    BINARY_CONTENT_NOT_TEXT_INSPECTED = "BINARY_CONTENT_NOT_TEXT_INSPECTED"


class SnapshotReviewStatus(StrEnum):
    LAWYER_AND_LEDGER_REVIEW_REQUIRED = "LAWYER_AND_LEDGER_REVIEW_REQUIRED"


class MetadataCandidateStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class MinimizedPublicQuery:
    matter_id: str
    question_id: str
    question_hash: str
    purpose: ResearchPurpose
    terms: tuple[str, ...]
    query_text: str
    query_hash: str
    status: str = "READY_FOR_EXTERNAL_AUTHORIZATION"


@dataclass(frozen=True)
class PublicSearchProviderInput:
    """The complete payload surface an adapter may serialize externally.

    Matter/question identifiers and the confidential question hash are
    intentionally absent.  The external-request reference binds this payload
    internally; it is not part of the provider's query body.
    """

    query_text: str
    language: str
    max_results: int
    response_schema_version: str = SEARCH_RESULT_SCHEMA_VERSION


@dataclass(frozen=True)
class ExternalRequestRef:
    """Abstract reference to the existing external-request and egress ledgers."""

    request_id: str
    authorization_hash: str
    egress_grant_id: str
    egress_grant_hash: str
    provider_id: str
    service_id: str
    input_hash: str
    allowed_hosts: tuple[str, ...]
    expires_at: datetime
    status: ExternalOperationStatus = ExternalOperationStatus.AUTHORIZED


@dataclass(frozen=True)
class ExternalAttemptRef:
    attempt_id: str
    request_id: str
    input_hash: str
    status: ExternalOperationStatus
    provider_request_ref_hash: str | None
    output_hash: str | None
    error_code: str | None


@dataclass(frozen=True)
class AuthoritySourceRegistration:
    source_id: str
    publisher: str
    authority_class: AuthorityClass
    exact_hosts: frozenset[str]
    allowed_media_types: frozenset[str]


@dataclass(frozen=True)
class SearchProviderRegistration:
    """Admin-pinned transport identity, not evidence that an adapter is live."""

    provider_id: str
    service_id: str
    exact_hosts: frozenset[str]


@dataclass(frozen=True)
class AuthorityClassification:
    authority_class: AuthorityClass
    official_source_id: str | None
    publisher: str | None
    official_domain: bool


@dataclass(frozen=True)
class PublicResearchLead:
    lead_id: str
    title: str
    url: str
    snippet: str
    published_on_candidate: date | None
    authority: AuthorityClassification
    prompt_injection_signals: tuple[PromptInjectionSignal, ...]
    status: ResearchLeadStatus = ResearchLeadStatus.PUBLIC_RESEARCH_LEAD


@dataclass(frozen=True)
class PublicSearchResultSet:
    query_hash: str
    provider_id: str
    external_request_id: str
    external_attempt_id: str
    leads: tuple[PublicResearchLead, ...]
    output_hash: str


@dataclass(frozen=True)
class OfficialSnapshotFetchSpec:
    source_id: str
    requested_url: str
    query_hash: str
    fetch_input_hash: str
    max_bytes: int


@dataclass(frozen=True)
class FetchedPublicPage:
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status_code: int
    media_type: str
    body: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class EgressReceiptRef:
    egress_grant_id: str
    egress_grant_hash: str
    request_id: str
    request_url: str
    redirect_chain: tuple[str, ...]
    resolved_peer_ips: tuple[str, ...]
    connected_peer_ip: str
    method: str
    request_count: int
    response_bytes: int
    response_sha256: str


@dataclass(frozen=True)
class PublicationMetadataCandidate:
    published_on: date | None
    effective_on: date | None
    ceased_on: date | None
    source_excerpt: str
    extraction_method: str
    status: MetadataCandidateStatus = MetadataCandidateStatus.NEEDS_REVIEW


@dataclass(frozen=True)
class OfficialPageSnapshotCandidate:
    source_id: str
    publisher: str
    authority_class: AuthorityClass
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    captured_at: datetime
    media_type: str
    content_sha256: str
    content_bytes: int
    external_request_id: str
    egress_grant_hash: str
    connected_peer_ip: str
    metadata: PublicationMetadataCandidate | None
    prompt_injection_signals: tuple[PromptInjectionSignal, ...]
    review_status: SnapshotReviewStatus
    legal_effect_confirmed: bool
    snapshot_hash: str
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.legal_effect_confirmed:
            raise ControlledResearchBlocked(
                "captured official bytes cannot self-confirm legal effect or case applicability"
            )
        if self.review_status is not SnapshotReviewStatus.LAWYER_AND_LEDGER_REVIEW_REQUIRED:
            raise ControlledResearchBlocked("official snapshot candidate cannot bypass review")


class SearchProvider(Protocol):
    """A server-owned search adapter; no browser URL, key or prompt surface."""

    provider_id: str

    def search(
        self,
        *,
        public_input: PublicSearchProviderInput,
        external_request: ExternalRequestRef,
    ) -> tuple[str | bytes, EgressReceiptRef]: ...


class SnapshotFetcher(Protocol):
    """A server-owned fetch adapter operating behind the egress broker."""

    def fetch(
        self,
        *,
        spec: OfficialSnapshotFetchSpec,
        external_request: ExternalRequestRef,
    ) -> tuple[FetchedPublicPage, EgressReceiptRef]: ...


DEFAULT_AUTHORITY_SOURCES: tuple[AuthoritySourceRegistration, ...] = (
    AuthoritySourceRegistration(
        "NATIONAL_LAWS_DATABASE",
        "国家法律法规数据库",
        AuthorityClass.PRIMARY_LEGISLATION,
        frozenset({"flk.npc.gov.cn", "wb.flk.npc.gov.cn"}),
        frozenset({"text/html", "application/json", "application/pdf"}),
    ),
    AuthoritySourceRegistration(
        "SUPREME_PEOPLES_COURT",
        "最高人民法院",
        AuthorityClass.JUDICIAL_INTERPRETATION,
        frozenset({"www.court.gov.cn", "court.gov.cn", "gongbao.court.gov.cn", "cicc.court.gov.cn"}),
        frozenset({"text/html", "application/xhtml+xml", "application/pdf"}),
    ),
    AuthoritySourceRegistration(
        "PEOPLES_COURT_CASE_DATABASE",
        "人民法院案例库",
        AuthorityClass.OFFICIAL_CASE_DATABASE,
        frozenset({"rmfyalk.court.gov.cn"}),
        frozenset({"text/html", "application/xhtml+xml", "application/pdf"}),
    ),
    AuthoritySourceRegistration(
        "CHINA_JUDGMENTS_ONLINE",
        "中国裁判文书网",
        AuthorityClass.OFFICIAL_COURT_PUBLICATION,
        frozenset({"wenshu.court.gov.cn"}),
        frozenset({"text/html", "application/json", "application/pdf"}),
    ),
    AuthoritySourceRegistration(
        "OFFICIAL_LPR_DATA",
        "全国银行间同业拆借中心",
        AuthorityClass.OFFICIAL_RATE_DATA,
        frozenset({"www.chinamoney.com.cn", "chinamoney.com.cn", "www.shibor.org", "shibor.org"}),
        frozenset({
            "text/html",
            "application/json",
            "application/pdf",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }),
    ),
    AuthoritySourceRegistration(
        "CENTRAL_GOVERNMENT_PORTAL",
        "中国政府网",
        AuthorityClass.CENTRAL_GOVERNMENT,
        frozenset({"www.gov.cn", "gov.cn"}),
        frozenset({"text/html", "application/xhtml+xml", "application/pdf"}),
    ),
)


class AuthoritySourceRegistry:
    def __init__(
        self,
        registrations: Iterable[AuthoritySourceRegistration] = DEFAULT_AUTHORITY_SOURCES,
    ) -> None:
        values = tuple(registrations)
        if not values:
            raise ValueError("authority source registry cannot be empty")
        self._sources: dict[str, AuthoritySourceRegistration] = {}
        self._by_host: dict[str, AuthoritySourceRegistration] = {}
        for source in values:
            _require_code(source.source_id, "source_id")
            _bounded_text(source.publisher, "publisher", 200)
            if source.source_id in self._sources or not source.exact_hosts:
                raise ValueError("authority source registry identifiers and hosts must be explicit")
            for host in source.exact_hosts:
                normalized = _public_dns_host(host)
                if normalized in self._by_host:
                    raise ValueError("authority source host belongs to multiple registrations")
                self._by_host[normalized] = source
            self._sources[source.source_id] = source

    def classify_url(self, url: str) -> AuthorityClassification:
        parsed = validate_public_https_url(url)
        source = self._by_host.get(parsed.hostname)
        if source is None:
            return AuthorityClassification(
                AuthorityClass.NON_OFFICIAL, None, None, False
            )
        return AuthorityClassification(
            source.authority_class, source.source_id, source.publisher, True
        )

    def source(self, source_id: str) -> AuthoritySourceRegistration:
        try:
            return self._sources[source_id]
        except KeyError as error:
            raise ControlledResearchBlocked("official source is not registered") from error


class SearchProviderRegistry:
    """Default-empty registry keeps public search fail-closed until deployment."""

    def __init__(self, registrations: Iterable[SearchProviderRegistration] = ()) -> None:
        self._providers: dict[str, SearchProviderRegistration] = {}
        for registration in registrations:
            _require_code(registration.provider_id, "search provider_id")
            _require_code(registration.service_id, "search service_id")
            if registration.provider_id in self._providers or not registration.exact_hosts:
                raise ValueError("search provider registration must be unique and bounded")
            exact_hosts = frozenset(_public_dns_host(host) for host in registration.exact_hosts)
            self._providers[registration.provider_id] = SearchProviderRegistration(
                registration.provider_id,
                registration.service_id,
                exact_hosts,
            )

    def provider(self, provider_id: str) -> SearchProviderRegistration:
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise ControlledResearchBlocked(
                "search provider is not registered; controlled Web research remains gated"
            ) from error


def prepare_minimized_public_query(
    *,
    matter_id: str,
    question_id: str,
    confidential_question: str,
    proposed_public_terms: Iterable[str],
    private_terms: Iterable[str],
    purpose: ResearchPurpose,
) -> MinimizedPublicQuery:
    """Produce the only text allowed to leave the case boundary for search.

    A model may propose terms, but code checks them against known case-private
    values plus common identifiers and quoted source passages.  The original
    question is hashed and then discarded from the returned object.
    """

    _require_uuid(matter_id, "matter_id")
    _require_uuid(question_id, "question_id")
    question = _bounded_text(confidential_question, "confidential question", 20_000)
    if not isinstance(purpose, ResearchPurpose):
        raise ControlledResearchBlocked("research purpose is invalid")
    confidential_values: set[str] = set()
    for value in private_terms:
        if not isinstance(value, str):
            raise ControlledResearchBlocked("private term registry contains an invalid value")
        normalized_private = _normalized_secret(value)
        if normalized_private:
            confidential_values.add(normalized_private)
    confidential_values.update(_case_private_spans(question))
    terms: list[str] = []
    seen: set[str] = set()
    for value in proposed_public_terms:
        term = _normalize_public_term(value)
        _reject_sensitive_public_text(term)
        normalized = _normalized_secret(term)
        if any(
            secret and (secret in normalized or normalized in secret)
            for secret in confidential_values
        ):
            raise ControlledResearchBlocked(
                "public research terms contain a known case-private value"
            )
        identity = term.casefold()
        if identity not in seen:
            terms.append(term)
            seen.add(identity)
    if not 2 <= len(terms) <= MAX_QUERY_TERMS:
        raise ControlledResearchBlocked(
            "public research query requires 2 to 24 minimized terms"
        )
    query_text = " ".join(terms)
    if len(query_text) > MAX_QUERY_CHARACTERS:
        raise ControlledResearchBlocked("public research query exceeds the public boundary")
    _reject_sensitive_public_text(query_text)
    question_hash = sha256(question.encode("utf-8")).hexdigest()
    query_hash = _canonical_hash(
        {
            "schema_version": "minimized-public-query-v1",
            "matter_id": matter_id,
            "question_id": question_id,
            "question_hash": question_hash,
            "purpose": purpose.value,
            "terms": terms,
            "query_text": query_text,
        }
    )
    return MinimizedPublicQuery(
        matter_id=matter_id,
        question_id=question_id,
        question_hash=question_hash,
        purpose=purpose,
        terms=tuple(terms),
        query_text=query_text,
        query_hash=query_hash,
    )


def compile_public_search_provider_input(
    query: MinimizedPublicQuery,
    *,
    max_results: int = 20,
    language: str = "zh-CN",
) -> PublicSearchProviderInput:
    """Expose only the reviewed public text to a server-owned search adapter."""

    if not isinstance(query, MinimizedPublicQuery):
        raise ControlledResearchBlocked("minimized public query is invalid")
    if query.status != "READY_FOR_EXTERNAL_AUTHORIZATION":
        raise ControlledResearchBlocked("minimized public query is not ready")
    _reject_sensitive_public_text(query.query_text)
    if query.query_text != " ".join(query.terms):
        raise ControlledResearchBlocked("minimized public query text differs from its terms")
    if language not in {"zh-CN", "en"}:
        raise ControlledResearchBlocked("public search language is not allowlisted")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 50:
        raise ControlledResearchBlocked("public search result limit is invalid")
    return PublicSearchProviderInput(query.query_text, language, max_results)


def require_external_submission_ready(
    *,
    request: ExternalRequestRef,
    expected_input_hash: str,
    prior_attempts: Iterable[ExternalAttemptRef],
    now: datetime,
) -> None:
    """Fail closed before query/fetch transport, especially after UNKNOWN."""

    _validate_external_request(request, expected_input_hash=expected_input_hash, now=now)
    attempts = tuple(prior_attempts)
    for attempt in attempts:
        _validate_attempt(attempt, request=request)
    if any(attempt.status is ExternalOperationStatus.UNKNOWN_SUBMISSION for attempt in attempts):
        raise ControlledResearchBlocked(
            "unknown external submission must be reconciled and cannot be retried"
        )
    if attempts:
        raise ControlledResearchBlocked(
            "external request already has an attempt; a new lawyer authorization is required"
        )


def require_public_search_ready(
    *,
    query: MinimizedPublicQuery,
    request: ExternalRequestRef,
    prior_attempts: Iterable[ExternalAttemptRef],
    provider_registry: SearchProviderRegistry,
    now: datetime,
) -> None:
    """Prove the search is bound to one registered provider and exact hosts."""

    registration = provider_registry.provider(request.provider_id)
    if request.service_id != registration.service_id:
        raise ControlledResearchBlocked("search request service differs from its provider")
    allowed_hosts = frozenset(_public_dns_host(host) for host in request.allowed_hosts)
    if allowed_hosts != registration.exact_hosts:
        raise ControlledResearchBlocked("search request must use the provider's exact host set")
    require_external_submission_ready(
        request=request,
        expected_input_hash=query.query_hash,
        prior_attempts=prior_attempts,
        now=now,
    )


def parse_public_search_results(
    raw: str | bytes,
    *,
    query: MinimizedPublicQuery,
    request: ExternalRequestRef,
    succeeded_attempt: ExternalAttemptRef,
    egress_receipt: EgressReceiptRef,
    provider_id: str,
    registry: AuthoritySourceRegistry | None = None,
) -> PublicSearchResultSet:
    """Turn provider JSON into untrusted leads, never legal-source records."""

    _require_code(provider_id, "provider_id")
    encoded = validate_successful_public_search_transport(
        raw,
        query=query,
        request=request,
        succeeded_attempt=succeeded_attempt,
        egress_receipt=egress_receipt,
        provider_id=provider_id,
    )
    output_hash = sha256(encoded).hexdigest()
    try:
        value = json.loads(
            encoded,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ControlledResearchBlocked("search provider response contains non-finite data")
            ),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlledResearchBlocked("search provider response must be one JSON object") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "query_hash", "provider_id", "results"
    }:
        raise ControlledResearchBlocked("search provider response schema is invalid")
    if (
        value["schema_version"] != SEARCH_RESULT_SCHEMA_VERSION
        or value["query_hash"] != query.query_hash
        or value["provider_id"] != provider_id
    ):
        raise ControlledResearchBlocked("search provider response is not bound to this query")
    values = value["results"]
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SEARCH_LEADS:
        raise ControlledResearchBlocked("search result count is invalid")
    authority_registry = registry or AuthoritySourceRegistry()
    leads: list[PublicResearchLead] = []
    ids: set[str] = set()
    urls: set[str] = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {
            "lead_id", "title", "url", "snippet", "published_on"
        }:
            raise ControlledResearchBlocked("search result item schema is invalid")
        lead_id = _item_id(item["lead_id"], "lead_id")
        if lead_id in ids:
            raise ControlledResearchBlocked("search lead ids must be unique")
        title = _bounded_text(item["title"], "search lead title", 500)
        snippet = _bounded_text(item["snippet"], "search lead snippet", 4_000, allow_empty=True)
        parsed = validate_public_https_url(item["url"])
        url = urlunsplit(parsed)
        if url in urls:
            raise ControlledResearchBlocked("search lead URLs must be unique")
        published = _optional_iso_date(item["published_on"], "published_on")
        signals = detect_prompt_injection(f"{title}\n{snippet}", binary=False)
        leads.append(
            PublicResearchLead(
                lead_id,
                title,
                url,
                snippet,
                published,
                authority_registry.classify_url(url),
                signals,
            )
        )
        ids.add(lead_id)
        urls.add(url)
    return PublicSearchResultSet(
        query_hash=query.query_hash,
        provider_id=provider_id,
        external_request_id=request.request_id,
        external_attempt_id=succeeded_attempt.attempt_id,
        leads=tuple(leads),
        output_hash=output_hash,
    )


def validate_successful_public_search_transport(
    raw: str | bytes,
    *,
    query: MinimizedPublicQuery,
    request: ExternalRequestRef,
    succeeded_attempt: ExternalAttemptRef,
    egress_receipt: EgressReceiptRef,
    provider_id: str,
) -> bytes:
    """Bind exact search-provider bytes to authorization, outcome and egress.

    Provider-specific adapters may use this boundary before translating their
    own response schema into :class:`PublicResearchLead` objects.  It validates
    the exact bytes received over HTTPS; a later local normalization must never
    replace the transport hash or pretend to be the provider response.
    """

    _require_code(provider_id, "provider_id")
    _validate_external_request_without_clock(request, expected_input_hash=query.query_hash)
    if request.provider_id != provider_id:
        raise ControlledResearchBlocked("search receipt is bound to another provider")
    _validate_attempt(succeeded_attempt, request=request)
    if succeeded_attempt.status is not ExternalOperationStatus.SUCCEEDED:
        raise ControlledResearchBlocked("search results require a successful external receipt")
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or not 2 <= len(encoded) <= MAX_SEARCH_RESPONSE_BYTES:
        raise ControlledResearchBlocked("search provider response size is invalid")
    output_hash = sha256(encoded).hexdigest()
    if succeeded_attempt.output_hash != output_hash:
        raise ControlledResearchBlocked("search provider bytes differ from the external receipt")
    _validate_search_egress_receipt(
        egress_receipt,
        request=request,
        response_bytes=len(encoded),
        output_hash=output_hash,
    )
    return encoded


def build_official_snapshot_fetch_spec(
    *,
    lead: PublicResearchLead,
    query_hash: str,
    registry: AuthoritySourceRegistry | None = None,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
) -> OfficialSnapshotFetchSpec:
    """Compile an official lead into an exact, separately authorized fetch."""

    _require_sha256(query_hash, "query_hash")
    authority_registry = registry or AuthoritySourceRegistry()
    classification = authority_registry.classify_url(lead.url)
    if (
        lead.status is not ResearchLeadStatus.PUBLIC_RESEARCH_LEAD
        or not classification.official_domain
        or classification.official_source_id is None
        or classification != lead.authority
    ):
        raise ControlledResearchBlocked(
            "only a registered official-domain lead may become a snapshot fetch candidate"
        )
    if not 1 <= max_bytes <= MAX_SNAPSHOT_BYTES:
        raise ControlledResearchBlocked("official snapshot byte limit is invalid")
    fetch_input_hash = _canonical_hash(
        {
            "schema_version": "official-snapshot-fetch-v1",
            "source_id": classification.official_source_id,
            "requested_url": lead.url,
            "query_hash": query_hash,
            "max_bytes": max_bytes,
        }
    )
    return OfficialSnapshotFetchSpec(
        classification.official_source_id,
        lead.url,
        query_hash,
        fetch_input_hash,
        max_bytes,
    )


def require_official_snapshot_fetch_ready(
    *,
    spec: OfficialSnapshotFetchSpec,
    request: ExternalRequestRef,
    prior_attempts: Iterable[ExternalAttemptRef],
    now: datetime,
    registry: AuthoritySourceRegistry | None = None,
) -> None:
    """Preflight an official fetch before any socket is opened.

    The generic external-request ledger binds the bytes and call budget.  This
    domain preflight additionally proves that every egress host belongs to the
    one registered official publisher selected by the fetch spec.  A receipt
    check after the call is not an SSRF control by itself.
    """

    authority_registry = registry or AuthoritySourceRegistry()
    source = authority_registry.source(spec.source_id)
    if request.provider_id != spec.source_id:
        raise ControlledResearchBlocked("official fetch provider differs from its source registry")
    requested = validate_public_https_url(spec.requested_url)
    if requested.hostname not in source.exact_hosts:
        raise ControlledResearchBlocked("official fetch URL is outside its registered source")
    for host in request.allowed_hosts:
        normalized = _public_dns_host(host)
        if normalized not in source.exact_hosts:
            raise ControlledResearchBlocked(
                "official fetch egress allowlist crosses a registered source boundary"
            )
    if requested.hostname not in request.allowed_hosts:
        raise ControlledResearchBlocked("official fetch host is absent from its egress grant")
    if request.service_id != OFFICIAL_SNAPSHOT_FETCH_SERVICE_ID:
        raise ControlledResearchBlocked("official fetch request uses the wrong service")
    require_external_submission_ready(
        request=request,
        expected_input_hash=spec.fetch_input_hash,
        prior_attempts=prior_attempts,
        now=now,
    )


def build_official_page_snapshot_candidate(
    *,
    spec: OfficialSnapshotFetchSpec,
    request: ExternalRequestRef,
    succeeded_attempt: ExternalAttemptRef,
    fetched: FetchedPublicPage,
    egress_receipt: EgressReceiptRef,
    captured_at: datetime,
    metadata: PublicationMetadataCandidate | None = None,
    registry: AuthoritySourceRegistry | None = None,
) -> OfficialPageSnapshotCandidate:
    """Bind an exact official response, egress evidence and review-only metadata."""

    authority_registry = registry or AuthoritySourceRegistry()
    source = authority_registry.source(spec.source_id)
    _validate_external_request_without_clock(request, expected_input_hash=spec.fetch_input_hash)
    _validate_attempt(succeeded_attempt, request=request)
    if succeeded_attempt.status is not ExternalOperationStatus.SUCCEEDED:
        raise ControlledResearchBlocked("official snapshot requires a successful fetch receipt")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ControlledResearchBlocked("official snapshot capture time must include a timezone")
    if not isinstance(fetched.body, bytes) or not 1 <= len(fetched.body) <= spec.max_bytes:
        raise ControlledResearchBlocked("official snapshot body is empty or exceeds its grant")
    content_hash = sha256(fetched.body).hexdigest()
    if succeeded_attempt.output_hash != content_hash:
        raise ControlledResearchBlocked("official snapshot bytes differ from the external receipt")
    _validate_egress_receipt(
        egress_receipt,
        request=request,
        fetched=fetched,
        content_hash=content_hash,
    )
    chain = validate_redirect_chain(
        fetched.redirect_chain,
        allowed_hosts=request.allowed_hosts,
    )
    requested = urlunsplit(validate_public_https_url(spec.requested_url))
    final = urlunsplit(validate_public_https_url(fetched.final_url))
    if (
        fetched.requested_url != requested
        or not chain
        or chain[0] != requested
        or chain[-1] != final
        or fetched.status_code != 200
    ):
        raise ControlledResearchBlocked("official snapshot URL or HTTP result is inconsistent")
    if any(authority_registry.classify_url(url).official_source_id != source.source_id for url in chain):
        raise ControlledResearchBlocked("official snapshot redirect left its registered source")
    media_type = _normalize_media_type(fetched.media_type)
    if media_type not in source.allowed_media_types:
        raise ControlledResearchBlocked("official snapshot media type is not registered for this source")
    _validate_content_shape(media_type, fetched.body)
    decoded = _decode_text_content(media_type, fetched.body)
    signals = detect_prompt_injection(
        decoded if decoded is not None else "",
        binary=decoded is None,
    )
    if metadata is not None:
        _validate_metadata_candidate(metadata, decoded_text=decoded)
    snapshot_hash = _canonical_hash(
        {
            "schema_version": "official-page-snapshot-candidate-v1",
            "source_id": source.source_id,
            "publisher": source.publisher,
            "authority_class": source.authority_class.value,
            "requested_url": requested,
            "final_url": final,
            "redirect_chain": chain,
            "captured_at": captured_at.isoformat(),
            "media_type": media_type,
            "content_sha256": content_hash,
            "content_bytes": len(fetched.body),
            "external_request_id": request.request_id,
            "egress_grant_hash": request.egress_grant_hash,
            "connected_peer_ip": egress_receipt.connected_peer_ip,
            "metadata": _metadata_payload(metadata),
            "prompt_injection_signals": [item.value for item in signals],
            "review_status": SnapshotReviewStatus.LAWYER_AND_LEDGER_REVIEW_REQUIRED.value,
            "legal_effect_confirmed": False,
        }
    )
    return OfficialPageSnapshotCandidate(
        source.source_id,
        source.publisher,
        source.authority_class,
        requested,
        final,
        chain,
        captured_at,
        media_type,
        content_hash,
        len(fetched.body),
        request.request_id,
        request.egress_grant_hash,
        egress_receipt.connected_peer_ip,
        metadata,
        signals,
        SnapshotReviewStatus.LAWYER_AND_LEDGER_REVIEW_REQUIRED,
        False,
        snapshot_hash,
        fetched.body,
    )


def validate_public_https_url(
    value: object,
    *,
    allowed_hosts: Iterable[str] | None = None,
) -> SplitResult:
    """Validate syntax only; egress broker must additionally pin global DNS/IP."""

    if not isinstance(value, str) or not 9 <= len(value) <= 4_096:
        raise ControlledResearchBlocked("public target URL is invalid")
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        raise ControlledResearchBlocked("public target URL contains ambiguous characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ControlledResearchBlocked("public target URL authority is malformed") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ControlledResearchBlocked(
            "public target must be canonical credential-free HTTPS on port 443"
        )
    host = _public_dns_host(parsed.hostname)
    if parsed.hostname != host:
        raise ControlledResearchBlocked("public target hostname is not canonical lowercase DNS")
    if allowed_hosts is not None:
        allowed = {_public_dns_host(item) for item in allowed_hosts}
        if not allowed or host not in allowed:
            raise ControlledResearchBlocked("public target is denied by the exact egress allowlist")
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        raise ControlledResearchBlocked("public target path is ambiguous")
    decoded_query = unquote(parsed.query)
    _reject_sensitive_public_text(decoded_query)
    if "@" in decoded_query:
        raise ControlledResearchBlocked("public target URL contains credential-like query data")
    blocked_query_keys = {
        "access_token", "api_key", "apikey", "auth", "authorization", "code",
        "credential", "key", "password", "secret", "sig", "signature", "token",
        "x-amz-credential", "x-amz-signature",
    }
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as error:
        raise ControlledResearchBlocked("public target query is malformed") from error
    if any(key.casefold() in blocked_query_keys for key, _ in query_pairs):
        raise ControlledResearchBlocked("public target URL contains credential-like query data")
    for _, item in query_pairs:
        if any(pattern.search(item) for _, pattern in _SENSITIVE_PATTERNS):
            raise ControlledResearchBlocked("public target URL contains private query data")
    return SplitResult("https", host, path, parsed.query, "")


def validate_redirect_chain(
    values: Iterable[str],
    *,
    allowed_hosts: Iterable[str],
) -> tuple[str, ...]:
    chain = tuple(values)
    if not 1 <= len(chain) <= 6:
        raise ControlledResearchBlocked("public fetch redirect chain is empty or too long")
    normalized: list[str] = []
    for value in chain:
        normalized.append(
            urlunsplit(validate_public_https_url(value, allowed_hosts=allowed_hosts))
        )
    if len(set(normalized)) != len(normalized):
        raise ControlledResearchBlocked("public fetch redirect chain contains a loop")
    return tuple(normalized)


def detect_prompt_injection(
    content: str,
    *,
    binary: bool,
) -> tuple[PromptInjectionSignal, ...]:
    """Return risk labels only; content is never converted into tool commands."""

    if binary:
        return (PromptInjectionSignal.BINARY_CONTENT_NOT_TEXT_INSPECTED,)
    if not isinstance(content, str):
        raise ControlledResearchBlocked("untrusted Web content must be decoded text")
    lowered = content.casefold()[:2_000_000]
    signals: list[PromptInjectionSignal] = []
    patterns = (
        (
            PromptInjectionSignal.IGNORE_PRIOR_INSTRUCTIONS,
            ("ignore previous instructions", "ignore all prior", "忽略之前", "忽略上述", "无视之前"),
        ),
        (
            PromptInjectionSignal.SYSTEM_OR_DEVELOPER_ROLE_TEXT,
            ("system message", "developer message", "系统提示词", "开发者指令", "you are chatgpt"),
        ),
        (
            PromptInjectionSignal.TOOL_OR_COMMAND_REQUEST,
            ("tool_call", "function_call", "执行命令", "运行 shell", "调用工具", "打开终端"),
        ),
        (
            PromptInjectionSignal.CREDENTIAL_OR_SECRET_REQUEST,
            ("api key", "password", "access token", "读取密钥", "上传凭证", "发送密码"),
        ),
        (
            PromptInjectionSignal.MODEL_CONTROL_TOKEN,
            ("<|system|>", "<|assistant|>", "[inst]", "[/inst]"),
        ),
    )
    for signal, needles in patterns:
        if any(needle in lowered for needle in needles):
            signals.append(signal)
    return tuple(signals)


def _validate_external_request(
    request: ExternalRequestRef,
    *,
    expected_input_hash: str,
    now: datetime,
) -> None:
    _validate_external_request_without_clock(request, expected_input_hash=expected_input_hash)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ControlledResearchBlocked("external request clock must include a timezone")
    if request.expires_at <= now:
        raise ControlledResearchBlocked("external request authorization expired")


def _validate_external_request_without_clock(
    request: ExternalRequestRef,
    *,
    expected_input_hash: str,
) -> None:
    if not isinstance(request, ExternalRequestRef):
        raise ControlledResearchBlocked("external request reference is invalid")
    _require_uuid(request.request_id, "external request_id")
    _require_sha256(request.authorization_hash, "external authorization_hash")
    _require_uuid(request.egress_grant_id, "egress_grant_id")
    _require_sha256(request.egress_grant_hash, "egress_grant_hash")
    _require_code(request.provider_id, "provider_id")
    _require_code(request.service_id, "service_id")
    _require_sha256(request.input_hash, "external input_hash")
    _require_sha256(expected_input_hash, "expected external input_hash")
    if request.input_hash != expected_input_hash:
        raise ControlledResearchBlocked("external request is bound to another input")
    if request.status is not ExternalOperationStatus.AUTHORIZED:
        raise ControlledResearchBlocked("external request is not in an authorized pre-submit state")
    if request.expires_at.tzinfo is None or request.expires_at.utcoffset() is None:
        raise ControlledResearchBlocked("external request expiry must include a timezone")
    if not request.allowed_hosts or len(request.allowed_hosts) > 20:
        raise ControlledResearchBlocked("external request requires a bounded exact host allowlist")
    for host in request.allowed_hosts:
        _public_dns_host(host)


def _validate_attempt(attempt: ExternalAttemptRef, *, request: ExternalRequestRef) -> None:
    if not isinstance(attempt, ExternalAttemptRef):
        raise ControlledResearchBlocked("external attempt reference is invalid")
    _require_uuid(attempt.attempt_id, "external attempt_id")
    if attempt.request_id != request.request_id or attempt.input_hash != request.input_hash:
        raise ControlledResearchBlocked("external attempt differs from its authorization")
    if attempt.status is ExternalOperationStatus.AUTHORIZED:
        raise ControlledResearchBlocked("external attempt cannot have authorization-only status")
    if attempt.status in {
        ExternalOperationStatus.SUBMISSION_STARTED,
    }:
        _require_sha256(attempt.provider_request_ref_hash or "", "provider_request_ref_hash")
        if attempt.output_hash is not None:
            raise ControlledResearchBlocked("unresolved external attempt cannot have output bytes")
        if attempt.error_code is not None:
            raise ControlledResearchBlocked("submission-started attempt cannot have an error code")
    elif attempt.status is ExternalOperationStatus.SUCCEEDED:
        _require_sha256(attempt.output_hash or "", "external output_hash")
        if attempt.provider_request_ref_hash is not None or attempt.error_code is not None:
            raise ControlledResearchBlocked("successful external attempt requires only output bytes")
    elif attempt.status in {
        ExternalOperationStatus.FAILED,
        ExternalOperationStatus.UNKNOWN_SUBMISSION,
        ExternalOperationStatus.CANCELLED,
        ExternalOperationStatus.EXPIRED,
    }:
        _require_code(attempt.error_code or "", "external error_code")
        if attempt.provider_request_ref_hash is not None or attempt.output_hash is not None:
            raise ControlledResearchBlocked("unsuccessful external attempt requires only an error code")
    else:
        raise ControlledResearchBlocked("external attempt status is invalid")


def _validate_egress_receipt(
    receipt: EgressReceiptRef,
    *,
    request: ExternalRequestRef,
    fetched: FetchedPublicPage,
    content_hash: str,
) -> None:
    if not isinstance(receipt, EgressReceiptRef):
        raise ControlledResearchBlocked("egress receipt type is invalid")
    if (
        receipt.egress_grant_id != request.egress_grant_id
        or receipt.egress_grant_hash != request.egress_grant_hash
        or receipt.request_id != request.request_id
        or receipt.method != "GET"
        or receipt.request_count < 1
        or receipt.request_count > 6
        or receipt.response_bytes != len(fetched.body)
        or receipt.response_sha256 != content_hash
        or receipt.request_url != fetched.requested_url
        or receipt.redirect_chain != fetched.redirect_chain
    ):
        raise ControlledResearchBlocked("egress receipt differs from the exact official fetch")
    if not receipt.resolved_peer_ips or receipt.connected_peer_ip not in receipt.resolved_peer_ips:
        raise ControlledResearchBlocked("egress receipt lacks the connected peer resolution")
    for value in receipt.resolved_peer_ips:
        _global_ip(value)


def _validate_search_egress_receipt(
    receipt: EgressReceiptRef,
    *,
    request: ExternalRequestRef,
    response_bytes: int,
    output_hash: str,
) -> None:
    if not isinstance(receipt, EgressReceiptRef):
        raise ControlledResearchBlocked("search egress receipt type is invalid")
    if (
        receipt.egress_grant_id != request.egress_grant_id
        or receipt.egress_grant_hash != request.egress_grant_hash
        or receipt.request_id != request.request_id
        or receipt.method != "GET"
        or receipt.request_count != 1
        or receipt.redirect_chain
        or receipt.response_bytes != response_bytes
        or receipt.response_sha256 != output_hash
    ):
        raise ControlledResearchBlocked("search egress receipt differs from the exact provider call")
    parsed = validate_public_https_url(receipt.request_url, allowed_hosts=request.allowed_hosts)
    if parsed.hostname not in request.allowed_hosts:
        raise ControlledResearchBlocked("search egress receipt left its provider grant")
    if not receipt.resolved_peer_ips or receipt.connected_peer_ip not in receipt.resolved_peer_ips:
        raise ControlledResearchBlocked("search egress receipt lacks connected peer resolution")
    for value in receipt.resolved_peer_ips:
        _global_ip(value)


def _validate_metadata_candidate(
    value: PublicationMetadataCandidate,
    *,
    decoded_text: str | None,
) -> None:
    if not isinstance(value, PublicationMetadataCandidate):
        raise ControlledResearchBlocked("publication metadata candidate is invalid")
    if value.status is not MetadataCandidateStatus.NEEDS_REVIEW:
        raise ControlledResearchBlocked("publication metadata cannot bypass lawyer review")
    if value.published_on is None and value.effective_on is None and value.ceased_on is None:
        raise ControlledResearchBlocked("publication metadata candidate contains no date")
    _require_code(value.extraction_method, "metadata extraction_method")
    excerpt = _bounded_text(value.source_excerpt, "metadata source_excerpt", 1_000)
    if decoded_text is None or excerpt not in decoded_text:
        raise ControlledResearchBlocked(
            "publication metadata candidate is not bound to exact captured text"
        )


def _case_private_spans(value: str) -> set[str]:
    result: set[str] = set()
    for _, pattern in _SENSITIVE_PATTERNS:
        result.update(_normalized_secret(match.group(0)) for match in pattern.finditer(value))
    for match in _ROLE_NAME_RE.finditer(value):
        result.add(_normalized_secret(match.group(1)))
    for match in _LABEL_VALUE_RE.finditer(value):
        result.add(_normalized_secret(match.group(1)))
    for match in _QUOTED_PASSAGE_RE.finditer(value):
        result.add(_normalized_secret(match.group(1)))
    return {item for item in result if item}


def _reject_sensitive_public_text(value: str) -> None:
    for label, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(value):
            raise ControlledResearchBlocked(
                f"public research query contains blocked private data: {label}"
            )
    if _ROLE_NAME_RE.search(value) or _LABEL_VALUE_RE.search(value):
        raise ControlledResearchBlocked(
            "public research query contains case-party or account information"
        )
    if any(marker in value for marker in ('"', "'", "“", "”", "《原文》")) and len(value) > 30:
        raise ControlledResearchBlocked("public research query appears to contain source text")
    if len(value) >= 16 and any(marker in value for marker in ("，", "。", "！", "？", "；", "\n", "\r")):
        raise ControlledResearchBlocked("public research query appears to contain a source passage")


def _normalize_public_term(value: object) -> str:
    term = _bounded_text(value, "public research term", 60)
    term = " ".join(term.split())
    if len(term) < 2 or "\n" in term or "\r" in term:
        raise ControlledResearchBlocked("public research term is too short or multiline")
    if any(ord(character) < 32 for character in term):
        raise ControlledResearchBlocked("public research term contains control characters")
    return term


def _normalize_media_type(value: str) -> str:
    if not isinstance(value, str):
        raise ControlledResearchBlocked("official snapshot media type is invalid")
    result = value.split(";", 1)[0].strip().casefold()
    if _MEDIA_TYPE_RE.fullmatch(result) is None:
        raise ControlledResearchBlocked("official snapshot media type is invalid")
    return result


def _validate_content_shape(media_type: str, body: bytes) -> None:
    if media_type == "application/pdf" and not body.startswith(b"%PDF-"):
        raise ControlledResearchBlocked("official snapshot PDF signature is invalid")
    if media_type in {"text/html", "application/xhtml+xml"}:
        prefix = body[:8_192].lstrip().lower()
        if b"<html" not in prefix and b"<!doctype html" not in prefix:
            raise ControlledResearchBlocked("official snapshot HTML signature is invalid")
    if media_type == "application/json":
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlledResearchBlocked("official snapshot JSON is malformed") from error
    if media_type.endswith("spreadsheetml.sheet") and not body.startswith(b"PK"):
        raise ControlledResearchBlocked("official snapshot XLSX signature is invalid")


def _decode_text_content(media_type: str, body: bytes) -> str | None:
    if media_type not in {"text/html", "application/xhtml+xml", "application/json", "text/plain"}:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return body.decode("gb18030")
        except UnicodeDecodeError:
            raise ControlledResearchBlocked("official snapshot text encoding is unsupported") from None


def _metadata_payload(value: PublicationMetadataCandidate | None) -> object:
    if value is None:
        return None
    return {
        "published_on": value.published_on.isoformat() if value.published_on else None,
        "effective_on": value.effective_on.isoformat() if value.effective_on else None,
        "ceased_on": value.ceased_on.isoformat() if value.ceased_on else None,
        "source_excerpt": value.source_excerpt,
        "extraction_method": value.extraction_method,
        "status": value.status.value,
    }


def _public_dns_host(value: object) -> str:
    if not isinstance(value, str) or value != value.casefold() or value.endswith("."):
        raise ControlledResearchBlocked("public host must be canonical lowercase DNS")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ControlledResearchBlocked("public host must be ASCII DNS") from None
    try:
        ip_address(value)
    except ValueError:
        pass
    else:
        raise ControlledResearchBlocked("literal IP targets are not allowed")
    if _PUBLIC_DNS_RE.fullmatch(value) is None or value.endswith(
        (".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".test", ".invalid")
    ):
        raise ControlledResearchBlocked("public host is private, local or invalid")
    return value


def _global_ip(value: str) -> None:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise ControlledResearchBlocked("egress peer IP is invalid") from error
    if not address.is_global:
        raise ControlledResearchBlocked("egress peer IP is not globally routable")


def _optional_iso_date(value: object, label: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlledResearchBlocked(f"{label} must be an ISO date or null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ControlledResearchBlocked(f"{label} must be an ISO date or null") from error
    if parsed.isoformat() != value:
        raise ControlledResearchBlocked(f"{label} must be a canonical ISO date")
    return parsed


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ControlledResearchBlocked("untrusted Web JSON contains duplicate keys")
        result[key] = value
    return result


def _normalized_secret(value: str) -> str:
    return re.sub(r"[\s\-_—:：,，.。/\\()（）\[\]【】]", "", value.casefold())


def _bounded_text(
    value: object,
    label: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ControlledResearchBlocked(f"{label} is invalid")
    result = value.strip()
    if not allow_empty and not result:
        raise ControlledResearchBlocked(f"{label} is empty")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in result):
        raise ControlledResearchBlocked(f"{label} contains control characters")
    return result


def _item_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ITEM_ID_RE.fullmatch(value) is None:
        raise ControlledResearchBlocked(f"{label} is invalid")
    return value


def _require_uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ControlledResearchBlocked(f"{label} must be a UUID") from error


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ControlledResearchBlocked(f"{label} must be a lowercase SHA-256")


def _require_code(value: object, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise ControlledResearchBlocked(f"{label} must be a stable code")


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


_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CN_ID", re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")),
    ("CN_MOBILE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("CN_LANDLINE", re.compile(r"(?<!\d)(?:0\d{2,3}[- ]?)?\d{7,8}(?!\d)")),
    ("BANK_OR_ACCOUNT", re.compile(r"(?<!\d)\d{12,24}(?!\d)")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("CASE_NUMBER", re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fffA-Za-z0-9]{2,30}号")),
)
_ROLE_NAME_RE = re.compile(
    r"(?:原告|被告|上诉人|被上诉人|申请人|被申请人|借款人|出借人|当事人|联系人|姓名)\s*[：:]?\s*"
    r"([\u4e00-\u9fff]{2,4})(?=主张|提出|诉称|认为|请求|与|向|，|。|、|\s|$)"
)
_LABEL_VALUE_RE = re.compile(
    r"(?:身份证|手机号|电话|微信号|微信昵称|银行卡|银行账号|账户|账号)\s*[：:]?\s*([A-Za-z0-9_\-\u4e00-\u9fff]{2,40})"
)
_QUOTED_PASSAGE_RE = re.compile(r"[“\"]([^”\"]{8,200})[”\"]")
_PUBLIC_DNS_RE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


__all__ = (
    "AuthorityClass",
    "AuthorityClassification",
    "AuthoritySourceRegistration",
    "AuthoritySourceRegistry",
    "CONTROLLED_WEB_RESEARCH_MATURITY",
    "CONTROLLED_WEB_RESEARCH_SKILL_ID",
    "ControlledResearchBlocked",
    "EgressReceiptRef",
    "ExternalAttemptRef",
    "ExternalOperationStatus",
    "ExternalRequestRef",
    "FetchedPublicPage",
    "MetadataCandidateStatus",
    "MinimizedPublicQuery",
    "OfficialPageSnapshotCandidate",
    "OfficialSnapshotFetchSpec",
    "PromptInjectionSignal",
    "PublicSearchProviderInput",
    "PublicResearchLead",
    "PublicSearchResultSet",
    "PublicationMetadataCandidate",
    "ResearchLeadStatus",
    "ResearchPurpose",
    "SEARCH_RESULT_SCHEMA_VERSION",
    "SearchProvider",
    "SearchProviderRegistration",
    "SearchProviderRegistry",
    "SnapshotFetcher",
    "SnapshotReviewStatus",
    "build_official_page_snapshot_candidate",
    "build_official_snapshot_fetch_spec",
    "compile_public_search_provider_input",
    "detect_prompt_injection",
    "parse_public_search_results",
    "prepare_minimized_public_query",
    "require_external_submission_ready",
    "require_official_snapshot_fetch_ready",
    "require_public_search_ready",
    "validate_public_https_url",
    "validate_redirect_chain",
    "validate_successful_public_search_transport",
)
