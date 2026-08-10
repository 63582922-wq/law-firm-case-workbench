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

from fastapi import FastAPI, HTTPException, Request, Response
import uvicorn

from case_api.desktop_enrollment import (
    DesktopEnrollmentBlocked,
    MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT,
    MACOS_KEYCHAIN_INSTALLATION_ACCOUNT,
    MACOS_KEYCHAIN_SERVICE,
    MacOSKeychainDesktopEnrollmentProvider,
    SignedDesktopEnrollmentVerifier,
)
from case_api.desktop_enrollment_http import (
    JsonFirmEnrollmentIssuer,
    PinnedHttpsJsonTransport,
)
from case_api.desktop_enrollment_lifecycle import (
    AuthenticatedFirmEnrollmentIssuer,
    DesktopEnrollmentLifecycle,
    DesktopEnrollmentLifecycleBlocked,
)
from case_api.desktop_identity_runtime import (
    DesktopIdentityRuntime,
    load_desktop_identity,
)
from case_api.desktop_trust_bootstrap import (
    DesktopEnrollmentTrustRuntime,
    DesktopTrustBootstrapBlocked,
    blocked_desktop_enrollment_trust,
    load_desktop_enrollment_trust,
)
from case_api.persistent_identity import PersistentAuthenticationBlocked
from case_api.desktop_persistent_runtime import (
    DesktopPersistentRuntimeBlocked,
    build_desktop_persistent_runtime,
)
from case_api.persistent_app import PersistentApiDependencies, create_persistent_app
from case_kernel.runtime import RuntimeConfigurationBlocked, RuntimeMode, RuntimeSettings


