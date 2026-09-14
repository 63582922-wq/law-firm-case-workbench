from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from uuid import uuid4
from unittest.mock import Mock

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_case_agent_documents import (
    PostgresWebCaseAgentDocumentReviewService,
    WebCaseAgentDocumentReviewBlocked,
)
from case_kernel.case_agent_document_delivery import ReviewableDocumentFormat
from case_kernel.case_agent_document_delivery_postgres import (
    AuthorizedDocumentSourceBinding,
    ReviewableDocumentArtifactRead,
    ReviewableDocumentPackageRead,
)
from case_kernel.case_agent_document_revisions import DocumentRevisionState, RegisteredContentResult
from case_kernel.case_agent_supervisor import ArtifactReceipt
from case_kernel.case_agent_verifier import (
    ArtifactFormatReceipt,
    ArtifactLineageReceipt,
    ManagedArtifactRead,
)
from case_kernel.models import Actor, Role


def _id() -> str:
    return str(uuid4())


def _hash(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _Cursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, parameters))
        if "FROM case_agent_reviewable_document_packages" in normalized:
            return _Cursor(self.row)
        return _Cursor()


class _PackageAccess:
    def __init__(self, package, error: Exception | None = None) -> None:
        self.package = package
        self.error = error
        self.calls = []

    def read_package(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.package


class _RevisionStore:
    def __init__(
        self,
        state: DocumentRevisionState,
        request_state: DocumentRevisionState | None = None,
    ) -> None:
        self.state = state
        self.request_state = request_state or state
        self.read_calls = []
        self.request_calls = []

    def read_state(self, **kwargs):
        self.read_calls.append(kwargs)
        return self.state

    def request_revision(self, **kwargs):
        self.request_calls.append(kwargs)
        return self.request_state


class WebCaseAgentDocumentReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime.now(timezone.utc)
        self.firm_id = _id()
        self.actor_id = _id()
        self.matter_id = _id()
        self.run_id = _id()
        self.graph_id = _id()
        self.task_id = _id()
        self.package_id = _id()
        self.work_plan_item_id = _id()
        self.candidate_id = _id()
        self.editable_id = _id()
        self.pdf_id = _id()
        self.source_ref = f"fact:{_id()}"
        self.task_input_hash = _hash("task-input")
        self.candidate_content = _canonical(
            {
                "binding": {
                    "binding_hash": _hash("binding"),
                    "deliverable_kind": "DEFENCE_STATEMENT",
                    "output_format": "DOCX",
                    "source_set_hash": _hash("sources"),
                    "task_input_hash": self.task_input_hash,
                    "template_hash": _hash("template"),
                    "template_id": "civil-defence-statement",
                    "template_version": "1.0.0",
                    "work_plan_item_id": self.work_plan_item_id,
                },
                "court_ready": False,
                "formal_fact": False,
                "formal_legal_conclusion": False,
                "review_status": "NEEDS_LAWYER_REVIEW",
                "schema_version": "case-agent-reviewable-docx-candidate-v1",
                "sections": [
                    {
                        "heading": "答辩意见",
                        "paragraphs": [
                            {
                                "source_refs": [self.source_ref],
                                "text": "原告主张的利息应按依法确认的期间和上限逐段核算。",
                            }
                        ],
                    }
                ],
                "title": "周雅丽案：答辩状候选/请复核",
            }
        )
        self.editable_content = b"PK\x03\x04verified-docx"
        self.pdf_content = b"%PDF-1.7\nverified-preview"
        candidate = ReviewableDocumentArtifactRead(
            artifact_id=self.candidate_id,
            artifact_kind="REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
            media_type="application/json",
            content_sha256=sha256(self.candidate_content).hexdigest(),
            byte_size=len(self.candidate_content),
            content=self.candidate_content,
        )
        editable = ReviewableDocumentArtifactRead(
            artifact_id=self.editable_id,
            artifact_kind="REVIEWABLE_DOCUMENT_EDITABLE",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content_sha256=sha256(self.editable_content).hexdigest(),
            byte_size=len(self.editable_content),
            content=self.editable_content,
        )
        pdf = ReviewableDocumentArtifactRead(
            artifact_id=self.pdf_id,
            artifact_kind="REVIEWABLE_DOCUMENT_PDF_PREVIEW",
            media_type="application/pdf",
            content_sha256=sha256(self.pdf_content).hexdigest(),
            byte_size=len(self.pdf_content),
            content=self.pdf_content,
        )
        self.package = ReviewableDocumentPackageRead(
            package_id=self.package_id,
            run_id=self.run_id,
            graph_id=self.graph_id,
            task_id=self.task_id,
            task_input_hash=self.task_input_hash,
            case_snapshot_hash=_hash("case-snapshot"),
            binding_hash=_hash("binding"),
            source_set_hash=_hash("sources"),
            authorized_source_refs=(self.source_ref,),
            authorized_source_refs_hash=_hash("source-refs"),
            authorized_source_manifest=(
                AuthorizedDocumentSourceBinding(
                    input_ref=self.source_ref,
                    source_kind="CONFIRMED_FACT",
                    source_version="1",
                    source_hash=_hash("fact-source"),
                    label="已确认事实：利息范围",
                    text_sha256=_hash("fact-text"),
                ),
            ),
            candidate_hash=_hash("candidate-semantic"),
            work_plan_id=_id(),
            work_plan_hash=_hash("work-plan"),
            work_plan_item_id=self.work_plan_item_id,
            posture_profile_id=_id(),
            posture_profile_hash=_hash("posture"),
            template_id="civil-defence-statement",
            template_version="1.0.0",
            template_hash=_hash("template"),
            deliverable_kind="DEFENCE_STATEMENT",
            output_format=ReviewableDocumentFormat.DOCX,
            review_input_hash=_hash("review-input"),
            render_verification_hash=_hash("render-verification"),
            receipt_hash=_hash("package-receipt"),
            candidate=candidate,
            editable=editable,
            review_pdf=pdf,
            selected_artifact_id=self.candidate_id,
            review_pdf_page_count=2,
        )
        self.row = {
            "package_id": self.package_id,
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "task_id": self.task_id,
            "task_input_hash": self.task_input_hash,
            "candidate_artifact_id": self.candidate_id,
            "candidate_artifact_kind": candidate.artifact_kind,
            "candidate_content_sha256": candidate.content_sha256,
            "candidate_byte_size": candidate.byte_size,
            "editable_artifact_id": self.editable_id,
            "editable_artifact_kind": editable.artifact_kind,
            "editable_sha256": editable.content_sha256,
            "editable_byte_size": editable.byte_size,
            "review_pdf_artifact_id": self.pdf_id,
            "review_pdf_artifact_kind": pdf.artifact_kind,
            "review_pdf_sha256": pdf.content_sha256,
            "review_pdf_byte_size": pdf.byte_size,
            "artifact_lineage": [
                asdict(self._lineage(item)) for item in (candidate, editable, pdf)
            ],
        }
        self.identity = ServerIdentityContext(
            actor=Actor(
                actor_id=self.actor_id,
                firm_id=self.firm_id,
                roles=frozenset({Role.LEAD_LAWYER}),
            ),
            session_id=_id(),
            issuer="https://identity.lawfirm.test",
            authentication_method=AuthenticationMethod.OIDC_MFA,
            authenticated_at=now - timedelta(minutes=2),
            expires_at=now + timedelta(hours=1),
        )
        self.revision_state = DocumentRevisionState(
            root_package_id=self.package_id,
            root_candidate_artifact_id=self.candidate_id,
            current_package_id=self.package_id,
            current_candidate_artifact_id=self.candidate_id,
            requested_artifact_id=self.candidate_id,
            deliverable_kind="DEFENCE_STATEMENT",
            output_format=ReviewableDocumentFormat.DOCX,
            revision_number=1,
            template_id="civil-defence-statement",
            template_version="1.0.0",
            template_hash=_hash("template"),
            package_receipt_hash=_hash("package-receipt"),
            current_revision_request_id=None,
            installed_template_version="1.0.0",
            installed_template_hash=_hash("template"),
            version_status="CURRENT",
            request_status=None,
            request_id=None,
            can_request_revision=False,
            run_status="READY_FOR_REVIEW",
        )

    def _lineage(self, artifact: ReviewableDocumentArtifactRead):
        receipt = ArtifactReceipt(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content_hash=artifact.content_sha256,
            byte_size=artifact.byte_size,
            source_input_hash=self.task_input_hash,
            managed_derivative=True,
        )
        return ArtifactLineageReceipt.build(
            artifact=receipt,
            task_id=self.task_id,
            managed=ManagedArtifactRead(
                artifact_id=artifact.artifact_id,
                artifact_kind=artifact.artifact_kind,
                content=b"",
                source_input_hash=self.task_input_hash,
                object_receipt_hash=_hash(f"object-{artifact.artifact_kind}"),
                media_type=artifact.media_type,
            ),
            format_receipt=ArtifactFormatReceipt(
                artifact_kind=artifact.artifact_kind,
                format_verifier_id="document-format",
                format_verifier_version="1.0.0",
                observed_content_hash=artifact.content_sha256,
                format_verification_hash=_hash(f"format-{artifact.artifact_kind}"),
            ),
        )

    def _service(self, *, row=..., access=None, revision_store=None):
        actual_row = self.row if row is ... else row
        connection = _Connection(actual_row)
        package_access = access or _PackageAccess(self.package)
        revisions = revision_store or _RevisionStore(self.revision_state)
        service = PostgresWebCaseAgentDocumentReviewService(
            dsn="postgresql://not-used.invalid/test",
            package_access=package_access,
            revision_store=revisions,
            connection_factory=lambda *_args, **_kwargs: connection,
        )
        return service, connection, package_access

    def test_verified_current_package_projects_source_bound_lawyer_preview(self) -> None:
        service, connection, access = self._service()
        review = service.read_review(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.candidate_id,
        )
        self.assertEqual(review.deliverable_label, "民事答辩状候选")
        self.assertEqual(review.sections[0].heading, "答辩意见")
        self.assertEqual(review.sections[0].paragraphs[0].sources[0].label, "已确认事实：利息范围")
        self.assertEqual(review.version_status, "CURRENT")
        self.assertTrue(review.download_ready)
        self.assertEqual(review.review_version, sha256(
            f"lawyer-document-review-v1:{self.package.package_id}:{self.package.receipt_hash}".encode()
        ).hexdigest())
        self.assertFalse(review.preview_truncated)
        self.assertEqual(len(access.calls), 1)
        sql = next(
            sql
            for sql, _ in connection.calls
            if "FROM case_agent_reviewable_document_packages" in sql
        )
        self.assertIn("receipt.outcome = 'PASSED'", sql)
        self.assertIn("run.current_graph_id = package.graph_id", sql)
        self.assertIn("current_matter.version = run.snapshot_matter_version", sql)
        self.assertIn("current_matter.firm_id = run.firm_id", sql)
        self.assertIn("role.revoked_at IS NULL", sql)
        projected = json.dumps(asdict(review), ensure_ascii=False)
        for forbidden in ("object_key", "content_sha256", "binding_hash", "provider", "prompt"):
            self.assertNotIn(forbidden, projected)

    def test_each_download_reauthorizes_and_returns_only_the_selected_verified_pair(self) -> None:
        service, _, access = self._service()
        editable = service.download(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.candidate_id,
            file_role="editable",
        )
        self.assertEqual(editable.content, self.editable_content)
        self.assertEqual(editable.disposition, "attachment")
        self.assertEqual(editable.ascii_file_name, "agent-document-candidate.docx")
        self.assertNotIn("/", editable.file_name)
        pdf = service.download(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.candidate_id,
            file_role="pdf-preview",
        )
        self.assertEqual(pdf.content, self.pdf_content)
        self.assertEqual(pdf.disposition, "inline")
        self.assertEqual(len(access.calls), 2)

    def test_download_requires_the_exact_reviewed_version_when_supplied(self) -> None:
        service, _, _ = self._service()
        args = dict(identity=self.identity, matter_id=self.matter_id,
                    run_id=self.run_id, artifact_id=self.candidate_id)
        version = service.read_review(**args).review_version
        for role in ("editable", "pdf-preview"):
            with self.subTest(role=role):
                result = service.download(**args, file_role=role, expected_review_version=version)
                self.assertEqual(result.review_version, version)
                with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "换版"):
                    service.download(**args, file_role=role, expected_review_version="f" * 64)
        with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "无效"):
            service.download(**args, file_role="editable", expected_review_version="not-a-version")

    def test_candidate_word_and_pdf_links_share_one_final_review_identity(self) -> None:
        package, state = self._content_successor()
        service, _, _ = self._service(access=_PackageAccess(package), revision_store=_RevisionStore(state))
        reviews = [service.read_review(identity=self.identity, matter_id=self.matter_id,
            run_id=self.run_id, artifact_id=id) for id in (self.candidate_id, self.editable_id, self.pdf_id)]
        self.assertEqual({review.review_artifact_id for review in reviews}, {self.candidate_id})
        self.assertEqual(len({review.review_version for review in reviews}), 1)
        self.assertEqual({review.artifact_id for review in reviews}, {self.candidate_id, self.editable_id, self.pdf_id})

    def _content_successor(self):
        payload = json.loads(self.candidate_content)
        payload["sections"][0]["paragraphs"][0]["text"] = "律师修改：请结合已确认的利息范围复核。"
        content = _canonical(payload)
        candidate = replace(
            self.package.candidate, artifact_id=_id(), content=content,
            content_sha256=sha256(content).hexdigest(), byte_size=len(content),
        )
        package = replace(
            self.package, package_id=_id(), candidate=candidate,
            selected_artifact_id=candidate.artifact_id,
            generation_mode="LAWYER_CONTENT_REVISION", revision_number=2,
            root_package_id=self.package_id, supersedes_package_id=self.package_id,
            revision_request_id=_id(), requested_by=self.actor_id,
            content_generation_claim_version=2, receipt_hash=_hash("content-successor"),
        )
        state = replace(
            self.revision_state, current_package_id=package.package_id,
            current_candidate_artifact_id=candidate.artifact_id,
            revision_number=2, current_revision_request_id=package.revision_request_id,
            package_receipt_hash=package.receipt_hash,
        )
        return package, state

    def test_unknown_content_files_are_checked_without_releasing_or_leaking_them(self) -> None:
        package, _ = self._content_successor()
        registered = RegisteredContentResult(package.package_id, package.candidate.artifact_id,
            package.receipt_hash, package.revision_request_id, package.root_package_id,
            package.supersedes_package_id, package.revision_number, package.candidate_hash,
            package.binding_hash, package.content_generation_claim_version)
        revisions = _RevisionStore(self.revision_state)
        revisions.read_content_proposal = Mock(return_value={"generation_status": "UNKNOWN_REGISTERED", "court_ready": False})
        revisions.read_registered_content_result = Mock(return_value=registered)
        access = _PackageAccess(package)
        service, _, _ = self._service(access=access, revision_store=revisions)
        args = dict(identity=self.identity, matter_id=self.matter_id, run_id=self.run_id,
                    artifact_id=self.candidate_id, proposal_id=_id())
        self.assertEqual(service.read_content_proposal(**args),
                         {"generation_status": "UNKNOWN_FILES_VERIFIED", "court_ready": False})
        self.assertEqual(access.calls[-1]["artifact_id"], registered.candidate_artifact_id)
        for invalid in (replace(package, receipt_hash="0" * 64), replace(package, run_id=_id()),
                        replace(package, candidate_hash="0" * 64), replace(package, revision_number=99),
                        replace(package, content_generation_claim_version=3)):
            access.package = invalid
            self.assertEqual(service.read_content_proposal(**args)["generation_status"], "UNKNOWN_REGISTERED")
        access.error = RuntimeError("private-object-secret")
        self.assertEqual(service.read_content_proposal(**args),
                         {"generation_status": "UNKNOWN_REGISTERED", "court_ready": False})
        access.error = PermissionError("revoked")
        with self.assertRaises(PermissionError):
            service.read_content_proposal(**args)
        access.calls.clear()
        revisions.read_content_proposal.return_value = {"generation_status": "UNKNOWN", "court_ready": False}
        self.assertEqual(service.read_content_proposal(**args)["generation_status"], "UNKNOWN")
        self.assertEqual(access.calls, [])

    def test_content_successor_can_be_read_and_downloaded_through_original_link(self) -> None:
        original_service, _, _ = self._service()
        original = original_service.read_review(identity=self.identity, matter_id=self.matter_id,
            run_id=self.run_id, artifact_id=self.candidate_id)
        package, state = self._content_successor()
        service, _, access = self._service(
            access=_PackageAccess(package), revision_store=_RevisionStore(state),
        )
        args = dict(identity=self.identity, matter_id=self.matter_id,
                    run_id=self.run_id, artifact_id=self.candidate_id)
        review = service.read_review(**args)
        self.assertEqual(review.sections[0].paragraphs[0].text,
                         "律师修改：请结合已确认的利息范围复核。")
        self.assertEqual(review.revision_number, 2)
        self.assertNotEqual(review.review_version, original.review_version)
        self.assertEqual(review.review_version, service.read_review(**args).review_version)
        self.assertTrue(review.download_ready)
        for role, expected in (("editable", package.editable.content),
                               ("pdf-preview", package.review_pdf.content)):
            self.assertEqual(service.download(**args, file_role=role).content, expected)
        self.assertEqual(len(access.calls), 4)
        self.assertTrue(all(call["artifact_id"] == package.candidate.artifact_id
                            for call in access.calls))

    def test_content_successor_rejects_invalid_claim_or_version_binding(self) -> None:
        package, state = self._content_successor()
        mutations = [
            {"content_generation_claim_version": value}
            for value in (None, True, 0, 4, "2")
        ] + [
            {"root_package_id": _id()}, {"revision_request_id": _id()},
            {"receipt_hash": _hash("wrong-receipt")},
            {"generation_mode": "UNREGISTERED_REVISION"},
            {"generation_mode": "DETERMINISTIC_TEMPLATE_REVISION"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                service, _, _ = self._service(
                    access=_PackageAccess(replace(package, **mutation)),
                    revision_store=_RevisionStore(state),
                )
                with self.assertRaises(WebCaseAgentDocumentReviewBlocked):
                    service.read_review(identity=self.identity, matter_id=self.matter_id,
                                        run_id=self.run_id, artifact_id=self.candidate_id)

    def test_outdated_template_exposes_status_but_not_old_document_bytes(self) -> None:
        state = replace(
            self.revision_state,
            installed_template_version="1.1.0",
            installed_template_hash=_hash("template-v1.1"),
            version_status="UPDATE_REQUIRED",
            can_request_revision=True,
        )
        revisions = _RevisionStore(state)
        service, connection, access = self._service(revision_store=revisions)

        review = service.read_review(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.candidate_id,
        )

        self.assertEqual(review.version_status, "UPDATE_REQUIRED")
        self.assertTrue(review.can_request_revision)
        self.assertFalse(review.download_ready)
        self.assertIsNone(review.review_version)
        self.assertEqual(review.sections, ())
        self.assertEqual(review.total_item_count, 0)
        self.assertEqual(connection.calls, [])
        self.assertEqual(access.calls, [])

    def test_revision_command_returns_durable_generating_state(self) -> None:
        outdated = replace(
            self.revision_state,
            installed_template_version="1.1.0",
            installed_template_hash=_hash("template-v1.1"),
            version_status="UPDATE_REQUIRED",
            can_request_revision=True,
        )
        request_id = _id()
        generating = replace(
            outdated,
            version_status="GENERATING",
            can_request_revision=False,
            request_status="READY",
            request_id=request_id,
        )
        revisions = _RevisionStore(outdated, request_state=generating)
        service, _, access = self._service(revision_store=revisions)

        review = service.request_revision(
            identity=self.identity,
            matter_id=self.matter_id,
            run_id=self.run_id,
            artifact_id=self.candidate_id,
            expected_revision_number=1,
            idempotency_key="document-revision-command-0001",
        )

        self.assertEqual(review.version_status, "GENERATING")
        self.assertEqual(review.request_id, request_id)
        self.assertEqual(revisions.request_calls[0]["expected_revision_number"], 1)
        self.assertEqual(access.calls, [])

    def test_missing_or_unverified_package_never_reads_private_objects(self) -> None:
        service, _, access = self._service(row=None)
        with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "不存在"):
            service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.candidate_id,
            )
        self.assertEqual(access.calls, [])

    def test_tampered_lineage_is_rejected_before_package_read(self) -> None:
        row = dict(self.row)
        row["artifact_lineage"] = list(self.row["artifact_lineage"])
        row["artifact_lineage"][0] = {
            **row["artifact_lineage"][0],
            "content_hash": _hash("tampered"),
        }
        service, _, access = self._service(row=row)
        with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "复核记录"):
            service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.candidate_id,
            )
        self.assertEqual(access.calls, [])

    def test_indeterminate_current_source_recheck_is_not_retried(self) -> None:
        access = _PackageAccess(self.package, RuntimeError("source manifest changed"))
        service, _, _ = self._service(access=access)
        with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "不会自动重试"):
            service.read_review(
                identity=self.identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.candidate_id,
            )
        self.assertEqual(len(access.calls), 1)

    def test_non_mfa_identity_is_rejected_before_database_access(self) -> None:
        service, connection, access = self._service()
        identity = ServerIdentityContext(
            **{
                **self.identity.__dict__,
                "authentication_method": AuthenticationMethod.OS_BOUND_LOCAL_SESSION,
            }
        )
        with self.assertRaisesRegex(WebCaseAgentDocumentReviewBlocked, "MFA"):
            service.read_review(
                identity=identity,
                matter_id=self.matter_id,
                run_id=self.run_id,
                artifact_id=self.candidate_id,
            )
        self.assertEqual(connection.calls, [])
        self.assertEqual(access.calls, [])


if __name__ == "__main__":
    unittest.main()
