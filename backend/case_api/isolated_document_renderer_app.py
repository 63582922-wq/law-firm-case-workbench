"""Internal-only FastAPI process for generated DOCX/XLSX PDF previews.

This application is not mounted into the lawyer Web API.  It is launched as a
separate, non-root Docker service on an ``internal: true`` bridge and accepts
only HMAC-authenticated bytes from the case Agent Worker.  It has no route for
browser paths, URLs, commands or original case objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import os
from pathlib import Path
import re
import subprocess
from threading import Lock
from time import time
from typing import Callable, Mapping

from fastapi import FastAPI, HTTPException, Request, Response, status

from case_kernel.isolated_document_renderer import (
    DEFAULT_CLOCK_SKEW_SECONDS,
    MAX_OFFICE_BYTES,
    MAX_RENDERED_PDF_BYTES,
    RENDER_HEALTH_BODY,
    RENDER_PATH,
    RENDER_PROTOCOL_VERSION,
    IsolatedDocumentRendererBlocked,
    decode_shared_secret_base64url,
    sign_render_request,
    sign_render_response,
    sign_renderer_health,
    validate_render_input,
    validate_shared_secret,
)
from case_kernel.office_pdf_conversion_worker import ConvertedOfficePdf
from case_kernel.web_office_pdf_converter import (
    WebOfficePdfConversionBlocked,
    WebOfficePdfConverter,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AUTHORIZATION = re.compile(r"^HMAC-SHA256 ([0-9a-f]{64})$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]{0,11})$")


class IsolatedDocumentRendererStartupBlocked(RuntimeError):
    """Production renderer settings or executable probes failed."""


@dataclass(frozen=True)
class IsolatedDocumentRendererServerSettings:
    shared_secret: bytes = field(repr=False)
    soffice_executable: Path
    pdftoppm_executable: Path
    worker_root: Path
    timeout_seconds: int = 180
    max_clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS
    replay_capacity: int = 20_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "shared_secret", validate_shared_secret(self.shared_secret))
        object.__setattr__(
            self,
            "soffice_executable",
            _absolute_path(self.soffice_executable, "LibreOffice executable"),
        )
        object.__setattr__(
            self,
            "pdftoppm_executable",
            _absolute_path(self.pdftoppm_executable, "pdftoppm executable"),
        )
        root = Path(self.worker_root)
        if not root.is_absolute() or root.is_symlink():
            raise IsolatedDocumentRendererStartupBlocked("renderer work root must be an absolute non-symbolic path")
        object.__setattr__(self, "worker_root", root)
        if not 10 <= self.timeout_seconds <= 300:
            raise IsolatedDocumentRendererStartupBlocked("renderer timeout is invalid")
        if not 10 <= self.max_clock_skew_seconds <= 300:
            raise IsolatedDocumentRendererStartupBlocked("renderer clock skew limit is invalid")
        if not 1_000 <= self.replay_capacity <= 100_000:
            raise IsolatedDocumentRendererStartupBlocked("renderer replay capacity is invalid")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "IsolatedDocumentRendererServerSettings":
        values = os.environ if environment is None else environment
        return cls(
            shared_secret=_decode_server_secret(
                _required(values, "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET")
            ),
            soffice_executable=Path(
                _required(values, "LAWCASE_DOCUMENT_RENDERER_SOFFICE_EXECUTABLE")
            ),
            pdftoppm_executable=Path(
                _required(values, "LAWCASE_DOCUMENT_RENDERER_PDFTOPPM_EXECUTABLE")
            ),
            worker_root=Path(_required(values, "LAWCASE_DOCUMENT_RENDERER_WORKER_ROOT")),
            timeout_seconds=_integer(
                values.get("LAWCASE_DOCUMENT_RENDERER_TIMEOUT_SECONDS", "180"),
                "renderer timeout",
            ),
            max_clock_skew_seconds=_integer(
                values.get("LAWCASE_DOCUMENT_RENDERER_MAX_CLOCK_SKEW_SECONDS", "60"),
                "renderer clock skew",
            ),
            replay_capacity=_integer(
                values.get("LAWCASE_DOCUMENT_RENDERER_REPLAY_CAPACITY", "20000"),
                "renderer replay capacity",
            ),
        )


class ShortLivedNonceStore:
    """Bounded single-process replay barrier for the one-replica sidecar."""

    def __init__(self, *, capacity: int, ttl_seconds: int, clock: Callable[[], float]) -> None:
        if capacity < 1 or ttl_seconds < 1:
            raise ValueError("renderer nonce store configuration is invalid")
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, float] = {}
        self._lock = Lock()

    def consume(self, nonce: str) -> bool:
        now = self._clock()
        with self._lock:
            expired = [key for key, expires_at in self._items.items() if expires_at <= now]
            for key in expired:
                self._items.pop(key, None)
            if nonce in self._items:
                return False
            if len(self._items) >= self._capacity:
                # Fail closed rather than evicting a still-valid nonce and
                # silently reopening its replay window.
                return False
            self._items[nonce] = now + self._ttl
            return True


class IsolatedDocumentRendererRuntime:
    def __init__(
        self,
        *,
        settings: IsolatedDocumentRendererServerSettings,
        converter: WebOfficePdfConverter,
        clock: Callable[[], float] = time,
        nonce_store: ShortLivedNonceStore | None = None,
        health_probe: Callable[[], None] | None = None,
    ) -> None:
        if not callable(getattr(converter, "convert_generated_document", None)):
            raise ValueError("isolated document converter is invalid")
        self.settings = settings
        self.converter = converter
        self.clock = clock
        self.nonces = nonce_store or ShortLivedNonceStore(
            capacity=settings.replay_capacity,
            ttl_seconds=settings.max_clock_skew_seconds * 2,
            clock=clock,
        )
        self.health_probe = health_probe or (
            lambda: preflight_renderer_executables(
                soffice_executable=settings.soffice_executable,
                pdftoppm_executable=settings.pdftoppm_executable,
            )
        )


def compose_isolated_document_renderer(
    settings: IsolatedDocumentRendererServerSettings,
) -> IsolatedDocumentRendererRuntime:
    """Probe real binaries, then construct the fixed LibreOffice converter."""

    preflight_renderer_executables(
        soffice_executable=settings.soffice_executable,
        pdftoppm_executable=settings.pdftoppm_executable,
    )
    try:
        converter = WebOfficePdfConverter(
            soffice_executable=settings.soffice_executable,
            pdf_renderer_executable=settings.pdftoppm_executable,
            worker_root=settings.worker_root,
            timeout_seconds=settings.timeout_seconds,
        )
    except (WebOfficePdfConversionBlocked, OSError) as error:
        raise IsolatedDocumentRendererStartupBlocked(
            "renderer converter could not be initialized"
        ) from error
    return IsolatedDocumentRendererRuntime(settings=settings, converter=converter)


def preflight_renderer_executables(
    *, soffice_executable: str | Path, pdftoppm_executable: str | Path
) -> None:
    """Execute both configured programs; file existence alone is insufficient."""

    soffice = _absolute_path(soffice_executable, "LibreOffice executable")
    renderer = _absolute_path(pdftoppm_executable, "pdftoppm executable")
    _probe_executable((str(soffice), "--version"), "LibreOffice")
    _probe_executable((str(renderer), "-v"), "pdftoppm")


def create_isolated_document_renderer_app(
    runtime: IsolatedDocumentRendererRuntime | None = None,
) -> FastAPI:
    selected = runtime or compose_isolated_document_renderer(
        IsolatedDocumentRendererServerSettings.from_environment()
    )
    app = FastAPI(
        title="Lawcase isolated document renderer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/healthz")
    async def healthz() -> Response:
        try:
            selected.health_probe()
        except Exception as error:
            raise HTTPException(status_code=503, detail="renderer executable preflight failed") from error
        return Response(
            content=RENDER_HEALTH_BODY,
            media_type="application/json",
            headers={
                "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
                "X-Lawcase-Health-Signature": sign_renderer_health(
                    secret=selected.settings.shared_secret
                ),
            },
        )

    @app.post(RENDER_PATH)
    async def render_office(request: Request) -> Response:
        metadata = _authenticate_request_headers(request, selected)
        body = await _read_bounded_body(request, metadata["content_length"])
        try:
            validate_render_input(
                content=body,
                content_sha256=metadata["source_sha256"],
                source_name=metadata["source_name"],
                detected_kind=metadata["detected_kind"],
            )
            converted = selected.converter.convert_generated_document(
                body,
                content_sha256=metadata["source_sha256"],
                source_name=metadata["source_name"],
                detected_kind=metadata["detected_kind"],
            )
            _validate_converted(converted, metadata=metadata)
        except (IsolatedDocumentRendererBlocked, WebOfficePdfConversionBlocked) as error:
            raise HTTPException(status_code=422, detail="document rendering was rejected") from error
        headers = _response_headers(
            converted=converted,
            secret=selected.settings.shared_secret,
            request_nonce=metadata["nonce"],
        )
        return Response(
            content=converted.pdf_content,
            media_type="application/pdf",
            headers=headers,
            status_code=status.HTTP_200_OK,
        )

    return app


def _authenticate_request_headers(
    request: Request, runtime: IsolatedDocumentRendererRuntime
) -> dict[str, str | int]:
    def header(name: str, maximum: int = 300) -> str:
        value = request.headers.get(name)
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise HTTPException(status_code=401, detail="renderer authentication failed")
        return value

    protocol = header("X-Lawcase-Render-Protocol")
    timestamp = header("X-Lawcase-Render-Timestamp", 12)
    nonce = header("X-Lawcase-Render-Nonce", 43)
    detected_kind = header("X-Lawcase-Detected-Kind", 32)
    source_name = header("X-Lawcase-Source-Name", 64)
    source_sha256 = header("X-Lawcase-Source-SHA256", 64)
    media_type = header("Content-Type", 120)
    content_length_text = header("Content-Length", 12)
    authorization = header("Authorization", 80)
    match = _AUTHORIZATION.fullmatch(authorization)
    if (
        protocol != RENDER_PROTOCOL_VERSION
        or _INTEGER.fullmatch(timestamp) is None
        or _NONCE.fullmatch(nonce) is None
        or _SHA256.fullmatch(source_sha256) is None
        or _INTEGER.fullmatch(content_length_text) is None
        or match is None
    ):
        raise HTTPException(status_code=401, detail="renderer authentication failed")
    content_length = int(content_length_text)
    if not 1 <= content_length <= MAX_OFFICE_BYTES:
        raise HTTPException(status_code=413, detail="renderer request size is invalid")
    now = int(runtime.clock())
    if abs(now - int(timestamp)) > runtime.settings.max_clock_skew_seconds:
        raise HTTPException(status_code=401, detail="renderer request expired")
    try:
        expected_media = validate_render_input_metadata(
            source_name=source_name,
            detected_kind=detected_kind,
            media_type=media_type,
        )
        expected = sign_render_request(
            secret=runtime.settings.shared_secret,
            timestamp=timestamp,
            nonce=nonce,
            detected_kind=detected_kind,
            source_name=source_name,
            content_sha256=source_sha256,
            content_length=content_length,
            media_type=expected_media,
        )
    except IsolatedDocumentRendererBlocked as error:
        raise HTTPException(status_code=401, detail="renderer authentication failed") from error
    if not hmac.compare_digest(match.group(1), expected):
        raise HTTPException(status_code=401, detail="renderer authentication failed")
    if not runtime.nonces.consume(nonce):
        raise HTTPException(status_code=409, detail="renderer request was replayed")
    return {
        "timestamp": timestamp,
        "nonce": nonce,
        "detected_kind": detected_kind,
        "source_name": source_name,
        "source_sha256": source_sha256,
        "media_type": media_type,
        "content_length": content_length,
    }


def validate_render_input_metadata(
    *, source_name: str, detected_kind: str, media_type: str
) -> str:
    expected = {
        "WORD_DOCUMENT": (
            "approved-draft.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "SPREADSHEET": (
            "approved-ledger.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }.get(detected_kind)
    if expected is None or (source_name, media_type) != expected:
        raise IsolatedDocumentRendererBlocked("renderer metadata is not server-fixed")
    return media_type


async def _read_bounded_body(request: Request, expected_length: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > expected_length or total > MAX_OFFICE_BYTES:
            raise HTTPException(status_code=413, detail="renderer body exceeded its declared size")
        chunks.append(chunk)
    if total != expected_length:
        raise HTTPException(status_code=400, detail="renderer body size differed")
    return b"".join(chunks)


def _validate_converted(converted: ConvertedOfficePdf, *, metadata: Mapping[str, object]) -> None:
    if not isinstance(converted, ConvertedOfficePdf):
        raise WebOfficePdfConversionBlocked("renderer returned an invalid conversion record")
    if (
        converted.source_sha256 != metadata["source_sha256"]
        or converted.detected_kind != metadata["detected_kind"]
        or _SHA256.fullmatch(converted.transform_hash or "") is None
        or _SHA256.fullmatch(converted.pdf_sha256 or "") is None
        or _SHA256.fullmatch(converted.render_verification_hash or "") is None
        or not converted.pdf_content.startswith(b"%PDF-")
        or not 1 <= len(converted.pdf_content) <= MAX_RENDERED_PDF_BYTES
        or converted.pdf_bytes != len(converted.pdf_content)
        or not hmac.compare_digest(sha256(converted.pdf_content).hexdigest(), converted.pdf_sha256)
        or not 1 <= converted.page_count <= 10_000
    ):
        raise WebOfficePdfConversionBlocked("renderer conversion record is not hash-bound")
    _safe_header(converted.converter_id, 80)
    _safe_header(converted.converter_version, 160)


def _response_headers(
    *, converted: ConvertedOfficePdf, secret: bytes, request_nonce: str
) -> dict[str, str]:
    signature = sign_render_response(
        secret=secret,
        request_nonce=request_nonce,
        source_sha256=converted.source_sha256,
        detected_kind=converted.detected_kind,
        converter_id=converted.converter_id,
        converter_version=converted.converter_version,
        transform_hash=converted.transform_hash,
        pdf_sha256=converted.pdf_sha256,
        pdf_bytes=converted.pdf_bytes,
        page_count=converted.page_count,
        render_verification_hash=converted.render_verification_hash,
    )
    return {
        "X-Lawcase-Render-Protocol": RENDER_PROTOCOL_VERSION,
        "X-Lawcase-Source-SHA256": converted.source_sha256,
        "X-Lawcase-Detected-Kind": converted.detected_kind,
        "X-Lawcase-Converter-Id": _safe_header(converted.converter_id, 80),
        "X-Lawcase-Converter-Version": _safe_header(converted.converter_version, 160),
        "X-Lawcase-Transform-SHA256": converted.transform_hash,
        "X-Lawcase-PDF-SHA256": converted.pdf_sha256,
        "X-Lawcase-PDF-Bytes": str(converted.pdf_bytes),
        "X-Lawcase-PDF-Page-Count": str(converted.page_count),
        "X-Lawcase-Render-Verification-SHA256": converted.render_verification_hash,
        "X-Lawcase-Response-Signature": signature,
    }


def _safe_header(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise WebOfficePdfConversionBlocked("renderer response header is invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise WebOfficePdfConversionBlocked("renderer response header is not ASCII") from error
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise WebOfficePdfConversionBlocked("renderer response header is invalid")
    return value


def _absolute_path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise IsolatedDocumentRendererStartupBlocked(f"{label} is not an explicit absolute path")
    return path


def _probe_executable(command: tuple[str, ...], label: str) -> None:
    executable = Path(command[0])
    if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
        raise IsolatedDocumentRendererStartupBlocked(f"{label} executable is unavailable")
    try:
        completed = subprocess.run(command, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IsolatedDocumentRendererStartupBlocked(f"{label} executable probe failed") from error
    output = (completed.stdout or completed.stderr).decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or not output:
        raise IsolatedDocumentRendererStartupBlocked(f"{label} executable probe failed")


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    if not isinstance(value, str) or not value or value.startswith("REPLACE_"):
        raise IsolatedDocumentRendererStartupBlocked(f"{name} is required")
    return value


def _decode_server_secret(value: str) -> bytes:
    try:
        return decode_shared_secret_base64url(value)
    except IsolatedDocumentRendererBlocked as error:
        raise IsolatedDocumentRendererStartupBlocked(
            "renderer shared secret must be unpadded base64url for 32 to 128 bytes"
        ) from error


def _integer(value: str, label: str) -> int:
    if not isinstance(value, str) or _INTEGER.fullmatch(value) is None:
        raise IsolatedDocumentRendererStartupBlocked(f"{label} is invalid")
    return int(value)


def main() -> None:
    # The host is intentionally fixed.  Docker network membership, not a
    # browser/server setting, decides who can reach this process.
    import uvicorn

    port = _integer(os.environ.get("LAWCASE_DOCUMENT_RENDERER_PORT", "8090"), "renderer port")
    if not 1024 <= port <= 65535:
        raise IsolatedDocumentRendererStartupBlocked("renderer port is invalid")
    uvicorn.run(
        create_isolated_document_renderer_app(),
        host="0.0.0.0",
        port=port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "IsolatedDocumentRendererRuntime",
    "IsolatedDocumentRendererServerSettings",
    "IsolatedDocumentRendererStartupBlocked",
    "ShortLivedNonceStore",
    "compose_isolated_document_renderer",
    "create_isolated_document_renderer_app",
    "preflight_renderer_executables",
    "validate_render_input_metadata",
]