PROTOCOL = "lawcase-local-api-v1"
_CHALLENGE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARENT_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BINDING_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,160}$")
_VERIFY_REQUEST_FIELDS = frozenset({"envelope_text", "installation_binding_sha256"})
_MAX_VERIFY_REQUEST_BYTES = 20_000
_ACTIVATION_REQUEST_FIELDS = frozenset({"activation_secret", "operation_id"})
_MAX_ACTIVATION_REQUEST_BYTES = 1_024
_RENEWAL_REQUEST_FIELDS = frozenset({"operation_id"})
_REMOTE_REVOCATION_FIELDS = frozenset({"confirmation", "operation_id"})
_REMOTE_REVOCATION_CONFIRMATION = "CONFIRM_REMOTE_REVOCATION"
_REMOTE_STATUS_FIELDS = frozenset({"operation_id", "operation_kind"})
_REMOTE_OPERATION_KINDS = frozenset({"ACTIVATE", "RENEW", "REVOKE"})


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
    identity: DesktopIdentityRuntime | None = None,
    enrollment_issuer: AuthenticatedFirmEnrollmentIssuer | None = None,
    keychain_runner=None,
    persistent_dependencies: PersistentApiDependencies | None = None,
) -> FastAPI:
    if persistent_dependencies is not None:
        # The persistent API includes the same one-use desktop-session exchange
        # route. Returning it directly preserves its request correlation,
        # CORS, error handling, and authorization middleware instead of
        # copying routes into a second FastAPI application.
        return create_persistent_app(persistent_dependencies)
    trust = trust or load_desktop_enrollment_trust()
    identity = identity or DesktopIdentityRuntime(
        phase="NOT_ENROLLED",
        message="本机登记尚未装配。",
    )
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
            "identity": identity.phase.lower().replace("_", "-"),
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

    if identity.phase == "ENROLLED" and identity.session_authority is not None:

        @app.post("/v1/desktop-sessions/exchange")
        async def exchange_desktop_session(
            request: Request,
            response: Response,
        ) -> dict[str, str]:
            bootstrap_token = request.headers.get("x-desktop-bootstrap", "")
            try:
                grant = identity.session_authority.exchange(
                    request=request,
                    bootstrap_token=bootstrap_token,
                )
            except PersistentAuthenticationBlocked:
                raise HTTPException(
                    status_code=401,
                    detail="desktop session unavailable",
                ) from None
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return {
                "status": "SESSION_READY",
                "access_token": grant.access_token,
                "session_id": grant.session_id,
                "expires_at": grant.expires_at.isoformat().replace("+00:00", "Z"),
            }

    if (
        trust.phase == "READY"
        and trust.catalog is not None
        and parent_api_token is not None
        and enrollment_issuer is not None
    ):

        @app.post("/v1/desktop-enrollment/activate")
        async def activate_desktop_enrollment(request: Request) -> dict[str, str]:
            _require_parent_authorization(request, parent_api_token)
            body = await request.body()
            if not body or len(body) > _MAX_ACTIVATION_REQUEST_BYTES:
                raise HTTPException(status_code=422, detail="activation unavailable")
            try:
                payload = _strict_request_json(body)
                if frozenset(payload) != _ACTIVATION_REQUEST_FIELDS:
                    raise ValueError("invalid fields")
                activation_secret = payload.get("activation_secret")
                operation_id = payload.get("operation_id")
                if (
                    not isinstance(activation_secret, str)
                    or not isinstance(operation_id, str)
                    or not _OPERATION_ID_PATTERN.fullmatch(operation_id)
                ):
                    raise ValueError("invalid secret")
                lifecycle, vault = _staged_registration_lifecycle(
                    trust=trust,
                    issuer=enrollment_issuer,
                    keychain_runner=keychain_runner,
                    operation_id=operation_id,
                )
                result = lifecycle.register(activation_secret=activation_secret)
            except (
                DesktopEnrollmentBlocked,
                DesktopEnrollmentLifecycleBlocked,
                UnicodeDecodeError,
                ValueError,
            ):
                raise HTTPException(status_code=422, detail="activation unavailable") from None
            envelope_text = vault.current_envelope()
            return {
                "status": "REGISTERED",
                "operation_id": operation_id,
                "enrollment_id": result.enrollment_id or "",
                "envelope_text": envelope_text,
                "envelope_sha256": sha256(envelope_text.encode("utf-8")).hexdigest(),
                "installation_binding_sha256": vault.installation_binding_sha256,
                "expires_at": result.expires_at.isoformat().replace("+00:00", "Z")
                if result.expires_at is not None
                else "",
            }

        @app.post("/v1/desktop-enrollment/renew")
        async def renew_desktop_enrollment(request: Request) -> dict[str, str]:
            _require_parent_authorization(request, parent_api_token)
            body = await request.body()
            if not body or len(body) > 256:
                raise HTTPException(status_code=422, detail="renewal unavailable")
            try:
                payload = _strict_request_json(body)
                if frozenset(payload) != _RENEWAL_REQUEST_FIELDS:
                    raise ValueError("invalid fields")
                operation_id = payload.get("operation_id")
                if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(
                    operation_id
                ):
                    raise ValueError("invalid operation")
                lifecycle, vault = _staged_lifecycle(
                    trust=trust,
                    issuer=enrollment_issuer,
                    keychain_runner=keychain_runner,
                    operation_id=operation_id,
                )
                result = lifecycle.renew()
            except (
                DesktopEnrollmentBlocked,
                DesktopEnrollmentLifecycleBlocked,
                UnicodeDecodeError,
                ValueError,
            ):
                raise HTTPException(status_code=422, detail="renewal unavailable") from None
            envelope_text = vault.current_envelope()
            return {
                "status": "RENEWED",
                "operation_id": operation_id,
                "enrollment_id": result.enrollment_id or "",
                "envelope_text": envelope_text,
                "envelope_sha256": sha256(envelope_text.encode("utf-8")).hexdigest(),
                "expected_current_sha256": vault.initial_envelope_sha256,
                "installation_binding_sha256": vault.installation_binding_sha256,
                "expires_at": result.expires_at.isoformat().replace("+00:00", "Z")
                if result.expires_at is not None
                else "",
            }

        @app.post("/v1/desktop-enrollment/revoke")
        async def revoke_desktop_enrollment(request: Request) -> dict[str, str | bool]:
            _require_parent_authorization(request, parent_api_token)
            body = await request.body()
            if not body or len(body) > 256:
                raise HTTPException(status_code=422, detail="invalid revocation request")
            try:
                payload = _strict_request_json(body)
                if frozenset(payload) != _REMOTE_REVOCATION_FIELDS:
                    raise ValueError("invalid fields")
                if payload.get("confirmation") != _REMOTE_REVOCATION_CONFIRMATION:
                    raise ValueError("invalid confirmation")
                operation_id = payload.get("operation_id")
                if not isinstance(operation_id, str) or not _OPERATION_ID_PATTERN.fullmatch(
                    operation_id
                ):
                    raise ValueError("invalid operation")
                lifecycle, vault = _staged_lifecycle(
                    trust=trust,
                    issuer=enrollment_issuer,
                    keychain_runner=keychain_runner,
                    operation_id=operation_id,
                )
                result = lifecycle.revoke(reason="用户在本机主动请求撤销登记")
            except (DesktopEnrollmentBlocked, DesktopEnrollmentLifecycleBlocked, ValueError):
                raise HTTPException(status_code=422, detail="revocation unavailable") from None
            return {
                "status": "REVOKED",
                "operation_id": operation_id,
                "enrollment_id": result.enrollment_id or "",
                "expected_current_sha256": vault.initial_envelope_sha256,
                "remote_revocation_confirmed": result.remote_revocation_confirmed,
            }

        @app.post("/v1/desktop-enrollment/status")
        async def desktop_enrollment_operation_status(
            request: Request,
        ) -> dict[str, str | bool | None]:
            _require_parent_authorization(request, parent_api_token)
            body = await request.body()
            if not body or len(body) > 512:
                raise HTTPException(status_code=422, detail="operation status unavailable")
            try:
                payload = _strict_request_json(body)
                if frozenset(payload) != _REMOTE_STATUS_FIELDS:
                    raise ValueError("invalid fields")
                operation_id = payload.get("operation_id")
                operation_kind = payload.get("operation_kind")
                if (
                    not isinstance(operation_id, str)
                    or not _OPERATION_ID_PATTERN.fullmatch(operation_id)
                    or not isinstance(operation_kind, str)
                    or operation_kind not in _REMOTE_OPERATION_KINDS
                ):
                    raise ValueError("invalid operation")
                if operation_kind == "ACTIVATE":
                    lifecycle, vault = _staged_registration_lifecycle(
                        trust=trust,
                        issuer=enrollment_issuer,
                        keychain_runner=keychain_runner,
                        operation_id=operation_id,
                    )
                else:
                    lifecycle, vault = _staged_lifecycle(
                        trust=trust,
                        issuer=enrollment_issuer,
                        keychain_runner=keychain_runner,
                        operation_id=operation_id,
                    )
                resolution = lifecycle.resolve_remote_operation(
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                )
                return _operation_resolution_payload(resolution, vault)
            except (
                DesktopEnrollmentBlocked,
                DesktopEnrollmentLifecycleBlocked,
                UnicodeDecodeError,
                ValueError,
            ):
                raise HTTPException(
                    status_code=422,
                    detail="operation status unavailable",
                ) from None

    return app


