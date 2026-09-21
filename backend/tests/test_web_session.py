from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Callable
from uuid import uuid4
import unittest

from fastapi import Request

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_session import (
    StoredWebSession,
    WebSessionAuthority,
    WebSessionBlocked,
    WebSessionPolicy,
)
from case_kernel.models import Actor, Role


ORIGIN = "https://workbench.example-law-firm.test"
ISSUER = "https://login.example-law-firm.test/realms/lawcase"
SESSION_TOKEN = "s" * 64
CSRF_TOKEN = "c" * 64
SECOND_SESSION_TOKEN = "q" * 64
SECOND_CSRF_TOKEN = "r" * 64


class _MemorySessionStore:
    def __init__(self) -> None:
        self.by_digest: dict[bytes, StoredWebSession] = {}
        self.fail_lookup = False

    def create_session(self, *, session: StoredWebSession) -> None:
        if session.session_token_sha256 in self.by_digest:
            raise ValueError("digest collision")
        self.by_digest[session.session_token_sha256] = session

    def find_session_by_digest(self, *, session_token_sha256: bytes) -> StoredWebSession | None:
        if self.fail_lookup:
            raise RuntimeError("storage details must not reach the browser")
        return self.by_digest.get(session_token_sha256)

    def revoke_session(self, *, session_id: str, revoked_at: datetime) -> bool:
        for digest, session in tuple(self.by_digest.items()):
            if session.session_id == session_id:
                self.by_digest[digest] = replace(session, revoked_at=revoked_at)
                return True
        return False

    def only_session(self) -> StoredWebSession:
        self.assertions()
        return next(iter(self.by_digest.values()))

    def assertions(self) -> None:
        if len(self.by_digest) != 1:
            raise AssertionError("expected exactly one in-memory session")


class _ActorDirectory:
    def __init__(self, actor: Actor | None) -> None:
        self.actor = actor
        self.calls: list[tuple[str, str]] = []

    def resolve_active_actor(self, *, actor_id: str, firm_id: str, issuer: str) -> Actor | None:
        self.calls.append((actor_id, firm_id, issuer))
        if self.actor is None:
            return None
        if (actor_id, firm_id) != (self.actor.actor_id, self.actor.firm_id):
            return None
        return self.actor


class WebSessionAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 11, 10, 30, tzinfo=timezone.utc)
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.store = _MemorySessionStore()
        self.directory = _ActorDirectory(self.actor)
        self.tokens = iter((SESSION_TOKEN, CSRF_TOKEN, SECOND_SESSION_TOKEN, SECOND_CSRF_TOKEN))
        self.authority = WebSessionAuthority(
            store=self.store,
            actor_directory=self.directory,
            policy=WebSessionPolicy(public_origin=ORIGIN),
            clock=lambda: self.now,
            token_factory=lambda: next(self.tokens),
        )

    def _identity(
        self,
        *,
        actor: Actor | None = None,
        method: AuthenticationMethod = AuthenticationMethod.OIDC_MFA,
        expires_in: timedelta = timedelta(minutes=45),
        authenticated_at: datetime | None = None,
    ) -> ServerIdentityContext:
        return ServerIdentityContext(
            actor=actor or self.actor,
            session_id=str(uuid4()),
            issuer=ISSUER,
            authentication_method=method,
            authenticated_at=authenticated_at or self.now - timedelta(minutes=5),
            expires_at=self.now + expires_in,
        )

    @staticmethod
    def _cookie_header(*, session_token: str, csrf_token: str | None = None, duplicate_session: bool = False) -> str:
        values = [f"__Host-lawcase_session={session_token}"]
        if duplicate_session:
            values.append(f"__Host-lawcase_session={session_token}")
        if csrf_token is not None:
            values.append(f"__Host-lawcase_csrf={csrf_token}")
        return "; ".join(values)

    def _request(
        self,
        *,
        method: str,
        session_token: str = SESSION_TOKEN,
        csrf_token: str | None = None,
        headers: dict[str, str] | None = None,
        duplicate_session: bool = False,
    ) -> Request:
        request_headers = {
            "Cookie": self._cookie_header(
                session_token=session_token,
                csrf_token=csrf_token,
                duplicate_session=duplicate_session,
            ),
        }
        request_headers.update(headers or {})
        return Request(
            {
                "type": "http",
                "method": method,
                "path": "/api/v1/cases",
                "headers": [
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in request_headers.items()
                ],
                "client": ("127.0.0.1", 50001),
                "server": ("workbench.example-law-firm.test", 443),
                "scheme": "https",
                "query_string": b"",
            }
        )

    def _issue(self) -> object:
        return self.authority.issue(identity=self._identity())

    def test_issue_stores_only_hashes_and_sets_host_scoped_cookie_pair(self) -> None:
        grant = self._issue()
        stored = self.store.only_session()

        self.assertEqual(stored.session_token_sha256, sha256(SESSION_TOKEN.encode("ascii")).digest())
        self.assertEqual(stored.csrf_token_sha256, sha256(CSRF_TOKEN.encode("ascii")).digest())
        self.assertFalse(hasattr(stored, "session_token"))
        self.assertFalse(hasattr(stored, "csrf_token"))
        self.assertNotIn(SESSION_TOKEN, repr(stored))
        self.assertNotIn(CSRF_TOKEN, repr(stored))
        self.assertNotIn(SESSION_TOKEN, repr(grant))
        self.assertNotIn(CSRF_TOKEN, repr(grant))

        self.assertEqual(grant.session_cookie.name, "__Host-lawcase_session")
        self.assertTrue(grant.session_cookie.secure)
        self.assertTrue(grant.session_cookie.httponly)
        self.assertEqual(grant.session_cookie.samesite, "strict")
        self.assertEqual(grant.session_cookie.path, "/")
        self.assertIsNone(grant.session_cookie.domain)
        self.assertTrue(grant.csrf_cookie.secure)
        self.assertFalse(grant.csrf_cookie.httponly)
        self.assertEqual(grant.csrf_cookie.samesite, "strict")

        # Hostile browser-supplied role/firm headers cannot influence identity.
        identity = asyncio.run(
            self.authority.resolve(
                self._request(
                    method="GET",
                    headers={
                        "X-Firm-ID": str(uuid4()),
                        "X-Roles": "SYSTEM_WORKER,FIRM_ADMIN",
                    },
                )
            )
        )
        self.assertEqual(identity.actor, self.actor)
        self.assertEqual(identity.authentication_method, AuthenticationMethod.OIDC_MFA)
        self.assertEqual(identity.session_id, grant.session_id)
        self.assertEqual(self.directory.calls, [(self.actor.actor_id, self.actor.firm_id, ISSUER)])

    def test_live_short_lived_oidc_identity_issues_policy_bounded_opaque_session(self) -> None:
        grant = self.authority.issue(identity=self._identity(expires_in=timedelta(minutes=10)))
        stored = self.store.only_session()

        expected_expiry = self.now + timedelta(hours=12)
        self.assertEqual(grant.expires_at, expected_expiry)
        self.assertEqual(stored.expires_at, expected_expiry)
        self.assertEqual(grant.session_cookie.max_age, 12 * 60 * 60)
        self.assertEqual(grant.csrf_cookie.max_age, 12 * 60 * 60)

    def test_only_live_oidc_mfa_server_identity_can_issue(self) -> None:
        with self.assertRaises(WebSessionBlocked):
            self.authority.issue(
                identity=self._identity(method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION),
            )
        system_actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.SYSTEM_WORKER}))
        with self.assertRaises(WebSessionBlocked):
            self.authority.issue(identity=self._identity(actor=system_actor))
        with self.assertRaises(WebSessionBlocked):
            self.authority.issue(
                identity=self._identity(authenticated_at=self.now - timedelta(hours=13)),
            )
        with self.assertRaises(WebSessionBlocked):
            self.authority.issue(identity=self._identity(expires_in=timedelta(seconds=0)))
        self.assertEqual(self.store.by_digest, {})

    def test_unsafe_requests_require_same_origin_fetch_site_and_double_submit_csrf(self) -> None:
        self._issue()
        valid = self._request(
            method="POST",
            csrf_token=CSRF_TOKEN,
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "X-Lawcase-CSRF": CSRF_TOKEN,
            },
        )
        self.assertEqual(asyncio.run(self.authority.resolve(valid)).actor, self.actor)

        failures: dict[str, Request] = {
            "missing_origin": self._request(
                method="POST",
                csrf_token=CSRF_TOKEN,
                headers={"X-Lawcase-CSRF": CSRF_TOKEN},
            ),
            "cross_origin": self._request(
                method="POST",
                csrf_token=CSRF_TOKEN,
                headers={"Origin": "https://attacker.example", "X-Lawcase-CSRF": CSRF_TOKEN},
            ),
            "cross_site": self._request(
                method="POST",
                csrf_token=CSRF_TOKEN,
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "cross-site",
                    "X-Lawcase-CSRF": CSRF_TOKEN,
                },
            ),
            "mismatched_double_submit": self._request(
                method="POST",
                csrf_token=SECOND_CSRF_TOKEN,
                headers={"Origin": ORIGIN, "X-Lawcase-CSRF": CSRF_TOKEN},
            ),
            "missing_header": self._request(
                method="POST",
                csrf_token=CSRF_TOKEN,
                headers={"Origin": ORIGIN},
            ),
        }
        for name, request in failures.items():
            with self.subTest(name=name), self.assertRaises(WebSessionBlocked) as blocked:
                asyncio.run(self.authority.resolve(request))
            self.assertNotIn(SESSION_TOKEN, str(blocked.exception))
            self.assertNotIn(CSRF_TOKEN, str(blocked.exception))

    def test_revoked_expired_or_offboarded_session_fails_closed(self) -> None:
        grant = self._issue()
        self.authority.revoke(session_id=grant.session_id)
        with self.assertRaises(WebSessionBlocked):
            asyncio.run(self.authority.resolve(self._request(method="GET")))

        second_grant = self.authority.issue(identity=self._identity())
        self.now += timedelta(hours=12, seconds=1)
        with self.assertRaises(WebSessionBlocked):
            asyncio.run(
                self.authority.resolve(
                    self._request(method="GET", session_token=SECOND_SESSION_TOKEN),
                )
            )
        self.assertNotEqual(grant.session_id, second_grant.session_id)

        # A currently inactive directory mapping cannot be revived by a cookie.
        self.now -= timedelta(hours=12, seconds=1)
        self.directory.actor = None
        with self.assertRaises(WebSessionBlocked):
            asyncio.run(
                self.authority.resolve(
                    self._request(method="GET", session_token=SECOND_SESSION_TOKEN),
                )
            )

    def test_ambiguous_cookie_bearer_and_storage_errors_are_rejected_without_secret_leakage(self) -> None:
        self._issue()
        with self.assertRaises(WebSessionBlocked):
            asyncio.run(self.authority.resolve(self._request(method="GET", duplicate_session=True)))
        with self.assertRaises(WebSessionBlocked):
            asyncio.run(
                self.authority.resolve(
                    self._request(
                        method="GET",
                        headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
                    )
                )
            )
        self.store.fail_lookup = True
        with self.assertRaises(WebSessionBlocked) as blocked:
            asyncio.run(self.authority.resolve(self._request(method="GET")))
        self.assertNotIn("storage details", str(blocked.exception))
        self.assertNotIn(SESSION_TOKEN, str(blocked.exception))

    def test_policy_rejects_relaxed_cookies_and_noncanonical_origin(self) -> None:
        with self.assertRaises(ValueError):
            WebSessionPolicy(public_origin="http://workbench.example-law-firm.test")
        with self.assertRaises(ValueError):
            WebSessionPolicy(public_origin=f"{ORIGIN}/")
        with self.assertRaises(ValueError):
            WebSessionPolicy(public_origin=ORIGIN, same_site="lax")
        with self.assertRaises(ValueError):
            WebSessionPolicy(public_origin=ORIGIN, session_cookie_name="lawcase_session")
        with self.assertRaises(ValueError):
            WebSessionPolicy(public_origin=ORIGIN, max_session_lifetime=timedelta(hours=12, seconds=1))


if __name__ == "__main__":
    unittest.main()
