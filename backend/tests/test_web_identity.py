from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import uuid4
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import Request

from case_api.persistent_identity import AuthenticationMethod
from case_api.web_identity import (
    MfaClaimRequirement,
    OidcVerificationPolicy,
    TokenSource,
    TokenTransportPolicy,
    WebIdentityBlocked,
    WebOidcIdentityResolver,
)
from case_kernel.models import Actor, Role


ISSUER = "https://login.example-law-firm.test/realms/lawcase"
AUDIENCE = "lawcase-web-api"
SUBJECT = "oidc-subject-1001"
KID = "test-rs256-key-1"


class _StaticJwks:
    def __init__(self, document: dict[str, Any] | None, *, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.calls = 0

    def load_jwks(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.document


class _RotatingJwks:
    def __init__(self, *, stale: dict[str, Any], fresh: dict[str, Any]) -> None:
        self.stale = stale
        self.fresh = fresh
        self.loads = 0
        self.refreshes = 0

    def load_jwks(self):
        self.loads += 1
        return self.stale

    def refresh_jwks(self):
        self.refreshes += 1
        return self.fresh


class _ServerActorMapping:
    def __init__(self, actor: Actor | None) -> None:
        self.actor = actor
        self.calls: list[tuple[str, str]] = []

    def resolve_actor(self, *, issuer: str, subject: str) -> Actor | None:
        self.calls.append((issuer, subject))
        return self.actor if (issuer, subject) == (ISSUER, SUBJECT) else None


class WebOidcIdentityResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 11, 8, 30, tzinfo=timezone.utc)
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.mapping = _ServerActorMapping(self.actor)
        self.jwks = _StaticJwks({"keys": [_jwk_from_private_key(self.private_key, kid=KID)]})
        self.resolver = self._resolver()

    def _resolver(
        self,
        *,
        jwks: _StaticJwks | None = None,
        mapping: _ServerActorMapping | None = None,
        transport: TokenTransportPolicy = TokenTransportPolicy(),
        policy: OidcVerificationPolicy | None = None,
    ) -> WebOidcIdentityResolver:
        return WebOidcIdentityResolver(
            policy=policy or OidcVerificationPolicy(issuer=ISSUER, audience=AUDIENCE),
            jwks_provider=jwks or self.jwks,
            actor_mapping=mapping or self.mapping,
            transport=transport,
            clock=lambda: self.now,
        )

    def _claims(self, **overrides: Any) -> dict[str, Any]:
        now_seconds = int(self.now.timestamp())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "exp": now_seconds + 600,
            "nbf": now_seconds - 30,
            "iat": now_seconds - 30,
            "auth_time": now_seconds - 90,
            "amr": ["pwd", "mfa"],
            # These deliberately hostile claims must never affect Actor mapping.
            "firm_id": str(uuid4()),
            "roles": ["FIRM_ADMIN", "SYSTEM_WORKER"],
        }
        claims.update(overrides)
        return claims

    def _token(self, *, claims: dict[str, Any] | None = None, header: dict[str, Any] | None = None) -> str:
        return _sign_rs256_jwt(
            self.private_key,
            header or {"alg": "RS256", "typ": "JWT", "kid": KID},
            claims or self._claims(),
        )

    def test_valid_rs256_jwt_resolves_only_server_mapped_actor(self) -> None:
        token = self._token()

        identity = self.resolver.resolve_token(token)

        self.assertEqual(identity.actor, self.actor)
        self.assertEqual(identity.authentication_method, AuthenticationMethod.OIDC_MFA)
        self.assertEqual(self.mapping.calls, [(ISSUER, SUBJECT)])
        self.assertNotIn(token, repr(identity))
        self.assertNotIn(token, repr(self.resolver))

        changed_browser_claims = self._claims(
            firm_id=str(uuid4()),
            roles=["SYSTEM_WORKER"],
            preferred_username="attacker",
        )
        self.assertEqual(self.resolver.resolve_token(self._token(claims=changed_browser_claims)).actor, self.actor)
        self.assertEqual(self.mapping.calls[-1], (ISSUER, SUBJECT))

    def test_cookie_and_bearer_policy_is_explicit_and_redacts_credentials(self) -> None:
        token = self._token()
        policy = TokenTransportPolicy(cookie_name="__Host-lawcase_oidc")

        bearer = policy.extract(headers={"Authorization": f"Bearer {token}"})
        cookie = policy.extract(headers={}, cookies={"__Host-lawcase_oidc": token})

        self.assertEqual(bearer.source, TokenSource.BEARER)
        self.assertEqual(cookie.source, TokenSource.COOKIE)
        self.assertNotIn(token, repr(bearer))
        with self.assertRaises(WebIdentityBlocked) as conflict:
            policy.extract(
                headers={"Authorization": f"Bearer {token}"},
                cookies={"__Host-lawcase_oidc": token},
            )
        self.assertNotIn(token, str(conflict.exception))
        with self.assertRaises(WebIdentityBlocked):
            TokenTransportPolicy().extract(headers={}, cookies={"__Host-lawcase_oidc": token})

    def test_async_resolver_contract_reads_configured_cookie_or_bearer_without_routes(self) -> None:
        token = self._token()
        resolver = self._resolver(transport=TokenTransportPolicy(cookie_name="__Host-lawcase_oidc"))
        request = _request(headers={"Authorization": f"Bearer {token}"})
        identity = asyncio.run(resolver.resolve(request))
        self.assertEqual(identity.actor, self.actor)

        cookie_request = _request(headers={"Cookie": f"__Host-lawcase_oidc={token}"})
        cookie_identity = asyncio.run(resolver.resolve(cookie_request))
        self.assertEqual(cookie_identity.actor, self.actor)

    def test_signature_algorithm_and_jwks_fail_closed(self) -> None:
        cases = {
            "wrong_alg": self._token(header={"alg": "HS256", "typ": "JWT", "kid": KID}),
            "unknown_kid": self._token(header={"alg": "RS256", "typ": "JWT", "kid": "not-current"}),
        }
        for name, token in cases.items():
            with self.subTest(name=name), self.assertRaises(WebIdentityBlocked) as blocked:
                self.resolver.resolve_token(token)
            self.assertNotIn(token, str(blocked.exception))

        malformed = self._resolver(jwks=_StaticJwks({"keys": "not-a-list"}))
        with self.assertRaises(WebIdentityBlocked):
            malformed.resolve_token(self._token())

        bad_key = _jwk_from_private_key(self.private_key, kid=KID)
        bad_key["n"] = "not-a-valid-modulus***"
        malformed_key = self._resolver(jwks=_StaticJwks({"keys": [bad_key]}))
        with self.assertRaises(WebIdentityBlocked):
            malformed_key.resolve_token(self._token())

        unsupported_operation = _jwk_from_private_key(self.private_key, kid=KID)
        unsupported_operation["key_ops"] = ["sign"]
        invalid_operation = self._resolver(jwks=_StaticJwks({"keys": [unsupported_operation]}))
        with self.assertRaises(WebIdentityBlocked):
            invalid_operation.resolve_token(self._token())

        unavailable = self._resolver(jwks=_StaticJwks(None, error=RuntimeError("do not leak provider internals")))
        with self.assertRaises(WebIdentityBlocked) as blocked:
            unavailable.resolve_token(self._token())
        self.assertNotIn("provider internals", str(blocked.exception))

    def test_unknown_key_id_can_force_one_server_owned_jwks_refresh(self) -> None:
        rotating = _RotatingJwks(
            stale={"keys": [_jwk_from_private_key(self.private_key, kid="previous-key")]},
            fresh={"keys": [_jwk_from_private_key(self.private_key, kid=KID)]},
        )
        identity = self._resolver(jwks=rotating).resolve_token(self._token())
        self.assertEqual(identity.actor, self.actor)
        self.assertEqual(rotating.loads, 1)
        self.assertEqual(rotating.refreshes, 1)

    def test_registered_claim_and_mfa_failures_are_rejected(self) -> None:
        now_seconds = int(self.now.timestamp())
        cases = {
            "issuer": self._claims(iss="https://attacker.example"),
            "audience": self._claims(aud="other-api"),
            "expired": self._claims(exp=now_seconds - 31),
            "missing_exp": {key: value for key, value in self._claims().items() if key != "exp"},
            "not_before": self._claims(nbf=now_seconds + 31),
            "missing_nbf": {key: value for key, value in self._claims().items() if key != "nbf"},
            "future_iat": self._claims(iat=now_seconds + 31),
            "missing_iat": {key: value for key, value in self._claims().items() if key != "iat"},
            "missing_auth_time": {key: value for key, value in self._claims().items() if key != "auth_time"},
            "old_auth": self._claims(auth_time=now_seconds - int(timedelta(hours=13).total_seconds())),
            "no_mfa": self._claims(amr=["pwd"]),
            "multi_aud_without_azp": self._claims(aud=[AUDIENCE, "other-api"]),
        }
        for name, claims in cases.items():
            with self.subTest(name=name), self.assertRaises(WebIdentityBlocked):
                self.resolver.resolve_token(self._token(claims=claims))

    def test_acr_requirements_and_unknown_subject_are_checked_server_side(self) -> None:
        policy = OidcVerificationPolicy(
            issuer=ISSUER,
            audience=AUDIENCE,
            mfa=MfaClaimRequirement(required_amr=frozenset({"mfa"}), accepted_acr=frozenset({"urn:lawfirm:mfa"})),
        )
        resolver = self._resolver(policy=policy)
        with self.assertRaises(WebIdentityBlocked):
            resolver.resolve_token(self._token())
        identity = resolver.resolve_token(self._token(claims=self._claims(acr="urn:lawfirm:mfa")))
        self.assertEqual(identity.actor, self.actor)
        with self.assertRaises(WebIdentityBlocked):
            resolver.resolve_token(self._token(claims=self._claims(sub="unknown-subject")))

    def test_invalid_mapped_actor_cannot_create_system_identity(self) -> None:
        system_actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        resolver = self._resolver(mapping=_ServerActorMapping(system_actor))
        with self.assertRaises(WebIdentityBlocked):
            resolver.resolve_token(self._token())


def _jwk_from_private_key(private_key: rsa.RSAPrivateKey, *, kid: str) -> dict[str, str]:
    public_numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _base64url_uint(public_numbers.n),
        "e": _base64url_uint(public_numbers.e),
    }


def _sign_rs256_jwt(
    private_key: rsa.RSAPrivateKey,
    header: dict[str, Any],
    claims: dict[str, Any],
) -> str:
    encoded_header = _base64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _base64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


def _base64url_uint(value: int) -> str:
    return _base64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request(*, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/identity",
            "headers": [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()],
            "client": ("127.0.0.1", 50001),
            "server": ("127.0.0.1", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


if __name__ == "__main__":
    unittest.main()