def _require_parent_authorization(request: Request, parent_api_token: str) -> None:
    authorization = request.headers.get("authorization", "")
    if not compare_digest(authorization, f"Bearer {parent_api_token}"):
        raise HTTPException(status_code=404, detail="not found")


class _StagedEnrollmentVault:
    def __init__(self, *, envelope_text: str | None, installation_secret: bytes) -> None:
        self._envelope = envelope_text
        self._installation_secret = installation_secret
        self.initial_envelope_sha256 = (
            sha256(envelope_text.encode("utf-8")).hexdigest()
            if envelope_text is not None
            else None
        )
        self.installation_binding_sha256 = sha256(installation_secret).hexdigest()

    def installation_secret(self) -> bytes:
        return self._installation_secret

    def current_envelope(self) -> str:
        if self._envelope is None:
            raise KeyError("missing")
        return self._envelope

    def current_envelope_optional(self) -> str | None:
        return self._envelope

    def replace_enrollment(self, *, expected_sha256: str | None, envelope_text: str) -> None:
        current = (
            sha256(self._envelope.encode("utf-8")).hexdigest()
            if self._envelope is not None
            else None
        )
        if current != expected_sha256:
            raise RuntimeError("staged enrollment changed")
        self._envelope = envelope_text

    def delete_enrollment(self, *, expected_sha256: str) -> None:
        current = sha256(self.current_envelope().encode("utf-8")).hexdigest()
        if current != expected_sha256:
            raise RuntimeError("staged enrollment changed")
        self._envelope = None


def _staged_lifecycle(
    *,
    trust: DesktopEnrollmentTrustRuntime,
    issuer: AuthenticatedFirmEnrollmentIssuer,
    keychain_runner=None,
    operation_id: str | None = None,
) -> tuple[DesktopEnrollmentLifecycle, _StagedEnrollmentVault]:
    if trust.catalog is None:
        raise DesktopEnrollmentLifecycleBlocked("trust catalog is unavailable")
    verifier = SignedDesktopEnrollmentVerifier(trusted_catalog=trust.catalog)
    provider = MacOSKeychainDesktopEnrollmentProvider(
        service=MACOS_KEYCHAIN_SERVICE,
        enrollment_account=MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT,
        installation_secret_account=MACOS_KEYCHAIN_INSTALLATION_ACCOUNT,
        verifier=verifier,
        runner=keychain_runner,
    )
    material = provider.load_material_optional()
    if material is None:
        raise DesktopEnrollmentLifecycleBlocked("current enrollment is unavailable")
    vault = _StagedEnrollmentVault(
        envelope_text=material.envelope_text,
        installation_secret=material.installation_secret,
    )
    return DesktopEnrollmentLifecycle(
        issuer=issuer,
        vault=vault,
        verifier=verifier,
        nonce_factory=(lambda: operation_id) if operation_id is not None else None,
    ), vault


