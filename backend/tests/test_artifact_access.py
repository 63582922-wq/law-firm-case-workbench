from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_kernel.artifact_access import (
    ArtifactAccessBlocked,
    ArtifactAccessPurpose,
    EphemeralArtifactAccessBroker,
    VerifiedDerivativeLocator,
)
from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role


class ArtifactAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="artifact-access-test-")
        root = Path(self.temporary.name)
        self.case_root = root / "case"
        self.case_root.mkdir()
        self.store = LocalEncryptedArtifactStore(
            root / "managed",
            key_id="synthetic-test-key-v1",
            encryption_key=b"k" * 32,
        )
        self.content = b"%PDF-1.4\nSYNTHETIC VERIFIED PDF\n%%EOF\n"
        source = root / "worker-output.pdf"
        source.write_bytes(self.content)
        artifact_hash = sha256(self.content).hexdigest()
        stored = self.store.put_file(
            source,
            expected_sha256=artifact_hash,
            case_root=self.case_root,
        )
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.matter_id = str(uuid4())
        self.locator = VerifiedDerivativeLocator(
            firm_id=self.actor.firm_id,
            matter_id=self.matter_id,
            derivative_id=str(uuid4()),
            manifest_id=str(uuid4()),
            artifact_type="ANNOTATED_RELATED_PAGES_PDF",
            object_key=stored.object_key,
            artifact_sha256=artifact_hash,
            page_count=1,
            status="VERIFIED",
        )
        self.now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=10),
        )
        self.broker = EphemeralArtifactAccessBroker()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def issue(self):
        return self.broker.issue(
            locator=self.locator,
            purpose=ArtifactAccessPurpose.INLINE_PREVIEW,
            actor=self.actor,
            session=self.session,
            now=self.now,
        )

    def test_verified_pdf_is_delivered_once_only_over_loopback(self) -> None:
        issued = self.issue()
        self.assertNotIn(issued.access_token, repr(issued))
        delivery = self.broker.deliver(
            access_token=issued.access_token,
            actor=self.actor,
            matter_id=self.matter_id,
            derivative_id=self.locator.derivative_id,
            session=self.session,
            client_ip="127.0.0.1",
            artifact_store=self.store,
            now=self.now + timedelta(seconds=10),
        )
        self.assertEqual(delivery.content, self.content)
        self.assertEqual(delivery.file_name, "related-pages-red-box.pdf")
        self.assertNotIn("SYNTHETIC VERIFIED PDF", repr(delivery))
        with self.assertRaisesRegex(ArtifactAccessBlocked, "already used"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                derivative_id=self.locator.derivative_id,
                session=self.session,
                client_ip="::1",
                artifact_store=self.store,
                now=self.now + timedelta(seconds=11),
            )

    def test_remote_client_expiry_cross_scope_and_unverified_artifact_fail_closed(self) -> None:
        issued = self.issue()
        with self.assertRaisesRegex(ArtifactAccessBlocked, "loopback"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                derivative_id=self.locator.derivative_id,
                session=self.session,
                client_ip="192.0.2.10",
                artifact_store=self.store,
                now=self.now,
            )
        with self.assertRaisesRegex(ArtifactAccessBlocked, "outside the authenticated scope"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=str(uuid4()),
                derivative_id=self.locator.derivative_id,
                session=self.session,
                client_ip="127.0.0.1",
                artifact_store=self.store,
                now=self.now,
            )
        with self.assertRaisesRegex(ArtifactAccessBlocked, "missing, expired"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                derivative_id=self.locator.derivative_id,
                session=self.session,
                client_ip="127.0.0.1",
                artifact_store=self.store,
                now=self.now + timedelta(minutes=2),
            )
        unverified = VerifiedDerivativeLocator(**{**self.locator.__dict__, "status": "CANDIDATE"})
        with self.assertRaisesRegex(ArtifactAccessBlocked, "currently verified"):
            self.broker.issue(
                locator=unverified,
                purpose=ArtifactAccessPurpose.DOWNLOAD,
                actor=self.actor,
                session=self.session,
                now=self.now,
            )

    def test_session_revocation_and_non_os_or_admin_access_fail_closed(self) -> None:
        issued = self.issue()
        self.assertEqual(self.broker.revoke_session(self.session.session_id), 1)
        with self.assertRaisesRegex(ArtifactAccessBlocked, "missing, expired"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                derivative_id=self.locator.derivative_id,
                session=self.session,
                client_ip="127.0.0.1",
                artifact_store=self.store,
                now=self.now,
            )
        oidc_session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OIDC_MFA",
            authenticated_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        with self.assertRaisesRegex(ArtifactAccessBlocked, "OS-bound"):
            self.broker.issue(
                locator=self.locator,
                purpose=ArtifactAccessPurpose.DOWNLOAD,
                actor=self.actor,
                session=oidc_session,
                now=self.now,
            )
        admin = Actor(
            actor_id=str(uuid4()),
            firm_id=self.actor.firm_id,
            roles=frozenset({Role.FIRM_ADMIN}),
        )
        with self.assertRaisesRegex(ArtifactAccessBlocked, "cannot preview"):
            self.broker.issue(
                locator=self.locator,
                purpose=ArtifactAccessPurpose.DOWNLOAD,
                actor=admin,
                session=self.session,
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
