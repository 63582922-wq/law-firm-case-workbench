from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from hashlib import sha256
import unittest
from uuid import uuid4

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_case_agent_documents import WebCaseAgentDocumentReview
from case_api.web_case_agent_final_review import (
    WebCaseAgentFinalReviewReadiness,
    WebCaseAgentFinalReviewReadinessBlocked,
)
from case_kernel.case_agent_supervisor import ArtifactReceipt
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _artifact(kind: str, source: str) -> ArtifactReceipt:
    return ArtifactReceipt(
        artifact_id=_id(),
        artifact_kind=kind,
        content_hash=_hash(f"content:{kind}:{source}"),
        byte_size=100,
        source_input_hash=_hash(source),
        managed_derivative=True,
    )


class _ReviewPort:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.result: object | None = None

    def read_review(self, *, artifact_id: str, **_: object) -> object:
        self.calls.append(artifact_id)
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else _document_review(artifact_id)


def _document_review(artifact_id: str) -> WebCaseAgentDocumentReview:
    return WebCaseAgentDocumentReview(
        artifact_id=artifact_id, title="答辩状候选", deliverable_kind="DEFENCE_STATEMENT",
        review_artifact_id=artifact_id,
        deliverable_label="答辩状候选", output_format="DOCX", review_notice="待律师复核",
        version_status="CURRENT", revision_number=1, template_version="1.0.0",
        installed_template_version="1.0.0", can_request_revision=False,
        request_status=None, request_id=None, download_ready=True,
        review_pdf_page_count=1, total_item_count=1, displayed_item_count=1,
        preview_truncated=False,
    )


class WebCaseAgentFinalReviewReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime.now(timezone.utc)
        self.identity = ServerIdentityContext(
            actor=Actor(_id(), _id(), frozenset({Role.LEAD_LAWYER})),
            session_id=_id(),
            issuer="https://identity.example.cn",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        self.artifact_review = _ReviewPort()
        self.document_review = _ReviewPort()
        self.readiness = WebCaseAgentFinalReviewReadiness(
            artifact_review=self.artifact_review,
            document_review=self.document_review,
        )

    def assert_ready(self, artifacts: tuple[ArtifactReceipt, ...]) -> None:
        self.readiness.assert_ready(
            identity=self.identity,
            matter_id=_id(),
            run_id=_id(),
            artifacts=artifacts,
        )

    def test_reopens_generic_artifacts_and_each_complete_document_package(self) -> None:
        generic = _artifact("LAWYER_DECISION_PACKAGE_CANDIDATE", "analysis")
        document = tuple(
            _artifact(kind, "memo")
            for kind in (
                "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
                "REVIEWABLE_DOCUMENT_EDITABLE",
                "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
            )
        )

        self.assert_ready((generic, *document))

        self.assertEqual(self.artifact_review.calls, [generic.artifact_id])
        self.assertEqual(self.document_review.calls, [document[0].artifact_id])

    def test_incomplete_document_package_cannot_be_finally_reviewed(self) -> None:
        incomplete = (
            _artifact("REVIEWABLE_DOCUMENT_CANDIDATE_JSON", "memo"),
            _artifact("REVIEWABLE_DOCUMENT_EDITABLE", "memo"),
        )

        with self.assertRaisesRegex(
            WebCaseAgentFinalReviewReadinessBlocked, "缺少候选正文"
        ):
            self.assert_ready(incomplete)

        self.assertEqual(self.document_review.calls, [])

    def test_status_only_or_mismatched_document_response_cannot_pass_final_review(self) -> None:
        document = tuple(_artifact(kind, "memo") for kind in (
            "REVIEWABLE_DOCUMENT_CANDIDATE_JSON", "REVIEWABLE_DOCUMENT_EDITABLE",
            "REVIEWABLE_DOCUMENT_PDF_PREVIEW"))
        valid = _document_review(document[0].artifact_id)
        responses = [object()] + [replace(valid, version_status=status, download_ready=False)
            for status in ("GENERATING", "UPDATE_REQUIRED", "FAILED", "UNKNOWN")]
        responses += [replace(valid, download_ready=False), replace(valid, artifact_id=_id()),
                      replace(valid, installed_template_version="2.0.0"),
                      replace(valid, review_pdf_page_count=0), replace(valid, review_pdf_page_count=True)]
        for response in responses:
            with self.subTest(response=response):
                self.document_review.result = response
                with self.assertRaisesRegex(WebCaseAgentFinalReviewReadinessBlocked, "当前复核稿"):
                    self.assert_ready(document)
        self.document_review.result = valid
        self.assert_ready(document)

    def test_template_or_source_drift_is_normalized_to_one_safe_blocker(self) -> None:
        self.document_review.error = RuntimeError("installed template differs")
        document = tuple(
            _artifact(kind, "memo")
            for kind in (
                "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
                "REVIEWABLE_DOCUMENT_EDITABLE",
                "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
            )
        )

        with self.assertRaisesRegex(
            WebCaseAgentFinalReviewReadinessBlocked, "模板已更新"
        ):
            self.assert_ready(document)

    def test_empty_verified_output_cannot_be_finally_reviewed(self) -> None:
        with self.assertRaisesRegex(
            WebCaseAgentFinalReviewReadinessBlocked, "没有可供律师终审"
        ):
            self.assert_ready(())

    def test_final_review_compares_exact_lawyer_seen_document_versions(self) -> None:
        document = tuple(_artifact(kind, "memo") for kind in (
            "REVIEWABLE_DOCUMENT_CANDIDATE_JSON", "REVIEWABLE_DOCUMENT_EDITABLE",
            "REVIEWABLE_DOCUMENT_PDF_PREVIEW"))
        candidate = document[0].artifact_id
        self.document_review.result = replace(_document_review(candidate), review_version="a" * 64)
        args = dict(identity=self.identity, matter_id=_id(), run_id=_id(), artifacts=document)
        self.readiness.assert_ready(**args, expected_document_versions=((candidate, "a" * 64),))
        for bindings in ((), ((candidate, "b" * 64),), ((_id(), "a" * 64),),
                         ((candidate, "a" * 64), (candidate, "a" * 64)), ((candidate, None),)):
            with self.subTest(bindings=bindings):
                with self.assertRaises(WebCaseAgentFinalReviewReadinessBlocked):
                    self.readiness.assert_ready(**args, expected_document_versions=bindings)


if __name__ == "__main__":
    unittest.main()
