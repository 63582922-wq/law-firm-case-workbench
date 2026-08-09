from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from reportlab.pdfgen import canvas

from case_kernel.local_access_grants import (
    LocalFolderGrantRegistry,
    LocalSessionProof,
)
from case_kernel.local_case_folder import root_fingerprint
from case_kernel.models import Actor, Role
from case_kernel.original_page_access import (
    OriginalPageAccessBlocked,
    OriginalPageAccessBroker,
    OriginalPageLocator,
)


class OriginalPageAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="original-page-access-test-")
        self.root = Path(self.temporary.name) / "case-folder"
        self.root.mkdir()
        self.source = self.root / "synthetic-source.pdf"
        document = canvas.Canvas(str(self.source), pagesize=(300, 400))
        document.drawString(30, 350, "SYNTHETIC PAGE ONE")
        document.showPage()
        document.drawString(30, 350, "SYNTHETIC PAGE TWO")
        document.showPage()
        document.save()
        self.actor = Actor(
            actor_id=str(uuid4()),
            firm_id=str(uuid4()),
            roles=frozenset({Role.LEAD_LAWYER}),
        )
        self.matter_id = str(uuid4())
        self.page_id = str(uuid4())
        self.now = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=10),
        )
        self.folder_grants = LocalFolderGrantRegistry(max_ttl=timedelta(minutes=5))
        self.folder_grant = self.folder_grants.issue_read_grant(
            selected_root=self.root,
            confirmed_root_fingerprint=root_fingerprint(self.root),
            actor=self.actor,
            matter_id=self.matter_id,
            session=self.session,
            now=self.now,
        )
        self.locator = OriginalPageLocator(
            firm_id=self.actor.firm_id,
            matter_id=self.matter_id,
            evidence_page_id=self.page_id,
            evidence_file_id=str(uuid4()),
            original_label=self.source.name,
            original_file_sha256=sha256(self.source.read_bytes()).hexdigest(),
            byte_size=self.source.stat().st_size,
            media_type="application/pdf",
            page_count=2,
            page_number=2,
        )
        self.broker = OriginalPageAccessBroker(folder_grants=self.folder_grants)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_one_registered_page_is_rendered_to_one_use_png_without_source_path(self) -> None:
        issued = self.broker.issue(
            locator=self.locator,
            folder_grant_id=self.folder_grant.grant_id,
            actor=self.actor,
            session=self.session,
            now=self.now,
        )
        self.assertNotIn(str(self.root), repr(issued))
        delivery = self.broker.deliver(
            access_token=issued.access_token,
            actor=self.actor,
            matter_id=self.matter_id,
            evidence_page_id=self.page_id,
            session=self.session,
            client_ip="127.0.0.1",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(delivery.media_type, "image/png")
        self.assertTrue(delivery.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(delivery.width, 0)
        self.assertGreater(delivery.height, 0)
        self.assertEqual(sha256(delivery.content).hexdigest(), delivery.content_sha256)
        self.assertNotIn(str(self.root), repr(delivery))
        with self.assertRaisesRegex(OriginalPageAccessBlocked, "already used"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                evidence_page_id=self.page_id,
                session=self.session,
                client_ip="127.0.0.1",
                now=self.now + timedelta(seconds=2),
            )

    def test_non_loopback_wrong_page_and_changed_source_fail_closed(self) -> None:
        issued = self.broker.issue(
            locator=self.locator,
            folder_grant_id=self.folder_grant.grant_id,
            actor=self.actor,
            session=self.session,
            now=self.now,
        )
        with self.assertRaisesRegex(OriginalPageAccessBlocked, "local device"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                evidence_page_id=self.page_id,
                session=self.session,
                client_ip="192.0.2.1",
                now=self.now,
            )
        with self.assertRaisesRegex(OriginalPageAccessBlocked, "outside"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                evidence_page_id=str(uuid4()),
                session=self.session,
                client_ip="127.0.0.1",
                now=self.now,
            )
        self.source.write_bytes(b"changed after authorization")
        with self.assertRaisesRegex(PermissionError, "changed"):
            self.broker.deliver(
                access_token=issued.access_token,
                actor=self.actor,
                matter_id=self.matter_id,
                evidence_page_id=self.page_id,
                session=self.session,
                client_ip="127.0.0.1",
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
