from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4
import unittest

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_document_draft_delivery import (
    WebDocumentDraftDeliveryBlocked,
    WebDocumentDraftDeliveryService,
)
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_access import (
    ReviewableDraftAccessPurpose,
    ReviewableOfficeDraftArtifactLocator,
)


class _ReviewableStore:
    def __init__(self, *, actor: Actor, matter_id: str, pair_id: str, content: bytes) -> None:
        self.actor = actor
        self.matter_id = matter_id
        self.pair_id = pair_id
        self.content = content
        self.calls: list[dict[str, object]] = []

    def get_reviewable_office_draft_artifact_locator(self, **kwargs):
        self.calls.append(kwargs)
        content_hash = sha256(self.content).hexdigest()
        purpose = kwargs["purpose"]
        return ReviewableOfficeDraftArtifactLocator(
            firm_id=self.actor.firm_id,
            matter_id=self.matter_id,
            pair_id=self.pair_id,
            purpose=purpose,
            media_type=(
                "application/pdf"
                if purpose is ReviewableDraftAccessPurpose.REVIEW_PDF
                else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            object_key=f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.lca",
            artifact_sha256=content_hash,
            byte_size=len(self.content),
            pair_status="CANDIDATE",
        )


class _Objects:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def read_verified_review_artifact(self, object_key: str, expected_hash: str) -> bytes:
        self.calls.append((object_key, expected_hash))
        return self.content


def _identity(actor: Actor) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=actor,
        session_id=str(uuid4()),
        issuer="https://identity.example.test/oidc",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
    )


class WebDocumentDraftDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = Actor(str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER}))
        self.matter_id = str(uuid4())
        self.pair_id = str(uuid4())

    def test_delivers_hash_bound_pdf_and_editable_outputs(self) -> None:
        for purpose, content, expected_media_type, expected_name in (
            ("REVIEW_PDF", b"%PDF-1.7\nreview\n%%EOF", "application/pdf", "文书审阅候选.pdf"),
            (
                "DOWNLOAD_EDITABLE",
                b"PK\x03\x04reviewable-docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "文书草稿候选.docx",
            ),
        ):
            with self.subTest(purpose=purpose):
                store = _ReviewableStore(
                    actor=self.actor,
                    matter_id=self.matter_id,
                    pair_id=self.pair_id,
                    content=content,
                )
                objects = _Objects(content)
                service = WebDocumentDraftDeliveryService(
                    reviewable_store=store, object_store=objects
                )
                result = service.download(
                    identity=_identity(self.actor),
                    matter_id=self.matter_id,
                    pair_id=self.pair_id,
                    purpose=purpose,  # type: ignore[arg-type]
                )
                self.assertEqual(result.media_type, expected_media_type)
                self.assertEqual(result.file_name, expected_name)
                self.assertEqual(result.content, content)
                self.assertEqual(len(objects.calls), 1)
                self.assertNotIn("object_key", result.__dict__)

    def test_rejects_non_reviewer_before_private_object_read(self) -> None:
        content = b"%PDF-1.7\nreview\n%%EOF"
        store = _ReviewableStore(
            actor=self.actor,
            matter_id=self.matter_id,
            pair_id=self.pair_id,
            content=content,
        )
        objects = _Objects(content)
        service = WebDocumentDraftDeliveryService(reviewable_store=store, object_store=objects)
        assistant = Actor(str(uuid4()), self.actor.firm_id, frozenset({Role.ASSISTANT}))
        with self.assertRaises(WebDocumentDraftDeliveryBlocked):
            service.download(
                identity=_identity(assistant),
                matter_id=self.matter_id,
                pair_id=self.pair_id,
                purpose="REVIEW_PDF",
            )
        self.assertEqual(store.calls, [])
        self.assertEqual(objects.calls, [])

    def test_rejects_tampered_private_object_without_leaking_locator(self) -> None:
        expected = b"%PDF-1.7\nreview\n%%EOF"
        store = _ReviewableStore(
            actor=self.actor,
            matter_id=self.matter_id,
            pair_id=self.pair_id,
            content=expected,
        )
        objects = _Objects(b"%PDF-1.7\ntampered\n%%EOF")
        service = WebDocumentDraftDeliveryService(reviewable_store=store, object_store=objects)
        with self.assertRaises(WebDocumentDraftDeliveryBlocked) as raised:
            service.download(
                identity=_identity(self.actor),
                matter_id=self.matter_id,
                pair_id=self.pair_id,
                purpose="REVIEW_PDF",
            )
        self.assertNotIn(".lca", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
