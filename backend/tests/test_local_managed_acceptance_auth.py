from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import uuid4

from fastapi import Request

from case_api.local_managed_acceptance_auth import (
    LocalManagedAcceptanceSessionBootstrap,
)
from case_api.web_session import (
    StoredWebSession,
    WebSessionAuthority,
    WebSessionBlocked,
    WebSessionPolicy,
)
from case_kernel.models import Actor, Role


class _Store:
    def __init__(self) -> None:
        self.sessions: list[StoredWebSession] = []

    def create_session(self, *, session: StoredWebSession) -> None:
        self.sessions.append(session)

    def find_session_by_digest(self, *, session_token_sha256: bytes):
        return next(
            (
                session
                for session in self.sessions
                if session.session_token_sha256 == session_token_sha256
            ),
            None,
        )

    def revoke_session(self, *, session_id: str, revoked_at: datetime) -> bool:
        del revoked_at
        return any(session.session_id == session_id for session in self.sessions)


class _Directory:
    def __init__(self, actor: Actor) -> None:
        self.actor = actor

    def resolve_active_actor(self, *, actor_id: str, firm_id: str, issuer: str):
        del issuer
        if actor_id == self.actor.actor_id and firm_id == self.actor.firm_id:
            return self.actor
        return None


def _request(*, method: str = "GET", host: str = "workbench.127.0.0.1.nip.io") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/api/v1/session",
            "raw_path": b"/api/v1/session",
            "query_string": b"",
            "headers": [(b"host", host.encode("ascii"))],
            "client": ("127.0.0.1", 12345),
            "server": (host, 443),
        }
    )


class LocalManagedAcceptanceSessionBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.store = _Store()
        self.authority = WebSessionAuthority(
            store=self.store,
            actor_directory=_Directory(self.actor),
            policy=WebSessionPolicy(
                public_origin="https://workbench.127.0.0.1.nip.io"
            ),
            clock=lambda: datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
            token_factory=iter(("A" * 48, "B" * 48)).__next__,
        )

    def test_fixed_loopback_bootstrap_issues_a_real_persisted_web_session(self) -> None:
        bootstrap = LocalManagedAcceptanceSessionBootstrap(
            public_origin="https://workbench.127.0.0.1.nip.io",
            issuer="https://identity.127.0.0.1.nip.io/realms/lawcase-test",
            actor=self.actor,
            session_authority=self.authority,
            clock=lambda: datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        )

        identity, grant = bootstrap.issue(request=_request())

        self.assertEqual(identity.actor, self.actor)
        self.assertEqual(identity.session_id, grant.session_id)
        self.assertEqual(len(self.store.sessions), 1)
        self.assertEqual(self.store.sessions[0].session_id, grant.session_id)

    def test_production_origin_and_unsafe_write_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback origin"):
            LocalManagedAcceptanceSessionBootstrap(
                public_origin="https://workbench.lawfirm.cn",
                issuer="https://identity.127.0.0.1.nip.io/realms/lawcase-test",
                actor=self.actor,
                session_authority=self.authority,
            )

        bootstrap = LocalManagedAcceptanceSessionBootstrap(
            public_origin="https://workbench.127.0.0.1.nip.io",
            issuer="https://identity.127.0.0.1.nip.io/realms/lawcase-test",
            actor=self.actor,
            session_authority=self.authority,
        )
        with self.assertRaisesRegex(WebSessionBlocked, "safe request"):
            bootstrap.issue(request=_request(method="POST"))


if __name__ == "__main__":
    unittest.main()
