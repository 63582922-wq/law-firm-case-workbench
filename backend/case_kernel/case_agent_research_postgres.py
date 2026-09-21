"""Production PostgreSQL/S3/HTTPS ports for controlled public research.

The browser and planner provide only opaque governed input references.  This
module resolves their current same-matter rows under FORCE RLS, derives a
private lawyer question plus a small server-owned legal vocabulary, and
persists one exact A3 binding.  The transport then consumes the already
durable 0031 submission marker, pins global DNS and the connected TLS peer,
forbids redirects, archives the exact response and receipt in private S3, and
only then returns bytes to the review-only adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import http.client
from ipaddress import ip_address
import json
import re
import socket
import ssl
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .brave_public_search import (
    BRAVE_API_VERSION,
    BRAVE_SEARCH_ENDPOINT,
    BRAVE_SEARCH_HOST,
    BRAVE_SEARCH_PROVIDER_ID,
    BRAVE_SEARCH_SERVICE_ID,
    BraveSearchTransportRequest,
    BraveSearchTransportResult,
    MAX_BRAVE_RESPONSE_BYTES,
)
from .case_agent_research_adapters import (
    AuthorizedPublicResearchBinding,
    CaseAgentResearchAdapterBlocked,
    RecoveredPublicSearch,
    RecoveredSearchStatus,
    authorized_public_research_query,
    authorized_public_research_source_binding_hash,
)
from .controlled_web_research import (
    EgressReceiptRef,
    ExternalOperationStatus,
    ExternalRequestRef,
    ResearchPurpose,
    validate_public_https_url,
)
from .public_legal_vocabulary import derive_public_legal_terms
from .models import Actor, Role
from .web_object_store import (
    S3CompatiblePrivateObjectStore,
    StoredCaseAgentResearchResponse,
    WebObjectStoreBlocked,
)


class CaseAgentResearchPersistenceBlocked(RuntimeError):
    """A research binding, external state or archive is not trustworthy."""


class PublicSearchNetworkBlocked(RuntimeError):
    """The exact HTTPS exchange could not produce a durable safe result."""


_REF = re.compile(
    r"^(issue|work-plan-item|legal-source):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")

class PinnedHttpsConnectionFactory(Protocol):
    def __call__(
        self, address: tuple[str, int], timeout: float, ssl_context: ssl.SSLContext,
        server_hostname: str,
    ) -> Any: ...


class PostgresPublicResearchBindingPort:
    """Resolve only current governed refs into one immutable private binding."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        clock: Callable[[], datetime] | None = None,
        authorization_ttl_seconds: int = 900,
        max_results: int = 20,
    ) -> None:
        _worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("public-research PostgreSQL DSN is required")
        if not 60 <= authorization_ttl_seconds <= 1800:
            raise ValueError("public-research authorization TTL is invalid")
        if not 1 <= max_results <= 20:
            raise ValueError("public-research result limit is invalid")
        self._dsn = dsn
        self._worker = worker_actor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = authorization_ttl_seconds
        self._max_results = max_results

    def resolve_public_research(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        task_input_hash: str,
        input_refs: tuple[str, ...],
    ) -> AuthorizedPublicResearchBinding:
        for value, label in (
            (run_id, "run_id"), (task_id, "task_id"), (attempt_id, "attempt_id")
        ):
            _uuid(value, label)
        _hash(task_input_hash, "task_input_hash")
        _input_refs(input_refs)
        now = self._clock()
        _aware(now, "research clock")
        with _transaction(
            self._dsn, self._worker, read_only=False, repeatable_read=True
        ) as connection:
            existing = _read_binding(
                connection,
                firm_id=self._worker.firm_id,
                attempt_id=attempt_id,
            )
            if existing is not None:
                result = _binding_from_row(existing)
                _require_call_binding(
                    result,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    task_input_hash=task_input_hash,
                    input_refs=input_refs,
                )
                attempt_status = str(existing["current_attempt_status"])
                if attempt_status == "RUNNING":
                    # A crash may leave an immutable binding behind before the
                    # 0031 submission boundary is written.  Reusing it for the
                    # first network send is safe only while the exact task,
                    # lawyer approval, Worker role and governed source rows are
                    # still current.  RECONCILING is deliberately different:
                    # the provider may already have received the request, so
                    # recovery must remain lookup-only even after authority is
                    # later revoked.
                    task = _read_research_task(
                        connection,
                        firm_id=self._worker.firm_id,
                        run_id=run_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        for_update=True,
                    )
                    if task is None:
                        raise CaseAgentResearchPersistenceBlocked(
                            "public-research task attempt is unavailable"
                        )
                    _require_research_task(
                        task,
                        task_input_hash=task_input_hash,
                        input_refs=input_refs,
                    )
                    matter_id = str(task["matter_id"])
                    if matter_id != result.matter_id:
                        raise CaseAgentResearchPersistenceBlocked(
                            "existing public-research binding changed matter"
                        )
                    _require_dedicated_worker(
                        connection, worker=self._worker, matter_id=matter_id
                    )
                    _require_current_lawyer_approval(
                        connection,
                        firm_id=self._worker.firm_id,
                        matter_id=matter_id,
                        approved_by=str(task["approved_by"]),
                    )
                    confidential_question, private_terms, question_id = (
                        _resolve_private_question(
                            connection,
                            firm_id=self._worker.firm_id,
                            matter_id=matter_id,
                            run_id=run_id,
                            graph_hash=str(task["graph_hash"]),
                            input_refs=input_refs,
                        )
                    )
                    if (
                        confidential_question != result.confidential_question
                        or private_terms != result.private_terms
                        or question_id != result.question_id
                        or _derive_public_terms(confidential_question)
                        != result.proposed_public_terms
                    ):
                        raise CaseAgentResearchPersistenceBlocked(
                            "existing public-research binding source is no longer current"
                        )
                elif attempt_status != "RECONCILING":
                    raise CaseAgentResearchPersistenceBlocked(
                        "existing public-research binding is not recoverable"
                    )
                return result

            task = _read_research_task(
                connection,
                firm_id=self._worker.firm_id,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                for_update=True,
            )
            if task is None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research task attempt is unavailable"
                )
            _require_research_task(task, task_input_hash=task_input_hash, input_refs=input_refs)
            matter_id = str(task["matter_id"])
            _require_dedicated_worker(
                connection, worker=self._worker, matter_id=matter_id
            )
            _require_current_lawyer_approval(
                connection,
                firm_id=self._worker.firm_id,
                matter_id=matter_id,
                approved_by=str(task["approved_by"]),
            )
            confidential_question, private_terms, question_id = _resolve_private_question(
                connection,
                firm_id=self._worker.firm_id,
                matter_id=matter_id,
                run_id=run_id,
                graph_hash=str(task["graph_hash"]),
                input_refs=input_refs,
            )
            public_terms = _derive_public_terms(confidential_question)
            external_request_id = str(uuid5(UUID(attempt_id), "public-research-request-v1"))
            egress_grant_id = str(uuid5(UUID(attempt_id), "public-research-egress-v1"))
            expires_at = now + timedelta(seconds=self._ttl)
            binding = AuthorizedPublicResearchBinding.build(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                task_input_hash=task_input_hash,
                matter_id=matter_id,
                question_id=question_id,
                input_refs=input_refs,
                confidential_question=confidential_question,
                proposed_public_terms=public_terms,
                private_terms=private_terms,
                purpose=ResearchPurpose.LEGAL_AUTHORITY_DISCOVERY,
                max_results=self._max_results,
                external_request_id=external_request_id,
                egress_grant_id=egress_grant_id,
                expires_at=expires_at,
            )
            query = authorized_public_research_query(binding)
            source_binding_hash = authorized_public_research_source_binding_hash(binding)
            connection.execute(
                """
                INSERT INTO case_agent_public_research_bindings (
                    binding_id, external_request_id, egress_grant_id,
                    run_id, graph_id, task_id, attempt_id, approval_id,
                    firm_id, matter_id, question_id, input_refs,
                    confidential_question, confidential_question_hash,
                    private_terms, private_terms_hash, public_terms,
                    public_query_text, query_hash, purpose, language, max_results,
                    task_input_hash, graph_hash, source_binding_hash,
                    authorization_hash, binding_hash, provider_id, service_id,
                    allowed_host, approved_by, created_by_worker,
                    expires_at, created_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'brave_web_search','web_search_v1','api.search.brave.com',
                    %s,%s,%s,%s
                )
                """,
                (
                    str(uuid5(UUID(attempt_id), "public-research-binding-v1")),
                    external_request_id, egress_grant_id, run_id,
                    str(task["graph_id"]), task_id, attempt_id,
                    str(task["external_approval_id"]), self._worker.firm_id,
                    matter_id, question_id, Jsonb(list(input_refs)),
                    confidential_question,
                    sha256(confidential_question.encode("utf-8")).hexdigest(),
                    Jsonb(list(private_terms)), _canonical_hash(private_terms),
                    Jsonb(list(public_terms)), query.query_text, query.query_hash,
                    binding.purpose.value, binding.language, binding.max_results,
                    task_input_hash, str(task["graph_hash"]), source_binding_hash,
                    binding.external_request.authorization_hash,
                    binding.binding_hash, str(task["approved_by"]),
                    self._worker.actor_id, expires_at, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO case_agent_public_research_egress_grants (
                    egress_grant_id, binding_id, external_request_id,
                    firm_id, matter_id, method, exact_host, exact_scheme,
                    exact_port, redirects_allowed, max_requests,
                    max_response_bytes, query_data_minimized, grant_hash,
                    issued_by_worker, expires_at, issued_at
                ) VALUES (
                    %s,%s,%s,%s,%s,'GET','api.search.brave.com','https',443,
                    false,1,2097152,true,%s,%s,%s,%s
                )
                """,
                (
                    egress_grant_id,
                    str(uuid5(UUID(attempt_id), "public-research-binding-v1")),
                    external_request_id, self._worker.firm_id, matter_id,
                    binding.external_request.egress_grant_hash,
                    self._worker.actor_id, expires_at, now,
                ),
            )
            return binding


class PinnedBraveHttpsTransport:
    """One raw HTTPS GET pinned to a globally routable DNS answer and peer."""

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        connection_factory: PinnedHttpsConnectionFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(resolver):
            raise ValueError("public-search DNS resolver is invalid")
        self._resolver = resolver
        self._connect = connection_factory or _open_pinned_tls_connection
        if not callable(self._connect):
            raise ValueError("public-search HTTPS connector is invalid")
        self._ssl = ssl_context or ssl.create_default_context()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def send(
        self,
        *,
        request: BraveSearchTransportRequest,
        external_request: ExternalRequestRef,
    ) -> BraveSearchTransportResult:
        _validate_exact_request(request, external_request)
        now = self._clock()
        _aware(now, "public-search transport clock")
        if external_request.expires_at <= now:
            raise PublicSearchNetworkBlocked(
                "public-search authorization expired before DNS resolution"
            )
        parsed = validate_public_https_url(
            request.endpoint, allowed_hosts=(BRAVE_SEARCH_HOST,)
        )
        if parsed.geturl() != request.endpoint:
            raise PublicSearchNetworkBlocked("public-search endpoint is not canonical")
        try:
            answers = self._resolver(
                BRAVE_SEARCH_HOST, 443, type=socket.SOCK_STREAM
            )
        except Exception as error:
            raise PublicSearchNetworkBlocked("public-search DNS resolution failed") from error
        resolved = tuple(
            sorted({str(answer[4][0]) for answer in answers if len(answer) >= 5})
        )
        if not resolved:
            raise PublicSearchNetworkBlocked("public-search DNS returned no address")
        for value in resolved:
            _global_ip(value)
        selected = resolved[0]
        connection = None
        try:
            connection = self._connect(
                (selected, 443), request.timeout_seconds, self._ssl,
                BRAVE_SEARCH_HOST,
            )
            peer = str(connection.getpeername()[0])
            _global_ip(peer)
            if peer not in resolved:
                raise PublicSearchNetworkBlocked(
                    "public-search connected peer differs from pinned DNS"
                )
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            headers = {
                "Host": BRAVE_SEARCH_HOST,
                "Connection": "close",
                **dict(request.headers),
            }
            _safe_headers(headers)
            raw_request = (
                f"GET {path} HTTP/1.1\r\n"
                + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
                + "\r\n"
            ).encode("ascii")
            connection.sendall(raw_request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            if 300 <= response.status < 400:
                raise PublicSearchNetworkBlocked("public-search redirects are forbidden")
            if response.status != 200:
                raise PublicSearchNetworkBlocked("public-search HTTP status is not successful")
            content_encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
            if content_encoding != "identity":
                raise PublicSearchNetworkBlocked(
                    "public-search content encoding must be identity"
                )
            content_length = response.getheader("Content-Length")
            if content_length is not None and int(content_length) > request.max_response_bytes:
                raise PublicSearchNetworkBlocked("public-search response exceeds its byte grant")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise PublicSearchNetworkBlocked("public-search response is not JSON")
            body = response.read(request.max_response_bytes + 1)
            if not isinstance(body, bytes) or not 2 <= len(body) <= request.max_response_bytes:
                raise PublicSearchNetworkBlocked("public-search response size is invalid")
            response_hash = sha256(body).hexdigest()
            receipt = EgressReceiptRef(
                egress_grant_id=external_request.egress_grant_id,
                egress_grant_hash=external_request.egress_grant_hash,
                request_id=external_request.request_id,
                request_url=request.endpoint,
                redirect_chain=(),
                resolved_peer_ips=resolved,
                connected_peer_ip=peer,
                method="GET",
                request_count=1,
                response_bytes=len(body),
                response_sha256=response_hash,
            )
            return BraveSearchTransportResult(body, receipt)
        except PublicSearchNetworkBlocked:
            raise
        except Exception as error:
            raise PublicSearchNetworkBlocked(
                "public-search HTTPS outcome is indeterminate"
            ) from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class PostgresDurablePublicSearchExchange:
    """Persist STARTED -> HTTPS -> S3 -> DB, and reconcile without resend."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        object_store: S3CompatiblePrivateObjectStore,
        transport: PinnedBraveHttpsTransport,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("public-research PostgreSQL DSN is required")
        if not isinstance(object_store, S3CompatiblePrivateObjectStore):
            raise ValueError("public-research private object store is required")
        if not callable(getattr(transport, "send", None)):
            raise ValueError("public-research pinned transport is required")
        self._dsn = dsn
        self._worker = worker_actor
        self._objects = object_store
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def send(
        self,
        *,
        request: BraveSearchTransportRequest,
        external_request: ExternalRequestRef,
    ) -> BraveSearchTransportResult:
        _validate_exact_request(request, external_request)
        started_at = self._clock()
        _aware(started_at, "research exchange clock")
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            binding = _read_exchange_binding(
                connection,
                firm_id=self._worker.firm_id,
                external_request_id=external_request.request_id,
                for_update=True,
            )
            if binding is None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research binding is unavailable"
                )
            _require_external_request(binding, external_request)
            if binding["expires_at"] <= started_at:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research authorization expired before transport"
                )
            prior = connection.execute(
                """
                SELECT exchange_id FROM case_agent_public_research_exchanges
                WHERE firm_id = %s AND external_request_id = %s
                """,
                (self._worker.firm_id, external_request.request_id),
            ).fetchone()
            if prior is not None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research request already crossed its one-call boundary"
                )
            submission = connection.execute(
                """
                SELECT submission_id, request_hash, destination,
                       submission_state, recorded_by
                FROM case_agent_external_submissions
                WHERE firm_id = %s AND matter_id = %s AND run_id = %s
                  AND attempt_id = %s AND external_request_id = %s
                """,
                (
                    self._worker.firm_id, str(binding["matter_id"]),
                    str(binding["run_id"]), str(binding["attempt_id"]),
                    external_request.request_id,
                ),
            ).fetchone()
            if (
                submission is None
                or submission["request_hash"] != request.request_hash
                or submission["destination"] != BRAVE_SEARCH_HOST
                or submission["submission_state"] != "STARTED"
                or str(submission["recorded_by"]) != self._worker.actor_id
            ):
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research 0031 submission boundary is absent or differs"
                )
            exchange_id = str(
                uuid5(UUID(external_request.request_id), "public-research-exchange-v1")
            )
            connection.execute(
                """
                INSERT INTO case_agent_public_research_exchanges (
                    exchange_id, binding_id, external_request_id,
                    egress_grant_id, run_id, task_id, attempt_id, firm_id,
                    matter_id, request_hash, request_url, method,
                    request_body_bytes, submission_record_id,
                    started_by_worker, started_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'GET',0,%s,%s,%s)
                """,
                (
                    exchange_id, str(binding["binding_id"]),
                    external_request.request_id, external_request.egress_grant_id,
                    str(binding["run_id"]), str(binding["task_id"]),
                    str(binding["attempt_id"]), self._worker.firm_id,
                    str(binding["matter_id"]), request.request_hash,
                    request.endpoint, str(submission["submission_id"]),
                    self._worker.actor_id, started_at,
                ),
            )

        try:
            result = self._transport.send(
                request=request, external_request=external_request
            )
            _validate_transport_result(request, external_request, result)
            receipt_payload = _receipt_payload(result.egress_receipt)
            stored = self._objects.put_case_agent_research_response(
                result.response_body,
                receipt_payload=receipt_payload,
                firm_id=self._worker.firm_id,
                matter_id=str(binding["matter_id"]),
                external_request_id=external_request.request_id,
                request_hash=request.request_hash,
            )
            self._record_success(
                exchange_id=exchange_id,
                binding=binding,
                request_hash=request.request_hash,
                stored=stored,
                receipt_payload=receipt_payload,
                recovered=False,
            )
            return result
        except Exception as error:
            # A response may have reached us even if TLS read, S3 or the final
            # DB commit failed.  Record UNKNOWN when possible.  Never convert
            # this path into FAILED or a second send.
            try:
                self._record_unknown(
                    exchange_id=exchange_id,
                    binding=binding,
                    request_hash=request.request_hash,
                )
            except Exception:
                pass
            if isinstance(error, (CaseAgentResearchPersistenceBlocked, PublicSearchNetworkBlocked)):
                raise
            raise CaseAgentResearchPersistenceBlocked(
                "public-research outcome is indeterminate and requires recovery"
            ) from error

    def recover(
        self,
        *,
        external_request_id: str,
        request_hash: str,
    ) -> RecoveredPublicSearch:
        _uuid(external_request_id, "external_request_id")
        _hash(request_hash, "request_hash")
        with _transaction(self._dsn, self._worker, read_only=True) as connection:
            binding = _read_exchange_binding(
                connection,
                firm_id=self._worker.firm_id,
                external_request_id=external_request_id,
                for_update=False,
            )
            exchange = connection.execute(
                """
                SELECT exchange_id, request_hash FROM case_agent_public_research_exchanges
                WHERE firm_id = %s AND external_request_id = %s
                """,
                (self._worker.firm_id, external_request_id),
            ).fetchone()
            if binding is None or exchange is None or exchange["request_hash"] != request_hash:
                return RecoveredPublicSearch(RecoveredSearchStatus.UNRESOLVED)
            outcome = connection.execute(
                """
                SELECT * FROM case_agent_public_research_outcomes
                WHERE firm_id = %s AND external_request_id = %s
                ORDER BY outcome_sequence DESC LIMIT 1
                """,
                (self._worker.firm_id, external_request_id),
            ).fetchone()
        if outcome is not None and outcome["status"] == "FAILED":
            return RecoveredPublicSearch(
                RecoveredSearchStatus.FAILED, error_code=str(outcome["error_code"])
            )
        if outcome is not None and outcome["status"] == "SUCCEEDED":
            return self._recover_success(binding=binding, outcome=outcome)

        # UNKNOWN or a missing final commit can only inspect the deterministic
        # S3 key.  This path never calls the network transport.
        try:
            stored, body, receipt_payload = (
                self._objects.recover_case_agent_research_response(
                    firm_id=self._worker.firm_id,
                    matter_id=str(binding["matter_id"]),
                    external_request_id=external_request_id,
                    request_hash=request_hash,
                )
            )
            receipt = _receipt_from_payload(receipt_payload)
            _validate_recovered_receipt(
                binding=binding,
                request_hash=request_hash,
                body=body,
                receipt=receipt,
            )
            if outcome is None:
                sequence = 1
                recovered = False
            else:
                sequence = 2
                recovered = True
            self._record_success(
                exchange_id=str(exchange["exchange_id"]),
                binding=binding,
                request_hash=request_hash,
                stored=stored,
                receipt_payload=receipt_payload,
                recovered=recovered,
                sequence=sequence,
            )
            return RecoveredPublicSearch(
                RecoveredSearchStatus.SUCCEEDED,
                response_body=body,
                egress_receipt=receipt,
            )
        except Exception:
            return RecoveredPublicSearch(RecoveredSearchStatus.UNRESOLVED)

    def _recover_success(
        self, *, binding: Mapping[str, Any], outcome: Mapping[str, Any]
    ) -> RecoveredPublicSearch:
        stored = _stored_from_outcome(outcome)
        body, receipt_payload = self._objects.read_case_agent_research_response(
            stored,
            firm_id=self._worker.firm_id,
            matter_id=str(binding["matter_id"]),
            external_request_id=str(binding["external_request_id"]),
        )
        receipt = _receipt_from_payload(receipt_payload)
        _validate_recovered_receipt(
            binding=binding,
            request_hash=str(outcome["request_hash"]),
            body=body,
            receipt=receipt,
        )
        return RecoveredPublicSearch(
            RecoveredSearchStatus.SUCCEEDED,
            response_body=body,
            egress_receipt=receipt,
        )

    def _record_success(
        self,
        *,
        exchange_id: str,
        binding: Mapping[str, Any],
        request_hash: str,
        stored: StoredCaseAgentResearchResponse,
        receipt_payload: dict[str, object],
        recovered: bool,
        sequence: int = 1,
    ) -> None:
        receipt_hash = _canonical_hash(receipt_payload)
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_public_research_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, outcome_sequence, status, request_hash,
                    response_sha256, response_bytes, archive_sha256,
                    archive_bytes, archive_object_key, archive_object_version_id,
                    egress_receipt, egress_receipt_hash, error_code,
                    recovered_from_unknown, recorded_by_worker, recorded_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,'SUCCEEDED',%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,NULL,%s,%s,%s
                ) ON CONFLICT (exchange_id, outcome_sequence) DO NOTHING
                """,
                (
                    str(uuid5(UUID(exchange_id), f"research-outcome:{sequence}")),
                    exchange_id, str(binding["external_request_id"]),
                    self._worker.firm_id, str(binding["matter_id"]), sequence,
                    request_hash, stored.response_sha256, stored.response_bytes,
                    stored.archive_sha256, stored.archive_bytes, stored.object_key,
                    stored.object_version_id, Jsonb(receipt_payload), receipt_hash,
                    recovered, self._worker.actor_id, self._clock(),
                ),
            )
            row = connection.execute(
                """
                SELECT status, response_sha256, request_hash
                FROM case_agent_public_research_outcomes
                WHERE exchange_id = %s AND outcome_sequence = %s
                """,
                (exchange_id, sequence),
            ).fetchone()
            if row is None or row["status"] != "SUCCEEDED" or (
                row["response_sha256"] != stored.response_sha256
                or row["request_hash"] != request_hash
            ):
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research success outcome could not be proven"
                )

    def _record_unknown(
        self, *, exchange_id: str, binding: Mapping[str, Any], request_hash: str
    ) -> None:
        with _transaction(self._dsn, self._worker, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO case_agent_public_research_outcomes (
                    outcome_id, exchange_id, external_request_id, firm_id,
                    matter_id, outcome_sequence, status, request_hash,
                    error_code, recovered_from_unknown, recorded_by_worker,
                    recorded_at
                ) VALUES (%s,%s,%s,%s,%s,1,'UNKNOWN_SUBMISSION',%s,
                          'PUBLIC_SEARCH_OUTCOME_UNKNOWN',false,%s,%s)
                ON CONFLICT (exchange_id, outcome_sequence) DO NOTHING
                """,
                (
                    str(uuid5(UUID(exchange_id), "research-outcome:1")),
                    exchange_id, str(binding["external_request_id"]),
                    self._worker.firm_id, str(binding["matter_id"]),
                    request_hash, self._worker.actor_id, self._clock(),
                ),
            )


