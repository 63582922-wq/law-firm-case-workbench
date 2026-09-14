"""Authenticated, one-shot client for the network-isolated Office renderer.

The case Agent Worker may have tightly controlled provider egress.  LibreOffice
must not run in that process.  This module is the small byte-only boundary
between the Agent Worker and a renderer container which is attached solely to
an internal Docker network.

No caller-controlled URL, filesystem path, command, template or MIME type is
accepted.  A request contains exactly one generated DOCX or XLSX body.  Both
request and response metadata are HMAC authenticated, and the client performs
one HTTP attempt only.  A timeout or unverifiable response is reported as an
unknown result and is never automatically retransmitted.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import re
from secrets import token_urlsafe
import socket
from time import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .office_pdf_conversion_worker import ConvertedOfficePdf
from .reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
)


RENDER_PROTOCOL_VERSION = "lawcase-isolated-office-render-v1"
RENDER_PATH = "/internal/v1/render-office"
RENDER_HEALTH_PATH = "/healthz"
RENDER_HEALTH_BODY = b'{"service":"isolated-document-renderer","status":"ready"}'
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_OFFICE_BYTES = 100 * 1024 * 1024
MAX_RENDERED_PDF_BYTES = 128 * 1024 * 1024
DEFAULT_CLOCK_SKEW_SECONDS = 60

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ALLOWED_INPUTS = {
    "WORD_DOCUMENT": ("approved-draft.docx", DOCX_MEDIA_TYPE),
    "SPREADSHEET": ("approved-ledger.xlsx", XLSX_MEDIA_TYPE),
}


class IsolatedDocumentRendererBlocked(ReviewOfficeConversionBlocked):
    """The renderer contract or a known renderer rejection is unsafe."""


class IsolatedDocumentRendererUnknown(ReviewOfficeConversionUnknown):
    """The render request may have completed, so it must not be resubmitted."""


@dataclass(frozen=True)
class RendererHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class DocumentRendererTransport(Protocol):
    """One-attempt HTTP transport; implementations must never retry."""

    def post_once(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> RendererHttpResponse: ...

    def get_once(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> RendererHttpResponse: ...


class UrllibDocumentRendererTransport:
    """Standard-library transport with a hard response-size bound and no retry."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def post_once(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> RendererHttpResponse:
        return self._open_once(
            Request(endpoint, data=body, headers=dict(headers), method="POST"),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def get_once(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> RendererHttpResponse:
        return self._open_once(
            Request(endpoint, headers=dict(headers), method="GET"),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def _open_once(
        self,
        request: Request,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> RendererHttpResponse:
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                payload = response.read(max_response_bytes + 1)
                if len(payload) > max_response_bytes:
                    raise IsolatedDocumentRendererUnknown(
                        "isolated renderer response exceeded its byte limit"
                    )
                return RendererHttpResponse(
                    status_code=int(response.status),
                    headers={key.casefold(): value for key, value in response.headers.items()},
                    body=payload,
                )
        except HTTPError as error:
            # An HTTP status is a known renderer response.  Read only a tiny,
            # non-secret diagnostic body and never turn it into a retry.
            payload = error.read(4097)
            return RendererHttpResponse(
                status_code=int(error.code),
                headers={key.casefold(): value for key, value in error.headers.items()},
                body=payload[:4096],
            )
        except IsolatedDocumentRendererUnknown:
            raise
        except (TimeoutError, socket.timeout, ConnectionError, OSError, URLError) as error:
            raise IsolatedDocumentRendererUnknown(
                "isolated renderer result is unknown; request was not retransmitted"
            ) from error


@dataclass(frozen=True)
class IsolatedDocumentRendererClientSettings:
    endpoint: str
    shared_secret: bytes = field(repr=False)
    timeout_seconds: int = 180
    max_clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS
    health_endpoint: str = field(init=False)

    def __post_init__(self) -> None:
        origin = _validated_origin(self.endpoint)
        object.__setattr__(self, "endpoint", origin + RENDER_PATH)
        object.__setattr__(self, "health_endpoint", origin + RENDER_HEALTH_PATH)
        object.__setattr__(self, "shared_secret", validate_shared_secret(self.shared_secret))
        if not 10 <= self.timeout_seconds <= 300:
            raise IsolatedDocumentRendererBlocked("renderer timeout must be between 10 and 300 seconds")
        if not 10 <= self.max_clock_skew_seconds <= 300:
            raise IsolatedDocumentRendererBlocked("renderer clock skew limit is invalid")

    @classmethod
    def from_worker_environment(
        cls, environment: Mapping[str, str]
    ) -> "IsolatedDocumentRendererClientSettings":
        """Parse the exact server-admin contract used by the Agent Worker.

        This method is intentionally not an OS-global settings loader: the
        Worker entrypoint supplies its already selected environment mapping,
        and no browser or case value can override the endpoint or secret.
        """

        if not isinstance(environment, Mapping):
            raise IsolatedDocumentRendererBlocked("renderer environment mapping is invalid")

        def required(name: str) -> str:
            value = environment.get(name)
            if (
                not isinstance(value, str)
                or not value
                or value.startswith("REPLACE_")
                or len(value) > 2_048
            ):
                raise IsolatedDocumentRendererBlocked(f"{name} is required")
            return value

        timeout_text = required("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS")
        if re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", timeout_text) is None:
            raise IsolatedDocumentRendererBlocked("renderer timeout is invalid")
        timeout_seconds = int(timeout_text)
        # The dynamic document task has one 90-second provider exchange before
        # rendering and a hard 300-second runtime receipt budget.  Leave a
        # bounded margin for deterministic Office generation and staging.
        if not 10 <= timeout_seconds <= 180:
            raise IsolatedDocumentRendererBlocked(
                "Worker renderer timeout must be between 10 and 180 seconds"
            )
        return cls(
            endpoint=required("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT"),
            shared_secret=decode_shared_secret_base64url(
                required("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET")
            ),
            timeout_seconds=timeout_seconds,
        )


class IsolatedDocumentRendererClient:
    """Implement ``ReviewOfficeConverter`` over the authenticated sidecar."""

    def __init__(
        self,
        *,
        settings: IsolatedDocumentRendererClientSettings,
        transport: DocumentRendererTransport | None = None,
        clock: Callable[[], float] = time,
        nonce_factory: Callable[[], str] = lambda: token_urlsafe(32),
    ) -> None:
        if not isinstance(settings, IsolatedDocumentRendererClientSettings):
            raise ValueError("isolated renderer client settings are invalid")
        chosen = transport or UrllibDocumentRendererTransport()
        if not callable(getattr(chosen, "post_once", None)):
            raise ValueError("isolated renderer transport is invalid")
        self._settings = settings
        self._transport = chosen
        self._clock = clock
        self._nonce_factory = nonce_factory

    def preflight(self) -> None:
        """Prove the fixed sidecar is reachable and reports executable health.

        The server health route actually invokes both ``soffice --version``
        and ``pdftoppm -v``.  This client performs exactly one short GET,
        accepts only the canonical body, and verifies a shared-secret HMAC.
        """

        get_once = getattr(self._transport, "get_once", None)
        if not callable(get_once):
            raise IsolatedDocumentRendererBlocked("renderer health transport is unavailable")
        try:
            response = get_once(
                endpoint=self._settings.health_endpoint,
                headers={
                    "Accept": "application/json",
                    "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
                },
                timeout_seconds=min(10, self._settings.timeout_seconds),
                max_response_bytes=512,
            )
        except (IsolatedDocumentRendererUnknown, TimeoutError, ConnectionError, OSError) as error:
            raise IsolatedDocumentRendererBlocked("renderer health preflight failed") from error
        if not isinstance(response, RendererHttpResponse):
            raise IsolatedDocumentRendererBlocked("renderer health response is invalid")
        headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}
        signature = headers.get("x-lawcase-health-signature", "")
        if (
            response.status_code != 200
            or response.body != RENDER_HEALTH_BODY
            or headers.get("x-lawcase-render-protocol") != RENDER_PROTOCOL_VERSION
            or not headers.get("content-type", "").casefold().startswith("application/json")
            or _SHA256.fullmatch(signature) is None
            or not hmac.compare_digest(
                signature, sign_renderer_health(secret=self._settings.shared_secret)
            )
        ):
            raise IsolatedDocumentRendererBlocked("renderer health response is not authenticated")

    def convert_generated_document(
        self,
        content: bytes,
        *,
        content_sha256: str,
        source_name: str,
        detected_kind: str,
    ) -> ConvertedOfficePdf:
        media_type = validate_render_input(
            content=content,
            content_sha256=content_sha256,
            source_name=source_name,
            detected_kind=detected_kind,
        )
        timestamp = str(int(self._clock()))
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise IsolatedDocumentRendererBlocked("renderer request nonce is invalid")
        signature = sign_render_request(
            secret=self._settings.shared_secret,
            timestamp=timestamp,
            nonce=nonce,
            detected_kind=detected_kind,
            source_name=source_name,
            content_sha256=content_sha256,
            content_length=len(content),
            media_type=media_type,
        )
        headers = {
            "Authorization": f"HMAC-SHA256 {signature}",
            "Content-Type": media_type,
            "Content-Length": str(len(content)),
            "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
            "X-Lawcase-Render-Timestamp": timestamp,
            "X-Lawcase-Render-Nonce": nonce,
            "X-Lawcase-Detected-Kind": detected_kind,
            "X-Lawcase-Source-Name": source_name,
            "X-Lawcase-Source-SHA256": content_sha256,
        }
        try:
            response = self._transport.post_once(
                endpoint=self._settings.endpoint,
                headers=headers,
                body=content,
                timeout_seconds=self._settings.timeout_seconds,
                max_response_bytes=MAX_RENDERED_PDF_BYTES,
            )
        except IsolatedDocumentRendererUnknown:
            raise
        except (TimeoutError, ConnectionError, OSError) as error:
            raise IsolatedDocumentRendererUnknown(
                "isolated renderer result is unknown; request was not retransmitted"
            ) from error
        if not isinstance(response, RendererHttpResponse):
            raise IsolatedDocumentRendererUnknown("isolated renderer returned an unverifiable response")
        if response.status_code != 200:
            raise IsolatedDocumentRendererBlocked(
                f"isolated renderer rejected the request with status {response.status_code}"
            )
        return _verified_render_response(
            response=response,
            secret=self._settings.shared_secret,
            request_nonce=nonce,
            expected_source_sha256=content_sha256,
            expected_detected_kind=detected_kind,
        )


def validate_shared_secret(value: bytes) -> bytes:
    if not isinstance(value, bytes) or not 32 <= len(value) <= 128:
        raise IsolatedDocumentRendererBlocked("renderer shared secret must contain 32 to 128 bytes")
    return value


def decode_shared_secret_base64url(value: str) -> bytes:
    """Decode an unpadded base64url secret containing 32 to 128 bytes."""

    try:
        if (
            not isinstance(value, str)
            or not 43 <= len(value) <= 171
            or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
            or "=" in value
        ):
            raise ValueError
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
            raise ValueError
        return validate_shared_secret(decoded)
    except (ValueError, TypeError, BinasciiError) as error:
        raise IsolatedDocumentRendererBlocked(
            "renderer shared secret must be unpadded base64url for 32 to 128 bytes"
        ) from error


def validate_render_input(
    *, content: bytes, content_sha256: str, source_name: str, detected_kind: str
) -> str:
    try:
        expected_name, media_type = _ALLOWED_INPUTS[detected_kind]
    except (KeyError, TypeError):
        raise IsolatedDocumentRendererBlocked("renderer accepts only generated DOCX or XLSX") from None
    if source_name != expected_name:
        raise IsolatedDocumentRendererBlocked("renderer source name is not server-fixed")
    if not isinstance(content, bytes) or not content or len(content) > MAX_OFFICE_BYTES:
        raise IsolatedDocumentRendererBlocked("renderer input byte size is invalid")
    if _SHA256.fullmatch(content_sha256 or "") is None or not hmac.compare_digest(
        sha256(content).hexdigest(), content_sha256
    ):
        raise IsolatedDocumentRendererBlocked("renderer input is not hash-bound")
    return media_type


def sign_render_request(
    *,
    secret: bytes,
    timestamp: str,
    nonce: str,
    detected_kind: str,
    source_name: str,
    content_sha256: str,
    content_length: int,
    media_type: str,
) -> str:
    return _hmac_hex(
        validate_shared_secret(secret),
        "\n".join(
            (
                RENDER_PROTOCOL_VERSION,
                "POST",
                RENDER_PATH,
                timestamp,
                nonce,
                detected_kind,
                source_name,
                content_sha256,
                str(content_length),
                media_type,
            )
        ),
    )


def sign_render_response(
    *,
    secret: bytes,
    request_nonce: str,
    source_sha256: str,
    detected_kind: str,
    converter_id: str,
    converter_version: str,
    transform_hash: str,
    pdf_sha256: str,
    pdf_bytes: int,
    page_count: int,
    render_verification_hash: str,
) -> str:
    return _hmac_hex(
        validate_shared_secret(secret),
        "\n".join(
            (
                RENDER_PROTOCOL_VERSION,
                "RESPONSE",
                request_nonce,
                source_sha256,
                detected_kind,
                converter_id,
                converter_version,
                transform_hash,
                pdf_sha256,
                str(pdf_bytes),
                str(page_count),
                render_verification_hash,
            )
        ),
    )


def sign_renderer_health(*, secret: bytes) -> str:
    return _hmac_hex(
        validate_shared_secret(secret),
        "\n".join((RENDER_PROTOCOL_VERSION, "HEALTH", sha256(RENDER_HEALTH_BODY).hexdigest())),
    )


def _verified_render_response(
    *,
    response: RendererHttpResponse,
    secret: bytes,
    request_nonce: str,
    expected_source_sha256: str,
    expected_detected_kind: str,
) -> ConvertedOfficePdf:
    headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}

    def required(name: str, maximum: int = 200) -> str:
        value = headers.get(name.casefold())
        if not isinstance(value, str) or not value or len(value) > maximum or "\n" in value or "\r" in value:
            raise IsolatedDocumentRendererUnknown("isolated renderer response metadata is invalid")
        return value

    if required("X-Lawcase-Render-Protocol") != RENDER_PROTOCOL_VERSION:
        raise IsolatedDocumentRendererUnknown("isolated renderer response protocol differs")
    source_sha256 = required("X-Lawcase-Source-SHA256", 64)
    detected_kind = required("X-Lawcase-Detected-Kind", 32)
    converter_id = required("X-Lawcase-Converter-Id", 80)
    converter_version = required("X-Lawcase-Converter-Version", 160)
    transform_hash = required("X-Lawcase-Transform-SHA256", 64)
    pdf_sha256 = required("X-Lawcase-PDF-SHA256", 64)
    render_hash = required("X-Lawcase-Render-Verification-SHA256", 64)
    signature = required("X-Lawcase-Response-Signature", 64)
    try:
        pdf_bytes = int(required("X-Lawcase-PDF-Bytes", 12))
        page_count = int(required("X-Lawcase-PDF-Page-Count", 8))
    except ValueError:
        raise IsolatedDocumentRendererUnknown("isolated renderer response counts are invalid") from None
    if (
        source_sha256 != expected_source_sha256
        or detected_kind != expected_detected_kind
        or _SHA256.fullmatch(transform_hash) is None
        or _SHA256.fullmatch(pdf_sha256) is None
        or _SHA256.fullmatch(render_hash) is None
        or not 1 <= pdf_bytes <= MAX_RENDERED_PDF_BYTES
        or pdf_bytes != len(response.body)
        or not 1 <= page_count <= 10_000
        or not response.body.startswith(b"%PDF-")
        or not hmac.compare_digest(sha256(response.body).hexdigest(), pdf_sha256)
    ):
        raise IsolatedDocumentRendererUnknown("isolated renderer response is not hash-bound")
    expected_signature = sign_render_response(
        secret=secret,
        request_nonce=request_nonce,
        source_sha256=source_sha256,
        detected_kind=detected_kind,
        converter_id=converter_id,
        converter_version=converter_version,
        transform_hash=transform_hash,
        pdf_sha256=pdf_sha256,
        pdf_bytes=pdf_bytes,
        page_count=page_count,
        render_verification_hash=render_hash,
    )
    if _SHA256.fullmatch(signature) is None or not hmac.compare_digest(signature, expected_signature):
        raise IsolatedDocumentRendererUnknown("isolated renderer response authentication failed")
    return ConvertedOfficePdf(
        source_sha256=source_sha256,
        detected_kind=detected_kind,
        converter_id=converter_id,
        converter_version=converter_version,
        transform_hash=transform_hash,
        pdf_sha256=pdf_sha256,
        pdf_bytes=pdf_bytes,
        page_count=page_count,
        render_verification_hash=render_hash,
        pdf_content=response.body,
    )


def _validated_origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise IsolatedDocumentRendererBlocked("renderer endpoint is invalid")
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or parts.port is None
    ):
        raise IsolatedDocumentRendererBlocked("renderer endpoint must be one fixed internal HTTP origin")
    return value.rstrip("/")


def _hmac_hex(secret: bytes, canonical: str) -> str:
    if any(character in canonical for character in ("\r", "\x00")):
        raise IsolatedDocumentRendererBlocked("renderer authenticated metadata is invalid")
    return hmac.new(secret, canonical.encode("utf-8"), "sha256").hexdigest()


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never let an internal service redirect secret-bearing POST metadata."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


__all__ = [
    "DEFAULT_CLOCK_SKEW_SECONDS",
    "DOCX_MEDIA_TYPE",
    "DocumentRendererTransport",
    "IsolatedDocumentRendererBlocked",
    "IsolatedDocumentRendererClient",
    "IsolatedDocumentRendererClientSettings",
    "IsolatedDocumentRendererUnknown",
    "MAX_OFFICE_BYTES",
    "MAX_RENDERED_PDF_BYTES",
    "RENDER_HEALTH_BODY",
    "RENDER_HEALTH_PATH",
    "RENDER_PATH",
    "RENDER_PROTOCOL_VERSION",
    "RendererHttpResponse",
    "UrllibDocumentRendererTransport",
    "XLSX_MEDIA_TYPE",
    "decode_shared_secret_base64url",
    "sign_render_request",
    "sign_render_response",
    "sign_renderer_health",
    "validate_render_input",
    "validate_shared_secret",
]
