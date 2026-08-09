from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from case_api.persistent_identity import (
    AuthenticationMethod,
    DesktopSessionAuthority,
    PersistentAuthenticationBlocked,
)
from case_kernel.models import Actor, Role


BOOTSTRAP = "b" * 64
ACCESS_TOKEN = "s" * 64


class DesktopSessionIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
        self.actor = Actor(
            str(uuid4()),
            str(uuid4()),
            frozenset({Role.LEAD_LAWYER}),
        )
        self.authority = DesktopSessionAuthority(
            actor=self.actor,
            bootstrap_token=BOOTSTRAP,
            bootstrap_expires_at=self.now + timedelta(seconds=30),
            session_expires_at=self.now + timedelta(minutes=30),
            clock=lambda: self.now,
            token_factory=lambda: ACCESS_TOKEN,
        )
        app = FastAPI()

        @app.post("/exchange")
        async def exchange(request: Request):
            grant = self.authority.exchange(
                request=request,
                bootstrap_token=request.headers.get("x-desktop-bootstrap", ""),
            )
            return {"access_token": grant.access_token, "session_id": grant.session_id}

        @app.get("/identity")
        async def identity(request: Request):
            resolved = await self.authority.resolve(request)
            return {
                "actor_id": resolved.actor.actor_id,
                "firm_id": resolved.actor.firm_id,
                "method": resolved.authentication_method.value,
            }

        self.client = TestClient(app, client=("127.0.0.1", 50001))

    def _headers(self) -> dict[str, str]:
        return {"Origin": "tauri://localhost", "X-Desktop-Bootstrap": BOOTSTRAP}

    def test_one_time_bootstrap_issues_server_enrolled_identity(self) -> None:
        exchanged = self.client.post("/exchange", headers=self._headers())
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        self.assertNotIn(BOOTSTRAP, repr(self.authority))
        resolved = self.client.get(
            "/identity",
            headers={"Origin": "tauri://localhost", "Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["actor_id"], self.actor.actor_id)
        self.assertEqual(resolved.json()["firm_id"], self.actor.firm_id)
        self.assertEqual(resolved.json()["method"], AuthenticationMethod.OS_BOUND_LOCAL_SESSION.value)

    def test_bootstrap_cannot_be_reused(self) -> None:
        self.assertEqual(self.client.post("/exchange", headers=self._headers()).status_code, 200)
        with self.assertRaises(PersistentAuthenticationBlocked):
            self.client.post("/exchange", headers=self._headers())

    def test_wrong_origin_non_numeric_host_and_wrong_token_fail_closed(self) -> None:
        wrong_origin = TestClient(self.client.app, client=("127.0.0.1", 50001))
        with self.assertRaises(PersistentAuthenticationBlocked):
            wrong_origin.post(
                "/exchange",
                headers={"Origin": "http://malicious.local", "X-Desktop-Bootstrap": BOOTSTRAP},
            )
        named_host = TestClient(self.client.app, client=("localhost", 50001))
        with self.assertRaises(PersistentAuthenticationBlocked):
            named_host.post("/exchange", headers=self._headers())
        with self.assertRaises(PersistentAuthenticationBlocked):
            self.client.post(
                "/exchange",
                headers={"Origin": "tauri://localhost", "X-Desktop-Bootstrap": "x" * 64},
            )

    def test_revoke_invalidates_bearer(self) -> None:
        grant = self.authority.exchange(
            request=self._request("POST", "/exchange", self._headers()),
            bootstrap_token=BOOTSTRAP,
        )
        self.authority.revoke(session_id=grant.session_id)
        with self.assertRaises(PersistentAuthenticationBlocked):
            self.client.get(
                "/identity",
                headers={"Origin": "tauri://localhost", "Authorization": f"Bearer {ACCESS_TOKEN}"},
            )

    async def test_expired_bearer_is_removed_and_rejected(self) -> None:
        self.authority.exchange(
            request=self._request("POST", "/exchange", self._headers()),
            bootstrap_token=BOOTSTRAP,
        )
        self.now += timedelta(minutes=31)
        request = self._request(
            "GET",
            "/identity",
            {"Origin": "tauri://localhost", "Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        with self.assertRaisesRegex(PersistentAuthenticationBlocked, "expired"):
            await self.authority.resolve(request)
        with self.assertRaisesRegex(PersistentAuthenticationBlocked, "invalid"):
            await self.authority.resolve(request)

    def test_secrets_are_redacted_from_grant_repr(self) -> None:
        grant = self.authority.exchange(
            request=self._request("POST", "/exchange", self._headers()),
            bootstrap_token=BOOTSTRAP,
        )
        self.assertNotIn(ACCESS_TOKEN, repr(grant))
        self.assertIn("<redacted>", repr(grant))

    def test_bootstrap_and_session_lifetimes_have_hard_maximums(self) -> None:
        with self.assertRaisesRegex(PersistentAuthenticationBlocked, "bootstrap lifetime"):
            DesktopSessionAuthority(
                actor=self.actor,
                bootstrap_token=BOOTSTRAP,
                bootstrap_expires_at=self.now + timedelta(seconds=61),
                session_expires_at=self.now + timedelta(minutes=30),
                clock=lambda: self.now,
            )
        with self.assertRaisesRegex(PersistentAuthenticationBlocked, "session lifetime"):
            DesktopSessionAuthority(
                actor=self.actor,
                bootstrap_token=BOOTSTRAP,
                bootstrap_expires_at=self.now + timedelta(seconds=30),
                session_expires_at=self.now + timedelta(minutes=31),
                clock=lambda: self.now,
            )

    @staticmethod
    def _request(method: str, path: str, headers: dict[str, str]) -> Request:
        raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": raw_headers,
                "client": ("127.0.0.1", 50001),
                "scheme": "http",
                "server": ("127.0.0.1", 43127),
                "query_string": b"",
            }
        )


if __name__ == "__main__":
    unittest.main()