def preflight_case_agent_research_runtime_contract(
    *, dsn: str, worker_actor: Actor
) -> None:
    """Fail startup for an enabled research adapter unless 0037 is complete."""

    _worker(worker_actor)
    required = {
        "case_agent_public_research_bindings": {
            "binding_id", "external_request_id", "attempt_id", "approval_id",
            "confidential_question", "private_terms", "public_terms",
            "query_hash", "source_binding_hash", "authorization_hash",
            "binding_hash", "created_by_worker", "expires_at",
        },
        "case_agent_public_research_egress_grants": {
            "egress_grant_id", "binding_id", "external_request_id",
            "method", "exact_host", "exact_scheme", "exact_port",
            "redirects_allowed", "max_requests", "max_response_bytes",
            "query_data_minimized", "grant_hash", "issued_by_worker",
        },
        "case_agent_public_research_exchanges": {
            "exchange_id", "binding_id", "external_request_id",
            "egress_grant_id", "attempt_id", "submission_record_id",
            "request_hash", "request_url", "method", "request_body_bytes",
            "started_by_worker",
        },
        "case_agent_public_research_outcomes": {
            "outcome_id", "exchange_id", "external_request_id", "status",
            "request_hash", "response_sha256", "archive_sha256",
            "archive_object_key", "egress_receipt", "egress_receipt_hash",
            "outcome_sequence", "recovered_from_unknown",
        },
    }
    triggers = {
        "case_agent_public_research_binding_guard",
        "case_agent_public_research_egress_grant_guard",
        "case_agent_public_research_exchange_guard",
        "case_agent_public_research_outcome_guard",
    }
    try:
        with _transaction(dsn, worker_actor, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            observed: dict[str, set[str]] = {}
            for row in rows:
                observed.setdefault(str(row["table_name"]), set()).add(
                    str(row["column_name"])
                )
            if any(not columns.issubset(observed.get(table, set())) for table, columns in required.items()):
                raise CaseAgentResearchPersistenceBlocked(
                    "case-Agent public research migration 0037 is incomplete"
                )
            guard_rows = connection.execute(
                """
                SELECT trigger_name FROM information_schema.triggers
                WHERE trigger_schema = 'public' AND trigger_name = ANY(%s)
                """,
                (list(triggers),),
            ).fetchall()
            if not triggers.issubset({str(row["trigger_name"]) for row in guard_rows}):
                raise CaseAgentResearchPersistenceBlocked(
                    "case-Agent public research guards are incomplete"
                )
            rls_rows = connection.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_catalog.pg_class JOIN pg_catalog.pg_namespace
                  ON pg_namespace.oid = pg_class.relnamespace
                WHERE pg_namespace.nspname = 'public' AND relname = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
            rls = {
                str(row["relname"]): (
                    bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])
                ) for row in rls_rows
            }
            if any(rls.get(table) != (True, True) for table in required):
                raise CaseAgentResearchPersistenceBlocked(
                    "case-Agent public research requires FORCE RLS"
                )
    except CaseAgentResearchPersistenceBlocked:
        raise
    except Exception as error:
        raise CaseAgentResearchPersistenceBlocked(
            "case-Agent public research preflight failed"
        ) from error


