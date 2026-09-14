"""Fail-closed browser session boundary for the self-hosted Web API.

The browser never receives an OIDC JWT from this module.  A Web composition
root first verifies OIDC/MFA and obtains a server-side
:class:`~case_api.persistent_identity.ServerIdentityContext`; this authority
then creates a new high-entropy opaque session and a separate CSRF token.
Only SHA-256 digests of those two values are passed to persistence.

This module intentionally contains no FastAPI routes, Tauri integration,
desktop grant, database driver, or frontend code.  A later Web composition
must provide a narrow server-owned store and current-actor directory, then
install the returned cookie directives on its same-origin HTTPS responses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
from math import isfinite
import re
from secrets import token_urlsafe
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Request

from case_api.persistent_identity import (
    AuthenticationMethod,
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
)
from case_kernel.models import Actor, Role


__all__ = (
    "CookieDirective",
    "StoredWebSession",
    "WebSessionActorDirectory",
    "WebSessionAuthority",
    "WebSessionBlocked",
    "WebSessionGrant",
    "WebSessionPolicy",
    "WebSessionStore",
)


_DIGEST_BYTES = 32
_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,160}$")
_SAFE_COOKIE_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_SAFE_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_FETCH_SITE_VALUES = frozenset({"same-origin", "same-site", "cross-site", "none"})


class WebSessionBlocked(PersistentAuthenticationBlocked):
    """A browser session is missing, unsafe, stale, or unavailable."""


@dataclass(frozen=True)
class CookieDirective:
    """A server-only cookie instruction with a redacted value representation."""

    name: str
    value: str = field(repr=False)
    secure: bool = True
    httponly: bool = True
    samesite: str = "strict"
    path: str = "/"
    domain: None = None
    max_age: int | None = None

    def as_response_kwargs(self) -> dict[str, object]:
        """Return keyword arguments suitable for a future response adapter.

        The caller must pass these directly to a server-side cookie setter and
        must not serialize this dictionary into API JSON or application logs.
        """

        return {
            "key": self.name,
            "value": self.value,
            "secure": self.secure,
            "httponly": self.httponly,
            "samesite": self.samesite,
            "path": self.path,
            "domain": self.domain,
            "max_age": self.max_age,
        }


@dataclass(frozen=True)
class WebSessionPolicy:
    """Fixed browser boundary for an HTTPS, same-origin lawyer workbench."""

    public_origin: str
    session_cookie_name: str = "__Host-lawcase_session"
    csrf_cookie_name: str = "__Host-lawcase_csrf"
    csrf_header_name: str = "X-Lawcase-CSRF"
    same_site: str = "strict"
    # A lawyer's matter review commonly spans multiple uploads, asynchronous
    # Agent work, and a later human decision.  Keep that authenticated work
    # window stable for one workday instead of forcing a fresh MFA challenge
    # halfway through a normal case flow.  This remains a hard, server-side
    # upper bound: each request still rechecks revocation and the active human
    # directory mapping.
    max_session_lifetime: timedelta = timedelta(hours=12)
    max_auth_age: timedelta = timedelta(hours=12)

    def __post_init__(self) -> None:
        origin = _normalize_https_origin(self.public_origin)
        object.__setattr__(self, "public_origin", origin)
        _validate_host_cookie_name(self.session_cookie_name, label="session")
        _validate_host_cookie_name(self.csrf_cookie_name, label="CSRF")
        if self.session_cookie_name == self.csrf_cookie_name:
            raise ValueError("Web session and CSRF cookie names must differ")
        if not isinstance(self.csrf_header_name, str) or not _SAFE_HEADER_NAME.fullmatch(self.csrf_header_name):
            raise ValueError("Web session CSRF header name is invalid")
        object.__setattr__(self, "csrf_header_name", self.csrf_header_name.lower())
        if self.same_site != "strict":
            raise ValueError("Web session cookies must use SameSite=Strict")
        if not timedelta(minutes=1) <= self.max_session_lifetime <= timedelta(hours=12):
            raise ValueError("Web session lifetime must be between one minute and twelve hours")
        if not timedelta(minutes=1) <= self.max_auth_age <= timedelta(hours=24):
            raise ValueError("Web session MFA age must be within one day")

    def issue_cookies(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime,
        expires_at: datetime,
    ) -> tuple[CookieDirective, CookieDirective]:
        """Create the paired HttpOnly session and readable CSRF cookies."""

        remaining = int((expires_at - now).total_seconds())
        if remaining <= 0:
            raise WebSessionBlocked("Web session expiry is invalid")
        return (
            CookieDirective(
                name=self.session_cookie_name,
                value=session_token,
                secure=True,
                httponly=True,
                samesite=self.same_site,
                path="/",
                domain=None,
                max_age=remaining,
            ),
            CookieDirective(
                name=self.csrf_cookie_name,
                value=csrf_token,
                secure=True,
                httponly=False,
                samesite=self.same_site,
                path="/",
                domain=None,
                max_age=remaining,
            ),
        )

    def clear_cookies(self) -> tuple[CookieDirective, CookieDirective]:
        """Return secure deletion directives for a future logout route."""

        return (
            CookieDirective(
                name=self.session_cookie_name,
                value="",
                secure=True,
                httponly=True,
                samesite=self.same_site,
                path="/",
                domain=None,
                max_age=0,
            ),
            CookieDirective(
                name=self.csrf_cookie_name,
                value="",
                secure=True,
                httponly=False,
                samesite=self.same_site,
                path="/",
                domain=None,
                max_age=0,
            ),
        )


@dataclass(frozen=True)
class StoredWebSession:
    """Persistence shape containing server-derived identity and digests only.

    No browser-provided role, firm, raw session token, raw CSRF token, OIDC
    JWT, display name, or email belongs in this structure.
    """

    session_id: str
    actor_id: str
    firm_id: str
    issuer: str
    session_token_sha256: bytes = field(repr=False)
    csrf_token_sha256: bytes = field(repr=False)
    authenticated_at: datetime
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def validate(self, *, now: datetime | None = None) -> None:
        for label, value in (
            ("session_id", self.session_id),
            ("actor_id", self.actor_id),
            ("firm_id", self.firm_id),
        ):
            try:
                UUID(value)
            except (TypeError, ValueError) as error:
                raise WebSessionBlocked("Web session record is invalid") from error
        try:
            _validate_https_issuer(self.issuer)
        except ValueError:
            raise WebSessionBlocked("Web session record issuer is invalid") from None
        if (
            not isinstance(self.session_token_sha256, bytes)
            or len(self.session_token_sha256) != _DIGEST_BYTES
            or not isinstance(self.csrf_token_sha256, bytes)
            or len(self.csrf_token_sha256) != _DIGEST_BYTES
        ):
            raise WebSessionBlocked("Web session record digest is invalid")
        authenticated_at = _aware_datetime(self.authenticated_at, label="Web session authentication time")
        created_at = _aware_datetime(self.created_at, label="Web session creation time")
        expires_at = _aware_datetime(self.expires_at, label="Web session expiry")
        if authenticated_at > expires_at or created_at >= expires_at:
            raise WebSessionBlocked("Web session record lifetime is invalid")
        if created_at < authenticated_at:
            raise WebSessionBlocked("Web session record creation time is invalid")
        if self.revoked_at is not None:
            revoked_at = _aware_datetime(self.revoked_at, label="Web session revocation time")
            if revoked_at < created_at:
                raise WebSessionBlocked("Web session record revocation time is invalid")
        if now is not None and expires_at <= _aware_datetime(now, label="Web session clock"):
            raise WebSessionBlocked("Web session has expired")


@dataclass(frozen=True)
class WebSessionGrant:
    """Server-only issuance result.  Cookie values remain redacted in repr."""

    session_id: str
    expires_at: datetime
    session_cookie: CookieDirective
    csrf_cookie: CookieDirective


class WebSessionStore(Protocol):
    """Narrow persistence port for opaque Web sessions.

    The production implementation must persist only ``StoredWebSession`` and
    look up a session only by its SHA-256 digest in a server-owned transaction.
    It must not persist raw browser cookies or OIDC JWTs.
    """

    def create_session(self, *, session: StoredWebSession) -> None: ...

    def find_session_by_digest(self, *, session_token_sha256: bytes) -> StoredWebSession | None: ...

    def revoke_session(self, *, session_id: str, revoked_at: datetime) -> bool: ...


class WebSessionActorDirectory(Protocol):
    """Load the currently active actor after the opaque session is found.

    It deliberately receives only server-stored IDs.  Browser request headers,
    cookie values, OIDC claims, and client role/firm fields must never enter
    this lookup.
    """

    def resolve_active_actor(self, *, actor_id: str, firm_id: str, issuer: str) -> Actor | None: ...


class WebSessionAuthority:
    """Issue, resolve, and revoke short-lived opaque browser sessions.

    ``issue`` accepts only a live OIDC+MFA ``ServerIdentityContext``.  ``resolve``
    is compatible with ``ServerIdentityResolver`` and automatically applies
    origin/CSRF checks to every unsafe HTTP method.
    """

    def __init__(
        self,
        *,
        store: WebSessionStore,
        actor_directory: WebSessionActorDirectory,
        policy: WebSessionPolicy,
        clock: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not callable(getattr(store, "create_session", None)):
            raise ValueError("Web session store is invalid")
        if not callable(getattr(store, "find_session_by_digest", None)):
            raise ValueError("Web session store is invalid")
        if not callable(getattr(store, "revoke_session", None)):
            raise ValueError("Web session store is invalid")
        if not callable(getattr(actor_directory, "resolve_active_actor", None)):
            raise ValueError("Web session actor directory is invalid")
        if not isinstance(policy, WebSessionPolicy):
            raise ValueError("Web session policy is required")
        self._store = store
        self._actor_directory = actor_directory
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: token_urlsafe(48))
        _aware_datetime(self._clock(), label="Web session clock")

    def issue(self, *, identity: ServerIdentityContext) -> WebSessionGrant:
        """Create a session from an already verified OIDC/MFA identity.

        This method deliberately has no parameter for an OIDC token, browser
        role, firm identifier, display name, or client-selected session expiry.
        """

        now = _aware_datetime(self._clock(), label="Web session clock")
        self._validate_issuance_identity(identity=identity, now=now)
        # The upstream ID token proves the identity only at issuance.  The
        # browser subsequently authenticates with this revocable, server-side
        # opaque session, whose fixed policy lifetime remains the authority
        # boundary.  Coupling it to a deliberately short OIDC token lifetime
        # would turn routine token rotation into repeated MFA prompts.
        expires_at = now + self._policy.max_session_lifetime
        if expires_at <= now:
            raise WebSessionBlocked("Web session expiry is invalid")
        session_token = self._new_secret()
        csrf_token = self._new_secret()
        if compare_digest(session_token, csrf_token):
            raise WebSessionBlocked("Web session secret generation failed")
        session = StoredWebSession(
            session_id=str(uuid4()),
            actor_id=identity.actor.actor_id,
            firm_id=identity.actor.firm_id,
            issuer=identity.issuer,
            session_token_sha256=_digest(session_token),
            csrf_token_sha256=_digest(csrf_token),
            authenticated_at=identity.authenticated_at.astimezone(timezone.utc),
            created_at=now,
            expires_at=expires_at,
        )
        session.validate(now=now)
        try:
            self._store.create_session(session=session)
        except Exception:
            raise WebSessionBlocked("Web session storage is unavailable") from None
        session_cookie, csrf_cookie = self._policy.issue_cookies(
            session_token=session_token,
            csrf_token=csrf_token,
            now=now,
            expires_at=expires_at,
        )
        return WebSessionGrant(
            session_id=session.session_id,
            expires_at=expires_at,
            session_cookie=session_cookie,
            csrf_cookie=csrf_cookie,
        )

    async def resolve(self, request: Request) -> ServerIdentityContext:
        """Resolve one same-origin browser request without exposing secrets."""

        if not isinstance(request, Request):
            raise WebSessionBlocked("Web session request is invalid")
        method = request.method.upper()
        unsafe = method not in _SAFE_METHODS
        self._validate_request_origin(request=request, unsafe=unsafe)
        if _header_values(request, "authorization"):
            raise WebSessionBlocked("Web session does not accept bearer credentials")
        session_token = _extract_unique_cookie(request=request, name=self._policy.session_cookie_name)
        session_digest = _digest(session_token)
        try:
            session = self._store.find_session_by_digest(session_token_sha256=session_digest)
        except Exception:
            raise WebSessionBlocked("Web session storage is unavailable") from None
        if session is None:
            raise WebSessionBlocked("Web session is missing or invalid")
        now = _aware_datetime(self._clock(), label="Web session clock")
        self._validate_stored_session(session=session, supplied_digest=session_digest, now=now)
        if unsafe:
            self._validate_csrf(request=request, session=session)
        actor = self._resolve_current_actor(session=session)
        context = ServerIdentityContext(
            actor=actor,
            session_id=session.session_id,
            issuer=session.issuer,
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=session.authenticated_at,
            expires_at=session.expires_at,
        )
        try:
            context.validate(now=now)
        except PersistentAuthenticationBlocked:
            raise WebSessionBlocked("Web session identity is invalid") from None
        return context

    def revoke(self, *, session_id: str) -> None:
        """Mark a known opaque session revoked; HTTP authorization comes later."""

        try:
            UUID(session_id)
        except (TypeError, ValueError):
            raise WebSessionBlocked("Web session identifier is invalid") from None
        now = _aware_datetime(self._clock(), label="Web session clock")
        try:
            revoked = self._store.revoke_session(session_id=session_id, revoked_at=now)
        except Exception:
            raise WebSessionBlocked("Web session storage is unavailable") from None
        if revoked is not True:
            raise WebSessionBlocked("Web session is missing or invalid")

    def clear_cookies(self) -> tuple[CookieDirective, CookieDirective]:
        """Return the paired deletion directives for a confirmed logout.

        The FastAPI composition root needs this narrow public method so it
        never reaches into a session authority's private policy object.  It
        does not inspect any live session or expose a credential.
        """

        return self._policy.clear_cookies()

    def _new_secret(self) -> str:
        try:
            value = self._token_factory()
        except Exception:
            raise WebSessionBlocked("Web session secret generation failed") from None
        if not _is_valid_secret(value):
            raise WebSessionBlocked("Web session secret generation failed")
        return value

    def _validate_issuance_identity(self, *, identity: ServerIdentityContext, now: datetime) -> None:
        if not isinstance(identity, ServerIdentityContext):
            raise WebSessionBlocked("Web session requires a verified server identity")
        if identity.authentication_method is not AuthenticationMethod.OIDC_MFA:
            raise WebSessionBlocked("Web session requires OIDC MFA identity")
        try:
            identity.validate(now=now)
        except PersistentAuthenticationBlocked:
            raise WebSessionBlocked("Web session identity is invalid") from None
        _validate_human_actor(identity.actor)
        try:
            issuer = _validate_https_issuer(identity.issuer)
        except ValueError:
            raise WebSessionBlocked("Web session OIDC issuer is invalid") from None
        if issuer != identity.issuer:
            raise WebSessionBlocked("Web session OIDC issuer is invalid")
        authenticated_at = identity.authenticated_at.astimezone(timezone.utc)
        if now - authenticated_at > self._policy.max_auth_age:
            raise WebSessionBlocked("Web session MFA authentication is too old")

    @staticmethod
    def _validate_stored_session(*, session: StoredWebSession, supplied_digest: bytes, now: datetime) -> None:
        if not isinstance(session, StoredWebSession):
            raise WebSessionBlocked("Web session record is invalid")
        session.validate(now=now)
        if session.revoked_at is not None:
            raise WebSessionBlocked("Web session has been revoked")
        if not compare_digest(session.session_token_sha256, supplied_digest):
            raise WebSessionBlocked("Web session is missing or invalid")

    def _resolve_current_actor(self, *, session: StoredWebSession) -> Actor:
        try:
            actor = self._actor_directory.resolve_active_actor(
                actor_id=session.actor_id,
                firm_id=session.firm_id,
                issuer=session.issuer,
            )
        except Exception:
            raise WebSessionBlocked("Web session actor directory is unavailable") from None
        _validate_human_actor(actor)
        if actor.actor_id != session.actor_id or actor.firm_id != session.firm_id:
            raise WebSessionBlocked("Web session actor directory returned an invalid actor")
        return actor

    def _validate_request_origin(self, *, request: Request, unsafe: bool) -> None:
        origins = _header_values(request, "origin")
        if len(origins) > 1:
            raise WebSessionBlocked("Web session origin is ambiguous")
        if origins and origins[0] != self._policy.public_origin:
            raise WebSessionBlocked("Web session cross-site request is blocked")
        if unsafe and origins != [self._policy.public_origin]:
            raise WebSessionBlocked("Web session unsafe request origin is invalid")
        fetch_sites = _header_values(request, "sec-fetch-site")
        if len(fetch_sites) > 1:
            raise WebSessionBlocked("Web session fetch site is ambiguous")
        if fetch_sites:
            fetch_site = fetch_sites[0].strip().lower()
            if fetch_site not in _FETCH_SITE_VALUES or fetch_site == "cross-site":
                raise WebSessionBlocked("Web session cross-site request is blocked")
            if unsafe and fetch_site not in {"same-origin", "same-site"}:
                raise WebSessionBlocked("Web session unsafe request site is invalid")

    def _validate_csrf(self, *, request: Request, session: StoredWebSession) -> None:
        csrf_values = _header_values(request, self._policy.csrf_header_name)
        if len(csrf_values) != 1 or not _is_valid_secret(csrf_values[0]):
            raise WebSessionBlocked("Web session CSRF token is missing or invalid")
        csrf_cookie = _extract_unique_cookie(request=request, name=self._policy.csrf_cookie_name)
        csrf_header = csrf_values[0]
        if not compare_digest(csrf_header, csrf_cookie):
            raise WebSessionBlocked("Web session CSRF tokens do not match")
        if not compare_digest(_digest(csrf_header), session.csrf_token_sha256):
            raise WebSessionBlocked("Web session CSRF token is invalid")


def _validate_human_actor(actor: Actor | None) -> None:
    if not isinstance(actor, Actor):
        raise WebSessionBlocked("Web session actor is unavailable")
    try:
        UUID(actor.actor_id)
        UUID(actor.firm_id)
    except (TypeError, ValueError):
        raise WebSessionBlocked("Web session actor is invalid") from None
    if not actor.roles or not all(isinstance(role, Role) for role in actor.roles):
        raise WebSessionBlocked("Web session actor has no valid role")
    if Role.SYSTEM_WORKER in actor.roles:
        raise WebSessionBlocked("Web session cannot resolve a system worker")


def _extract_unique_cookie(*, request: Request, name: str) -> str:
    values: list[str] = []
    for header in _header_values(request, "cookie"):
        for item in header.split(";"):
            candidate_name, separator, candidate_value = item.strip().partition("=")
            if separator and candidate_name == name:
                values.append(candidate_value)
    if len(values) != 1 or not _is_valid_secret(values[0]):
        raise WebSessionBlocked("Web session cookie is missing or invalid")
    return values[0]


def _header_values(request: Request, name: str) -> list[str]:
    values = request.headers.getlist(name)
    if not all(isinstance(value, str) for value in values):
        raise WebSessionBlocked("Web session request headers are invalid")
    return values


def _is_valid_secret(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return 43 <= len(encoded) <= 160 and bool(_SECRET_PATTERN.fullmatch(value))


def _digest(value: str) -> bytes:
    return sha256(value.encode("ascii")).digest()


def _validate_host_cookie_name(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not _SAFE_COOKIE_NAME.fullmatch(value) or not value.startswith("__Host-"):
        raise ValueError(f"Web {label} cookie must use the __Host- prefix")


def _normalize_https_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ValueError("Web session public origin is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("Web session public origin is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Web session public origin must be an HTTPS origin")
    normalized = f"https://{hostname.lower()}"
    if port is not None and port != 443:
        normalized += f":{port}"
    if value != normalized:
        raise ValueError("Web session public origin must be canonical")
    return normalized


def _validate_https_issuer(value: object) -> str:
    """Accept a canonical OIDC issuer URL, whose path may be non-empty."""

    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ValueError("Web session OIDC issuer is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("Web session OIDC issuer is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Web session OIDC issuer must be an HTTPS issuer URL")
    return value


def _aware_datetime(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebSessionBlocked(f"{label} must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    try:
        timestamp = normalized.timestamp()
    except (OverflowError, OSError, ValueError) as error:
        raise WebSessionBlocked(f"{label} is invalid") from error
    if not isfinite(timestamp):
        raise WebSessionBlocked(f"{label} is invalid")
    return normalized
