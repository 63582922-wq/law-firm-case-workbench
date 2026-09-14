"""Brave Web Search adapter for controlled public research.

The adapter deliberately consumes only the ordinary Web Search endpoint.  It
does not use Brave Answers or LLM Context because search-provider prose cannot
become legal authority.  Results are converted to untrusted
``PublicResearchLead`` objects; an official page still needs a separately
authorized, byte-exact snapshot and lawyer/legal-ledger review.

Credentials, endpoint and API version are administrator-owned server
configuration.  The browser cannot choose any of them.  The transport is
injected because the production egress broker must persist SUBMISSION_STARTED,
pin DNS/peer IPs, enforce the exact host grant and produce an
``EgressReceiptRef``.  This module never opens a socket by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
import json
from typing import Mapping, Protocol
from urllib.parse import urlencode

from .controlled_web_research import (
    AuthoritySourceRegistry,
    ControlledResearchBlocked,
    EgressReceiptRef,
    ExternalAttemptRef,
    ExternalOperationStatus,
    ExternalRequestRef,
    MinimizedPublicQuery,
    PublicResearchLead,
    PublicSearchProviderInput,
    PublicSearchResultSet,
    ResearchLeadStatus,
    detect_prompt_injection,
    validate_public_https_url,
    validate_successful_public_search_transport,
)


BRAVE_SEARCH_PROVIDER_ID = "brave_web_search"
BRAVE_SEARCH_SERVICE_ID = "web_search_v1"
BRAVE_SEARCH_HOST = "api.search.brave.com"
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_API_VERSION = "2023-01-01"
MAX_BRAVE_RESPONSE_BYTES = 2 * 1024 * 1024


class BravePublicSearchBlocked(ControlledResearchBlocked):
    """The configured provider or its response violated the search boundary."""


@dataclass(frozen=True, repr=False)
class BraveSearchCredentials:
    subscription_token: str = field(repr=False)

    def __post_init__(self) -> None:
        value = self.subscription_token
        if (
            not isinstance(value, str)
            or not 20 <= len(value) <= 512
            or value != value.strip()
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Brave Search credential is invalid")

    def __repr__(self) -> str:
        return "BraveSearchCredentials(subscription_token=<redacted>)"


@dataclass(frozen=True)
class BraveSearchTransportRequest:
    endpoint: str
    method: str
    headers: Mapping[str, str] = field(repr=False, compare=False)
    body: bytes = field(repr=False, compare=False)
    max_response_bytes: int
    timeout_seconds: float
    request_hash: str


@dataclass(frozen=True)
class BraveSearchTransportResult:
    response_body: bytes = field(repr=False, compare=False)
    egress_receipt: EgressReceiptRef


class BraveSearchEgressTransport(Protocol):
    """One brokered HTTPS GET; the implementation owns DNS/TLS/receipts."""

    def send(
        self,
        *,
        request: BraveSearchTransportRequest,
        external_request: ExternalRequestRef,
    ) -> BraveSearchTransportResult: ...


class BravePublicSearchProvider:
    """Translate one exact Brave Web Search response into research leads."""

    provider_id = BRAVE_SEARCH_PROVIDER_ID

    def __init__(
        self,
        *,
        credentials: BraveSearchCredentials,
        transport: BraveSearchEgressTransport | None = None,
        authority_registry: AuthoritySourceRegistry | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if transport is not None and not callable(getattr(transport, "send", None)):
            raise ValueError("Brave Search egress transport is required")
        if not 1.0 <= timeout_seconds <= 60.0:
            raise ValueError("Brave Search timeout must be between 1 and 60 seconds")
        self._credentials = credentials
        self._transport = transport
        self._authority_registry = authority_registry or AuthoritySourceRegistry()
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return "BravePublicSearchProvider(<server-configured>)"

    def search(
        self,
        *,
        public_input: PublicSearchProviderInput,
        external_request: ExternalRequestRef,
    ) -> tuple[bytes, EgressReceiptRef]:
        request = self.prepare_request(
            public_input=public_input,
            external_request=external_request,
        )
        return self.submit_prepared(
            public_input=public_input,
            external_request=external_request,
            request=request,
        )

    def prepare_request(
        self,
        *,
        public_input: PublicSearchProviderInput,
        external_request: ExternalRequestRef,
    ) -> BraveSearchTransportRequest:
        """Compile exact bytes before the durable submission boundary.

        A network-capable Agent adapter must persist ``request_hash`` before
        calling :meth:`submit_prepared`.  Keeping compilation separate closes
        the unsafe gap where transport could start before the task ledger knew
        which request was being sent.
        """

        _validate_provider_binding(
            public_input=public_input,
            external_request=external_request,
        )
        return _compile_transport_request(
            public_input=public_input,
            credentials=self._credentials,
            timeout_seconds=self._timeout_seconds,
        )

    def submit_prepared(
        self,
        *,
        public_input: PublicSearchProviderInput,
        external_request: ExternalRequestRef,
        request: BraveSearchTransportRequest,
    ) -> tuple[bytes, EgressReceiptRef]:
        """Send only an exact request previously compiled by this provider."""

        _validate_provider_binding(
            public_input=public_input,
            external_request=external_request,
        )
        expected = _compile_transport_request(
            public_input=public_input,
            credentials=self._credentials,
            timeout_seconds=self._timeout_seconds,
        )
        if not _same_transport_request(request, expected):
            raise BravePublicSearchBlocked(
                "prepared Brave Search request differs from its exact provider input"
            )
        if self._transport is None:
            raise BravePublicSearchBlocked(
                "Brave Search transport is not configured; use a durable exchange"
            )
        result = self._transport.send(
            request=request,
            external_request=external_request,
        )
        return self.validate_prepared_result(
            public_input=public_input,
            external_request=external_request,
            request=request,
            result=result,
        )

    def validate_prepared_result(
        self,
        *,
        public_input: PublicSearchProviderInput,
        external_request: ExternalRequestRef,
        request: BraveSearchTransportRequest,
        result: BraveSearchTransportResult,
    ) -> tuple[bytes, EgressReceiptRef]:
        """Validate a result returned by a durable exchange.

        The case-Agent runtime uses a persistence-aware exchange rather than
        this provider's simple transport.  This method keeps exact request and
        response validation reusable without opening a second network path.
        """

        _validate_provider_binding(
            public_input=public_input,
            external_request=external_request,
        )
        expected = _compile_transport_request(
            public_input=public_input,
            credentials=self._credentials,
            timeout_seconds=self._timeout_seconds,
        )
        if not _same_transport_request(request, expected):
            raise BravePublicSearchBlocked(
                "prepared Brave Search request differs from its exact provider input"
            )
        if not isinstance(result, BraveSearchTransportResult):
            raise BravePublicSearchBlocked("Brave Search transport result is invalid")
        if (
            not isinstance(result.response_body, bytes)
            or not 2 <= len(result.response_body) <= MAX_BRAVE_RESPONSE_BYTES
        ):
            raise BravePublicSearchBlocked("Brave Search response size is invalid")
        return result.response_body, result.egress_receipt


def _same_transport_request(
    actual: object, expected: BraveSearchTransportRequest
) -> bool:
    return bool(
        isinstance(actual, BraveSearchTransportRequest)
        and actual.endpoint == expected.endpoint
        and actual.method == expected.method
        and dict(actual.headers) == dict(expected.headers)
        and actual.body == expected.body
        and actual.max_response_bytes == expected.max_response_bytes
        and actual.timeout_seconds == expected.timeout_seconds
        and actual.request_hash == expected.request_hash
    )


def parse_brave_public_search_results(
    raw: str | bytes,
    *,
    query: MinimizedPublicQuery,
    request: ExternalRequestRef,
    succeeded_attempt: ExternalAttemptRef,
    egress_receipt: EgressReceiptRef,
    max_results: int,
    authority_registry: AuthoritySourceRegistry | None = None,
) -> PublicSearchResultSet:
    """Normalize a durably successful provider response into untrusted leads."""

    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 20:
        raise BravePublicSearchBlocked("Brave Search result limit is invalid")
    encoded = validate_successful_public_search_transport(
        raw,
        query=query,
        request=request,
        succeeded_attempt=succeeded_attempt,
        egress_receipt=egress_receipt,
        provider_id=BRAVE_SEARCH_PROVIDER_ID,
    )
    leads = _parse_brave_response(
        encoded,
        authority_registry=authority_registry or AuthoritySourceRegistry(),
        maximum=max_results,
    )
    return PublicSearchResultSet(
        query_hash=query.query_hash,
        provider_id=BRAVE_SEARCH_PROVIDER_ID,
        external_request_id=request.request_id,
        external_attempt_id=succeeded_attempt.attempt_id,
        leads=leads,
        output_hash=sha256(encoded).hexdigest(),
    )


def _compile_transport_request(
    *,
    public_input: PublicSearchProviderInput,
    credentials: BraveSearchCredentials,
    timeout_seconds: float,
) -> BraveSearchTransportRequest:
    language = "zh-hans" if public_input.language == "zh-CN" else "en"
    ui_language = "zh-CN" if public_input.language == "zh-CN" else "en-US"
    # Brave's ordinary Web Search API is a GET endpoint.  Only the already
    # minimized public query is placed in its query string; the confidential
    # lawyer question, matter id and source text never enter this request.
    query_string = urlencode(
        {
            "q": public_input.query_text,
            "country": "CN" if public_input.language == "zh-CN" else "US",
            "search_lang": language,
            "ui_lang": ui_language,
            "count": min(public_input.max_results, 20),
            "offset": 0,
            "safesearch": "strict",
            "spellcheck": "false",
            "extra_snippets": "false",
        }
    )
    endpoint = f"{BRAVE_SEARCH_ENDPOINT}?{query_string}"
    request_hash = sha256(
        json.dumps(
            {
                "schema_version": "brave-web-search-request-v1",
                "endpoint": endpoint,
                "method": "GET",
                "api_version": BRAVE_API_VERSION,
                "max_response_bytes": MAX_BRAVE_RESPONSE_BYTES,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return BraveSearchTransportRequest(
        endpoint=endpoint,
        method="GET",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Api-Version": BRAVE_API_VERSION,
            "X-Subscription-Token": credentials.subscription_token,
        },
        body=b"",
        max_response_bytes=MAX_BRAVE_RESPONSE_BYTES,
        timeout_seconds=timeout_seconds,
        request_hash=request_hash,
    )


def _validate_provider_binding(
    *,
    public_input: PublicSearchProviderInput,
    external_request: ExternalRequestRef,
) -> None:
    if not isinstance(public_input, PublicSearchProviderInput):
        raise BravePublicSearchBlocked("Brave Search input is invalid")
    if (
        external_request.provider_id != BRAVE_SEARCH_PROVIDER_ID
        or external_request.service_id != BRAVE_SEARCH_SERVICE_ID
        or external_request.allowed_hosts != (BRAVE_SEARCH_HOST,)
        or external_request.status is not ExternalOperationStatus.AUTHORIZED
    ):
        raise BravePublicSearchBlocked("Brave Search authorization binding is invalid")


def _parse_brave_response(
    raw: bytes,
    *,
    authority_registry: AuthoritySourceRegistry,
    maximum: int,
) -> tuple[PublicResearchLead, ...]:
    try:
        payload = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BravePublicSearchBlocked("Brave Search response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise BravePublicSearchBlocked("Brave Search response must be one object")
    web = payload.get("web")
    if not isinstance(web, dict) or not isinstance(web.get("results"), list):
        raise BravePublicSearchBlocked("Brave Search response contains no Web results")
    values = web["results"]
    if not values:
        raise BravePublicSearchBlocked("Brave Search returned no Web results")
    leads: list[PublicResearchLead] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(values):
        if len(leads) >= maximum:
            break
        if not isinstance(item, dict):
            raise BravePublicSearchBlocked("Brave Search result item is invalid")
        allowed = {
            "title",
            "url",
            "description",
            "age",
            "page_age",
            "profile",
            "language",
            "family_friendly",
            "type",
            "subtype",
            "meta_url",
            "thumbnail",
            "extra_snippets",
            "deep_results",
            "article",
            "favicon",
            "is_source_local",
            "is_source_both",
            "cluster_type",
            "content_type",
            "location",
            "video",
            "qa",
            "rating",
            "product",
        }
        if not set(item).issubset(allowed):
            raise BravePublicSearchBlocked("Brave Search result contains unknown fields")
        title = _text(item.get("title"), "title", 500)
        description = _text(item.get("description", ""), "description", 4_000, allow_empty=True)
        parsed = validate_public_https_url(item.get("url"))
        url = parsed.geturl()
        if url in seen_urls:
            continue
        published = _published_date(item.get("page_age", item.get("age")))
        signals = detect_prompt_injection(f"{title}\n{description}", binary=False)
        authority = authority_registry.classify_url(url)
        lead_id = sha256(f"{index}:{url}".encode("utf-8")).hexdigest()[:32]
        leads.append(
            PublicResearchLead(
                lead_id=lead_id,
                title=title,
                url=url,
                snippet=description,
                published_on_candidate=published,
                authority=authority,
                prompt_injection_signals=signals,
                status=ResearchLeadStatus.PUBLIC_RESEARCH_LEAD,
            )
        )
        seen_urls.add(url)
    if not leads:
        raise BravePublicSearchBlocked("Brave Search produced no usable public leads")
    return tuple(leads)


def _published_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise BravePublicSearchBlocked("Brave Search publication time is invalid")
    candidate = value.strip()
    if len(candidate) >= 10:
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            return None
    return None


def _text(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise BravePublicSearchBlocked(f"Brave Search {label} is invalid")
    result = value.strip()
    if not allow_empty and not result:
        raise BravePublicSearchBlocked(f"Brave Search {label} is empty")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in result):
        raise BravePublicSearchBlocked(f"Brave Search {label} contains control characters")
    return result


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BravePublicSearchBlocked("Brave Search response contains duplicate keys")
        result[key] = value
    return result


__all__ = (
    "BRAVE_API_VERSION",
    "BRAVE_SEARCH_ENDPOINT",
    "BRAVE_SEARCH_HOST",
    "BRAVE_SEARCH_PROVIDER_ID",
    "BRAVE_SEARCH_SERVICE_ID",
    "BravePublicSearchBlocked",
    "BravePublicSearchProvider",
    "BraveSearchCredentials",
    "BraveSearchEgressTransport",
    "BraveSearchTransportRequest",
    "BraveSearchTransportResult",
    "parse_brave_public_search_results",
)
