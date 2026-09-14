from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from case_api.web_identity import OidcVerificationPolicy, WebOidcIdentityResolver
from case_api.web_oidc_login import (
    EphemeralOidcAuthorizationStateStore,
    OidcAuthorizationCodeLogin,
    OidcAuthorizationCodePolicy,
    OidcLoginBlocked,
    OidcTokenExchangeRequest,
    OidcTokenExchangeResponse,
    PendingOidcAuthorization,
    UrlLibOidcTokenEndpointClient,
)
from case_api.web_session import StoredWebSession, WebSessionAuthority, WebSessionPolicy
from case_kernel.models import Actor, Role


ORIGIN = "https://workbench.example-law-firm.test"
ISSUER = "https://login.example-law-firm.test/realms/lawcase"
AUTHORIZATION_ENDPOINT = "https://login.example-law-firm.test/realms/lawcase/protocol/openid-connect/auth"
TOKEN_ENDPOINT = "https://login.example-law-firm.test/realms/lawcase/protocol/openid-connect/token"
AUDIENCE = "lawcase-web-api"
CLIENT_ID = "lawcase-web-api"
CLIENT_SECRET = "synthetic-server-only-secret"
SUBJECT = "oidc-subject-1001"
KID = "test-rs256-key-1"
CODE = "authorization-code-1001"
SESSION_TOKEN = "s" * 64
CSRF_TOKEN = "c" * 64


class _StaticJwks:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def load_jwks(self) -> dict[str, Any]:
        return self.document


class _ActorMapping:
    def __init__(self, actor: Actor) -> None:
        self.actor = actor
        self.calls: list[tuple[str, str]] = []

    def resolve_actor(self, *, issuer: str, subject: str) -> Actor | None:
        self.calls.append((issuer, subject))
        return self.actor if (issuer, subject) == (ISSUER, SUBJECT) else None


class _MemorySessionStore:
    def __init__(self) -> None:
        self.by_digest: dict[bytes, StoredWebSession] = {}

    def create_session(self, *, session: StoredWebSession) -> None:
        self.by_digest[session.session_token_sha256] = session

    def find_session_by_digest(self, *, session_token_sha256: bytes) -> StoredWebSession | None:
        return self.by_digest.get(session_token_sha256)

    def revoke_session(self, *, session_id: str, revoked_at: datetime) -> bool:
        for digest, session in tuple(self.by_digest.items()):
            if session.session_id == session_id:
                self.by_digest[digest] = replace(session, revoked_at=revoked_at)
                return True
        return False


class _ActorDirectory:
    def __init__(self, actor: Actor) -> None:
        self.actor = actor

    def resolve_active_actor(self, *, actor_id: str, firm_id: str, issuer: str) -> Actor | None:
        if actor_id == self.actor.actor_id and firm_id == self.actor.firm_id and issuer == ISSUER:
            return self.actor
        return None


class _FakeTokenClient:
    def __init__(self, responder: Callable[[OidcTokenExchangeRequest], OidcTokenExchangeResponse]) -> None:
        self.responder = responder
        self.calls: list[OidcTokenExchangeRequest] = []

    def exchange_authorization_code(self, *, request: OidcTokenExchangeRequest) -> OidcTokenExchangeResponse:
        self.calls.append(request)
        return self.responder(request)


class _MalformedPendingStore:
    def __init__(self, pending: PendingOidcAuthorization) -> None:
        self.returned_pending = pending
        self.created_pending: PendingOidcAuthorization | None = None

    def store_pending(self, *, pending: PendingOidcAuthorization) -> bool:
        self.created_pending = pending
        return True

    def consume_pending(self, *, state_sha256: bytes, now: datetime) -> PendingOidcAuthorization | None:
        del state_sha256, now
        result = self.returned_pending
        self.returned_pending = None  # type: ignore[assignment]
        return result


class WebOidcAuthorizationCodeLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.mapping = _ActorMapping(self.actor)
        self.resolver = WebOidcIdentityResolver(
            policy=OidcVerificationPolicy(issuer=ISSUER, audience=AUDIENCE),
            jwks_provider=_StaticJwks({"keys": [_jwk_from_private_key(self.private_key, kid=KID)]}),
            actor_mapping=self.mapping,
            clock=lambda: self.now,
        )
        self.session_store = _MemorySessionStore()
        tokens = iter((SESSION_TOKEN, CSRF_TOKEN))
        self.session_authority = WebSessionAuthority(
            store=self.session_store,
            actor_directory=_ActorDirectory(self.actor),
            policy=WebSessionPolicy(public_origin=ORIGIN),
            clock=lambda: self.now,
            token_factory=lambda: next(tokens),
        )
        self.state_store = EphemeralOidcAuthorizationStateStore()
        self.token_client = _FakeTokenClient(lambda _: OidcTokenExchangeResponse(id_token=""))
        self.secret_values = iter(("a" * 43, "b" * 43, "c" * 86, "d" * 43, "e" * 43, "f" * 86))
        self.login = self._login()

    def _policy(self, **overrides: Any) -> OidcAuthorizationCodePolicy:
        values: dict[str, Any] = {
            "public_origin": ORIGIN,
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        values.update(overrides)
        return OidcAuthorizationCodePolicy(**values)

    def _login(self, *, state_store=None, token_client=None, policy=None) -> OidcAuthorizationCodeLogin:
        return OidcAuthorizationCodeLogin(
            policy=policy or self._policy(),
            state_store=state_store or self.state_store,
            token_client=token_client or self.token_client,
            identity_resolver=self.resolver,
            session_issuer=self.session_authority,
            clock=lambda: self.now,
            secret_factory=lambda _: next(self.secret_values),
        )

    def _claims(self, *, nonce: str | None, **overrides: Any) -> dict[str, Any]:
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
        }
        if nonce is not None:
            claims["nonce"] = nonce
        claims.update(overrides)
        return claims

    def _id_token(self, *, nonce: str | None, **overrides: Any) -> str:
        return _sign_rs256_jwt(
            self.private_key,
            {"alg": "RS256", "typ": "JWT", "kid": KID},
            self._claims(nonce=nonce, **overrides),
        )

    @staticmethod
    def _parameters(redirect, *, code: str = CODE, issuer: str | None = None) -> tuple[dict[str, str], list[tuple[str, str]]]:
        query = dict(parse_qsl(urlsplit(redirect.authorization_url).query, keep_blank_values=True))
        callback = [("code", code), ("state", query["state"])]
        if issuer is not None:
            callback.append(("iss", issuer))
        return query, callback

    def test_happy_path_uses_fixed_code_pkce_nonce_then_issues_opaque_cookie_session(self) -> None:
        redirect = self.login.begin_authorization()
        query, callback = self._parameters(redirect, issuer=ISSUER)
        self.token_client.responder = lambda request: OidcTokenExchangeResponse(id_token=self._id_token(nonce=query["nonce"]))

        grant = self.login.complete_callback(parameters=callback)

        self.assertEqual(urlsplit(redirect.authorization_url).scheme, "https")
        self.assertEqual(urlsplit(redirect.authorization_url).netloc, urlsplit(AUTHORIZATION_ENDPOINT).netloc)
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["response_mode"], "query")
        self.assertEqual(query["client_id"], CLIENT_ID)
        self.assertEqual(query["redirect_uri"], f"{ORIGIN}/api/v1/auth/oidc/callback")
        self.assertEqual(query["scope"], "openid")
        self.assertEqual(query["code_challenge_method"], "S256")
        expected_challenge = base64.urlsafe_b64encode(sha256(("c" * 86).encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertEqual(query["code_challenge"], expected_challenge)
        self.assertNotIn(query["state"], repr(redirect))
        self.assertNotIn(query["nonce"], repr(redirect))

        self.assertEqual(len(self.token_client.calls), 1)
        exchanged = self.token_client.calls[0]
        self.assertEqual(exchanged.token_endpoint, TOKEN_ENDPOINT)
        self.assertEqual(exchanged.client_id, CLIENT_ID)
        self.assertEqual(exchanged.redirect_uri, f"{ORIGIN}/api/v1/auth/oidc/callback")
        self.assertEqual(exchanged.code, CODE)
        self.assertEqual(exchanged.code_verifier, "c" * 86)
        self.assertNotIn(CODE, repr(exchanged))
        self.assertNotIn(CLIENT_SECRET, repr(exchanged))

        self.assertEqual(grant.session_cookie.name, "__Host-lawcase_session")
        self.assertEqual(grant.csrf_cookie.name, "__Host-lawcase_csrf")
        self.assertTrue(grant.session_cookie.httponly)
        self.assertFalse(grant.csrf_cookie.httponly)
        self.assertEqual(len(self.session_store.by_digest), 1)
        self.assertEqual(self.mapping.calls, [(ISSUER, SUBJECT)])

    def test_same_callback_is_one_time_even_after_a_successful_session_issue(self) -> None:
        redirect = self.login.begin_authorization()
        query, callback = self._parameters(redirect)
        self.token_client.responder = lambda _: OidcTokenExchangeResponse(id_token=self._id_token(nonce=query["nonce"]))

        self.login.complete_callback(parameters=callback)
        with self.assertRaises(OidcLoginBlocked):
            self.login.complete_callback(parameters=callback)

        self.assertEqual(len(self.token_client.calls), 1)
        self.assertEqual(len(self.session_store.by_digest), 1)

    def test_malformed_implicit_hybrid_or_ambiguous_callback_never_reaches_token_endpoint(self) -> None:
        cases = {
            "implicit": lambda callback: callback + [("access_token", "attacker-token")],
            "hybrid": lambda callback: callback + [("id_token", "attacker-token")],
            "missing_code": lambda callback: [item for item in callback if item[0] != "code"],
            "unsupported": lambda callback: callback + [("next", "/attacker")],
            "duplicate_code": lambda callback: callback + [("code", "second-code")],
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.secret_values = iter((f"{name[0]}" * 43, f"{name[0]}" * 43 + "x", f"{name[0]}" * 86))
                # Ensure state and nonce remain distinct for every subtest.
                self.secret_values = iter(("g" * 43, "h" * 43, "i" * 86))
                login = self._login()
                redirect = login.begin_authorization()
                _, callback = self._parameters(redirect)
                with self.assertRaises(OidcLoginBlocked):
                    login.complete_callback(parameters=mutate(callback))
                self.assertEqual(self.token_client.calls, [])
                self.assertEqual(self.session_store.by_digest, {})

    def test_nonce_missing_or_mismatched_id_token_is_rejected_before_actor_mapping_and_session(self) -> None:
        for name, token_nonce in (("missing", None), ("mismatch", "z" * 43)):
            with self.subTest(name=name):
                self.secret_values = iter(("j" * 43, "k" * 43, "l" * 86))
                login = self._login()
                redirect = login.begin_authorization()
                query, callback = self._parameters(redirect)
                self.token_client.responder = lambda _, token_nonce=token_nonce: OidcTokenExchangeResponse(
                    id_token=self._id_token(nonce=token_nonce)
                )
                with self.assertRaises(OidcLoginBlocked):
                    login.complete_callback(parameters=callback)
                self.assertEqual(self.mapping.calls, [])
                self.assertEqual(self.session_store.by_digest, {})
                self.token_client.calls.clear()

    def test_missing_id_token_or_mfa_fails_closed_without_a_session(self) -> None:
        for name, responder in (
            ("missing_id_token", lambda _: OidcTokenExchangeResponse(id_token="")),
            ("no_mfa", lambda nonce: OidcTokenExchangeResponse(id_token=self._id_token(nonce=nonce, amr=["pwd"]))),
        ):
            with self.subTest(name=name):
                self.secret_values = iter(("m" * 43, "n" * 43, "o" * 86))
                login = self._login()
                redirect = login.begin_authorization()
                query, callback = self._parameters(redirect)
                if name == "missing_id_token":
                    self.token_client.responder = responder
                else:
                    self.token_client.responder = lambda _, responder=responder: responder(query["nonce"])
                with self.assertRaises(OidcLoginBlocked):
                    login.complete_callback(parameters=callback)
                self.assertEqual(self.session_store.by_digest, {})
                self.token_client.calls.clear()

    def test_expired_or_corrupt_short_term_state_blocks_before_token_exchange(self) -> None:
        redirect = self.login.begin_authorization()
        _, callback = self._parameters(redirect)
        self.now += timedelta(minutes=5)
        with self.assertRaises(OidcLoginBlocked):
            self.login.complete_callback(parameters=callback)
        self.assertEqual(self.token_client.calls, [])

        wrong_digest_pending = PendingOidcAuthorization(
            state_sha256=sha256(("w" * 43).encode("ascii")).digest(),
            nonce="y" * 43,
            code_verifier="z" * 86,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        malformed_store = _MalformedPendingStore(wrong_digest_pending)
        self.secret_values = iter(("x" * 43, "y" * 43, "z" * 86))
        login = self._login(state_store=malformed_store)
        redirect = login.begin_authorization()
        _, callback = self._parameters(redirect)
        with self.assertRaises(OidcLoginBlocked):
            login.complete_callback(parameters=callback)
        self.assertEqual(self.token_client.calls, [])

        missing_pkce_pending = PendingOidcAuthorization(
            state_sha256=sha256(("u" * 43).encode("ascii")).digest(),
            nonce="v" * 43,
            code_verifier="too-short",
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        malformed_store = _MalformedPendingStore(missing_pkce_pending)
        self.secret_values = iter(("u" * 43, "v" * 43, "w" * 86))
        login = self._login(state_store=malformed_store)
        redirect = login.begin_authorization()
        _, callback = self._parameters(redirect)
        with self.assertRaises(OidcLoginBlocked):
            login.complete_callback(parameters=callback)
        self.assertEqual(self.token_client.calls, [])

    def test_policy_and_real_http_request_shape_refuse_http_open_redirects_and_invalid_token_endpoints(self) -> None:
        invalid = (
            {"public_origin": "http://workbench.example-law-firm.test"},
            {"issuer": "http://login.example-law-firm.test/realm"},
            {"authorization_endpoint": "http://login.example-law-firm.test/auth"},
            {"token_endpoint": "https://login.example-law-firm.test/token?next=attacker"},
            {"token_endpoint": "https://attacker.example/token"},
            {"callback_path": "https://attacker.example/callback"},
            {"callback_path": "//attacker.example/callback"},
            {"scopes": frozenset({"profile"})},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self._policy(**values)

        request = OidcTokenExchangeRequest(
            token_endpoint="http://attacker.example/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code=CODE,
            redirect_uri=f"{ORIGIN}/api/v1/auth/oidc/callback",
            code_verifier="p" * 86,
        )
        with self.assertRaises(OidcLoginBlocked):
            request.validate()
        with self.assertRaises(ValueError):
            UrlLibOidcTokenEndpointClient(timeout_seconds=0.5)


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


if __name__ == "__main__":
    unittest.main()
