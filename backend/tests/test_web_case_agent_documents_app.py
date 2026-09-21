from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from fastapi import Request
from fastapi.testclient import TestClient

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_app import WebApiDependencies, WebApiSettings, create_web_app
from case_api.web_case_agent_documents import (
    WebCaseAgentDocumentDownload,
    WebCaseAgentDocumentParagraph,
    WebCaseAgentDocumentReview,
    WebCaseAgentDocumentSection,
    WebCaseAgentDocumentSource,
)
from case_api.web_session import CookieDirective
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


class _Oidc:
    public_origin = "https://workbench.example.test"

    @staticmethod
    def begin_authorization():
        raise AssertionError("not used")

    @staticmethod
    def complete_callback(*, parameters):
        del parameters
        raise AssertionError("not used")


class _Session:
    def __init__(self, identity) -> None:
        self.identity = identity

    async def resolve(self, request: Request):
        del request
        return self.identity

    @staticmethod
    def revoke(*, session_id: str) -> None:
        del session_id

    @staticmethod
    def clear_cookies():
        return (
            CookieDirective(name="__Host-lawcase_session", value="", max_age=0),
            CookieDirective(
                name="__Host-lawcase_csrf", value="", httponly=False, max_age=0
            ),
        )


class _Matters:
    @staticmethod
    def create(**kwargs):
        del kwargs
        raise AssertionError("not used")

    @staticmethod
    def list_accessible(*, actor):
        del actor
        return []


class _CaseAgentControl:
    def __getattr__(self, _name):
        return lambda **_kwargs: None


class _Documents:
    def __init__(self, review) -> None:
        self.review = review
        self.read_calls = []
        self.download_calls = []
        self.revision_calls = []

    def read_review(self, **kwargs):
        self.read_calls.append(kwargs)
        return self.review

    def download(self, **kwargs):
        self.download_calls.append(kwargs)
        if kwargs["file_role"] == "pdf-preview":
            return WebCaseAgentDocumentDownload(
                file_name="周雅丽案答辩状候选.pdf",
                ascii_file_name="agent-document-preview.pdf",
                media_type="application/pdf",
                disposition="inline",
                content=b"%PDF-1.7 verified",
            )
        return WebCaseAgentDocumentDownload(
            file_name="周雅丽案答辩状候选.docx",
            ascii_file_name="agent-document-candidate.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            disposition="attachment",
            content=b"PK\x03\x04verified",
        )

    def request_revision(self, **kwargs):
        self.revision_calls.append(kwargs)
        return self.review


class WebCaseAgentDocumentAppTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime.now(timezone.utc)
        self.case_id = _id()
        self.run_id = _id()
        self.artifact_id = _id()
        source = WebCaseAgentDocumentSource(
            source_ref=f"fact:{_id()}",
            source_kind="CONFIRMED_FACT",
            label="已确认事实：利息范围",
        )
        review = WebCaseAgentDocumentReview(
            artifact_id=self.artifact_id,
            title="周雅丽案答辩状候选",
            deliverable_kind="DEFENCE_STATEMENT",
            deliverable_label="民事答辩状候选",
            output_format="DOCX",
            review_notice="这是待律师复核的可编辑候选，不是可直接提交法院的文书。",
            version_status="CURRENT",
            revision_number=1,
            template_version="1.0.0",
            installed_template_version="1.0.0",
            can_request_revision=False,
            request_status=None,
            request_id=None,
            download_ready=True,
            review_pdf_page_count=2,
            total_item_count=1,
            displayed_item_count=1,
            preview_truncated=False,
            sections=(
                WebCaseAgentDocumentSection(
                    section_id="section-1",
                    heading="答辩意见",
                    paragraphs=(
                        WebCaseAgentDocumentParagraph(
                            paragraph_id="section-1-paragraph-1",
                            text="利息应按依法确认的期间和上限逐段核算。",
                            sources=(source,),
                        ),
                    ),
                ),
            ),
        )
        self.documents = _Documents(review)
        identity = ServerIdentityContext(
            actor=Actor(
                actor_id=_id(),
                firm_id=_id(),
                roles=frozenset({Role.LEAD_LAWYER}),
            ),
            session_id=_id(),
            issuer="https://identity.lawfirm.test",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
        )
        dependencies = WebApiDependencies(
            settings=WebApiSettings(public_origin="https://workbench.example.test"),
            oidc_login=_Oidc(),
            session_authority=_Session(identity),
            matter_store=_Matters(),
            case_agent_control_service=_CaseAgentControl(),
            case_agent_document_review_service=self.documents,
            case_agent_document_runtime_ready=lambda _firm_id: True,
        )
        self.client = TestClient(
            create_web_app(dependencies),
            base_url="https://workbench.example.test",
        )

    def test_safe_review_projection_contains_no_internal_lineage_fields(self) -> None:
        response = self.client.get(
            f"/api/v1/cases/{self.case_id}/case-agent-runs/{self.run_id}/artifacts/{self.artifact_id}/document-review"
        )
        self.assertEqual(response.status_code, 200)
        review = response.json()["review"]
        self.assertEqual(review["deliverable_label"], "民事答辩状候选")
        self.assertEqual(review["sections"][0]["paragraphs"][0]["sources"][0]["label"], "已确认事实：利息范围")
        body = response.text
        for forbidden in ("object_key", "content_sha256", "binding_hash", "provider", "prompt"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(len(self.documents.read_calls), 1)

    def test_pdf_download_is_same_origin_no_store_nosniff_and_safe_named(self) -> None:
        response = self.client.get(
            f"/api/v1/cases/{self.case_id}/case-agent-runs/{self.run_id}/artifacts/{self.artifact_id}/document-files/pdf-preview"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.7 verified")
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")
        disposition = response.headers["content-disposition"]
        self.assertIn('inline; filename="agent-document-preview.pdf"', disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertEqual(len(self.documents.download_calls), 1)

    def test_revision_route_is_idempotent_and_version_bound(self) -> None:
        response = self.client.post(
            f"/api/v1/cases/{self.case_id}/case-agent-runs/{self.run_id}/artifacts/{self.artifact_id}/document-revisions",
            headers={"Idempotency-Key": "document-revision-route-0001"},
            json={"expected_revision_number": 1},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            self.documents.revision_calls[0]["expected_revision_number"], 1
        )
        self.assertEqual(
            self.documents.revision_calls[0]["idempotency_key"],
            "document-revision-route-0001",
        )


if __name__ == "__main__":
    unittest.main()
