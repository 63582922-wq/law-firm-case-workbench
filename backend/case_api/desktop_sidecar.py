"""Self-contained, supervised loopback service for the desktop application.

This process intentionally exposes no case routes until an enrolled lawyer
profile and guarded persistence dependencies are available.  The Tauri parent
starts it through a private stdin handshake and verifies the returned challenge
digest before treating the service as ready.
"""

from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
import json
import os
import re
import select
import socket
import sys
from threading import Thread
import time
from typing import Callable, TextIO

from fastapi import FastAPI, HTTPException, Request
import uvicorn

from case_api.desktop_enrollment import (
    DesktopEnrollmentBlocked,
    SignedDesktopEnrollmentVerifier,
)
from case_api.desktop_trust_bootstrap import (
    DesktopEnrollmentTrustRuntime,
    DesktopTrustBootstrapBlocked,
    blocked_desktop_enrollment_trust,
    load_desktop_enrollment_trust,
)


PROTOCOL = "lawcase-local-api-v1"
_CHALLENGE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARENT_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BINDING_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERIFY_REQUEST_FIELDS = frozenset({"envelope_text", "installation_binding_sha256"})
_MAX_VERIFY_REQUEST_BYTES = 20_000


class DesktopSidecarBlocked(RuntimeError):
    """The native parent handshake or loopback binding failed closed."""


def read_parent_handshake(stream: TextIO) -> tuple[str, int, str]:
    line = stream.readline(2049)
    if not line or len(line) > 2048 or not line.endswith("\n"):
        raise DesktopSidecarBlocked("desktop parent handshake is unavailable")
    try:
        payload = json.loads(line)
    except (TypeError, ValueError) as error:
        raise DesktopSidecarBlocked("desktop parent handshake is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {
        "protocol",
        "challenge",
        "parent_pid",
        "parent_api_token",
    }:
        raise DesktopSidecarBlocked("desktop parent handshake fields are invalid")
    challenge = payload.get("challenge")
    parent_pid = payload.get("parent_pid")
    parent_api_token = payload.get("parent_api_token")
    if payload.get("protocol") != PROTOCOL or not isinstance(challenge, str):
        raise DesktopSidecarBlocked("desktop parent handshake protocol is invalid")
    if not _CHALLENGE_PATTERN.fullmatch(challenge):
        raise DesktopSidecarBlocked("desktop parent challenge is invalid")
    if not isinstance(parent_pid, int) or isinstance(parent_pid, bool) or parent_pid <= 1:
        raise DesktopSidecarBlocked("desktop parent process is invalid")
    if not isinstance(parent_api_token, str) or not _PARENT_TOKEN_PATTERN.fullmatch(
        parent_api_token
    ):
        raise DesktopSidecarBlocked("desktop parent API token is invalid")
    return challenge, parent_pid, parent_api_token


def create_desktop_sidecar_app(
    trust: DesktopEnrollmentTrustRuntime | None = None,
    *,
    parent_api_token: str | None = None,
) -> FastAPI:
    trust = trust or load_desktop_enrollment_trust()
    app = FastAPI(
        title="律所案件 AI 工作台 · 本机受控服务",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "service": "lawcase-local-api",
            "mode": "desktop-disabled",
            "persistence": "not-configured",
            "identity": "not-enrolled",
            "enrollment_trust": trust.phase.lower().replace("_", "-"),
        }

    if trust.phase == "READY" and trust.catalog is not None and parent_api_token is not None:

        @app.post("/v1/desktop-enrollment/verify")
        async def verify_desktop_enrollment(request: Request) -> dict[str, str]:
            authorization = request.headers.get("authorization", "")
            expected_authorization = f"Bearer {parent_api_token}"
            if not compare_digest(authorization, expected_authorization):
                raise HTTPException(status_code=404, detail="not found")
            body = await request.body()
            if not body or len(body) > _MAX_VERIFY_REQUEST_BYTES:
                raise HTTPException(status_code=422, detail="invalid enrollment package")
            try:
                payload = _strict_request_json(body)
                if frozenset(payload) != _VERIFY_REQUEST_FIELDS:
                    raise ValueError("invalid fields")
                envelope_text = payload.get("envelope_text")
                binding = payload.get("installation_binding_sha256")
                if (
                    not isinstance(envelope_text, str)
                    or not isinstance(binding, str)
                    or not _BINDING_PATTERN.fullmatch(binding)
                ):
                    raise ValueError("invalid values")
                enrollment = SignedDesktopEnrollmentVerifier(
                    trusted_catalog=trust.catalog
                ).verify_for_installation_binding(
                    envelope_text=envelope_text,
                    installation_binding_sha256=binding,
                )
            except (DesktopEnrollmentBlocked, UnicodeDecodeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="invalid enrollment package",
                ) from None
            return {
                "status": "VERIFIED",
                "envelope_sha256": sha256(envelope_text.encode("utf-8")).hexdigest(),
                "enrollment_id": enrollment.enrollment_id,
                "expires_at": enrollment.expires_at.isoformat().replace("+00:00", "Z"),
            }

    return app


def _strict_request_json(body: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    parsed = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=reject_duplicates,
    )
    if not isinstance(parsed, dict):
        raise ValueError("request must be an object")
    return parsed


class ReadyAnnouncingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, on_ready: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_ready = on_ready

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self._on_ready()


def _bind_loopback() -> socket.socket:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
    except OSError:
        server_socket.close()
        raise
    return server_socket


def _watch_parent(*, server: uvicorn.Server, parent_pid: int, stream: TextIO) -> None:
    while not server.should_exit:
        try:
            os.kill(parent_pid, 0)
        except OSError:
            server.should_exit = True
            return
        try:
            readable, _, _ = select.select([stream], [], [], 1.0)
        except (OSError, ValueError):
            server.should_exit = True
            return
        if readable and stream.read(1) == "":
            server.should_exit = True
            return
        time.sleep(0.05)


def run() -> int:
    try:
        challenge, parent_pid, parent_api_token = read_parent_handshake(sys.stdin)
        server_socket = _bind_loopback()
    except (DesktopSidecarBlocked, OSError):
        print("本机受控服务启动前置条件未通过。", file=sys.stderr, flush=True)
        return 78

    try:
        trust = load_desktop_enrollment_trust()
    except DesktopTrustBootstrapBlocked:
        trust = blocked_desktop_enrollment_trust()

    port = int(server_socket.getsockname()[1])
    def announce_ready() -> None:
        print(
            json.dumps(
                {
                    "protocol": PROTOCOL,
                    "status": "READY",
                    "port": port,
                    "pid": os.getpid(),
                    "challenge_sha256": sha256(challenge.encode("ascii")).hexdigest(),
                    "identity": "NOT_ENROLLED",
                    "persistence": "NOT_CONFIGURED",
                    "enrollment_trust": trust.phase,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    config = uvicorn.Config(
        create_desktop_sidecar_app(trust, parent_api_token=parent_api_token),
        host="127.0.0.1",
        port=port,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        access_log=False,
        server_header=False,
        date_header=False,
        log_config=None,
    )
    server = ReadyAnnouncingServer(config, on_ready=announce_ready)
    Thread(
        target=_watch_parent,
        kwargs={"server": server, "parent_pid": parent_pid, "stream": sys.stdin},
        daemon=True,
        name="lawcase-parent-watch",
    ).start()
    try:
        server.run(sockets=[server_socket])
    finally:
        server_socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
