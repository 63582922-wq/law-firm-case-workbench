"""Server-derived identity contract for the separate persistent preview API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
import re
from secrets import token_urlsafe
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import Request

from case_kernel.models import Actor


class AuthenticationMethod(str, Enum):
    OIDC_MFA = "OIDC_MFA"
    OS_BOUND_LOCAL_SESSION = "OS_BOUND_LOCAL_SESSION"


class PersistentAuthenticationBlocked(PermissionError):
    """No valid strong server-side identity is available for persistent access."""


@dataclass(frozen=True)
class ServerIdentityContext:
    actor: Actor
    session_id: str
    issuer: str
    authentication_method: AuthenticationMethod
    authenticated_at: datetime
    expires_at: datetime

    def validate(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        for label, value in (
            ("actor_id", self.actor.actor_id),
            ("firm_id", self.actor.firm_id),
            ("session_id", self.session_id),
        ):
            try:
                UUID(value)
            except (TypeError, ValueError) as error:
                raise PersistentAuthenticationBlocked(f"persistent identity requires UUID {label}") from error
        if self.authenticated_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise PersistentAuthenticationBlocked("persistent identity timestamps must be timezone-aware")
        if not self.issuer.strip():
            raise PersistentAuthenticationBlocked("persistent identity issuer is required")
        if self.authenticated_at > current:
            raise PersistentAuthenticationBlocked("persistent identity authentication time is in the future")
        if self.expires_at <= current:
            raise PersistentAuthenticationBlocked("persistent identity session has expired")


class ServerIdentityResolver(Protocol):
    """Implemented by desktop OS-bound session or an OIDC/MFA middleware."""

    async def resolve(self, request: Request) -> ServerIdentityContext: ...


class RejectAllIdentityResolver:
    async def resolve(self, request: Request) -> ServerIdentityContext:
        del request
        raise PersistentAuthenticationBlocked("persistent identity provider is not configured")


@dataclass(frozen=True)
class DesktopSessionGrant:
    """Opaque bearer returned once after native desktop bootstrap exchange."""

    access_token: str
    session_id: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "DesktopSessionGrant(access_token=<redacted>, "
            f"session_id={self.session_id!r}, expires_at={self.expires_at!r})"
        )


class DesktopSessionAuthority:
    """Process-local authority for a Tauri-started loopback API.

    The enrolled ``Actor`` is supplied by trusted server-side setup.  Neither
    the bootstrap request nor subsequent browser requests can choose an actor,
    firm or role.  Only token digests are retained after issuance.
    """

    _TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,160}$")

    def __init__(
        self,
        *,
        actor: Actor,
        bootstrap_token: str,
        bootstrap_expires_at: datetime,
        session_expires_at: datetime,
        issuer: str = "lawcase-desktop-os-session",
        allowed_origin: str = "tauri://localhost",
        clock=None,
        token_factory=None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: token_urlsafe(48))
        now = self._clock()
        if now.tzinfo is None:
            raise PersistentAuthenticationBlocked("desktop session clock must be timezone-aware")
        if not self._valid_token(bootstrap_token):
            raise PersistentAuthenticationBlocked("desktop bootstrap token is invalid")
        if bootstrap_expires_at.tzinfo is None or bootstrap_expires_at <= now:
            raise PersistentAuthenticationBlocked("desktop bootstrap expiry is invalid")
        if bootstrap_expires_at > now + timedelta(seconds=60):
            raise PersistentAuthenticationBlocked("desktop bootstrap lifetime is too long")
        if session_expires_at.tzinfo is None or session_expires_at <= bootstrap_expires_at:
            raise PersistentAuthenticationBlocked("desktop session expiry is invalid")
        if session_expires_at > now + timedelta(minutes=30):
            raise PersistentAuthenticationBlocked("desktop session lifetime is too long")
        if not issuer.strip() or not allowed_origin.strip():
            raise PersistentAuthenticationBlocked("desktop issuer and origin are required")

        # Validate the server-enrolled identity before accepting any bootstrap.
        ServerIdentityContext(
            actor=actor,
            session_id=str(uuid4()),
            issuer=issuer.strip(),
            authentication_method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            authenticated_at=now,
            expires_at=session_expires_at,
        ).validate(now=now)

        self._actor = actor
        self._issuer = issuer.strip()
        self._allowed_origin = allowed_origin.strip()
        self._bootstrap_digest = self._digest(bootstrap_token)
        self._bootstrap_expires_at = bootstrap_expires_at
        self._session_expires_at = session_expires_at
        self._bootstrap_consumed = False
        self._sessions: dict[bytes, ServerIdentityContext] = {}
        self._lock = Lock()

    def exchange(self, *, request: Request, bootstrap_token: str) -> DesktopSessionGrant:
        self._validate_request_boundary(request)
        now = self._clock()
        with self._lock:
            if self._bootstrap_consumed or self._bootstrap_expires_at <= now:
                raise PersistentAuthenticationBlocked("desktop bootstrap is unavailable")
            supplied_digest = self._digest_checked(bootstrap_token)
            if not compare_digest(supplied_digest, self._bootstrap_digest):
                raise PersistentAuthenticationBlocked("desktop bootstrap is invalid")

            access_token = self._token_factory()
            if not self._valid_token(access_token):
                raise PersistentAuthenticationBlocked("desktop session token generation failed")
            session_id = str(uuid4())
            identity = ServerIdentityContext(
                actor=self._actor,
                session_id=session_id,
                issuer=self._issuer,
                authentication_method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
                authenticated_at=now,
                expires_at=self._session_expires_at,
            )
            identity.validate(now=now)
            self._sessions[self._digest(access_token)] = identity
            self._bootstrap_consumed = True
            self._bootstrap_digest = b"\x00" * 32
            return DesktopSessionGrant(
                access_token=access_token,
                session_id=session_id,
                expires_at=identity.expires_at,
            )

    async def resolve(self, request: Request) -> ServerIdentityContext:
        self._validate_request_boundary(request)
        authorization = request.headers.get("authorization", "")
        scheme, separator, access_token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not self._valid_token(access_token):
            raise PersistentAuthenticationBlocked("desktop bearer token is missing")
        supplied_digest = self._digest(access_token)
        now = self._clock()
        with self._lock:
            identity = self._sessions.get(supplied_digest)
            if identity is None:
                raise PersistentAuthenticationBlocked("desktop bearer token is invalid")
            try:
                identity.validate(now=now)
            except PersistentAuthenticationBlocked:
                self._sessions.pop(supplied_digest, None)
                raise
            return identity

    def revoke(self, *, session_id: str) -> None:
        with self._lock:
            for digest, identity in tuple(self._sessions.items()):
                if identity.session_id == session_id:
                    self._sessions.pop(digest, None)

    def resolve_native_session(self, *, session_id: str) -> ServerIdentityContext:
        """Resolve the current desktop session for the native parent only.

        This is intentionally not an HTTP authentication path.  The caller is
        additionally required to prove the per-process parent token held by
        Tauri, so a WebView cannot turn a session identifier into an original
        evidence read capability.
        """
        try:
            UUID(session_id)
        except (TypeError, ValueError) as error:
            raise PersistentAuthenticationBlocked("desktop native session is invalid") from error
        now = self._clock()
        with self._lock:
            for digest, identity in tuple(self._sessions.items()):
                if identity.session_id != session_id:
                    continue
                try:
                    identity.validate(now=now)
                except PersistentAuthenticationBlocked:
                    self._sessions.pop(digest, None)
                    raise
                return identity
        raise PersistentAuthenticationBlocked("desktop native session is unavailable")

    def _validate_request_boundary(self, request: Request) -> None:
        host = request.client.host if request.client is not None else ""
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise PersistentAuthenticationBlocked("desktop session requires numeric loopback")
        if request.headers.get("origin", "") != self._allowed_origin:
            raise PersistentAuthenticationBlocked("desktop session origin is not allowed")

    @classmethod
    def _valid_token(cls, token: str) -> bool:
        return bool(cls._TOKEN_PATTERN.fullmatch(token))

    @classmethod
    def _digest_checked(cls, token: str) -> bytes:
        if not cls._valid_token(token):
            raise PersistentAuthenticationBlocked("desktop token is invalid")
        return cls._digest(token)

    @staticmethod
    def _digest(token: str) -> bytes:
        return sha256(token.encode("ascii")).digest()
