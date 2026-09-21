"""Server-side OIDC authorization-code + PKCE login orchestration.

This module is intentionally a composition primitive, not a FastAPI router.
It starts a fixed, configured OIDC authorization-code request, retains only
short-lived server-side verification material, and exchanges the returned code
on the server.  The browser never receives an OIDC token, a client secret, a
PKCE verifier, or a selectable issuer/endpoint/redirect target.

``state`` necessarily traverses the browser as the OIDC protocol correlation
value.  Its raw value is never stored: the ephemeral state store receives only
its SHA-256 digest.  ``nonce`` and the PKCE verifier are kept only in a
short-lived server-side record.  A future clustered deployment must provide a
shared *ephemeral* store (for example a tightly scoped cache); a durable
database is deliberately not a valid implementation of this protocol.

After a callback is consumed, the ID token is verified and mapped by the
existing :class:`case_api.web_identity.WebOidcIdentityResolver`, then converted
to an opaque browser session by :class:`case_api.web_session.WebSessionAuthority`.
No route, persistence adapter, desktop integration, or frontend code belongs
here.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
from math import isfinite
import re
from secrets import token_urlsafe
from threading import Lock
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener

from case_api.persistent_identity import PersistentAuthenticationBlocked, ServerIdentityContext
from case_api.web_identity import VerifiedOidcClaims
from case_api.web_session import WebSessionGrant


__all__ = (
    "EphemeralOidcAuthorizationStateStore",
    "OidcAuthorizationCodeLogin",
    "OidcAuthorizationCodePolicy",
    "OidcAuthorizationRedirect",
    "OidcAuthorizationStateStore",
    "OidcLoginBlocked",
    "OidcLoginIdentityResolver",
    "OidcSessionIssuer",
    "OidcTokenEndpointClient",
    "OidcTokenExchangeRequest",
    "OidcTokenExchangeResponse",
    "PendingOidcAuthorization",
    "UrlLibOidcTokenEndpointClient",
)


_STATE_DIGEST_BYTES = 32
_MAX_CALLBACK_PARAMETERS = 12
_MAX_CALLBACK_VALUE_BYTES = 4_096
_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_URLSAFE_SECRET = re.compile(r"^[A-Za-z0-9_-]{43,160}$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CALLBACK_KEY = re.compile(r"^[a-z_]{1,64}$")
_CALLBACK_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,512}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")


class OidcLoginBlocked(PersistentAuthenticationBlocked):
    """A Web OIDC login request, callback, or server dependency is unsafe."""


@dataclass(frozen=True)
class OidcAuthorizationCodePolicy:
    """Fixed OIDC client and callback boundary for one HTTPS Web workbench.

    The callback is intentionally derived from ``public_origin`` plus a
    relative path.  A caller cannot choose a post-login destination, callback
    endpoint, issuer, authorization endpoint, or token endpoint per request.
    """

    public_origin: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    client_secret: str = field(repr=False)
    callback_path: str = "/api/v1/auth/oidc/callback"
    scopes: frozenset[str] = frozenset({"openid"})
    state_lifetime: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_origin", _normalize_https_origin(self.public_origin))
        object.__setattr__(self, "issuer", _normalize_https_url(self.issuer, label="OIDC issuer"))
        object.__setattr__(
            self,
            "authorization_endpoint",
            _normalize_https_url(self.authorization_endpoint, label="OIDC authorization endpoint"),
        )
        object.__setattr__(self, "token_endpoint", _normalize_https_url(self.token_endpoint, label="OIDC token endpoint"))
        issuer_origin = _https_origin(self.issuer)
        if _https_origin(self.authorization_endpoint) != issuer_origin or _https_origin(self.token_endpoint) != issuer_origin:
            raise ValueError("OIDC endpoints must use the configured issuer origin")
        if not isinstance(self.client_id, str) or not _CLIENT_ID.fullmatch(self.client_id):
            raise ValueError("OIDC client identifier is invalid")
        _validate_client_secret(self.client_secret)
        object.__setattr__(self, "callback_path", _normalize_callback_path(self.callback_path))
        object.__setattr__(self, "scopes", _normalize_scopes(self.scopes))
        if "openid" not in self.scopes:
            raise ValueError("OIDC scopes must include openid")
        if not isinstance(self.state_lifetime, timedelta) or not timedelta(minutes=1) <= self.state_lifetime <= timedelta(minutes=10):
            raise ValueError("OIDC authorization state lifetime must be between one and ten minutes")

    @property
    def redirect_uri(self) -> str:
        """The exact pre-registered HTTPS callback URI, never caller supplied."""

        return f"{self.public_origin}{self.callback_path}"


@dataclass(frozen=True)
class PendingOidcAuthorization:
    """Short-lived server-only correlation material for one authorization.

    ``state_sha256`` is the only retained representation of the browser-facing
    state.  ``nonce`` and ``code_verifier`` are secret-bearing fields and do
    not appear in representations or persistence contracts.
    """

    state_sha256: bytes = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    created_at: datetime
    expires_at: datetime

    def validate(self, *, now: datetime | None = None) -> None:
        if not isinstance(self.state_sha256, bytes) or len(self.state_sha256) != _STATE_DIGEST_BYTES:
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        if not _is_urlsafe_secret(self.nonce) or not _is_pkce_verifier(self.code_verifier):
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        created_at = _aware_datetime(self.created_at, label="OIDC authorization creation time")
        expires_at = _aware_datetime(self.expires_at, label="OIDC authorization expiry")
        if expires_at <= created_at or expires_at - created_at > timedelta(minutes=10):
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        if now is not None and expires_at <= _aware_datetime(now, label="OIDC login clock"):
            raise OidcLoginBlocked("OIDC authorization state has expired")


class OidcAuthorizationStateStore(Protocol):
    """Ephemeral, atomic store for pending server-side OIDC authorizations.

    Implementations must never write a raw state value, OIDC token, or this
    record to a durable audit/database table.  ``consume_pending`` must be an
    atomic remove/mark-consumed operation so a callback cannot be replayed.
    """

    def store_pending(self, *, pending: PendingOidcAuthorization) -> bool: ...

    def consume_pending(
        self,
        *,
        state_sha256: bytes,
        now: datetime,
    ) -> PendingOidcAuthorization | None: ...


class EphemeralOidcAuthorizationStateStore:
    """In-process short-term state store suitable for a single Web process.

    A process restart safely invalidates pending logins.  Multi-process or
    multi-node deployments must inject a shared ephemeral implementation; they
    must not replace this with persistent database storage.
    """

    def __init__(self, *, max_pending: int = 4_096) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 1 <= max_pending <= 65_536:
            raise ValueError("OIDC authorization state capacity is invalid")
        self._pending: dict[bytes, PendingOidcAuthorization] = {}
        self._max_pending = max_pending
        self._lock = Lock()

    def store_pending(self, *, pending: PendingOidcAuthorization) -> bool:
        if not isinstance(pending, PendingOidcAuthorization):
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        pending.validate()
        with self._lock:
            self._purge_expired(at=pending.created_at)
            if pending.state_sha256 in self._pending:
                return False
            if len(self._pending) >= self._max_pending:
                raise OidcLoginBlocked("OIDC authorization state capacity is exhausted")
            self._pending[pending.state_sha256] = pending
            return True

    def consume_pending(
        self,
        *,
        state_sha256: bytes,
        now: datetime,
    ) -> PendingOidcAuthorization | None:
        if not isinstance(state_sha256, bytes) or len(state_sha256) != _STATE_DIGEST_BYTES:
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        current = _aware_datetime(now, label="OIDC login clock")
        with self._lock:
            self._purge_expired(at=current)
            # Deleting before later validation is deliberate: expiry, a failed
            # exchange, and malformed callbacks cannot be replayed.
            return self._pending.pop(state_sha256, None)

    def _purge_expired(self, *, at: datetime) -> None:
        for digest, pending in tuple(self._pending.items()):
            if pending.expires_at <= at:
                self._pending.pop(digest, None)


@dataclass(frozen=True)
class OidcAuthorizationRedirect:
    """A redirect-only result.  Its URL must never be serialized or logged."""

    authorization_url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class OidcTokenExchangeRequest:
    """Server-only authorization-code token request with redacted secrets."""

    token_endpoint: str
    client_id: str
    client_secret: str = field(repr=False)
    code: str = field(repr=False)
    redirect_uri: str
    code_verifier: str = field(repr=False)

    def validate(self) -> None:
        try:
            _normalize_https_url(self.token_endpoint, label="OIDC token endpoint")
        except ValueError:
            raise OidcLoginBlocked("OIDC token request is invalid") from None
        if not isinstance(self.client_id, str) or not _CLIENT_ID.fullmatch(self.client_id):
            raise OidcLoginBlocked("OIDC token request is invalid")
        try:
            _validate_client_secret(self.client_secret)
            _normalize_https_url(self.redirect_uri, label="OIDC redirect URI")
        except ValueError:
            raise OidcLoginBlocked("OIDC token request is invalid") from None
        if not _is_authorization_code(self.code) or not _is_pkce_verifier(self.code_verifier):
            raise OidcLoginBlocked("OIDC token request is invalid")


@dataclass(frozen=True)
class OidcTokenExchangeResponse:
    """Minimal token response.  No access or refresh token is retained."""

    id_token: str = field(repr=False)

    def validate(self) -> None:
        if not _is_compact_token_text(self.id_token):
            raise OidcLoginBlocked("OIDC token response has no valid ID token")


class OidcTokenEndpointClient(Protocol):
    """Narrow, injectable server-to-server token exchange boundary."""

    def exchange_authorization_code(
        self,
        *,
        request: OidcTokenExchangeRequest,
    ) -> OidcTokenExchangeResponse: ...


class OidcLoginIdentityResolver(Protocol):
    """Subset of ``WebOidcIdentityResolver`` required by this orchestration."""

    def verify_token(self, token: str) -> VerifiedOidcClaims: ...

    def resolve_token(self, token: str) -> ServerIdentityContext: ...


class OidcSessionIssuer(Protocol):
    """Issue an opaque Web session only from a verified server identity."""

    def issue(self, *, identity: ServerIdentityContext) -> WebSessionGrant: ...


class _NoRedirect(HTTPRedirectHandler):
    """Do not follow token-endpoint redirects, including cross-origin ones."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


