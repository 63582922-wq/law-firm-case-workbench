"""Fail-closed OIDC identity verification for the self-hosted Web API.

This module is deliberately independent from routes, persistence, and JWT
libraries.  It verifies a compact RS256 JWT against an injected JWKS document,
then resolves *only* ``(issuer, subject)`` through a server-side mapping to an
internal :class:`~case_kernel.models.Actor`.  Browser claims such as ``roles``,
``firm_id``, and display names never enter that mapping or the returned actor.

The resolver implements the existing ``ServerIdentityResolver`` shape, so a
future persistent API composition root may inject it without accepting any
browser-supplied tenancy or authorization attributes.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
import re
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from fastapi import Request

from case_api.persistent_identity import (
    AuthenticationMethod,
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
)
from case_kernel.models import Actor, Role


__all__ = (
    "JwksProvider",
    "MfaClaimRequirement",
    "OidcActorMapping",
    "OidcVerificationPolicy",
    "PresentedWebToken",
    "TokenSource",
    "TokenTransportPolicy",
    "VerifiedOidcClaims",
    "WebIdentityBlocked",
    "WebOidcIdentityResolver",
)


_MAX_JWT_BYTES = 16 * 1024
_MAX_HEADER_BYTES = 2 * 1024
_MAX_PAYLOAD_BYTES = 12 * 1024
_MAX_SIGNATURE_BYTES = 1024
_MAX_JWKS_KEYS = 32
# Keep signature parsing bounded consistently with the largest permitted RSA key.
_MAX_RSA_MODULUS_BITS = 8_192
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_SESSION_NAMESPACE = uuid5(NAMESPACE_URL, "cn.lawcase.workbench/web-oidc-session/v1")


class WebIdentityBlocked(PersistentAuthenticationBlocked):
    """A Web OIDC credential or the server-side identity boundary is unsafe."""


class TokenSource(str, Enum):
    BEARER = "BEARER"
    COOKIE = "COOKIE"


@dataclass(frozen=True)
class PresentedWebToken:
    """An extracted credential whose repr never exposes the token."""

    source: TokenSource
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return f"PresentedWebToken(source={self.source.value!r}, token=<redacted>)"


@dataclass(frozen=True)
class TokenTransportPolicy:
    """Explicit bearer/cookie extraction policy, not a session implementation.

    Cookie authentication is opt-in: callers must provide a non-empty
    ``cookie_name`` and separately configure Secure, HttpOnly, SameSite, CSRF,
    and Origin protections at the HTTP boundary.  This class only extracts an
    opaque JWT and never logs or retains it.
    """

    cookie_name: str | None = None
    authorization_header: str = "authorization"
    max_token_bytes: int = _MAX_JWT_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.authorization_header, str):
            raise ValueError("Web OIDC authorization header is invalid")
        header = self.authorization_header.strip().lower()
        if header != "authorization":
            raise ValueError("Web OIDC transport only supports the Authorization header")
        if self.cookie_name is not None:
            if not isinstance(self.cookie_name, str) or not _is_safe_cookie_name(self.cookie_name):
                raise ValueError("Web OIDC cookie name is invalid")
        if not 512 <= self.max_token_bytes <= _MAX_JWT_BYTES:
            raise ValueError("Web OIDC maximum token size is invalid")

    def extract(
        self,
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str] | None = None,
    ) -> PresentedWebToken:
        authorization = _header_value(headers, "authorization")
        bearer = _extract_bearer_token(authorization, max_token_bytes=self.max_token_bytes)
        cookie_token: str | None = None
        if self.cookie_name is not None and cookies is not None:
            candidate = cookies.get(self.cookie_name)
            if candidate is not None:
                cookie_token = _validate_compact_token(candidate, max_token_bytes=self.max_token_bytes)
        if bearer is not None and cookie_token is not None:
            raise WebIdentityBlocked("Web OIDC credential source is ambiguous")
        if bearer is not None:
            return PresentedWebToken(source=TokenSource.BEARER, token=bearer)
        if cookie_token is not None:
            return PresentedWebToken(source=TokenSource.COOKIE, token=cookie_token)
        raise WebIdentityBlocked("Web OIDC credential is missing")


class JwksProvider(Protocol):
    """A server-owned JWKS source.  Fetching/caching belongs outside this module."""

    def load_jwks(self) -> Mapping[str, Any] | None: ...


class OidcActorMapping(Protocol):
    """Resolve a verified issuer/subject to a server-owned internal actor."""

    def resolve_actor(self, *, issuer: str, subject: str) -> Actor | None: ...


@dataclass(frozen=True)
class MfaClaimRequirement:
    """Require a known AMR value and, optionally, an approved ACR value."""

    required_amr: frozenset[str] = frozenset({"mfa"})
    accepted_acr: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        normalized_amr = _normalize_claim_values(self.required_amr, label="required AMR")
        normalized_acr = _normalize_claim_values(self.accepted_acr, label="accepted ACR")
        if not normalized_amr:
            raise ValueError("Web OIDC must require at least one MFA AMR value")
        object.__setattr__(self, "required_amr", normalized_amr)
        object.__setattr__(self, "accepted_acr", normalized_acr)


@dataclass(frozen=True)
class OidcVerificationPolicy:
    """Tight, explicit JWT validation policy for human Web sessions."""

    issuer: str
    audience: str
    mfa: MfaClaimRequirement = field(default_factory=MfaClaimRequirement)
    clock_skew: timedelta = timedelta(seconds=30)
    max_token_lifetime: timedelta = timedelta(hours=1)
    max_auth_age: timedelta = timedelta(hours=12)

    def __post_init__(self) -> None:
        _validate_https_issuer(self.issuer)
        if not _is_claim_text(self.audience, max_length=255):
            raise ValueError("Web OIDC audience is invalid")
        if not isinstance(self.mfa, MfaClaimRequirement):
            raise ValueError("Web OIDC MFA claim requirement is invalid")
        if not timedelta(0) <= self.clock_skew <= timedelta(minutes=2):
            raise ValueError("Web OIDC clock skew must be between zero and two minutes")
        if not timedelta(seconds=1) <= self.max_token_lifetime <= timedelta(hours=1):
            raise ValueError("Web OIDC token lifetime must be between one second and one hour")
        if not timedelta(seconds=1) <= self.max_auth_age <= timedelta(hours=24):
            raise ValueError("Web OIDC MFA authentication age must be within one day")


@dataclass(frozen=True)
class VerifiedOidcClaims:
    """Minimal verified claims retained after JWT verification.

    This intentionally excludes raw payload, roles, firm identifiers, names,
    emails, and the token itself.
    """

    issuer: str
    subject: str
    expires_at: datetime
    not_before: datetime
    issued_at: datetime
    authenticated_at: datetime
    authentication_methods: frozenset[str]
    authentication_context: str | None
    token_fingerprint: str = field(repr=False)


class WebOidcIdentityResolver:
    """Verify OIDC JWTs and resolve a server-owned actor without routes.

    ``resolve`` is async solely to match ``ServerIdentityResolver``.  The JWKS
    source and mapping protocol are synchronous and injected, so tests and
    self-hosted compositions do not perform live network calls here.
    """

    def __init__(
        self,
        *,
        policy: OidcVerificationPolicy,
        jwks_provider: JwksProvider,
        actor_mapping: OidcActorMapping,
        transport: TokenTransportPolicy = TokenTransportPolicy(),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, OidcVerificationPolicy):
            raise ValueError("Web OIDC verification policy is required")
        if not callable(getattr(jwks_provider, "load_jwks", None)):
            raise ValueError("Web OIDC JWKS provider is invalid")
        if not callable(getattr(actor_mapping, "resolve_actor", None)):
            raise ValueError("Web OIDC actor mapping is invalid")
        if not isinstance(transport, TokenTransportPolicy):
            raise ValueError("Web OIDC token transport policy is invalid")
        self._policy = policy
        self._jwks_provider = jwks_provider
        self._actor_mapping = actor_mapping
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        _aware_now(self._clock())

    async def resolve(self, request: Request) -> ServerIdentityContext:
        credentials = self._transport.extract(headers=request.headers, cookies=request.cookies)
        return self.resolve_token(credentials.token)

    def resolve_token(self, token: str) -> ServerIdentityContext:
        """Resolve one raw credential without storing or exposing it."""

        verified = self.verify_token(token)
        try:
            actor = self._actor_mapping.resolve_actor(
                issuer=verified.issuer,
                subject=verified.subject,
            )
        except Exception as error:
            raise WebIdentityBlocked("Web OIDC actor mapping is unavailable") from error
        _validate_mapped_human_actor(actor)
        now = _aware_now(self._clock())
        session_id = str(
            uuid5(
                _SESSION_NAMESPACE,
                f"{verified.issuer}\x1f{verified.subject}\x1f{verified.token_fingerprint}",
            )
        )
        context = ServerIdentityContext(
            actor=actor,
            session_id=session_id,
            issuer=verified.issuer,
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=verified.authenticated_at,
            expires_at=verified.expires_at,
        )
        try:
            context.validate(now=now)
        except PersistentAuthenticationBlocked as error:
            raise WebIdentityBlocked("Web OIDC mapped identity is invalid") from error
        return context

    def verify_token(self, token: str) -> VerifiedOidcClaims:
        """Validate token structure, key, signature, registered claims, and MFA."""

        compact = _validate_compact_token(token, max_token_bytes=self._transport.max_token_bytes)
        header_segment, payload_segment, signature_segment = compact.split(".")
        header = _decode_json_object(header_segment, label="JWT header", max_bytes=_MAX_HEADER_BYTES)
        payload = _decode_json_object(payload_segment, label="JWT payload", max_bytes=_MAX_PAYLOAD_BYTES)
        _validate_jwt_header(header)
        key_id = _required_claim_text(header, "kid", max_length=255, label="JWT key identifier")
        public_key = self._select_public_key(key_id)
        signature = _decode_base64url(signature_segment, label="JWT signature", max_bytes=_MAX_SIGNATURE_BYTES)
        try:
            public_key.verify(
                signature,
                f"{header_segment}.{payload_segment}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as error:
            raise WebIdentityBlocked("Web OIDC JWT signature is invalid") from error
        except Exception as error:
            raise WebIdentityBlocked("Web OIDC JWT signature verification failed") from error
        return self._validate_claims(payload, token=compact)

    def _select_public_key(self, key_id: str) -> rsa.RSAPublicKey:
        matching = self._matching_jwks(key_id)
        # Key rotations commonly surface as an unknown kid while a bounded
        # provider cache remains fresh.  A provider may offer one explicit
        # refresh; it is not required by the base JwksProvider contract, and
        # a failed refresh stays fail-closed.
        if not matching:
            refresh = getattr(self._jwks_provider, "refresh_jwks", None)
            if callable(refresh):
                try:
                    document = refresh()
                except Exception as error:
                    raise WebIdentityBlocked("Web OIDC JWKS is unavailable") from error
                matching = _matching_jwks_document(document, key_id)
        if len(matching) != 1:
            raise WebIdentityBlocked("Web OIDC signing key is unavailable")
        try:
            return _rsa_public_key_from_jwk(matching[0])
        except WebIdentityBlocked:
            raise
        except Exception as error:
            raise WebIdentityBlocked("Web OIDC signing JWK is malformed") from error

    def _matching_jwks(self, key_id: str) -> list[Mapping[str, Any]]:
        try:
            document = self._jwks_provider.load_jwks()
        except Exception as error:
            raise WebIdentityBlocked("Web OIDC JWKS is unavailable") from error
        return _matching_jwks_document(document, key_id)

    def _validate_claims(self, payload: Mapping[str, Any], *, token: str) -> VerifiedOidcClaims:
        issuer = _required_claim_text(payload, "iss", max_length=1024, label="OIDC issuer")
        if issuer != self._policy.issuer:
            raise WebIdentityBlocked("Web OIDC issuer is not accepted")
        subject = _required_claim_text(payload, "sub", max_length=255, label="OIDC subject")
        audiences = _validated_audiences(payload.get("aud"))
        if self._policy.audience not in audiences:
            raise WebIdentityBlocked("Web OIDC audience is not accepted")
        if len(audiences) > 1 and payload.get("azp") != self._policy.audience:
            raise WebIdentityBlocked("Web OIDC authorized party is required for multiple audiences")

        now = _aware_now(self._clock())
        expires_at = _numeric_date(payload, "exp")
        not_before = _numeric_date(payload, "nbf")
        issued_at = _numeric_date(payload, "iat")
        authenticated_at = _numeric_date(payload, "auth_time")
        skew = self._policy.clock_skew
        if expires_at <= now - skew:
            raise WebIdentityBlocked("Web OIDC JWT has expired")
        if not_before > now + skew:
            raise WebIdentityBlocked("Web OIDC JWT is not active")
        if issued_at > now + skew or authenticated_at > now + skew:
            raise WebIdentityBlocked("Web OIDC JWT time is in the future")
        if expires_at <= issued_at or not_before > expires_at:
            raise WebIdentityBlocked("Web OIDC JWT time range is invalid")
        if authenticated_at > issued_at + skew:
            raise WebIdentityBlocked("Web OIDC authentication time is invalid")
        if expires_at - issued_at > self._policy.max_token_lifetime + skew:
            raise WebIdentityBlocked("Web OIDC JWT lifetime is too long")
        if now - authenticated_at > self._policy.max_auth_age + skew:
            raise WebIdentityBlocked("Web OIDC MFA authentication is too old")

        authentication_methods = _validated_amr(payload.get("amr"))
        if not authentication_methods.intersection(self._policy.mfa.required_amr):
            raise WebIdentityBlocked("Web OIDC JWT does not satisfy MFA")
        authentication_context = payload.get("acr")
        if authentication_context is not None and not _is_claim_text(authentication_context, max_length=255):
            raise WebIdentityBlocked("Web OIDC authentication context is invalid")
        if self._policy.mfa.accepted_acr:
            if authentication_context not in self._policy.mfa.accepted_acr:
                raise WebIdentityBlocked("Web OIDC JWT authentication context is not accepted")
        return VerifiedOidcClaims(
            issuer=issuer,
            subject=subject,
            expires_at=expires_at,
            not_before=not_before,
            issued_at=issued_at,
            authenticated_at=authenticated_at,
            authentication_methods=authentication_methods,
            authentication_context=authentication_context,
            token_fingerprint=sha256(token.encode("ascii")).hexdigest(),
        )


def _matching_jwks_document(document: object, key_id: str) -> list[Mapping[str, Any]]:
    """Validate a bounded JWKS shape and select an exact key identifier."""

    if not isinstance(document, Mapping):
        raise WebIdentityBlocked("Web OIDC JWKS is unavailable")
    try:
        keys = document.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= _MAX_JWKS_KEYS:
            raise WebIdentityBlocked("Web OIDC JWKS is malformed")
        matching: list[Mapping[str, Any]] = []
        for candidate in keys:
            if not isinstance(candidate, Mapping):
                raise WebIdentityBlocked("Web OIDC JWKS is malformed")
            candidate_id = candidate.get("kid")
            if isinstance(candidate_id, str) and candidate_id == key_id:
                matching.append(candidate)
        return matching
    except WebIdentityBlocked:
        raise
    except Exception as error:
        raise WebIdentityBlocked("Web OIDC JWKS is malformed") from error


def _extract_bearer_token(value: str | None, *, max_token_bytes: int) -> str | None:
    if value is None:
        return None
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token or "\t" in token:
        raise WebIdentityBlocked("Web OIDC bearer credential is malformed")
    return _validate_compact_token(token, max_token_bytes=max_token_bytes)


def _validate_compact_token(token: str, *, max_token_bytes: int) -> str:
    if not isinstance(token, str) or not token:
        raise WebIdentityBlocked("Web OIDC credential is malformed")
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as error:
        raise WebIdentityBlocked("Web OIDC credential is malformed") from error
    if len(encoded) > max_token_bytes:
        raise WebIdentityBlocked("Web OIDC credential is malformed")
    segments = token.split(".")
    if len(segments) != 3 or any(not segment or not _TOKEN_SEGMENT.fullmatch(segment) for segment in segments):
        raise WebIdentityBlocked("Web OIDC credential is malformed")
    return token


def _decode_json_object(segment: str, *, label: str, max_bytes: int) -> Mapping[str, Any]:
    raw = _decode_base64url(segment, label=label, max_bytes=max_bytes)
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_members,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed") from error
    if not isinstance(value, Mapping):
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed")
    return value


def _decode_base64url(segment: str, *, label: str, max_bytes: int) -> bytes:
    if not _BASE64URL.fullmatch(segment) or len(segment) % 4 == 1:
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed")
    try:
        padding_bytes = "=" * (-len(segment) % 4)
        decoded = base64.urlsafe_b64decode((segment + padding_bytes).encode("ascii"))
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed") from error
    if not decoded or len(decoded) > max_bytes:
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed")
    return decoded


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON constant")


def _validate_jwt_header(header: Mapping[str, Any]) -> None:
    if header.get("alg") != "RS256":
        raise WebIdentityBlocked("Web OIDC JWT algorithm is not RS256")
    token_type = header.get("typ")
    if token_type is not None and token_type != "JWT":
        raise WebIdentityBlocked("Web OIDC JWT type is invalid")
    if any(key in header for key in {"b64", "crit", "jku", "jwk", "x5c", "x5u", "zip"}):
        raise WebIdentityBlocked("Web OIDC JWT header contains an unsupported key source or extension")


def _rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    if jwk.get("kty") != "RSA" or jwk.get("alg") != "RS256" or jwk.get("use") != "sig":
        raise WebIdentityBlocked("Web OIDC signing JWK is not an RS256 signature key")
    _required_claim_text(jwk, "kid", max_length=255, label="JWK key identifier")
    if any(parameter in jwk for parameter in {"d", "p", "q", "dp", "dq", "qi", "oth"}):
        raise WebIdentityBlocked("Web OIDC signing JWK must not contain private key material")
    key_ops = jwk.get("key_ops")
    if key_ops is not None:
        if (
            not isinstance(key_ops, list)
            or len(key_ops) != 1
            or key_ops[0] != "verify"
        ):
            raise WebIdentityBlocked("Web OIDC signing JWK key operations are invalid")
    modulus = _jwk_positive_integer(jwk.get("n"), label="JWK modulus")
    exponent = _jwk_positive_integer(jwk.get("e"), label="JWK exponent")
    if modulus.bit_length() < 2048 or modulus.bit_length() > _MAX_RSA_MODULUS_BITS:
        raise WebIdentityBlocked("Web OIDC signing JWK modulus length is invalid")
    if exponent < 3 or exponent % 2 == 0 or exponent > 2**32 - 1:
        raise WebIdentityBlocked("Web OIDC signing JWK exponent is invalid")
    try:
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except ValueError as error:
        raise WebIdentityBlocked("Web OIDC signing JWK is malformed") from error


def _jwk_positive_integer(value: object, *, label: str) -> int:
    if not isinstance(value, str):
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed")
    encoded = _decode_base64url(value, label=label, max_bytes=_MAX_RSA_MODULUS_BITS // 8)
    number = int.from_bytes(encoded, "big")
    if number <= 0:
        raise WebIdentityBlocked(f"Web OIDC {label} is malformed")
    return number


def _required_claim_text(
    payload: Mapping[str, Any],
    claim: str,
    *,
    max_length: int,
    label: str,
) -> str:
    value = payload.get(claim)
    if not _is_claim_text(value, max_length=max_length):
        raise WebIdentityBlocked(f"Web OIDC {label} is missing or invalid")
    return value


def _is_claim_text(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_length
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _validated_audiences(value: object) -> frozenset[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        raise WebIdentityBlocked("Web OIDC audience is missing or invalid")
    if not 1 <= len(candidates) <= 8 or not all(_is_claim_text(item, max_length=255) for item in candidates):
        raise WebIdentityBlocked("Web OIDC audience is missing or invalid")
    audiences = frozenset(candidates)
    if len(audiences) != len(candidates):
        raise WebIdentityBlocked("Web OIDC audience contains duplicates")
    return audiences


def _numeric_date(payload: Mapping[str, Any], claim: str) -> datetime:
    value = payload.get(claim)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 4_102_444_800:
        raise WebIdentityBlocked(f"Web OIDC {claim} is missing or invalid")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise WebIdentityBlocked(f"Web OIDC {claim} is missing or invalid") from error


def _validated_amr(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise WebIdentityBlocked("Web OIDC AMR claim is missing or invalid")
    methods = _normalize_claim_values(value, label="AMR claim")
    if len(methods) != len(value):
        raise WebIdentityBlocked("Web OIDC AMR claim contains duplicates")
    return methods


def _normalize_claim_values(values: object, *, label: str) -> frozenset[str]:
    if not isinstance(values, (frozenset, set, tuple, list)):
        raise ValueError(f"{label} must be a collection")
    if not all(_is_claim_text(value, max_length=255) for value in values):
        raise ValueError(f"{label} contains an invalid value")
    normalized = frozenset(value.strip().lower() for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{label} contains an invalid value")
    return normalized


def _validate_mapped_human_actor(actor: Actor | None) -> None:
    if not isinstance(actor, Actor):
        raise WebIdentityBlocked("Web OIDC subject is not mapped to an active internal actor")
    try:
        UUID(actor.actor_id)
        UUID(actor.firm_id)
    except (TypeError, ValueError) as error:
        raise WebIdentityBlocked("Web OIDC mapped actor is invalid") from error
    if not actor.roles or not all(isinstance(role, Role) for role in actor.roles):
        raise WebIdentityBlocked("Web OIDC mapped actor has no valid role")
    if Role.SYSTEM_WORKER in actor.roles:
        raise WebIdentityBlocked("Web OIDC cannot resolve a system worker actor")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    result: str | None = None
    for key, value in headers.items():
        if not isinstance(key, str):
            raise WebIdentityBlocked("Web OIDC authorization header is malformed")
        if key.lower() == name:
            if not isinstance(value, str):
                raise WebIdentityBlocked("Web OIDC authorization header is malformed")
            if result is not None:
                raise WebIdentityBlocked("Web OIDC authorization header is ambiguous")
            result = value
    return result


def _is_safe_cookie_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}", value))


def _validate_https_issuer(value: str) -> None:
    if not _is_claim_text(value, max_length=1024):
        raise ValueError("Web OIDC issuer is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("Web OIDC issuer must be an HTTPS issuer URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Web OIDC issuer must be an HTTPS issuer URL")


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebIdentityBlocked("Web OIDC clock must return a timezone-aware timestamp")
    result = value.astimezone(timezone.utc)
    try:
        timestamp = result.timestamp()
    except (OverflowError, OSError, ValueError) as error:
        raise WebIdentityBlocked("Web OIDC clock is invalid") from error
    if not isfinite(timestamp):
        raise WebIdentityBlocked("Web OIDC clock is invalid")
    return result