def _resolve_private_question(
    connection: Any,
    *,
    firm_id: str,
    matter_id: str,
    run_id: str,
    graph_hash: str,
    input_refs: tuple[str, ...],
) -> tuple[str, tuple[str, ...], str]:
    chunks: list[str] = []
    private_terms: set[str] = set()
    question_id: str | None = None
    for ref in input_refs:
        match = _REF.fullmatch(ref)
        assert match is not None
        kind, identifier = match.groups()
        if question_id is None:
            question_id = identifier
        if kind == "issue":
            row = connection.execute(
                """
                SELECT question FROM case_dispute_issues
                WHERE issue_id = %s AND firm_id = %s AND matter_id = %s
                  AND status = 'CONFIRMED'
                """,
                (identifier, firm_id, matter_id),
            ).fetchone()
            if row is None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research issue is not current and confirmed"
                )
            chunks.append(str(row["question"]))
        elif kind == "work-plan-item":
            row = connection.execute(
                """
                SELECT item.title, item.rationale, item.risk_if_omitted
                FROM case_work_plan_items item
                JOIN case_work_plans plan ON plan.plan_id = item.plan_id
                  AND plan.firm_id = item.firm_id AND plan.matter_id = item.matter_id
                JOIN matters matter ON matter.matter_id = plan.matter_id
                  AND matter.firm_id = plan.firm_id
                WHERE item.item_id = %s AND item.firm_id = %s
                  AND item.matter_id = %s AND plan.status = 'ACTIVE'
                  AND plan.activated_matter_version = matter.version
                  AND (item.item_kind = 'RESEARCH_TASK' OR item.readiness = 'NEEDS_RESEARCH')
                """,
                (identifier, firm_id, matter_id),
            ).fetchone()
            if row is None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research work-plan item is not current"
                )
            chunks.extend(str(row[key]) for key in ("title", "rationale", "risk_if_omitted"))
        else:
            row = connection.execute(
                """
                SELECT source.publisher, source.authority_level,
                       signal.signal_id, signal.summary
                FROM official_legal_source_snapshots source
                JOIN case_agent_lawyer_decision_signals signal
                  ON signal.run_id = %s AND signal.firm_id = %s
                 AND signal.matter_id = %s AND signal.category = 'LEGAL_GAP'
                 AND signal.is_current AND signal.superseded_at IS NULL
                 AND signal.graph_hash = %s
                 AND signal.source_ref_ids ? %s
                WHERE source.snapshot_id = %s AND source.firm_id = %s
                  AND source.verification_status = 'VERIFIED'
                  AND source.license_status = 'ACTIVE'
                  AND source.license_review_hash IS NOT NULL
                ORDER BY signal.decided_at DESC LIMIT 1
                """,
                (run_id, firm_id, matter_id, graph_hash, ref, identifier, firm_id),
            ).fetchone()
            if row is None:
                raise CaseAgentResearchPersistenceBlocked(
                    "public-research legal source lacks a current legal gap"
                )
            chunks.append(str(row["summary"]))
            question_id = str(row["signal_id"])
    question = "\n".join(value.strip() for value in chunks if value.strip())
    if not 1 <= len(question) <= 20_000:
        raise CaseAgentResearchPersistenceBlocked(
            "public-research private question is empty or oversized"
        )
    for value in re.findall(r"[“‘\"']([^\"'”’]{1,100})[”’\"']", question):
        private_terms.add(value.strip())
    for value in re.findall(r"(?:原告|被告|当事人|借款人|出借人|昵称)[：:\s]*([\w\-一-鿿]{2,40})", question):
        private_terms.add(value.strip())
    return question, tuple(sorted(item for item in private_terms if item)), question_id or str(uuid4())


