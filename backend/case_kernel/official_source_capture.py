"""Fail-closed capture of an explicitly authorized public official source.

The worker performs a direct TLS fetch without cookies or credentials, checks
the actual peer is globally routable, and writes the exact response body into
the local encrypted content-addressed store.  Capture never means legal
approval: every result remains HUMAN_REVIEW_REQUIRED until the separate
PostgreSQL legal-source command is approved by an authorized lawyer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPSConnection
from ipaddress import ip_address
import json
import re
import ssl
from typing import Mapping, Protocol
from urllib.parse import urlparse

from .managed_artifact_store import LocalEncryptedArtifactStore, StoredArtifactObject
from .research_gateway import (
    ExternalResearchReceipt,
    ExternalResearchRequest,
    PublicResearchGateway,
    PublicSource,
)


class OfficialSourceCaptureBlocked(ValueError):
    """The source fetch or archive response failed a security invariant."""


@dataclass(frozen=True)
class OfficialHttpResponse:
    status_code: int
    final_url: str
    media_type: str
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str


class OfficialSourceTransport(Protocol):
    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse: ...


@dataclass(frozen=True)
class CapturedOfficialSource:
    request_id: str
    source_id: str
    publisher: str
    source_tier: str
    requested_url: str
    final_url: str
    retrieved_at: datetime
    media_type: str
    content_sha256: str
    content_bytes: int
    encrypted_object: StoredArtifactObject
    response_receipt: ExternalResearchReceipt
    verification_hash: str
    review_status: str = "HUMAN_REVIEW_REQUIRED"


_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/pdf",
        "application/json",
        "application/xhtml+xml",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
_DEFAULT_MAX_BYTES = 32 * 1024 * 1024
_MAX_AUTHORIZATION_AGE = timedelta(minutes=15)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


class DirectHttpsOfficialSourceTransport:
    """Minimal credential-free HTTPS client with post-connect peer checks."""

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("official source timeout must be between 0 and 60 seconds")
        self._timeout_seconds = timeout_seconds
        self._tls_context = ssl.create_default_context()

    def fetch(self, *, url: str, max_bytes: int) -> OfficialHttpResponse:
        parsed = _validated_https_url(url)
        if max_bytes < 1 or max_bytes > 64 * 1024 * 1024:
            raise OfficialSourceCaptureBlocked("official source byte limit is invalid")
        host = parsed.hostname
        if host is None:
            raise OfficialSourceCaptureBlocked("official source hostname is missing")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = HTTPSConnection(
            host,
            port=443,
            timeout=self._timeout_seconds,
            context=self._tls_context,
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise OfficialSourceCaptureBlocked("official source TLS socket is unavailable")
            peer_ip = str(connection.sock.getpeername()[0])
            _require_global_peer(peer_ip)
            request_headers = {
                "Accept": "text/html,application/xhtml+xml,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Connection": "close",
                "User-Agent": "LawCaseWorkbench-OfficialSourceCapture/0.1",
            }
            if host in {"chinamoney.com.cn", "www.chinamoney.com.cn"} and path.startswith("/ags/ms/"):
                request_headers.update(
                    {
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Referer": "https://www.chinamoney.com.cn/r/cms/chinese/chinamoney/html/currency/lpr-shibor-history-download.html",
                        "X-Requested-With": "XMLHttpRequest",
                    }
                )
            connection.request(
                "GET",
                path,
                headers=request_headers,
            )
            response = connection.getresponse()
            headers = {key.lower(): value.strip() for key, value in response.getheaders()}
            if 300 <= response.status < 400:
                raise OfficialSourceCaptureBlocked(
                    "official source redirects are not followed; authorize the canonical final URL"
                )
            if response.status != 200:
                raise OfficialSourceCaptureBlocked(
                    f"official source returned unexpected HTTP status {response.status}"
                )
            if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise OfficialSourceCaptureBlocked("official source content encoding must be identity")
            declared_length = _declared_content_length(headers)
            if declared_length is not None and declared_length > max_bytes:
                raise OfficialSourceCaptureBlocked("official source exceeds the authorized byte limit")
            body = _read_bounded(response, max_bytes=max_bytes)
            media_type = _normalize_media_type(headers.get("content-type", ""))
            return OfficialHttpResponse(
                status_code=response.status,
                final_url=url,
                media_type=media_type,
                headers=headers,
                body=body,
                peer_ip=peer_ip,
            )
        except OfficialSourceCaptureBlocked:
            raise
        except ssl.SSLError as error:
            raise OfficialSourceCaptureBlocked(
                "official source TLS certificate verification failed"
            ) from error
        except Exception as error:
            raise OfficialSourceCaptureBlocked("official source TLS fetch failed") from error
        finally:
            connection.close()


def capture_authorized_official_source(
    *,
    request: ExternalResearchRequest,
    gateway: PublicResearchGateway,
    artifact_store: LocalEncryptedArtifactStore,
    case_root: str,
    transport: OfficialSourceTransport,
    now: datetime | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> CapturedOfficialSource:
    current = _aware_now(now)
    try:
        registered_request = gateway.request(request.request_id)
    except KeyError as error:
        raise OfficialSourceCaptureBlocked("official source request is not registered") from error
    if registered_request != request:
        raise OfficialSourceCaptureBlocked("official source request differs from its authorization record")
    if request.authorized_at.tzinfo is None:
        raise OfficialSourceCaptureBlocked("official source authorization time must include a timezone")
    age = current - request.authorized_at
    if age < timedelta(0) or age > _MAX_AUTHORIZATION_AGE:
        raise OfficialSourceCaptureBlocked("official source authorization is expired or future-dated")
    source = gateway.source(request.source_id)
    _validate_request_source(request=request, source=source)
    response = transport.fetch(url=request.target_url, max_bytes=max_bytes)
    _validate_response(response=response, request=request, source=source, max_bytes=max_bytes)
    content_hash = sha256(response.body).hexdigest()
    encrypted = artifact_store.put_bytes(
        response.body,
        expected_sha256=content_hash,
        case_root=case_root,
    )
    receipt = gateway.record_response(
        request_id=request.request_id,
        source_url=request.target_url,
        response_body=response.body,
    )
    verification_hash = sha256(
        json.dumps(
            {
                "schema_version": "official-source-capture-verification-v1",
                "request_id": request.request_id,
                "plan_id": request.plan_id,
                "source_id": request.source_id,
                "query_hash": request.query_hash,
                "requested_url": request.target_url,
                "final_url": response.final_url,
                "retrieved_at": current.isoformat(),
                "media_type": response.media_type,
                "content_sha256": content_hash,
                "content_bytes": len(response.body),
                "encrypted_object_key": encrypted.object_key,
                "peer_ip": response.peer_ip,
                "review_status": "HUMAN_REVIEW_REQUIRED",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CapturedOfficialSource(
        request_id=request.request_id,
        source_id=source.source_id,
        publisher=source.publisher,
        source_tier=source.source_tier,
        requested_url=request.target_url,
        final_url=response.final_url,
        retrieved_at=current,
        media_type=response.media_type,
        content_sha256=content_hash,
        content_bytes=len(response.body),
        encrypted_object=encrypted,
        response_receipt=receipt,
        verification_hash=verification_hash,
    )


def _validate_request_source(*, request: ExternalResearchRequest, source: PublicSource) -> None:
    parsed = _validated_https_url(request.target_url)
    if parsed.hostname not in source.allowed_domains:
        raise OfficialSourceCaptureBlocked("authorized URL is outside the registered source domains")


def _validate_response(
    *,
    response: OfficialHttpResponse,
    request: ExternalResearchRequest,
    source: PublicSource,
    max_bytes: int,
) -> None:
    if response.status_code != 200:
        raise OfficialSourceCaptureBlocked("official source response is not successful")
    if response.final_url != request.target_url:
        raise OfficialSourceCaptureBlocked("official source final URL differs from the authorized URL")
    parsed = _validated_https_url(response.final_url)
    if parsed.hostname not in source.allowed_domains:
        raise OfficialSourceCaptureBlocked("official source final URL is outside the source domains")
    _require_global_peer(response.peer_ip)
    media_type = _normalize_media_type(response.media_type)
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise OfficialSourceCaptureBlocked("official source media type is not archiveable")
    if not response.body or len(response.body) > max_bytes:
        raise OfficialSourceCaptureBlocked("official source body is empty or exceeds the byte limit")
    _validate_content_shape(media_type, response.body)


def _validated_https_url(url: str):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise OfficialSourceCaptureBlocked("official source requires canonical credential-free HTTPS")
    return parsed


def _require_global_peer(value: str) -> None:
    try:
        address = ip_address(value)
    except ValueError as error:
        raise OfficialSourceCaptureBlocked("official source peer IP is invalid") from error
    if not address.is_global:
        raise OfficialSourceCaptureBlocked("official source peer IP is not globally routable")


def _declared_content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise OfficialSourceCaptureBlocked("official source Content-Length is invalid") from error
    if value < 0:
        raise OfficialSourceCaptureBlocked("official source Content-Length is invalid")
    return value


def _read_bounded(response, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise OfficialSourceCaptureBlocked("official source body exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _normalize_media_type(value: str) -> str:
    media_type = value.split(";", 1)[0].strip().lower()
    if not media_type or not _SAFE_TOKEN.fullmatch(media_type.replace("/", "", 1)) or "/" not in media_type:
        raise OfficialSourceCaptureBlocked("official source Content-Type is invalid")
    return media_type


def _validate_content_shape(media_type: str, body: bytes) -> None:
    if media_type == "application/pdf" and not body.startswith(b"%PDF-"):
        raise OfficialSourceCaptureBlocked("official source PDF signature is invalid")
    if media_type in {"text/html", "application/xhtml+xml"}:
        prefix = body[:4096].lstrip().lower()
        if b"<html" not in prefix and b"<!doctype html" not in prefix:
            raise OfficialSourceCaptureBlocked("official source HTML signature is invalid")
    if media_type == "application/json":
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OfficialSourceCaptureBlocked("official source JSON structure is invalid") from error
    if media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" and not body.startswith(b"PK"):
        raise OfficialSourceCaptureBlocked("official source XLSX signature is invalid")


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise OfficialSourceCaptureBlocked("official source capture time must include a timezone")
    return current
