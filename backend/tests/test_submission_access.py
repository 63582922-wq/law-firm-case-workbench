from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest
from zipfile import ZIP_STORED, ZipFile

from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.submission_access import (
    SubmissionAccessBlocked,
    SubmissionExportAccessBroker,
    VerifiedSubmissionExportLocator,
)


class SubmissionAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
        self.firm_id = str(uuid4())
        self.matter_id = str(uuid4())
        self.export_id = str(uuid4())
        self.bundle_id = str(uuid4())
        self.actor = Actor(str(uuid4()), self.firm_id, frozenset({Role.LEAD_LAWYER}))
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=self.now - timedelta(minutes=2),
            expires_at=self.now + timedelta(minutes=30),
        )

    def test_verified_export_download_is_loopback_bound_and_one_use(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_root = root / "case"
            case_root.mkdir()
            source = root / "court.zip"
            with ZipFile(source, "w", ZIP_STORED) as archive:
                archive.writestr("01_民事答辩状.pdf", b"%PDF-1.4\n%%EOF\n")
            content = source.read_bytes()
            digest = sha256(content).hexdigest()
            store = LocalEncryptedArtifactStore(
                root / "managed", key_id="test-key-v1", encryption_key=b"k" * 32
            )
            stored = store.put_file(source, expected_sha256=digest, case_root=case_root)
            locator = VerifiedSubmissionExportLocator(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                export_id=self.export_id,
                bundle_id=self.bundle_id,
                object_key=stored.object_key,
                court_zip_sha256=digest,
                court_zip_bytes=len(content),
                lifecycle="EXPORTED",
                validity="VALID",
            )
            broker = SubmissionExportAccessBroker()
            issued = broker.issue(
                locator=locator, actor=self.actor, session=self.session, now=self.now
            )
            delivered = broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                export_id=self.export_id,
                session=self.session,
                client_ip="127.0.0.1",
                artifact_store=store,
                now=self.now,
            )
            self.assertEqual(delivered.content, content)
            self.assertEqual(delivered.file_name, "法院提交材料.zip")
            with self.assertRaisesRegex(SubmissionAccessBlocked, "already used"):
                broker.deliver(
                    access_token=issued.access_token,
                    actor=self.actor,
                    matter_id=self.matter_id,
                    export_id=self.export_id,
                    session=self.session,
                    client_ip="127.0.0.1",
                    artifact_store=store,
                    now=self.now,
                )

    def test_non_loopback_wrong_role_stale_and_cross_scope_are_blocked(self) -> None:
        digest = "a" * 64
        locator = VerifiedSubmissionExportLocator(
            firm_id=self.firm_id,
            matter_id=self.matter_id,
            export_id=self.export_id,
            bundle_id=self.bundle_id,
            object_key=f"aa/aa/{digest}.lca",
            court_zip_sha256=digest,
            court_zip_bytes=100,
            lifecycle="EXPORTED",
            validity="VALID",
        )
        broker = SubmissionExportAccessBroker()
        assistant = Actor(str(uuid4()), self.firm_id, frozenset({Role.ASSISTANT}))
        with self.assertRaisesRegex(SubmissionAccessBlocked, "cannot download"):
            broker.issue(locator=locator, actor=assistant, session=self.session, now=self.now)
        stale = VerifiedSubmissionExportLocator(**{**locator.__dict__, "validity": "STALE"})
        with self.assertRaisesRegex(SubmissionAccessBlocked, "current valid"):
            broker.issue(locator=stale, actor=self.actor, session=self.session, now=self.now)
        issued = broker.issue(locator=locator, actor=self.actor, session=self.session, now=self.now)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalEncryptedArtifactStore(
                root / "managed", key_id="test-key-v1", encryption_key=b"k" * 32
            )
            with self.assertRaisesRegex(SubmissionAccessBlocked, "loopback"):
                broker.deliver(
                    access_token=issued.access_token,
                    actor=self.actor,
                    matter_id=self.matter_id,
                    export_id=self.export_id,
                    session=self.session,
                    client_ip="203.0.113.10",
                    artifact_store=store,
                    now=self.now,
                )
            with self.assertRaisesRegex(SubmissionAccessBlocked, "outside"):
                broker.deliver(
                    access_token=issued.access_token,
                    actor=self.actor,
                    matter_id=str(uuid4()),
                    export_id=self.export_id,
                    session=self.session,
                    client_ip="127.0.0.1",
                    artifact_store=store,
                    now=self.now,
                )


if __name__ == "__main__":
    unittest.main()