def _derive_public_terms(question: str) -> tuple[str, ...]:
    try:
        return derive_public_legal_terms((question,))
    except ValueError as error:
        raise CaseAgentResearchPersistenceBlocked(
            "public-research question has no safe public legal vocabulary"
        ) from error


def _require_research_task(
    row: Mapping[str, Any], *, task_input_hash: str, input_refs: tuple[str, ...]
) -> None:
    budget = row.get("resource_budget")
    if (
        str(row["input_hash"]) != task_input_hash
        or tuple(row["input_refs"]) != input_refs
        or str(row["skill_id"]) != "controlled_web_search"
        or str(row["tool_id"]) != "search_public_web"
        or str(row["execution_mode"]) != "NETWORK_CONNECTOR"
        or str(row["network_policy"]) != "EXACT_ALLOWLIST"
        or tuple(row["allowed_domains"]) != (BRAVE_SEARCH_HOST,)
        or str(row["autonomy_level"]) != "A3_LAWYER_APPROVAL"
        or str(row["approval_gate"]) != "LAWYER_REVIEW"
        or str(row["retry_mode"]) != "NEVER_AUTOMATIC"
        or row["external_request_approval_required"] is not True
        or not isinstance(budget, dict)
        or budget.get("max_external_calls") != 1
        or budget.get("max_attempts") != 1
        or str(row["attempt_status"]) not in {"RUNNING", "RECONCILING"}
        or row["external_approval_id"] is None
        or str(row["approval_kind"]) != "TASK"
        or str(row["approval_input_hash"]) != task_input_hash
        or str(row["approval_graph_hash"]) != str(row["graph_hash"])
        or str(row["approval_gate_value"]) != "LAWYER_REVIEW"
        or str(row["current_graph_id"]) != str(row["graph_id"])
        or str(row["current_graph_hash"]) != str(row["graph_hash"])
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research task is not the current exact A3 approved task"
        )