class UrlLibOidcTokenEndpointClient:
    """HTTPS-only standard-library token endpoint client.

    It uses HTTP Basic client authentication, refuses redirects, requires a
    bounded JSON response, and returns only the ID token needed by this flow.
    It is intentionally not exercised against a live provider in unit tests;
    callers should inject a fake ``OidcTokenEndpointClient`` there.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not isfinite(float(timeout_seconds))
            or not 1.0 <= float(timeout_seconds) <= 30.0
        ):
            raise ValueError("OIDC token endpoint timeout must be between one and thirty seconds")
        self._timeout_seconds = float(timeout_seconds)
        self._opener = build_opener(_NoRedirect())

    def exchange_authorization_code(
        self,
        *,
        request: OidcTokenExchangeRequest,
    ) -> OidcTokenExchangeResponse:
        if not isinstance(request, OidcTokenExchangeRequest):
            raise OidcLoginBlocked("OIDC token request is invalid")
        request.validate()
        body = urlencode(
            (
                ("grant_type", "authorization_code"),
                ("code", request.code),
                ("redirect_uri", request.redirect_uri),
                ("code_verifier", request.code_verifier),
            )
        ).encode("ascii")
        basic = base64.b64encode(f"{request.client_id}:{request.client_secret}".encode("utf-8")).decode("ascii")
        outgoing = UrlRequest(
            request.token_endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self._opener.open(outgoing, timeout=self._timeout_seconds) as response:
                if response.getcode() != 200:
                    raise OidcLoginBlocked("OIDC token exchange failed")
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise OidcLoginBlocked("OIDC token exchange returned an invalid response")
                body_bytes = response.read(_MAX_TOKEN_RESPONSE_BYTES + 1)
        except OidcLoginBlocked:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise OidcLoginBlocked("OIDC token exchange failed") from None
        if not isinstance(body_bytes, bytes) or not body_bytes or len(body_bytes) > _MAX_TOKEN_RESPONSE_BYTES:
            raise OidcLoginBlocked("OIDC token exchange returned an invalid response")
        try:
            decoded = body_bytes.decode("utf-8")
            response_payload = json.loads(
                decoded,
                object_pairs_hook=_reject_duplicate_json_members,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise OidcLoginBlocked("OIDC token exchange returned an invalid response") from None
        if not isinstance(response_payload, Mapping):
            raise OidcLoginBlocked("OIDC token exchange returned an invalid response")
        result = OidcTokenExchangeResponse(id_token=response_payload.get("id_token"))
        result.validate()
        return result


class OidcAuthorizationCodeLogin:
    """Create and complete a fixed OIDC authorization-code + PKCE flow.

    The future route adapter should call :meth:`begin_authorization` only on a
    user-initiated login endpoint and use its ``authorization_url`` solely as
    an HTTP redirect.  It should pass the callback query as an ordered sequence
    of key/value pairs to :meth:`complete_callback`, preserving duplicate
    parameters rather than collapsing them into a mapping.
    """

    def __init__(
        self,
        *,
        policy: OidcAuthorizationCodePolicy,
        state_store: OidcAuthorizationStateStore,
        token_client: OidcTokenEndpointClient,
        identity_resolver: OidcLoginIdentityResolver,
        session_issuer: OidcSessionIssuer,
        clock: Callable[[], datetime] | None = None,
        secret_factory: Callable[[int], str] | None = None,
    ) -> None:
        if not isinstance(policy, OidcAuthorizationCodePolicy):
            raise ValueError("OIDC authorization-code policy is required")
        if not callable(getattr(state_store, "store_pending", None)) or not callable(
            getattr(state_store, "consume_pending", None)
        ):
            raise ValueError("OIDC authorization state store is invalid")
        if not callable(getattr(token_client, "exchange_authorization_code", None)):
            raise ValueError("OIDC token endpoint client is invalid")
        if not callable(getattr(identity_resolver, "verify_token", None)) or not callable(
            getattr(identity_resolver, "resolve_token", None)
        ):
            raise ValueError("OIDC identity resolver is invalid")
        if not callable(getattr(session_issuer, "issue", None)):
            raise ValueError("OIDC Web session issuer is invalid")
        self._policy = policy
        self._state_store = state_store
        self._token_client = token_client
        self._identity_resolver = identity_resolver
        self._session_issuer = session_issuer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._secret_factory = secret_factory or token_urlsafe
        _aware_datetime(self._clock(), label="OIDC login clock")

    @property
    def public_origin(self) -> str:
        """Configured workbench origin, exposed for server composition checks.

        This is not a browser response field and contains no client secret,
        code verifier, nonce, state, or token.  Keeping the check public lets
        the FastAPI composition reject a mismatched cookie/login callback
        origin before it mounts case routes.
        """

        return self._policy.public_origin

    def begin_authorization(self) -> OidcAuthorizationRedirect:
        """Store one short-lived authorization and return an HTTPS redirect.

        The raw state exists only while this method constructs the redirect; it
        is stored as a digest.  The resulting URL itself is sensitive protocol
        material and must only be placed in the redirect ``Location`` header.
        """

        now = _aware_datetime(self._clock(), label="OIDC login clock")
        for _ in range(3):
            state = self._new_urlsafe_secret(size=32, label="state")
            nonce = self._new_urlsafe_secret(size=32, label="nonce")
            verifier = self._new_pkce_verifier()
            if len({state, nonce, verifier}) != 3:
                raise OidcLoginBlocked("OIDC secret generation failed")
            pending = PendingOidcAuthorization(
                state_sha256=_sha256_ascii(state),
                nonce=nonce,
                code_verifier=verifier,
                created_at=now,
                expires_at=now + self._policy.state_lifetime,
            )
            pending.validate(now=now)
            try:
                stored = self._state_store.store_pending(pending=pending)
            except OidcLoginBlocked:
                raise
            except Exception:
                raise OidcLoginBlocked("OIDC authorization state is unavailable") from None
            if stored is not True:
                if stored is False:
                    continue
                raise OidcLoginBlocked("OIDC authorization state is unavailable")
            query = urlencode(
                (
                    ("response_type", "code"),
                    ("response_mode", "query"),
                    ("client_id", self._policy.client_id),
                    ("redirect_uri", self._policy.redirect_uri),
                    ("scope", " ".join(sorted(self._policy.scopes))),
                    ("state", state),
                    ("nonce", nonce),
                    ("code_challenge", _pkce_challenge(verifier)),
                    ("code_challenge_method", "S256"),
                )
            )
            return OidcAuthorizationRedirect(
                authorization_url=f"{self._policy.authorization_endpoint}?{query}",
                expires_at=pending.expires_at,
            )
        raise OidcLoginBlocked("OIDC authorization state could not be created")

    def complete_callback(
        self,
        *,
        parameters: Sequence[tuple[str, str]],
    ) -> WebSessionGrant:
        """Consume one callback, exchange its code, and issue an opaque session.

        The state is consumed before token exchange.  Therefore a repeated,
        expired, malformed, failed, or hybrid callback cannot be retried using
        the same state value.
        """

        values = _normalize_callback_parameters(parameters)
        state = values.get("state")
        if not _is_urlsafe_secret(state):
            raise OidcLoginBlocked("OIDC callback state is missing or invalid")
        now = _aware_datetime(self._clock(), label="OIDC login clock")
        state_digest = _sha256_ascii(state)
        try:
            pending = self._state_store.consume_pending(state_sha256=state_digest, now=now)
        except OidcLoginBlocked:
            raise
        except Exception:
            raise OidcLoginBlocked("OIDC authorization state is unavailable") from None
        if pending is None:
            raise OidcLoginBlocked("OIDC callback state is missing, expired, or already consumed")
        if not isinstance(pending, PendingOidcAuthorization):
            raise OidcLoginBlocked("OIDC authorization state is invalid")
        pending.validate(now=now)
        if not compare_digest(pending.state_sha256, state_digest):
            raise OidcLoginBlocked("OIDC authorization state is invalid")

        # An authorization error/hybrid callback consumes state but never
        # reaches the token endpoint.  This prevents a later retry of a code
        # with the same state and refuses implicit/hybrid response modes.
        disallowed = {"id_token", "access_token", "token_type", "expires_in", "error", "error_description", "error_uri"}
        if disallowed.intersection(values):
            raise OidcLoginBlocked("OIDC callback did not contain an authorization code")
        if set(values).difference({"code", "state", "iss"}):
            raise OidcLoginBlocked("OIDC callback contains unsupported parameters")
        if values.get("iss") is not None and values["iss"] != self._policy.issuer:
            raise OidcLoginBlocked("OIDC callback issuer is not accepted")
        code = values.get("code")
        if not _is_authorization_code(code):
            raise OidcLoginBlocked("OIDC callback code is missing or invalid")

        exchange = OidcTokenExchangeRequest(
            token_endpoint=self._policy.token_endpoint,
            client_id=self._policy.client_id,
            client_secret=self._policy.client_secret,
            code=code,
            redirect_uri=self._policy.redirect_uri,
            code_verifier=pending.code_verifier,
        )
        try:
            token_response = self._token_client.exchange_authorization_code(request=exchange)
        except OidcLoginBlocked:
            raise
        except Exception:
            raise OidcLoginBlocked("OIDC token exchange failed") from None
        if not isinstance(token_response, OidcTokenExchangeResponse):
            raise OidcLoginBlocked("OIDC token exchange returned an invalid response")
        token_response.validate()
        id_token = token_response.id_token

        try:
            verified = self._identity_resolver.verify_token(id_token)
        except Exception:
            raise OidcLoginBlocked("OIDC ID token is invalid") from None
        if not isinstance(verified, VerifiedOidcClaims) or verified.issuer != self._policy.issuer:
            raise OidcLoginBlocked("OIDC ID token is invalid")
        if not compare_digest(_verified_id_token_nonce(id_token), pending.nonce):
            raise OidcLoginBlocked("OIDC ID token nonce is invalid")
        try:
            identity = self._identity_resolver.resolve_token(id_token)
        except Exception:
            raise OidcLoginBlocked("OIDC ID token identity is invalid") from None
        _validate_identity_matches_verified(identity=identity, verified=verified)
        try:
            grant = self._session_issuer.issue(identity=identity)
        except Exception:
            raise OidcLoginBlocked("OIDC Web session could not be issued") from None
        if not isinstance(grant, WebSessionGrant):
            raise OidcLoginBlocked("OIDC Web session could not be issued")
        return grant

    def _new_urlsafe_secret(self, *, size: int, label: str) -> str:
        try:
            value = self._secret_factory(size)
        except Exception:
            raise OidcLoginBlocked("OIDC secret generation failed") from None
        if not _is_urlsafe_secret(value):
            raise OidcLoginBlocked(f"OIDC {label} generation failed")
        return value

    def _new_pkce_verifier(self) -> str:
        try:
            value = self._secret_factory(64)
        except Exception:
            raise OidcLoginBlocked("OIDC PKCE generation failed") from None
        if not _is_pkce_verifier(value):
            raise OidcLoginBlocked("OIDC PKCE generation failed")
        return value


def _normalize_callback_parameters(parameters: Sequence[tuple[str, str]]) -> dict[str, str]:
    if isinstance(parameters, (str, bytes)) or not isinstance(parameters, Sequence) or not 1 <= len(parameters) <= _MAX_CALLBACK_PARAMETERS:
        raise OidcLoginBlocked("OIDC callback parameters are invalid")
    result: dict[str, str] = {}
    for item in parameters:
        if not isinstance(item, tuple) or len(item) != 2:
            raise OidcLoginBlocked("OIDC callback parameters are invalid")
        key, value = item
        if not isinstance(key, str) or not _CALLBACK_KEY.fullmatch(key) or not _is_callback_value(value):
            raise OidcLoginBlocked("OIDC callback parameters are invalid")
        if key in result:
            raise OidcLoginBlocked("OIDC callback parameters are ambiguous")
        result[key] = value
    return result


def _verified_id_token_nonce(token: str) -> str:
    """Read a nonce only after ``verify_token`` cryptographically succeeded."""

    try:
        _, payload_segment, _ = token.split(".")
        padding = "=" * (-len(payload_segment) % 4)
        payload_bytes = base64.urlsafe_b64decode((payload_segment + padding).encode("ascii"))
        if not payload_bytes or len(payload_bytes) > 12 * 1024:
            raise ValueError("payload length")
        payload = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise OidcLoginBlocked("OIDC ID token nonce is invalid") from None
    if not isinstance(payload, Mapping):
        raise OidcLoginBlocked("OIDC ID token nonce is invalid")
    nonce = payload.get("nonce")
    if not _is_urlsafe_secret(nonce):
        raise OidcLoginBlocked("OIDC ID token nonce is invalid")
    return nonce


def _validate_identity_matches_verified(*, identity: object, verified: VerifiedOidcClaims) -> None:
    if not isinstance(identity, ServerIdentityContext):
        raise OidcLoginBlocked("OIDC ID token identity is invalid")
    if (
        identity.issuer != verified.issuer
        or identity.expires_at.astimezone(timezone.utc) != verified.expires_at
        or identity.authenticated_at.astimezone(timezone.utc) != verified.authenticated_at
    ):
        raise OidcLoginBlocked("OIDC ID token identity is invalid")


def _pkce_challenge(verifier: str) -> str:
    if not _is_pkce_verifier(verifier):
        raise OidcLoginBlocked("OIDC PKCE verifier is invalid")
    return base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _sha256_ascii(value: str) -> bytes:
    return sha256(value.encode("ascii")).digest()


def _is_urlsafe_secret(value: object) -> bool:
    return isinstance(value, str) and bool(_URLSAFE_SECRET.fullmatch(value))


def _is_pkce_verifier(value: object) -> bool:
    return isinstance(value, str) and bool(_PKCE_VERIFIER.fullmatch(value))


def _is_authorization_code(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_CALLBACK_VALUE_BYTES or value != value.strip():
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return not any(byte < 33 or byte == 127 for byte in encoded)


def _is_compact_token_text(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 16 * 1024:
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return len(value.split(".")) == 3 and not any(byte < 33 or byte == 127 for byte in encoded)


def _is_callback_value(value: object) -> bool:
    if not isinstance(value, str) or len(value) > _MAX_CALLBACK_VALUE_BYTES:
        return False
    return not any(ord(character) < 32 or ord(character) == 127 for character in value)


def _normalize_scopes(value: object) -> frozenset[str]:
    if not isinstance(value, (frozenset, set, tuple, list)) or not 1 <= len(value) <= 8:
        raise ValueError("OIDC scopes are invalid")
    normalized = frozenset(value)
    if len(normalized) != len(value) or not all(
        isinstance(scope, str) and re.fullmatch(r"[A-Za-z0-9._~-]{1,64}", scope) for scope in normalized
    ):
        raise ValueError("OIDC scopes are invalid")
    return normalized


def _validate_client_secret(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_CALLBACK_VALUE_BYTES or value != value.strip():
        raise ValueError("OIDC client secret is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("OIDC client secret is invalid")


def _normalize_callback_path(value: object) -> str:
    if not isinstance(value, str) or not _CALLBACK_PATH.fullmatch(value) or value.startswith("//"):
        raise ValueError("OIDC callback path is invalid")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path != value:
        raise ValueError("OIDC callback path is invalid")
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise ValueError("OIDC callback path is invalid")
    return value


def _normalize_https_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1_024:
        raise ValueError("OIDC public origin is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("OIDC public origin is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OIDC public origin must be an HTTPS origin")
    normalized = f"https://{hostname.lower()}"
    if port is not None and port != 443:
        normalized += f":{port}"
    if value != normalized:
        raise ValueError("OIDC public origin must be canonical")
    return normalized


def _normalize_https_url(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError(f"{label} is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTPS URL")
    normalized = f"https://{hostname.lower()}"
    if port is not None and port != 443:
        normalized += f":{port}"
    normalized += parsed.path
    if value != normalized:
        raise ValueError(f"{label} must be canonical")
    return normalized


def _https_origin(value: str) -> str:
    """Return an origin only after the caller already normalized an HTTPS URL."""

    parsed = urlsplit(value)
    hostname = parsed.hostname
    if not hostname:  # Defensive; all callers use ``_normalize_https_url`` first.
        raise ValueError("OIDC HTTPS URL is invalid")
    origin = f"https://{hostname.lower()}"
    if parsed.port is not None and parsed.port != 443:
        origin += f":{parsed.port}"
    return origin


def _aware_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OidcLoginBlocked(f"{label} must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    try:
        timestamp = normalized.timestamp()
    except (OverflowError, OSError, ValueError):
        raise OidcLoginBlocked(f"{label} is invalid") from None
    if not isfinite(timestamp):
        raise OidcLoginBlocked(f"{label} is invalid")
    return normalized


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant")
