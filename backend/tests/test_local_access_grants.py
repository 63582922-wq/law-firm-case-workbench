from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_kernel.local_access_grants import (
    LocalFolderAccessBlocked,
    LocalFolderGrantRegistry,
    LocalSessionProof,
    scan_authorized_case_folder,
)
from case_kernel.local_case_folder import root_fingerprint
from case_kernel.models import Actor, Role


class LocalFolderGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="local-folder-grant-test-")
        self.root = Path(self.temporary.name) / "selected-case"
        self.root.mkdir()
        (self.root / "synthetic.pdf").write_bytes(b"synthetic evidence")
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.matter_id = str(uuid4())
        self.now = datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=20),
        )
        self.registry = LocalFolderGrantRegistry(max_ttl=timedelta(minutes=5))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def issue(self):
        return self.registry.issue_read_grant(
            selected_root=self.root,
            confirmed_root_fingerprint=root_fingerprint(self.root),
            actor=self.actor,
            matter_id=self.matter_id,
            session=self.session,
            now=self.now,
        )

    def test_os_bound_case_scoped_grant_allows_only_the_confirmed_folder(self) -> None:
        handle = self.issue()
        manifest = scan_authorized_case_folder(
            self.root,
            registry=self.registry,
            grant_id=handle.grant_id,
            actor=self.actor,
            matter_id=self.matter_id,
            session=self.session,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(manifest.total_files, 1)
        self.assertEqual(handle.expires_at, self.now + timedelta(minutes=5))
        self.assertNotIn(str(self.root), repr(handle))

    def test_grant_cannot_cross_actor_matter_session_or_folder(self) -> None:
        handle = self.issue()
        other_root = Path(self.temporary.name) / "other-case"
        other_root.mkdir()
        cases = (
            {"matter_id": str(uuid4())},
            {
                "actor": Actor(
                    actor_id=str(uuid4()),
                    firm_id=self.actor.firm_id,
                    roles=self.actor.roles,
                )
            },
            {
                "session": LocalSessionProof(
                    session_id=str(uuid4()),
                    authentication_method="OS_BOUND_LOCAL_SESSION",
                    authenticated_at=self.session.authenticated_at,
                    expires_at=self.session.expires_at,
                )
            },
            {"selected_root": other_root},
        )
        for overrides in cases:
            kwargs = {
                "grant_id": handle.grant_id,
                "selected_root": self.root,
                "actor": self.actor,
                "matter_id": self.matter_id,
                "session": self.session,
                "now": self.now,
                **overrides,
            }
            with self.subTest(overrides=overrides), self.assertRaises(LocalFolderAccessBlocked):
                self.registry.validate_read(**kwargs)

    def test_expiry_revoke_non_os_session_and_admin_fail_closed(self) -> None:
        handle = self.issue()
        with self.assertRaisesRegex(LocalFolderAccessBlocked, "missing or expired"):
            self.registry.validate_read(
                grant_id=handle.grant_id,
                selected_root=self.root,
                actor=self.actor,
                matter_id=self.matter_id,
                session=self.session,
                now=self.now + timedelta(minutes=6),
            )
        handle = self.issue()
        self.assertEqual(self.registry.revoke_session(self.session.session_id), 1)
        with self.assertRaisesRegex(LocalFolderAccessBlocked, "missing or expired"):
            self.registry.validate_read(
                grant_id=handle.grant_id,
                selected_root=self.root,
                actor=self.actor,
                matter_id=self.matter_id,
                session=self.session,
                now=self.now,
            )
        non_os = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OIDC_MFA",
            authenticated_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        with self.assertRaisesRegex(LocalFolderAccessBlocked, "OS-bound"):
            self.registry.issue_read_grant(
                selected_root=self.root,
                confirmed_root_fingerprint=root_fingerprint(self.root),
                actor=self.actor,
                matter_id=self.matter_id,
                session=non_os,
                now=self.now,
            )
        admin = Actor(
            actor_id=str(uuid4()),
            firm_id=self.actor.firm_id,
            roles=frozenset({Role.FIRM_ADMIN}),
        )
        with self.assertRaisesRegex(LocalFolderAccessBlocked, "cannot read"):
            self.registry.issue_read_grant(
                selected_root=self.root,
                confirmed_root_fingerprint=root_fingerprint(self.root),
                actor=admin,
                matter_id=self.matter_id,
                session=self.session,
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
