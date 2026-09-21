"""Pinned Qwen lawyer-analysis transport and lookup-only recovery.

The managed Worker writes ``case_agent_external_submissions`` before calling
this module.  The exchange re-verifies that exact durable marker and the
compiled task policy, performs at most one pinned HTTPS request, archives the
complete provider response in private object storage, and returns bytes only
after archive verification.  Reconciliation can only inspect the deterministic
archive key; it has no path to the network transport.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import http.client
from ipaddress import ip_address
import json
import logging
import re
import socket
import ssl
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .case_agent_lawyer_analysis import (
    LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
    LAWYER_ANALYSIS_MAX_RESPONSE_BYTES,
    LAWYER_ANALYSIS_MODEL_ID,
    PreparedLawyerAnalysisRequest,
    qwen_lawyer_analysis_host,
)
from .case_agent_lawyer_analysis_adapters import (
    CaseAgentLawyerAnalysisAdapterBlocked,
    LAWYER_ANALYSIS_BINDING_REJECTED,
    LAWYER_ANALYSIS_BINDING_UNAVAILABLE,
    LAWYER_ANALYSIS_CONNECT_FAILED,
    LAWYER_ANALYSIS_DNS_FAILED,
    LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED,
    LAWYER_ANALYSIS_SKILL_ID,
    LAWYER_ANALYSIS_TOOL_ID,
    LAWYER_ANALYSIS_UNKNOWN_ERROR_CODES,
    LawyerAnalysisRecoveryBinding,
    LawyerAnalysisKnownFailure,
    QWEN_LAWYER_ANALYSIS_MANIFEST,
    RecoveredLawyerAnalysis,
)
from .models import Actor, Role
from .web_object_store import S3CompatiblePrivateObjectStore, WebObjectStoreBlocked


LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA = (
    "case-agent-lawyer-analysis-transport-receipt-v1"
)
LAWYER_ANALYSIS_ENDPOINT_PATH = "/compatible-mode/v1/chat/completions"
# The provider call has a single hard cap.  The task budget is deliberately
# longer so the response-envelope validation, private archive write and
# durable receipt commit cannot overrun a just-finished provider call.
LAWYER_ANALYSIS_TIMEOUT_SECONDS = 300.0
LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS = 360
_LEGACY_LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS = 180

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_RESPONSE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,500}$")
_UNKNOWN_CODES = LAWYER_ANALYSIS_UNKNOWN_ERROR_CODES
_HTTP_ERROR_DIAGNOSTIC_LIMIT = 8192
_logger = logging.getLogger(__name__)


def _safe_http_failure_diagnostic(body: bytes) -> dict[str, object]:
    """Log a fixed category and digest only; provider prose may echo case data or keys."""
    sample = body[:_HTTP_ERROR_DIAGNOSTIC_LIMIT]
    category = "UNCLASSIFIED"
    try:
        value = json.loads(sample)
        error = value.get("error", value) if isinstance(value, dict) else {}
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            if code in {"invalid_api_key", "InvalidApiKey", "InvalidApiKeyError"}:
                category = "CREDENTIAL_REJECTED"
            elif code in {"insufficient_quota", "Arrearage"}:
                category = "QUOTA_REJECTED"
            elif isinstance(message, str) and any(term in message.lower() for term in ("json schema", "json_schema", "response_format")):
                category = "SCHEMA_REJECTION_INDICATED"
    except (ValueError, UnicodeError):
        pass
    return {"category": category, "sample_sha256": sha256(sample).hexdigest(),
            "sample_bytes": len(sample), "truncated": len(body) > _HTTP_ERROR_DIAGNOSTIC_LIMIT}


def _safe_transport_exception_diagnostic(error: Exception) -> str:
    """Return a fixed transport category without recording provider or case text."""
    if isinstance(error, socket.timeout):
        return "SOCKET_TIMEOUT"
    if isinstance(error, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(error, http.client.RemoteDisconnected):
        return "REMOTE_DISCONNECTED"
    if isinstance(error, http.client.BadStatusLine):
        return "BAD_STATUS_LINE"
    if isinstance(error, http.client.HTTPException):
        return "HTTP_PROTOCOL_ERROR"
    if isinstance(error, OSError):
        return "SOCKET_ERROR"
    return "UNCLASSIFIED"


class LawyerAnalysisTransportBlocked(CaseAgentLawyerAnalysisAdapterBlocked):
    """The exact transport or durable task binding is unsafe."""


class LawyerAnalysisUnknownSubmission(LawyerAnalysisTransportBlocked):
    """The request may have reached Qwen and must never be resent."""

    def __init__(self, *, external_request_id: str, error_code: str) -> None:
        _uuid(external_request_id, "lawyer-analysis external request id")
        if error_code not in _UNKNOWN_CODES:
            raise ValueError("lawyer-analysis unknown error code is invalid")
        self.external_request_id = external_request_id
        self.error_code = error_code
        super().__init__("lawyer-analysis outcome is indeterminate")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(external_request_id=<bound>, "
            "error_code=<controlled>)"
        )


@dataclass(frozen=True, repr=False)
class QwenLawyerAnalysisCredentials:
    api_key: str = field(repr=False)
    workspace_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or self.api_key != self.api_key.strip()
            or not 20 <= len(self.api_key) <= 512
            or any(character.isspace() for character in self.api_key)
        ):
            raise ValueError("Qwen lawyer-analysis credential is invalid")
        qwen_lawyer_analysis_host(self.workspace_id)

    @property
    def endpoint_host(self) -> str:
        return qwen_lawyer_analysis_host(self.workspace_id)

    def __repr__(self) -> str:
        return (
            "QwenLawyerAnalysisCredentials(api_key=<redacted>, "
            "workspace_id=<configured>)"
        )


@dataclass(frozen=True)
class LawyerAnalysisTransportResult:
    external_request_id: str
    request_hash: str
    response_body: bytes = field(repr=False, compare=False)
    transport_receipt: Mapping[str, object]


@dataclass(frozen=True)
class _LawyerAnalysisRequestIdentity:
    run_id: str
    task_id: str
    attempt_id: str
    firm_id: str
    matter_id: str
    task_input_hash: str
    input_refs: tuple[str, ...]
    external_request_id: str
    endpoint_host: str
    request_hash: str


class PinnedHttpsConnectionFactory(Protocol):
    def __call__(
        self,
        address: tuple[str, int],
        timeout: float,
        ssl_context: ssl.SSLContext,
        server_hostname: str,
    ) -> Any: ...


class LawyerAnalysisNetworkTransport(Protocol):
    def send(
        self, *, request: PreparedLawyerAnalysisRequest
    ) -> LawyerAnalysisTransportResult: ...


class PinnedQwenLawyerAnalysisHttpsTransport:
    """Perform one raw POST to one workspace host without redirects."""

    def __init__(
        self,
        *,
        credentials: QwenLawyerAnalysisCredentials,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        connection_factory: PinnedHttpsConnectionFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
        timeout_seconds: float = LAWYER_ANALYSIS_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(credentials, QwenLawyerAnalysisCredentials):
            raise ValueError("Qwen lawyer-analysis credentials are required")
        if not callable(resolver):
            raise ValueError("Qwen lawyer-analysis DNS resolver is invalid")
        if not 1.0 <= timeout_seconds <= LAWYER_ANALYSIS_TIMEOUT_SECONDS:
            raise ValueError("Qwen lawyer-analysis timeout is invalid")
        self._credentials = credentials
        self._resolver = resolver
        self._connect = connection_factory or _open_pinned_tls_connection
        if not callable(self._connect):
            raise ValueError("Qwen lawyer-analysis HTTPS connector is invalid")
        self._ssl = ssl_context or ssl.create_default_context()
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return "PinnedQwenLawyerAnalysisHttpsTransport(<server-configured>)"

    def send(
        self, *, request: PreparedLawyerAnalysisRequest
    ) -> LawyerAnalysisTransportResult:
        _validate_prepared_request(
            request, endpoint_host=self._credentials.endpoint_host
        )
        try:
            answers = self._resolver(
                request.endpoint_host, 443, type=socket.SOCK_STREAM
            )
            resolved = tuple(
                sorted(
                    {
                        str(answer[4][0])
                        for answer in answers
                        if isinstance(answer, tuple) and len(answer) >= 5
                    }
                )
            )
            if not resolved:
                raise ValueError("DNS returned no address")
            for value in resolved:
                _global_ip(value)
        except Exception as error:
            raise LawyerAnalysisKnownFailure(
                LAWYER_ANALYSIS_DNS_FAILED
            ) from error

        connection = None
        try:
            connect_error: Exception | None = None
            for candidate in resolved:
                candidate_connection = None
                try:
                    candidate_connection = self._connect(
                        (candidate, 443),
                        self._timeout,
                        self._ssl,
                        request.endpoint_host,
                    )
                    peer = str(candidate_connection.getpeername()[0])
                    _global_ip(peer)
                    if peer != candidate:
                        raise ValueError("connected peer differs from pinned address")
                except Exception as error:
                    connect_error = error
                    if candidate_connection is not None:
                        try:
                            candidate_connection.close()
                        except Exception:
                            pass
                    continue
                connection = candidate_connection
                break
            if connection is None:
                # Every candidate failed before an HTTP request byte was
                # written.  This remains a known zero-call outcome rather than
                # an indeterminate provider submission.
                raise LawyerAnalysisKnownFailure(
                    LAWYER_ANALYSIS_CONNECT_FAILED
                ) from connect_error

            headers = {
                "Host": request.endpoint_host,
                "Authorization": f"Bearer {self._credentials.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Content-Length": str(len(request.body)),
            }
            _safe_headers(headers)
            raw_head = (
                f"POST {LAWYER_ANALYSIS_ENDPOINT_PATH} HTTP/1.1\r\n"
                + "".join(
                    f"{name}: {value}\r\n" for name, value in headers.items()
                )
                + "\r\n"
            ).encode("ascii")
            try:
                connection.sendall(raw_head + request.body)
            except Exception as error:
                raise LawyerAnalysisUnknownSubmission(
                    external_request_id=request.external_request_id,
                    error_code="LAWYER_ANALYSIS_UNKNOWN_SEND",
                ) from error
            try:
                response = http.client.HTTPResponse(connection)
                response.begin()
            except Exception as error:
                _logger.warning(
                    "lawyer analysis response-head failure phase=%s request_hash=%s category=%s",
                    "response_head",
                    request.request_hash,
                    _safe_transport_exception_diagnostic(error),
                )
                raise LawyerAnalysisUnknownSubmission(
                    external_request_id=request.external_request_id,
                    error_code="LAWYER_ANALYSIS_UNKNOWN_RESPONSE_HEAD",
                ) from error
            if 300 <= response.status < 400:
                raise LawyerAnalysisKnownFailure("LAWYER_ANALYSIS_REDIRECT_FORBIDDEN")
            if response.status != 200:
                # A known HTTP rejection stays known even if its diagnostic body cannot be read.
                diagnostic = {"category": "BODY_UNAVAILABLE"}
                try:
                    diagnostic = _safe_http_failure_diagnostic(response.read(_HTTP_ERROR_DIAGNOSTIC_LIMIT + 1))
                except Exception:
                    pass
                _logger.warning("lawyer analysis HTTP rejection status=%s request_hash=%s diagnostic=%s",
                    response.status, request.request_hash, json.dumps(diagnostic, sort_keys=True))
                raise LawyerAnalysisKnownFailure(
                    f"LAWYER_ANALYSIS_HTTP_{response.status}"
                )
            content_type = (
                response.getheader("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise LawyerAnalysisKnownFailure(
                    "LAWYER_ANALYSIS_RESPONSE_CONTENT_TYPE"
                )
            encoding = (
                response.getheader("Content-Encoding") or "identity"
            ).strip().lower()
            if encoding != "identity":
                raise LawyerAnalysisKnownFailure(
                    "LAWYER_ANALYSIS_RESPONSE_ENCODING"
                )
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except (TypeError, ValueError):
                    raise LawyerAnalysisKnownFailure(
                        "LAWYER_ANALYSIS_RESPONSE_LENGTH"
                    ) from None
                if not 2 <= declared_bytes <= LAWYER_ANALYSIS_MAX_RESPONSE_BYTES:
                    raise LawyerAnalysisKnownFailure(
                        "LAWYER_ANALYSIS_RESPONSE_TOO_LARGE"
                    )
            try:
                body = response.read(LAWYER_ANALYSIS_MAX_RESPONSE_BYTES + 1)
            except Exception as error:
                raise LawyerAnalysisUnknownSubmission(
                    external_request_id=request.external_request_id,
                    error_code="LAWYER_ANALYSIS_UNKNOWN_RESPONSE_BODY",
                ) from error
            provider_response_id = _validate_provider_envelope(body)
            receipt = {
                "schema_version": LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA,
                "external_request_id": request.external_request_id,
                "request_hash": request.request_hash,
                "endpoint_host_hash": sha256(
                    request.endpoint_host.encode("ascii")
                ).hexdigest(),
                "method": "POST",
                "redirect_chain": [],
                "resolved_peer_ips": list(resolved),
                "connected_peer_ip": peer,
                "response_sha256": sha256(body).hexdigest(),
                "response_bytes": len(body),
                "provider_response_id_hash": sha256(
                    provider_response_id.encode("utf-8")
                ).hexdigest(),
                "model_id": LAWYER_ANALYSIS_MODEL_ID,
            }
            _validate_transport_receipt(
                receipt,
                request=request,
                response_body=body,
            )
            return LawyerAnalysisTransportResult(
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
                response_body=body,
                transport_receipt=receipt,
            )
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class PostgresBoundRecoverableLawyerAnalysisExchange:
    """Consume one durable submission marker and archive before returning."""

    def __init__(
        self,
        *,
        dsn: str,
        worker_actor: Actor,
        credentials: QwenLawyerAnalysisCredentials,
        object_store: S3CompatiblePrivateObjectStore,
        transport: LawyerAnalysisNetworkTransport | None = None,
    ) -> None:
        _worker(worker_actor)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("lawyer-analysis PostgreSQL DSN is required")
        if not isinstance(credentials, QwenLawyerAnalysisCredentials):
            raise ValueError("lawyer-analysis credentials are required")
        if not all(
            callable(getattr(object_store, method, None))
            for method in (
                "put_case_agent_lawyer_analysis_response",
                "recover_case_agent_lawyer_analysis_response",
            )
        ):
            raise ValueError("lawyer-analysis private object store is required")
        selected_transport = transport or PinnedQwenLawyerAnalysisHttpsTransport(
            credentials=credentials
        )
        if not callable(getattr(selected_transport, "send", None)):
            raise ValueError("lawyer-analysis transport is required")
        self._dsn = dsn
        self._worker = worker_actor
        self._credentials = credentials
        self._objects = object_store
        self._transport = selected_transport

    @property
    def endpoint_host(self) -> str:
        return self._credentials.endpoint_host

    def __repr__(self) -> str:
        return (
            "PostgresBoundRecoverableLawyerAnalysisExchange"
            "(<server-configured>)"
        )

    def send(self, *, request: PreparedLawyerAnalysisRequest) -> bytes:
        _validate_prepared_request(request, endpoint_host=self.endpoint_host)
        try:
            binding = _load_bound_submission(
                dsn=self._dsn,
                worker=self._worker,
                external_request_id=request.external_request_id,
            )
        except Exception as error:
            raise LawyerAnalysisKnownFailure(
                LAWYER_ANALYSIS_BINDING_UNAVAILABLE
            ) from error
        if binding is None:
            raise LawyerAnalysisKnownFailure(
                LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED
            )
        try:
            _require_bound_submission(
                binding,
                request=request,
                recovering=False,
                worker_actor_id=self._worker.actor_id,
                expected_lease_owner=f"case-agent-worker:{self._worker.firm_id}",
            )
        except LawyerAnalysisTransportBlocked as error:
            raise LawyerAnalysisKnownFailure(
                LAWYER_ANALYSIS_BINDING_REJECTED
            ) from error
        result = self._transport.send(request=request)
        if (
            not isinstance(result, LawyerAnalysisTransportResult)
            or result.external_request_id != request.external_request_id
            or result.request_hash != request.request_hash
        ):
            raise LawyerAnalysisUnknownSubmission(
                external_request_id=request.external_request_id,
                error_code="LAWYER_ANALYSIS_UNKNOWN_ARCHIVE",
            )
        _validate_transport_receipt(
            result.transport_receipt,
            request=request,
            response_body=result.response_body,
        )
        try:
            self._objects.put_case_agent_lawyer_analysis_response(
                result.response_body,
                receipt_payload=dict(result.transport_receipt),
                firm_id=self._worker.firm_id,
                matter_id=request.matter_id,
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
            )
        except WebObjectStoreBlocked as error:
            raise LawyerAnalysisUnknownSubmission(
                external_request_id=request.external_request_id,
                error_code="LAWYER_ANALYSIS_UNKNOWN_ARCHIVE",
            ) from error
        return result.response_body

    def recover(
        self, *, external_request_id: str, request_hash: str
    ) -> RecoveredLawyerAnalysis:
        _uuid(external_request_id, "recovery external request id")
        _hash(request_hash, "recovery request hash")
        binding = _load_bound_submission(
            dsn=self._dsn,
            worker=self._worker,
            external_request_id=external_request_id,
        )
        if binding is None:
            return RecoveredLawyerAnalysis("UNRESOLVED")
        request = _bound_recovery_request(
            binding,
            external_request_id=external_request_id,
            request_hash=request_hash,
            endpoint_host=self.endpoint_host,
            worker_actor_id=self._worker.actor_id,
            expected_lease_owner=f"case-agent-worker:{self._worker.firm_id}",
        )
        return self._recover_archived_response(request=request)

    def recover_bound(
        self, *, binding: LawyerAnalysisRecoveryBinding
    ) -> RecoveredLawyerAnalysis:
        """Recover from the original durable boundary without rebuilding a prompt.

        ``binding`` is constructed from the immutable task claim.  The request
        hash comes only from the already-recorded submission row, which makes
        recovery safe even when application code has changed how it renders
        the same case context since the original call.
        """

        if not isinstance(binding, LawyerAnalysisRecoveryBinding):
            raise LawyerAnalysisTransportBlocked(
                "lawyer-analysis recovery binding is invalid"
            )
        binding.validate()
        stored = _load_bound_submission(
            dsn=self._dsn,
            worker=self._worker,
            external_request_id=binding.external_request_id,
        )
        if stored is None:
            return RecoveredLawyerAnalysis("UNRESOLVED")
        request_hash = stored.get("request_hash")
        _hash(request_hash, "durable recovery request hash")
        request = _bound_recovery_request(
            stored,
            external_request_id=binding.external_request_id,
            request_hash=request_hash,
            endpoint_host=self.endpoint_host,
            worker_actor_id=self._worker.actor_id,
            expected_lease_owner=f"case-agent-worker:{self._worker.firm_id}",
        )
        if (
            request.run_id != binding.run_id
            or request.task_id != binding.task_id
            or request.attempt_id != binding.attempt_id
            or request.task_input_hash != binding.task_input_hash
            or request.input_refs != binding.input_refs
        ):
            raise LawyerAnalysisTransportBlocked(
                "lawyer-analysis durable recovery identity differs"
            )
        return self._recover_archived_response(request=request)

    def _recover_archived_response(
        self, *, request: _LawyerAnalysisRequestIdentity
    ) -> RecoveredLawyerAnalysis:
        try:
            recovered = self._objects.recover_case_agent_lawyer_analysis_response(
                firm_id=self._worker.firm_id,
                matter_id=request.matter_id,
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
            )
        except WebObjectStoreBlocked as error:
            raise LawyerAnalysisTransportBlocked(
                "lawyer-analysis recovery archive failed authentication"
            ) from error
        if recovered is None:
            return RecoveredLawyerAnalysis(
                "UNRESOLVED", request_hash=request.request_hash
            )
        _stored, body, receipt = recovered
        _validate_transport_receipt(receipt, request=request, response_body=body)
        return RecoveredLawyerAnalysis(
            "SUCCEEDED", response_body=body, request_hash=request.request_hash
        )


def _request_identity_from_binding(
    binding: Mapping[str, Any],
    *,
    external_request_id: str,
    request_hash: str,
    endpoint_host: str,
) -> _LawyerAnalysisRequestIdentity:
    """Build only the fields needed to authenticate lookup-only recovery."""

    # The provider request body is deliberately absent from PostgreSQL.  This
    # identity carries only immutable fields needed to authenticate an S3
    # lookup and has no ``body`` field, so it cannot reach the send transport.
    return _LawyerAnalysisRequestIdentity(
        run_id=str(binding.get("run_id", "")),
        task_id=str(binding.get("task_id", "")),
        attempt_id=str(binding.get("attempt_id", "")),
        firm_id=str(binding.get("firm_id", "")),
        matter_id=str(binding.get("matter_id", "")),
        task_input_hash=str(binding.get("input_hash", "")),
        input_refs=tuple(binding.get("input_refs") or ()),
        external_request_id=external_request_id,
        endpoint_host=endpoint_host,
        request_hash=request_hash,
    )


def _bound_recovery_request(
    binding: Mapping[str, Any],
    *,
    external_request_id: str,
    request_hash: str,
    endpoint_host: str,
    worker_actor_id: str,
    expected_lease_owner: str,
) -> _LawyerAnalysisRequestIdentity:
    request = _request_identity_from_binding(
        binding,
        external_request_id=external_request_id,
        request_hash=request_hash,
        endpoint_host=endpoint_host,
    )
    _require_bound_submission(
        binding,
        request=request,
        recovering=True,
        worker_actor_id=worker_actor_id,
        expected_lease_owner=expected_lease_owner,
    )
    return request


def _load_bound_submission(
    *, dsn: str, worker: Actor, external_request_id: str
) -> Mapping[str, Any] | None:
    with _transaction(dsn, worker, read_only=True) as connection:
        return connection.execute(
            """
            SELECT submission.submission_id, submission.run_id,
                   submission.attempt_id, submission.task_id,
                   submission.firm_id, submission.matter_id,
                   submission.external_request_id, submission.destination,
                   submission.request_hash, submission.submission_state,
                   submission.recorded_by, attempt.status AS attempt_status,
                   attempt.lease_owner, attempt.lease_expires_at,
                   (attempt.lease_expires_at > pg_catalog.clock_timestamp())
                       AS lease_current,
                   attempt.input_hash, attempt.retry_mode AS attempt_retry_mode,
                   attempt.adapter_id AS attempt_adapter_id,
                   attempt.adapter_version AS attempt_adapter_version,
                   task.input_hash AS task_definition_input_hash,
                   task.input_refs, task.skill_id, task.tool_id,
                   task.adapter_id, task.adapter_version,
                   task.execution_mode, task.network_policy,
                   task.allowed_domains, task.external_request_approval_required,
                   task.sandbox_policy_version, task.sandbox_policy_hash,
                   task.risk_level, task.autonomy_level, task.approval_gate,
                   task.retry_mode, task.resource_budget,
                   approval.approval_kind, approval.task_input_hash,
                   approval.gate AS approved_gate,
                   approval.graph_hash AS approval_graph_hash,
                   run.current_graph_hash
            FROM case_agent_external_submissions submission
            JOIN case_agent_task_attempts attempt
              ON attempt.attempt_id = submission.attempt_id
             AND attempt.run_id = submission.run_id
             AND attempt.task_id = submission.task_id
             AND attempt.firm_id = submission.firm_id
             AND attempt.matter_id = submission.matter_id
            JOIN case_agent_tasks task
              ON task.graph_id = attempt.graph_id
             AND task.task_id = attempt.task_id
             AND task.run_id = attempt.run_id
             AND task.firm_id = attempt.firm_id
             AND task.matter_id = attempt.matter_id
            JOIN case_agent_runs run
              ON run.run_id = task.run_id AND run.firm_id = task.firm_id
             AND run.matter_id = task.matter_id
             AND run.current_graph_id = task.graph_id
            JOIN case_agent_approvals approval
              ON approval.approval_id = attempt.external_approval_id
             AND approval.run_id = attempt.run_id
             AND approval.task_id = attempt.task_id
             AND approval.firm_id = attempt.firm_id
             AND approval.matter_id = attempt.matter_id
            JOIN matter_actor_roles worker_role
              ON worker_role.firm_id = task.firm_id
             AND worker_role.matter_id = task.matter_id
             AND worker_role.user_id = %s
             AND worker_role.role = 'SYSTEM_WORKER'
             AND worker_role.revoked_at IS NULL
            JOIN users worker_user
              ON worker_user.user_id = worker_role.user_id
             AND worker_user.firm_id = worker_role.firm_id
             AND worker_user.status = 'ACTIVE'
            WHERE submission.firm_id = %s
              AND submission.external_request_id = %s
              AND submission.recorded_by = %s
            """,
            (
                worker.actor_id,
                worker.firm_id,
                external_request_id,
                worker.actor_id,
            ),
        ).fetchone()


def _require_bound_submission(
    binding: Mapping[str, Any],
    *,
    request: PreparedLawyerAnalysisRequest | _LawyerAnalysisRequestIdentity,
    recovering: bool,
    worker_actor_id: str,
    expected_lease_owner: str,
) -> None:
    budget = binding.get("resource_budget")
    recorded_by = str(binding.get("recorded_by", ""))
    try:
        _uuid(recorded_by, "lawyer-analysis submission actor")
        _uuid(worker_actor_id, "lawyer-analysis worker actor")
    except (TypeError, ValueError, AttributeError) as error:
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis durable submission actor is invalid"
        ) from error
    expected_budget = {
        "max_attempts": 1,
        "timeout_seconds": LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
        "max_external_calls": 1,
        "max_cost_minor_units": LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
        "max_output_bytes": 4 * 1024 * 1024,
    }
    legacy_recovery_budget = {
        **expected_budget,
        "timeout_seconds": _LEGACY_LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
    }
    accepted_budgets = (
        (expected_budget, legacy_recovery_budget)
        if recovering
        else (expected_budget,)
    )
    allowed_status = (
        {"RUNNING", "RECONCILING", "UNKNOWN"}
        if recovering
        else {"RUNNING"}
    )
    if (
        not isinstance(binding, Mapping)
        or str(binding.get("run_id")) != request.run_id
        or str(binding.get("task_id")) != request.task_id
        or str(binding.get("attempt_id")) != request.attempt_id
        or str(binding.get("firm_id")) != request.firm_id
        or str(binding.get("matter_id")) != request.matter_id
        or binding.get("external_request_id") != request.external_request_id
        or binding.get("destination") != request.endpoint_host
        or binding.get("request_hash") != request.request_hash
        or binding.get("submission_state") != "STARTED"
        or binding.get("attempt_status") not in allowed_status
        or recorded_by != worker_actor_id
        or binding.get("lease_owner") != expected_lease_owner
        or binding.get("lease_current") is not True
        or binding.get("input_hash") != request.task_input_hash
        or binding.get("task_definition_input_hash") != request.task_input_hash
        or tuple(binding.get("input_refs") or ()) != request.input_refs
        or binding.get("skill_id") != LAWYER_ANALYSIS_SKILL_ID
        or binding.get("tool_id") != LAWYER_ANALYSIS_TOOL_ID
        or binding.get("adapter_id") != QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_id
        or binding.get("adapter_version")
        != QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_version
        or binding.get("attempt_adapter_id")
        != QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_id
        or binding.get("attempt_adapter_version")
        != QWEN_LAWYER_ANALYSIS_MANIFEST.adapter_version
        or binding.get("execution_mode") != "NETWORK_CONNECTOR"
        or binding.get("network_policy") != "EXACT_ALLOWLIST"
        or tuple(binding.get("allowed_domains") or ())
        != (request.endpoint_host,)
        or binding.get("external_request_approval_required") is not True
        or binding.get("sandbox_policy_version")
        != QWEN_LAWYER_ANALYSIS_MANIFEST.sandbox_policy_version
        or binding.get("sandbox_policy_hash")
        != QWEN_LAWYER_ANALYSIS_MANIFEST.sandbox_policy_hash
        or binding.get("risk_level") != "HIGH"
        or binding.get("autonomy_level") != "A3_LAWYER_APPROVAL"
        or binding.get("approval_gate") != "LAWYER_REVIEW"
        or binding.get("retry_mode") != "NEVER_AUTOMATIC"
        or binding.get("attempt_retry_mode") != "NEVER_AUTOMATIC"
        or binding.get("approval_kind") != "TASK"
        or binding.get("task_input_hash") != request.task_input_hash
        or binding.get("approved_gate") != "LAWYER_REVIEW"
        or binding.get("approval_graph_hash")
        != binding.get("current_graph_hash")
        or not isinstance(budget, dict)
        or not any(budget == candidate for candidate in accepted_budgets)
    ):
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis durable submission or compiled policy differs"
        )


def _validate_prepared_request(
    request: PreparedLawyerAnalysisRequest, *, endpoint_host: str
) -> None:
    if not isinstance(request, PreparedLawyerAnalysisRequest):
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis prepared request is invalid"
        )
    request.validate()
    if request.endpoint_host != endpoint_host:
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis request host differs from credentials"
        )


def _validate_provider_envelope(body: object) -> str:
    if (
        not isinstance(body, bytes)
        or not 2 <= len(body) <= LAWYER_ANALYSIS_MAX_RESPONSE_BYTES
    ):
        raise LawyerAnalysisKnownFailure("LAWYER_ANALYSIS_RESPONSE_SIZE")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LawyerAnalysisKnownFailure("LAWYER_ANALYSIS_RESPONSE_JSON") from None
    provider_id = value.get("id") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("model") != LAWYER_ANALYSIS_MODEL_ID
        or not isinstance(provider_id, str)
        or _PROVIDER_RESPONSE_ID.fullmatch(provider_id) is None
    ):
        raise LawyerAnalysisKnownFailure("LAWYER_ANALYSIS_RESPONSE_IDENTITY")
    return provider_id


def _validate_transport_receipt(
    receipt: object,
    *,
    request: PreparedLawyerAnalysisRequest | _LawyerAnalysisRequestIdentity,
    response_body: bytes,
) -> None:
    provider_id = _validate_provider_envelope(response_body)
    expected_keys = {
        "schema_version",
        "external_request_id",
        "request_hash",
        "endpoint_host_hash",
        "method",
        "redirect_chain",
        "resolved_peer_ips",
        "connected_peer_ip",
        "response_sha256",
        "response_bytes",
        "provider_response_id_hash",
        "model_id",
    }
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != expected_keys
        or receipt.get("schema_version")
        != LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA
        or receipt.get("external_request_id") != request.external_request_id
        or receipt.get("request_hash") != request.request_hash
        or receipt.get("endpoint_host_hash")
        != sha256(request.endpoint_host.encode("ascii")).hexdigest()
        or receipt.get("method") != "POST"
        or receipt.get("redirect_chain") != []
        or receipt.get("response_sha256") != sha256(response_body).hexdigest()
        or receipt.get("response_bytes") != len(response_body)
        or receipt.get("provider_response_id_hash")
        != sha256(provider_id.encode("utf-8")).hexdigest()
        or receipt.get("model_id") != LAWYER_ANALYSIS_MODEL_ID
    ):
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis transport receipt differs"
        )
    resolved = receipt.get("resolved_peer_ips")
    connected = receipt.get("connected_peer_ip")
    if (
        not isinstance(resolved, list)
        or not resolved
        or len(resolved) != len(set(resolved))
        or not isinstance(connected, str)
        or connected not in resolved
    ):
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis peer receipt is invalid"
        )
    for value in resolved:
        _global_ip(value)


def _safe_headers(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,100}", name) is None
            or "\r" in value
            or "\n" in value
            or len(value) > 1024
        ):
            raise LawyerAnalysisTransportBlocked(
                "lawyer-analysis request header is unsafe"
            )


def _global_ip(value: object) -> None:
    try:
        parsed = ip_address(str(value))
    except ValueError:
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis peer address is invalid"
        ) from None
    if not parsed.is_global:
        raise LawyerAnalysisTransportBlocked(
            "lawyer-analysis peer must be globally routable"
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


@contextmanager
def _transaction(
    dsn: str, worker: Actor, *, read_only: bool
) -> Iterator[Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            + (" READ ONLY" if read_only else "")
        )
        connection.execute(
            "SELECT set_config('app.firm_id', %s, true)", (worker.firm_id,)
        )
        connection.execute(
            "SELECT set_config('app.actor_id', %s, true)", (worker.actor_id,)
        )
        yield connection


def _worker(actor: Actor) -> None:
    if (
        not isinstance(actor, Actor)
        or actor.roles != frozenset({Role.SYSTEM_WORKER})
    ):
        raise PermissionError(
            "lawyer analysis requires a dedicated SYSTEM_WORKER"
        )
    _uuid(actor.actor_id, "lawyer-analysis worker actor id")
    _uuid(actor.firm_id, "lawyer-analysis worker firm id")


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise LawyerAnalysisTransportBlocked(f"{label} is invalid") from None


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise LawyerAnalysisTransportBlocked(f"{label} is invalid")


__all__ = (
    "LAWYER_ANALYSIS_ENDPOINT_PATH",
    "LAWYER_ANALYSIS_RESPONSE_ARCHIVE_SCHEMA",
    "LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS",
    "LAWYER_ANALYSIS_TIMEOUT_SECONDS",
    "LawyerAnalysisTransportBlocked",
    "LawyerAnalysisTransportResult",
    "LawyerAnalysisUnknownSubmission",
    "PinnedQwenLawyerAnalysisHttpsTransport",
    "PostgresBoundRecoverableLawyerAnalysisExchange",
    "QwenLawyerAnalysisCredentials",
)