def _staged_registration_lifecycle(
    *,
    trust: DesktopEnrollmentTrustRuntime,
    issuer: AuthenticatedFirmEnrollmentIssuer,
    keychain_runner=None,
    operation_id: str | None = None,
) -> tuple[DesktopEnrollmentLifecycle, _StagedEnrollmentVault]:
    if trust.catalog is None:
        raise DesktopEnrollmentLifecycleBlocked("trust catalog is unavailable")
    verifier = SignedDesktopEnrollmentVerifier(trusted_catalog=trust.catalog)
    provider = MacOSKeychainDesktopEnrollmentProvider(
        service=MACOS_KEYCHAIN_SERVICE,
        enrollment_account=MACOS_KEYCHAIN_ENROLLMENT_ACCOUNT,
        installation_secret_account=MACOS_KEYCHAIN_INSTALLATION_ACCOUNT,
        verifier=verifier,
        runner=keychain_runner,
    )
    installation_secret = provider.load_registration_installation_secret()
    vault = _StagedEnrollmentVault(
        envelope_text=None,
        installation_secret=installation_secret,
    )
    return DesktopEnrollmentLifecycle(
        issuer=issuer,
        vault=vault,
        verifier=verifier,
        nonce_factory=(lambda: operation_id) if operation_id is not None else None,
    ), vault


def _operation_resolution_payload(resolution, vault: _StagedEnrollmentVault):
    result = resolution.result
    envelope_text = ""
    envelope_sha256 = ""
    if resolution.state == "SUCCEEDED" and resolution.operation_kind in {
        "ACTIVATE",
        "RENEW",
    }:
        envelope_text = vault.current_envelope()
        envelope_sha256 = sha256(envelope_text.encode("utf-8")).hexdigest()
    return {
        "status": resolution.state,
        "operation_id": resolution.operation_id,
        "operation_kind": resolution.operation_kind,
        "enrollment_id": result.enrollment_id if result is not None else "",
        "envelope_text": envelope_text,
        "envelope_sha256": envelope_sha256,
        "expected_current_sha256": vault.initial_envelope_sha256,
        "installation_binding_sha256": vault.installation_binding_sha256,
        "expires_at": (
            result.expires_at.isoformat().replace("+00:00", "Z")
            if result is not None and result.expires_at is not None
            else ""
        ),
        "remote_revocation_confirmed": (
            result.remote_revocation_confirmed if result is not None else False
        ),
    }


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
    identity = load_desktop_identity(
        trust=trust,
        bootstrap_token=parent_api_token,
    )
    try:
        runtime_settings = RuntimeSettings.from_environment(os.environ)
        persistent_runtime = (
            build_desktop_persistent_runtime(
                identity=identity,
                environ=os.environ,
            )
            if runtime_settings.mode is RuntimeMode.POSTGRES_INTERNAL_PREVIEW
            else None
        )
    except (RuntimeConfigurationBlocked, DesktopPersistentRuntimeBlocked):
        server_socket.close()
        print("本机受控服务的持久化前置条件未通过。", file=sys.stderr, flush=True)
        return 78
    enrollment_issuer = None
    if trust.phase == "READY" and trust.catalog is not None:
        enrollment_issuer = JsonFirmEnrollmentIssuer(
            PinnedHttpsJsonTransport(
                origin=trust.catalog.enrollment_api_origin,
                tls_spki_sha256=trust.catalog.tls_spki_sha256,
            )
        )

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
                    "identity": identity.phase,
                    "persistence": (
                        "CONFIGURED"
                        if persistent_runtime is not None
                        else "NOT_CONFIGURED"
                    ),
                    "enrollment_trust": trust.phase,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )

    config = uvicorn.Config(
        create_desktop_sidecar_app(
            trust,
            parent_api_token=parent_api_token,
            identity=identity,
            enrollment_issuer=enrollment_issuer,
            persistent_dependencies=(
                persistent_runtime.dependencies
                if persistent_runtime is not None
                else None
            ),
        ),
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
