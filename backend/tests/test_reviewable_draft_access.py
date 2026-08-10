from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_kernel.local_access_grants import LocalSessionProof
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_access import (
    ReviewableDraftAccessBlocked,
    ReviewableDraftAccessPurpose,
    ReviewableOfficeDraftAccessBroker,
    ReviewableOfficeDraftArtifactLocator,
)


class ReviewableDraftAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.matter_id = str(uuid4())
        self.session = LocalSessionProof(
            session_id=str(uuid4()),
            authentication_method="OS_BOUND_LOCAL_SESSION",
            authenticated_at=self.now - timedelta(minutes=1),
            expires_at=self.now + timedelta(minutes=10),
        )

    def test_review_pdf_and_editable_office_are_one_use_and_loopback_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "case"; root.mkdir()
            store = LocalEncryptedArtifactStore(Path(temporary) / "managed", key_id="test", encryption_key=b"k" * 32)
            pdf = b"%PDF-1.4\ntrailer <<>>\n%%EOF\n"
            editable = b"PK\x03\x04editable-office"
            pdf_object = store.put_bytes(pdf, expected_sha256=sha256(pdf).hexdigest(), case_root=root)
            editable_object = store.put_bytes(editable, expected_sha256=sha256(editable).hexdigest(), case_root=root)
            broker = ReviewableOfficeDraftAccessBroker()
            pair_id = str(uuid4())
            preview = broker.issue(
                locator=ReviewableOfficeDraftArtifactLocator(
                    self.actor.firm_id, self.matter_id, pair_id, ReviewableDraftAccessPurpose.REVIEW_PDF,
                    "application/pdf", pdf_object.object_key, pdf_object.plaintext_sha256, pdf_object.plaintext_bytes, "CANDIDATE",
                ), actor=self.actor, session=self.session, now=self.now,
            )
            delivery = broker.deliver(
                access_token=preview.access_token, actor=self.actor, matter_id=self.matter_id, pair_id=pair_id,
                session=self.session, client_ip="127.0.0.1", artifact_store=store, now=self.now,
            )
            self.assertEqual(delivery.content, pdf)
            self.assertEqual(delivery.file_name, "文书审阅稿.pdf")
            with self.assertRaisesRegex(ReviewableDraftAccessBlocked, "already used"):
                broker.deliver(
                    access_token=preview.access_token, actor=self.actor, matter_id=self.matter_id, pair_id=pair_id,
                    session=self.session, client_ip="127.0.0.1", artifact_store=store, now=self.now,
                )
            editable_grant = broker.issue(
                locator=ReviewableOfficeDraftArtifactLocator(
                    self.actor.firm_id, self.matter_id, pair_id, ReviewableDraftAccessPurpose.DOWNLOAD_EDITABLE,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", editable_object.object_key,
                    editable_object.plaintext_sha256, editable_object.plaintext_bytes, "CANDIDATE",
                ), actor=self.actor, session=self.session, now=self.now,
            )
            with self.assertRaisesRegex(ReviewableDraftAccessBlocked, "local loopback"):
                broker.deliver(
                    access_token=editable_grant.access_token, actor=self.actor, matter_id=self.matter_id, pair_id=pair_id,
                    session=self.session, client_ip="203.0.113.9", artifact_store=store, now=self.now,
                )