def _require_dedicated_worker(connection: Any, *, worker: Actor, matter_id: str) -> None:
    rows = connection.execute(
        """
        SELECT role.role FROM matter_actor_roles role
        JOIN users principal ON principal.user_id = role.user_id
          AND principal.firm_id = role.firm_id
        WHERE role.firm_id = %s AND role.matter_id = %s AND role.user_id = %s
          AND role.revoked_at IS NULL AND principal.status = 'ACTIVE'
        """,
        (worker.firm_id, matter_id, worker.actor_id),
    ).fetchall()
    if {str(row["role"]) for row in rows} != {"SYSTEM_WORKER"}:
        raise PermissionError("public research requires a dedicated matter Worker")


def _require_current_lawyer_approval(
    connection: Any, *, firm_id: str, matter_id: str, approved_by: str
) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM users lawyer JOIN matter_actor_roles role
          ON role.user_id = lawyer.user_id AND role.firm_id = lawyer.firm_id
        WHERE lawyer.user_id = %s AND lawyer.firm_id = %s
          AND lawyer.status = 'ACTIVE' AND role.matter_id = %s
          AND role.role IN ('LEAD_LAWYER', 'REVIEWER')
          AND role.revoked_at IS NULL
        """,
        (approved_by, firm_id, matter_id),
    ).fetchone()
    if row is None:
        raise PermissionError("public-research approval is no longer authorized")


def _read_research_task(
    connection: Any,
    *,
    firm_id: str,
    run_id: str,
    task_id: str,
    attempt_id: str,
    for_update: bool,
) -> Mapping[str, Any] | None:
    suffix = " FOR UPDATE OF attempt" if for_update else ""
    return connection.execute(
        """
        SELECT task.graph_id, task.run_id, task.matter_id, task.input_refs,
               task.input_hash, task.skill_id, task.tool_id,
               task.execution_mode, task.network_policy,
               task.allowed_domains, task.autonomy_level,
               task.approval_gate, task.retry_mode,
               task.external_request_approval_required,
               task.resource_budget, graph.graph_hash,
               run.current_graph_id, run.current_graph_hash,
               attempt.status AS attempt_status,
               attempt.external_approval_id, approval.approved_by,
               approval.approval_kind,
               approval.task_input_hash AS approval_input_hash,
               approval.graph_hash AS approval_graph_hash,
               approval.gate AS approval_gate_value
        FROM case_agent_task_attempts attempt
        JOIN case_agent_tasks task
          ON task.graph_id = attempt.graph_id AND task.task_id = attempt.task_id
         AND task.run_id = attempt.run_id AND task.firm_id = attempt.firm_id
         AND task.matter_id = attempt.matter_id
        JOIN case_agent_task_graphs graph
          ON graph.graph_id = task.graph_id AND graph.run_id = task.run_id
         AND graph.firm_id = task.firm_id AND graph.matter_id = task.matter_id
        JOIN case_agent_runs run
          ON run.run_id = task.run_id AND run.firm_id = task.firm_id
         AND run.matter_id = task.matter_id
        LEFT JOIN case_agent_approvals approval
          ON approval.approval_id = attempt.external_approval_id
         AND approval.run_id = attempt.run_id
         AND approval.task_id = attempt.task_id
         AND approval.firm_id = attempt.firm_id
         AND approval.matter_id = attempt.matter_id
        WHERE attempt.attempt_id = %s AND attempt.run_id = %s
          AND attempt.task_id = %s AND attempt.firm_id = %s
        """ + suffix,
        (attempt_id, run_id, task_id, firm_id),
    ).fetchone()


def _read_binding(connection: Any, *, firm_id: str, attempt_id: str) -> Mapping[str, Any] | None:
    return connection.execute(
        """
        SELECT binding.*, grant.grant_hash,
               attempt.status AS current_attempt_status
        FROM case_agent_public_research_bindings binding
        JOIN case_agent_public_research_egress_grants grant
          ON grant.binding_id = binding.binding_id
         AND grant.external_request_id = binding.external_request_id
         AND grant.firm_id = binding.firm_id AND grant.matter_id = binding.matter_id
        JOIN case_agent_task_attempts attempt
          ON attempt.attempt_id = binding.attempt_id
         AND attempt.run_id = binding.run_id
         AND attempt.task_id = binding.task_id
         AND attempt.firm_id = binding.firm_id
         AND attempt.matter_id = binding.matter_id
        WHERE binding.firm_id = %s AND binding.attempt_id = %s
        """,
        (firm_id, attempt_id),
    ).fetchone()


def _read_exchange_binding(
    connection: Any,
    *,
    firm_id: str,
    external_request_id: str,
    for_update: bool,
) -> Mapping[str, Any] | None:
    suffix = " FOR UPDATE OF binding" if for_update else ""
    return connection.execute(
        """
        SELECT binding.*, grant.grant_hash
        FROM case_agent_public_research_bindings binding
        JOIN case_agent_public_research_egress_grants grant
          ON grant.binding_id = binding.binding_id
         AND grant.external_request_id = binding.external_request_id
         AND grant.firm_id = binding.firm_id AND grant.matter_id = binding.matter_id
        WHERE binding.firm_id = %s AND binding.external_request_id = %s
        """ + suffix,
        (firm_id, external_request_id),
    ).fetchone()


def _binding_from_row(row: Mapping[str, Any]) -> AuthorizedPublicResearchBinding:
    external = ExternalRequestRef(
        request_id=str(row["external_request_id"]),
        authorization_hash=str(row["authorization_hash"]),
        egress_grant_id=str(row["egress_grant_id"]),
        egress_grant_hash=str(row["grant_hash"]),
        provider_id=str(row["provider_id"]),
        service_id=str(row["service_id"]),
        input_hash=str(row["query_hash"]),
        allowed_hosts=(str(row["allowed_host"]),),
        expires_at=row["expires_at"],
        status=ExternalOperationStatus.AUTHORIZED,
    )
    binding = AuthorizedPublicResearchBinding(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        attempt_id=str(row["attempt_id"]),
        task_input_hash=str(row["task_input_hash"]),
        matter_id=str(row["matter_id"]),
        question_id=str(row["question_id"]),
        input_refs=tuple(row["input_refs"]),
        confidential_question=str(row["confidential_question"]),
        proposed_public_terms=tuple(row["public_terms"]),
        private_terms=tuple(row["private_terms"]),
        purpose=ResearchPurpose(str(row["purpose"])),
        language=str(row["language"]),
        max_results=int(row["max_results"]),
        external_request=external,
        binding_hash=str(row["binding_hash"]),
    )
    binding.validate()
    return binding


def _require_call_binding(binding: AuthorizedPublicResearchBinding, **values: object) -> None:
    if (
        binding.run_id != values["run_id"]
        or binding.task_id != values["task_id"]
        or binding.attempt_id != values["attempt_id"]
        or binding.task_input_hash != values["task_input_hash"]
        or binding.input_refs != values["input_refs"]
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "existing public-research binding differs from task"
        )


def _require_external_request(
    row: Mapping[str, Any], external: ExternalRequestRef
) -> None:
    binding = _binding_from_row(row)
    if binding.external_request != external:
        raise CaseAgentResearchPersistenceBlocked(
            "public-research external request differs from its binding"
        )


def _validate_exact_request(
    request: BraveSearchTransportRequest, external: ExternalRequestRef
) -> None:
    if (
        not isinstance(request, BraveSearchTransportRequest)
        or not isinstance(external, ExternalRequestRef)
        or request.method != "GET"
        or request.body != b""
        or request.max_response_bytes != MAX_BRAVE_RESPONSE_BYTES
        or not 1 <= request.timeout_seconds <= 60
        or _SHA256.fullmatch(request.request_hash) is None
        or request.request_hash != _brave_request_hash(request.endpoint)
        or not request.endpoint.startswith(BRAVE_SEARCH_ENDPOINT + "?")
        or external.provider_id != BRAVE_SEARCH_PROVIDER_ID
        or external.service_id != BRAVE_SEARCH_SERVICE_ID
        or external.allowed_hosts != (BRAVE_SEARCH_HOST,)
        or external.status is not ExternalOperationStatus.AUTHORIZED
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research prepared request is not exact"
        )
    validate_public_https_url(request.endpoint, allowed_hosts=(BRAVE_SEARCH_HOST,))
    headers = dict(request.headers)
    if (
        set(headers)
        != {
            "Accept", "Accept-Encoding", "Api-Version", "X-Subscription-Token"
        }
        or headers["Accept"] != "application/json"
        or headers["Accept-Encoding"] != "identity"
        or headers["Api-Version"] != BRAVE_API_VERSION
        or not 20 <= len(headers["X-Subscription-Token"]) <= 512
        or headers["X-Subscription-Token"] != headers["X-Subscription-Token"].strip()
        or any(character.isspace() for character in headers["X-Subscription-Token"])
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research prepared request headers are not exact"
        )


def _validate_transport_result(
    request: BraveSearchTransportRequest,
    external: ExternalRequestRef,
    result: BraveSearchTransportResult,
) -> None:
    if not isinstance(result, BraveSearchTransportResult):
        raise CaseAgentResearchPersistenceBlocked("public-research transport result is invalid")
    receipt = result.egress_receipt
    if (
        not isinstance(result.response_body, bytes)
        or not 2 <= len(result.response_body) <= request.max_response_bytes
        or receipt.request_id != external.request_id
        or receipt.egress_grant_id != external.egress_grant_id
        or receipt.egress_grant_hash != external.egress_grant_hash
        or receipt.request_url != request.endpoint
        or receipt.redirect_chain
        or receipt.method != "GET"
        or receipt.request_count != 1
        or receipt.response_bytes != len(result.response_body)
        or receipt.response_sha256 != sha256(result.response_body).hexdigest()
        or receipt.connected_peer_ip not in receipt.resolved_peer_ips
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research transport receipt differs from exact bytes"
        )
    for value in receipt.resolved_peer_ips:
        _global_ip(value)


def _validate_recovered_receipt(
    *,
    binding: Mapping[str, Any],
    request_hash: str,
    body: bytes,
    receipt: EgressReceiptRef,
) -> None:
    external = _binding_from_row(binding).external_request
    if (
        receipt.request_id != external.request_id
        or receipt.egress_grant_id != external.egress_grant_id
        or receipt.egress_grant_hash != external.egress_grant_hash
        or receipt.response_bytes != len(body)
        or receipt.response_sha256 != sha256(body).hexdigest()
        or receipt.request_count != 1
        or receipt.method != "GET"
        or receipt.redirect_chain
        or _brave_request_hash(receipt.request_url) != request_hash
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "recovered public-research receipt differs"
        )
    validate_public_https_url(receipt.request_url, allowed_hosts=(BRAVE_SEARCH_HOST,))
    if receipt.connected_peer_ip not in receipt.resolved_peer_ips:
        raise CaseAgentResearchPersistenceBlocked(
            "recovered public-research peer is not DNS-bound"
        )
    for value in receipt.resolved_peer_ips:
        _global_ip(value)


def _brave_request_hash(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research request endpoint is invalid"
        )
    return _canonical_hash(
        {
            "schema_version": "brave-web-search-request-v1",
            "endpoint": endpoint,
            "method": "GET",
            "api_version": BRAVE_API_VERSION,
            "max_response_bytes": MAX_BRAVE_RESPONSE_BYTES,
        }
    )


def _receipt_payload(receipt: EgressReceiptRef) -> dict[str, object]:
    return {
        "schema_version": "case-agent-public-search-egress-receipt-v1",
        "egress_grant_id": receipt.egress_grant_id,
        "egress_grant_hash": receipt.egress_grant_hash,
        "request_id": receipt.request_id,
        "request_url": receipt.request_url,
        "redirect_chain": list(receipt.redirect_chain),
        "resolved_peer_ips": list(receipt.resolved_peer_ips),
        "connected_peer_ip": receipt.connected_peer_ip,
        "method": receipt.method,
        "request_count": receipt.request_count,
        "response_bytes": receipt.response_bytes,
        "response_sha256": receipt.response_sha256,
    }


def _receipt_from_payload(value: Mapping[str, object]) -> EgressReceiptRef:
    expected = {
        "schema_version", "egress_grant_id", "egress_grant_hash", "request_id",
        "request_url", "redirect_chain", "resolved_peer_ips", "connected_peer_ip",
        "method", "request_count", "response_bytes", "response_sha256",
    }
    if set(value) != expected or value["schema_version"] != "case-agent-public-search-egress-receipt-v1":
        raise CaseAgentResearchPersistenceBlocked("research receipt schema is invalid")
    return EgressReceiptRef(
        egress_grant_id=str(value["egress_grant_id"]),
        egress_grant_hash=str(value["egress_grant_hash"]),
        request_id=str(value["request_id"]),
        request_url=str(value["request_url"]),
        redirect_chain=tuple(value["redirect_chain"]),  # type: ignore[arg-type]
        resolved_peer_ips=tuple(value["resolved_peer_ips"]),  # type: ignore[arg-type]
        connected_peer_ip=str(value["connected_peer_ip"]),
        method=str(value["method"]),
        request_count=int(value["request_count"]),
        response_bytes=int(value["response_bytes"]),
        response_sha256=str(value["response_sha256"]),
    )


def _stored_from_outcome(row: Mapping[str, Any]) -> StoredCaseAgentResearchResponse:
    return StoredCaseAgentResearchResponse(
        object_key=str(row["archive_object_key"]),
        request_hash=str(row["request_hash"]),
        response_sha256=str(row["response_sha256"]),
        response_bytes=int(row["response_bytes"]),
        archive_sha256=str(row["archive_sha256"]),
        archive_bytes=int(row["archive_bytes"]),
        object_version_id=(
            str(row["archive_object_version_id"])
            if row.get("archive_object_version_id") is not None
            else None
        ),
    )


def _open_pinned_tls_connection(
    address: tuple[str, int],
    timeout: float,
    ssl_context: ssl.SSLContext,
    server_hostname: str,
) -> Any:
    raw = socket.create_connection(address, timeout=timeout)
    try:
        return ssl_context.wrap_socket(raw, server_hostname=server_hostname)
    except Exception:
        raw.close()
        raise


def _safe_headers(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if (
            not isinstance(name, str) or not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,100}", name) is None
            or "\r" in value or "\n" in value or len(value) > 1024
        ):
            raise PublicSearchNetworkBlocked("public-search header is unsafe")


@contextmanager
def _transaction(
    dsn: str, worker: Actor, *, read_only: bool, repeatable_read: bool = False
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        if read_only:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        elif repeatable_read:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)", (worker.firm_id,)
        )
        connection.execute(
            "SELECT set_config('app.actor_id', %s, true)", (worker.actor_id,)
        )
        yield connection


def _worker(actor: Actor) -> None:
    if not isinstance(actor, Actor) or actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("public research requires a dedicated SYSTEM_WORKER")
    _uuid(actor.actor_id, "worker actor_id")
    _uuid(actor.firm_id, "worker firm_id")


def _input_refs(values: tuple[str, ...]) -> None:
    if (
        not isinstance(values, tuple) or not 1 <= len(values) <= 100
        or len(set(values)) != len(values)
        or any(not isinstance(value, str) or _REF.fullmatch(value) is None for value in values)
    ):
        raise CaseAgentResearchPersistenceBlocked(
            "public-research input refs are invalid"
        )


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseAgentResearchPersistenceBlocked(f"{label} must be a UUID") from error


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CaseAgentResearchPersistenceBlocked(f"{label} must be a SHA-256 digest")


def _aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CaseAgentResearchPersistenceBlocked(f"{label} must include a timezone")


def _global_ip(value: str) -> None:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise PublicSearchNetworkBlocked("public-search DNS or peer IP is invalid") from error
    if not address.is_global:
        raise PublicSearchNetworkBlocked("public-search DNS or peer IP is not global")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = (
    "CaseAgentResearchPersistenceBlocked",
    "PinnedBraveHttpsTransport",
    "PostgresDurablePublicSearchExchange",
    "PostgresPublicResearchBindingPort",
    "PublicSearchNetworkBlocked",
    "preflight_case_agent_research_runtime_contract",
)
