"""Administrator-configured Qwen OCR HTTPS and lookup-only recovery.

The provider endpoint, workspace, model, credential and output instruction are
server-owned.  Raw evidence never receives a public URL: the already
normalized PNG is embedded as a Base64 data URL in the fixed JSON request.
The transport itself is intentionally injected; production must provide an
egress broker and a durable recovery API keyed by the stable external request
id.  There is no direct urllib fallback that could bypass the Agent ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import http.client
from ipaddress import ip_address
import json
import re
import socket
import ssl
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from .case_agent_external_failure import CaseAgentKnownExternalFailure
from .qwen_visual_ocr_adapter import (
    DurableQwenVisualOcrExchange,
    QWEN_VISUAL_OCR_MODEL_ID,
    QwenVisualOcrBlocked,
    QwenVisualOcrRequest,
    QwenVisualOcrResult,
    RecoveredVisualOcr,
    RecoveredVisualOcrStatus,
)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

QWEN_VISUAL_OCR_UNKNOWN_CODES = frozenset(
    {
        "QWEN_VISUAL_OCR_UNKNOWN_DNS",
        "QWEN_VISUAL_OCR_UNKNOWN_CONNECT",
        "QWEN_VISUAL_OCR_UNKNOWN_SEND",
        "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
        "QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY",
    }
)


class QwenVisualOcrNetworkBlocked(QwenVisualOcrBlocked):
    """The exact pinned HTTPS exchange failed or became indeterminate."""

    def __init__(self, *, external_request_id: str, error_code: str) -> None:
        try:
            UUID(external_request_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Qwen OCR unknown request id is invalid") from None
        if error_code not in QWEN_VISUAL_OCR_UNKNOWN_CODES:
            raise ValueError("Qwen OCR unknown stage code is invalid")
        self.external_request_id = external_request_id
        self.error_code = error_code
        super().__init__("Qwen OCR HTTPS outcome is indeterminate")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(external_request_id="
            "<bound>, error_code=<controlled>)"
        )


class QwenVisualOcrKnownFailure(CaseAgentKnownExternalFailure):
    """Qwen returned a complete response that cannot produce a usable result."""


class PinnedHttpsConnectionFactory(Protocol):
    def __call__(
        self,
        address: tuple[str, int],
        timeout: float,
        ssl_context: ssl.SSLContext,
        server_hostname: str,
    ) -> Any: ...


@dataclass(frozen=True, repr=False)
class QwenVisualOcrCredentials:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or not 20 <= len(self.api_key) <= 512
            or self.api_key != self.api_key.strip()
            or any(char.isspace() for char in self.api_key)
        ):
            raise ValueError("Qwen OCR credential is invalid")

    def __repr__(self) -> str:
        return "QwenVisualOcrCredentials(api_key=<redacted>)"


@dataclass(frozen=True)
class QwenVisualOcrTransportRequest:
    external_request_id: str
    endpoint: str
    endpoint_host: str
    method: str
    headers: Mapping[str, str] = field(repr=False, compare=False)
    body: bytes = field(repr=False, compare=False)
    request_hash: str
    projection_hash: str
    rendered_page_sha256: str
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True)
class QwenVisualOcrTransportResult:
    external_request_id: str
    request_hash: str
    provider_request_id: str
    response_body: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class QwenVisualOcrTransportFailure:
    external_request_id: str
    request_hash: str
    error_code: str


class QwenVisualOcrEgressBroker(Protocol):
    """Pinned-TLS broker. It must not follow redirects or change the host."""

    def send(
        self, *, request: QwenVisualOcrTransportRequest
    ) -> QwenVisualOcrTransportResult: ...


class QwenVisualOcrRecoveryBroker(Protocol):
    """Lookup only. It is not allowed to submit or retry a request."""

    def lookup(
        self, *, external_request_id: str, request_hash: str
    ) -> QwenVisualOcrTransportResult | QwenVisualOcrTransportFailure | None: ...


class PinnedQwenVisualOcrHttpsBroker(QwenVisualOcrEgressBroker):
    """One POST to the exact Qwen workspace host; redirects are forbidden."""

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        connection_factory: PinnedHttpsConnectionFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not callable(resolver):
            raise ValueError("Qwen OCR DNS resolver is invalid")
        self._resolver = resolver
        self._connect = connection_factory or _open_pinned_tls_connection
        if not callable(self._connect):
            raise ValueError("Qwen OCR HTTPS connector is invalid")
        self._ssl = ssl_context or ssl.create_default_context()

    def send(
        self, *, request: QwenVisualOcrTransportRequest
    ) -> QwenVisualOcrTransportResult:
        _validate_transport_request(request)
        try:
            answers = self._resolver(
                request.endpoint_host, 443, type=socket.SOCK_STREAM
            )
            resolved = tuple(
                sorted(
                    {str(answer[4][0]) for answer in answers if len(answer) >= 5}
                )
            )
            if not resolved:
                raise ValueError("DNS returned no address")
            for value in resolved:
                _global_ip(value)
        except Exception as error:
            raise QwenVisualOcrNetworkBlocked(
                external_request_id=request.external_request_id,
                error_code="QWEN_VISUAL_OCR_UNKNOWN_DNS",
            ) from error
        connection = None
        try:
            try:
                connection = self._connect(
                    (resolved[0], 443),
                    request.timeout_seconds,
                    self._ssl,
                    request.endpoint_host,
                )
                peer = str(connection.getpeername()[0])
                _global_ip(peer)
                if peer not in resolved:
                    raise ValueError("connected peer differs from pinned DNS")
            except Exception as error:
                raise QwenVisualOcrNetworkBlocked(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_UNKNOWN_CONNECT",
                ) from error
            parsed = urlsplit(request.endpoint)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            headers = {
                "Host": request.endpoint_host,
                "Connection": "close",
                "Content-Length": str(len(request.body)),
                **dict(request.headers),
            }
            _safe_headers(headers)
            head = (
                f"POST {path} HTTP/1.1\r\n"
                + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
                + "\r\n"
            ).encode("ascii")
            try:
                connection.sendall(head + request.body)
            except Exception as error:
                raise QwenVisualOcrNetworkBlocked(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_UNKNOWN_SEND",
                ) from error
            try:
                response = http.client.HTTPResponse(connection)
                response.begin()
            except Exception as error:
                raise QwenVisualOcrNetworkBlocked(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_HEAD",
                ) from error
            if 300 <= response.status < 400:
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code=f"QWEN_VISUAL_OCR_HTTP_{response.status}",
                )
            if response.status != 200:
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code=f"QWEN_VISUAL_OCR_HTTP_{response.status}",
                )
            content_type = (
                response.getheader("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_RESPONSE_CONTENT_TYPE",
                )
            content_encoding = (
                response.getheader("Content-Encoding") or "identity"
            ).strip().lower()
            if content_encoding != "identity":
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_RESPONSE_ENCODING",
                )
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError):
                    raise QwenVisualOcrKnownFailure(
                        external_request_id=request.external_request_id,
                        error_code="QWEN_VISUAL_OCR_RESPONSE_LENGTH",
                    ) from None
                if declared_length > request.max_response_bytes:
                    raise QwenVisualOcrKnownFailure(
                        external_request_id=request.external_request_id,
                        error_code="QWEN_VISUAL_OCR_RESPONSE_TOO_LARGE",
                    )
            try:
                body = response.read(request.max_response_bytes + 1)
            except Exception as error:
                raise QwenVisualOcrNetworkBlocked(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_UNKNOWN_RESPONSE_BODY",
                ) from error
            if not isinstance(body, bytes) or not 2 <= len(body) <= request.max_response_bytes:
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_RESPONSE_SIZE",
                )
            try:
                outer = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_RESPONSE_JSON",
                ) from None
            provider_request_id = outer.get("id") if isinstance(outer, dict) else None
            if (
                not isinstance(provider_request_id, str)
                or re.fullmatch(r"[A-Za-z0-9._:-]{1,500}", provider_request_id)
                is None
            ):
                raise QwenVisualOcrKnownFailure(
                    external_request_id=request.external_request_id,
                    error_code="QWEN_VISUAL_OCR_RESPONSE_ID",
                )
            return QwenVisualOcrTransportResult(
                external_request_id=request.external_request_id,
                request_hash=request.request_hash,
                provider_request_id=provider_request_id,
                response_body=body,
            )
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class QwenVisualOcrRecoverableExchange(DurableQwenVisualOcrExchange):
    def __init__(
        self,
        *,
        credentials: QwenVisualOcrCredentials,
        egress: QwenVisualOcrEgressBroker,
        recovery: QwenVisualOcrRecoveryBroker,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not callable(getattr(egress, "send", None)):
            raise ValueError("Qwen OCR egress broker is required")
        if not callable(getattr(recovery, "lookup", None)):
            raise ValueError("Qwen OCR lookup-only broker is required")
        if not 1.0 <= timeout_seconds <= 120.0:
            raise ValueError("Qwen OCR timeout is invalid")
        self._credentials = credentials
        self._egress = egress
        self._recovery = recovery
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return "QwenVisualOcrRecoverableExchange(<server-configured>)"

    def send(self, *, request: QwenVisualOcrRequest) -> QwenVisualOcrResult:
        prepared = self._prepare(request)
        result = self._egress.send(request=prepared)
        return self._validated_result(result, expected=prepared)

    def recover(
        self, *, external_request_id: str, request_hash: str
    ) -> RecoveredVisualOcr:
        result = self._recovery.lookup(
            external_request_id=external_request_id,
            request_hash=request_hash,
        )
        if result is None:
            return RecoveredVisualOcr(RecoveredVisualOcrStatus.UNRESOLVED)
        # Reconstruct the only fields needed for exact lookup validation. This
        # path never calls the egress broker and therefore cannot resubmit.
        if (
            result.external_request_id != external_request_id
            or result.request_hash != request_hash
        ):
            raise QwenVisualOcrBlocked(
                "recovered Qwen OCR result differs from the unknown request"
            )
        if isinstance(result, QwenVisualOcrTransportFailure):
            if (
                re.fullmatch(
                    r"QWEN_VISUAL_OCR_[A-Z0-9_]{3,57}", result.error_code
                )
                is None
            ):
                raise QwenVisualOcrBlocked(
                    "recovered Qwen OCR failure code is invalid"
                )
            return RecoveredVisualOcr(
                RecoveredVisualOcrStatus.FAILED,
                error_code=result.error_code,
            )
        if not isinstance(result, QwenVisualOcrTransportResult):
            raise QwenVisualOcrBlocked("recovered Qwen OCR result is invalid")
        return RecoveredVisualOcr(
            RecoveredVisualOcrStatus.SUCCEEDED,
            result=self._parsed_result(result),
        )

    def _prepare(
        self, request: QwenVisualOcrRequest
    ) -> QwenVisualOcrTransportRequest:
        request.validate()
        return QwenVisualOcrTransportRequest(
            external_request_id=request.external_request_id,
            endpoint=(
                f"https://{request.endpoint_host}/compatible-mode/v1/chat/completions"
            ),
            endpoint_host=request.endpoint_host,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._credentials.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            body=request.body,
            request_hash=request.request_hash,
            projection_hash=request.projection_hash,
            rendered_page_sha256=request.rendered_page_sha256,
            timeout_seconds=self._timeout,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )

    def _validated_result(
        self,
        result: object,
        *,
        expected: QwenVisualOcrTransportRequest,
    ) -> QwenVisualOcrResult:
        if not isinstance(result, QwenVisualOcrTransportResult):
            raise QwenVisualOcrBlocked("Qwen OCR transport result is invalid")
        if (
            result.external_request_id != expected.external_request_id
            or result.request_hash != expected.request_hash
        ):
            raise QwenVisualOcrBlocked(
                "Qwen OCR transport result differs from its exact request"
            )
        return self._parsed_result(result)

    @staticmethod
    def _parsed_result(result: QwenVisualOcrTransportResult) -> QwenVisualOcrResult:
        if (
            not isinstance(result.response_body, bytes)
            or not 2 <= len(result.response_body) <= _MAX_RESPONSE_BYTES
            or not isinstance(result.provider_request_id, str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,500}", result.provider_request_id)
            is None
        ):
            raise QwenVisualOcrBlocked("Qwen OCR transport result is invalid")
        raw_candidate, response_request_id = _extract_single_content(
            result.response_body
        )
        if response_request_id != result.provider_request_id:
            raise QwenVisualOcrBlocked(
                "Qwen OCR provider response id differs from transport receipt"
            )
        ref_hash = sha256(
            json.dumps(
                {
                    "schema_version": "qwen-visual-provider-request-ref-v1",
                    "external_request_id": result.external_request_id,
                    "request_hash": result.request_hash,
                    "provider_request_id": result.provider_request_id,
                    "provider_response_sha256": sha256(
                        result.response_body
                    ).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        value = QwenVisualOcrResult(
            response_body=raw_candidate,
            provider_request_ref_hash=ref_hash,
            external_request_id=result.external_request_id,
            request_hash=result.request_hash,
        )
        value.validate()
        return value


def _extract_single_content(body: bytes) -> tuple[bytes, str]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise QwenVisualOcrBlocked("Qwen OCR response is not JSON") from None
    if not isinstance(value, dict) or value.get("model") != QWEN_VISUAL_OCR_MODEL_ID:
        raise QwenVisualOcrBlocked("Qwen OCR response model is invalid")
    provider_request_id = value.get("id")
    if (
        not isinstance(provider_request_id, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,500}", provider_request_id) is None
    ):
        raise QwenVisualOcrBlocked("Qwen OCR response id is invalid")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise QwenVisualOcrBlocked("Qwen OCR response choice count is invalid")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES
    ):
        raise QwenVisualOcrBlocked("Qwen OCR response content is invalid")
    # The strict visual parser, not this wrapper, validates the candidate JSON.
    return content.encode("utf-8"), provider_request_id


def _validate_transport_request(request: object) -> None:
    if not isinstance(request, QwenVisualOcrTransportRequest):
        raise QwenVisualOcrBlocked("Qwen OCR transport request is invalid")
    expected = (
        f"https://{request.endpoint_host}/compatible-mode/v1/chat/completions"
    )
    if (
        request.endpoint != expected
        or request.method != "POST"
        or sha256(request.body).hexdigest() != request.request_hash
        or not 1.0 <= request.timeout_seconds <= 120.0
        or request.max_response_bytes != _MAX_RESPONSE_BYTES
    ):
        raise QwenVisualOcrBlocked("Qwen OCR transport request is invalid")
    parsed = urlsplit(request.endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != request.endpoint_host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise QwenVisualOcrBlocked("Qwen OCR transport endpoint is invalid")
    expected_headers = {"Authorization", "Content-Type", "Accept", "Accept-Encoding"}
    authorization = request.headers.get("Authorization")
    if (
        set(request.headers) != expected_headers
        or not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or len(authorization) <= len("Bearer ")
        or request.headers.get("Content-Type") != "application/json"
        or request.headers.get("Accept") != "application/json"
        or request.headers.get("Accept-Encoding") != "identity"
    ):
        raise QwenVisualOcrBlocked("Qwen OCR transport headers are invalid")


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
            raise QwenVisualOcrBlocked("Qwen OCR header is unsafe")


def _global_ip(value: str) -> None:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise ValueError("Qwen OCR DNS or peer IP is invalid") from error
    if not address.is_global:
        raise ValueError("Qwen OCR DNS or peer IP is not global")


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


__all__ = (
    "QWEN_VISUAL_OCR_UNKNOWN_CODES",
    "QwenVisualOcrCredentials",
    "QwenVisualOcrEgressBroker",
    "QwenVisualOcrKnownFailure",
    "QwenVisualOcrNetworkBlocked",
    "PinnedQwenVisualOcrHttpsBroker",
    "PinnedHttpsConnectionFactory",
    "QwenVisualOcrRecoverableExchange",
    "QwenVisualOcrRecoveryBroker",
    "QwenVisualOcrTransportFailure",
    "QwenVisualOcrTransportRequest",
    "QwenVisualOcrTransportResult",
)
