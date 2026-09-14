from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID, uuid4, uuid5
import zipfile

from PIL import Image

from case_kernel.case_agent_skill_adapters import (
    BoundCommonDocument,
    COMMON_DOCUMENT_READER_MANIFEST,
    CaseAgentSkillAdapterBlocked,
    CommonDocumentTaskAdapter,
    DeterministicVisualPageProjectionPort,
    EvidenceProjectionAuthorization,
    LocalVisualPageTaskAdapter,
    PDF_TEXT_READER_MANIFEST,
    PdfTextTaskAdapter,
    REVIEW_STATUS,
    ReviewCandidateStagingRequest,
    ServerBoundVisualSource,
    StagedReviewCandidate,
    WebEvidencePageTaskProjectionPort,
    configured_local_visual_page_adapter,
)
from case_kernel.case_agent_supervisor import ExternalSubmissionState, ResultStatus
from case_kernel.common_document_reader import (
    CommonDocumentFormat,
    MaterializedDocumentSource,
)
from case_kernel.models import Actor, Role
from case_kernel.visual_page_understanding import (
    VISUAL_PAGE_SCHEMA_VERSION,
    VisualSourceKind,
    visual_page_request_hash,
)
from case_kernel.web_agent_material_review import AgentEvidencePageProjection


def digest(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return sha256(raw).hexdigest()


def context(*, input_refs=("material-a",), max_output_bytes=1024 * 1024):
    task_id = str(uuid4())
    task = SimpleNamespace(
        task_id=task_id,
        input_hash=digest("compiled-task-input"),
        input_refs=tuple(input_refs),
        budget=SimpleNamespace(timeout_seconds=30, max_output_bytes=max_output_bytes),
    )
    claim = SimpleNamespace(
        run_id=str(uuid4()), task_id=task_id, attempt_id=str(uuid4())
    )
    return SimpleNamespace(claim=claim, task=task, input_refs=task.input_refs)


class InMemoryCandidateStaging:
    def __init__(self) -> None:
        self.requests: list[ReviewCandidateStagingRequest] = []
        self._receipts: dict[str, StagedReviewCandidate] = {}

    def stage_review_candidate(self, request):
        request.validate()
        self.requests.append(request)
        receipt = self._receipts.get(request.idempotency_key)
        if receipt is None:
            receipt = StagedReviewCandidate.build(
                request,
                artifact_id=str(
                    uuid5(UUID(request.task_id), request.idempotency_key)
                ),
            )
            self._receipts[request.idempotency_key] = receipt
        return receipt


class CommonInputPort:
    def __init__(self, bindings):
        self.bindings = bindings
        self.calls = []

    def open_common_documents(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def __enter__(self):
        return self.bindings

    def __exit__(self, *_):
        for binding in self.bindings:
            binding.source.path.unlink(missing_ok=True)
        return False


class PdfProjectionPort:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def project_pdf_pages(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages


class VisualSourcePort:
    def __init__(self, sources):
        self.sources = sources
        self.calls = []

    def resolve_visual_sources(self, **kwargs):
        self.calls.append(kwargs)
        return self.sources


class LocalVisualProvider:
    provider_id = "localocr"
    model_id = "layout-reader"
    provider_version = "1.2.0"
    network_capable = False

    def __init__(self):
        self.calls = []

    def analyze_page(self, *, projection, external_request_id):
        self.calls.append((projection, external_request_id))
        return json.dumps(
            {
                "schema_version": VISUAL_PAGE_SCHEMA_VERSION,
                "request_hash": visual_page_request_hash(projection),
                "matter_id": projection.matter_id,
                "evidence_page_id": projection.evidence_page_id,
                "source_file_sha256": projection.source_file_sha256,
                "source_page_sha256": projection.source_page_sha256,
                "rendered_page_sha256": projection.rendered_page_sha256,
                "projection_hash": projection.projection_hash,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "text_blocks": [
                    {
                        "block_id": "block-1",
                        "kind": "TEXT",
                        "text": "候选转账100元",
                        "region": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.5,
                            "height": 0.1,
                        },
                        "confidence": 0.91,
                    }
                ],
                "tables": [],
                "fields": [],
                "quality_risks": [],
            },
            ensure_ascii=False,
        )


def png_bytes() -> bytes:
    image = Image.new("RGB", (120, 80), (245, 245, 245))
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class CaseAgentSkillAdapterTests(unittest.TestCase):
    def test_manifests_are_exact_local_read_only_bindings(self):
        for manifest, tool_id in (
            (COMMON_DOCUMENT_READER_MANIFEST, "parse_office_document"),
            (PDF_TEXT_READER_MANIFEST, "extract_pdf_text"),
        ):
            manifest.validate()
            self.assertEqual(manifest.tool_id, tool_id)
            self.assertTrue(manifest.supports_idempotency)
            self.assertFalse(manifest.supports_reconciliation)
            self.assertFalse(manifest.network_capable)

    def test_common_document_adapter_stages_review_candidate_with_source_hash(self):
        package = BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>原告主张还款100元</w:t></w:r></w:p></w:body></w:document>""",
            )
        raw = package.getvalue()
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "source.docx"
            path.write_bytes(raw)
            path.chmod(0o600)
            source = MaterializedDocumentSource(
                source_object_id=str(uuid4()),
                source_object_version="opaque-v1",
                materialization_root=root,
                path=path,
                byte_size=len(raw),
                content_sha256=digest(raw),
                admitted_format=CommonDocumentFormat.DOCX,
            )
            port = CommonInputPort((BoundCommonDocument("material-a", source),))
            staging = InMemoryCandidateStaging()
            execution = context()
            adapter = CommonDocumentTaskAdapter(
                input_port=port, staging_port=staging
            )
            outcome = adapter.execute(context=execution)

        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(
            outcome.external_submission_state, ExternalSubmissionState.NOT_APPLICABLE
        )
        self.assertEqual(outcome.external_calls, 0)
        self.assertEqual(len(outcome.artifacts), 1)
        self.assertFalse(outcome.artifacts[0].managed_derivative)
        request = staging.requests[0]
        self.assertEqual(request.review_status, REVIEW_STATUS)
        self.assertEqual(request.task_input_hash, execution.task.input_hash)
        payload = json.loads(request.payload)
        self.assertFalse(payload["formal_fact"])
        self.assertFalse(payload["formal_transaction"])
        self.assertFalse(payload["legal_conclusion"])
        self.assertEqual(payload["documents"][0]["source_sha256"], digest(raw))
        self.assertEqual(
            payload["documents"][0]["candidates"][0]["text"],
            "原告主张还款100元",
        )
        self.assertNotIn(str(path), repr(adapter))
        self.assertNotIn("原告主张", repr(request))
        self.assertEqual(
            set(port.calls[0]),
            {"run_id", "task_id", "task_input_hash", "input_refs"},
        )

    def test_compiled_ref_cannot_smuggle_a_url_path_or_command(self):
        for input_ref in (
            "https:evil.example",
            "file:private.pdf",
            "path:secret",
            "cmd:whoami",
            "shell:echo",
        ):
            with self.subTest(input_ref=input_ref), self.assertRaisesRegex(
                CaseAgentSkillAdapterBlocked, "URL, path or command"
            ):
                PdfTextTaskAdapter(
                    projection_port=PdfProjectionPort(()),
                    staging_port=InMemoryCandidateStaging(),
                ).execute(context=context(input_refs=(input_ref,)))

    def test_common_document_binding_must_match_compiled_refs_exactly(self):
        staging = InMemoryCandidateStaging()
        adapter = CommonDocumentTaskAdapter(
            input_port=CommonInputPort(()), staging_port=staging
        )
        with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "compiled input refs"):
            adapter.execute(context=context())
        self.assertFalse(staging.requests)

    def test_office_adapter_rejects_other_common_formats_until_they_have_a_skill(self):
        raw = b"plain text"
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "source.txt"
            path.write_bytes(raw)
            path.chmod(0o600)
            source = MaterializedDocumentSource(
                source_object_id=str(uuid4()),
                source_object_version="opaque-v1",
                materialization_root=root,
                path=path,
                byte_size=len(raw),
                content_sha256=digest(raw),
                admitted_format=CommonDocumentFormat.TXT,
            )
            with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "DOCX or XLSX"):
                CommonDocumentTaskAdapter(
                    input_port=CommonInputPort(
                        (BoundCommonDocument("material-a", source),)
                    ),
                    staging_port=InMemoryCandidateStaging(),
                ).execute(context=context())

    def test_pdf_adapter_accepts_only_server_projection_and_stages_candidate(self):
        page = AgentEvidencePageProjection.build(
            evidence_page_id=str(uuid4()),
            source_file_sha256=digest("registered-pdf"),
            page_number=3,
            extracted_text="第三页候选文字",
        )
        port = PdfProjectionPort((page,))
        staging = InMemoryCandidateStaging()
        execution = context(input_refs=("evidence-page-a",))
        outcome = PdfTextTaskAdapter(
            projection_port=port, staging_port=staging
        ).execute(context=execution)

        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        payload = json.loads(staging.requests[0].payload)
        self.assertEqual(payload["pages"][0]["evidence_page_id"], page.evidence_page_id)
        self.assertEqual(payload["pages"][0]["extracted_text"], "第三页候选文字")
        self.assertEqual(port.calls[0]["input_refs"], ("evidence-page-a",))
        self.assertNotIn("path", port.calls[0])
        self.assertNotIn("url", port.calls[0])
        self.assertNotIn("command", port.calls[0])

    def test_pdf_adapter_rejects_changed_projection_text_hash(self):
        valid = AgentEvidencePageProjection.build(
            evidence_page_id=str(uuid4()),
            source_file_sha256=digest("registered-pdf"),
            page_number=1,
            extracted_text="original",
        )
        changed = AgentEvidencePageProjection(
            valid.evidence_page_id,
            valid.source_file_sha256,
            valid.page_number,
            "changed",
            valid.extracted_text_sha256,
        )
        with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "hash differs"):
            PdfTextTaskAdapter(
                projection_port=PdfProjectionPort((changed,)),
                staging_port=InMemoryCandidateStaging(),
            ).execute(context=context(input_refs=("evidence-page-a",)))

    def test_adapter_fails_before_staging_when_compiled_output_budget_is_exceeded(self):
        page = AgentEvidencePageProjection.build(
            evidence_page_id=str(uuid4()),
            source_file_sha256=digest("registered-pdf"),
            page_number=1,
            extracted_text="x" * 100,
        )
        staging = InMemoryCandidateStaging()
        with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "output budget"):
            PdfTextTaskAdapter(
                projection_port=PdfProjectionPort((page,)), staging_port=staging
            ).execute(
                context=context(input_refs=("evidence-page-a",), max_output_bytes=10)
            )
        self.assertFalse(staging.requests)

    def test_visual_adapter_is_honestly_gated_without_a_local_provider(self):
        self.assertIsNone(
            configured_local_visual_page_adapter(
                projection_port=None, provider=None, staging_port=None
            )
        )

    def test_visual_adapter_rejects_network_provider_at_composition(self):
        provider = LocalVisualProvider()
        provider.network_capable = True
        with self.assertRaisesRegex(ValueError, "durable external-submission"):
            LocalVisualPageTaskAdapter(
                projection_port=SimpleNamespace(project_visual_pages=lambda **_: ()),
                provider=provider,
                staging_port=InMemoryCandidateStaging(),
            )

    def test_local_visual_adapter_stages_review_only_source_bound_candidate(self):
        raw = png_bytes()
        source = ServerBoundVisualSource(
            input_ref="image-page-a",
            matter_id=str(uuid4()),
            evidence_page_id=str(uuid4()),
            page_number=1,
            source_kind=VisualSourceKind.NATIVE_IMAGE,
            source_file_sha256=digest("registered-source-file"),
            source_page_sha256=digest(raw),
            source_media_type="image/png",
            source_bytes=raw,
        )
        source_port = VisualSourcePort((source,))
        projection_port = DeterministicVisualPageProjectionPort(source_port)
        provider = LocalVisualProvider()
        staging = InMemoryCandidateStaging()
        execution = context(input_refs=("image-page-a",))
        adapter = LocalVisualPageTaskAdapter(
            projection_port=projection_port,
            provider=provider,
            staging_port=staging,
        )
        outcome = adapter.execute(context=execution)

        self.assertFalse(adapter.manifest.network_capable)
        self.assertEqual(outcome.status, ResultStatus.SUCCEEDED)
        self.assertEqual(outcome.external_calls, 0)
        payload = json.loads(staging.requests[0].payload)
        self.assertEqual(payload["review_status"], REVIEW_STATUS)
        self.assertFalse(payload["evidence_decision"])
        self.assertEqual(payload["pages"][0]["source_page_sha256"], digest(raw))
        self.assertEqual(
            payload["pages"][0]["text_blocks"][0]["text"], "候选转账100元"
        )
        self.assertNotIn(raw.hex()[:24], repr(adapter))
        self.assertEqual(len(provider.calls), 1)

    def test_web_projection_bridge_rejects_binding_for_another_compiled_task(self):
        actor = Actor(
            str(uuid4()), str(uuid4()), frozenset({Role.LEAD_LAWYER})
        )
        execution = context(input_refs=("evidence-page-a",))
        binding = EvidenceProjectionAuthorization.build(
            run_id=execution.claim.run_id,
            task_id=str(uuid4()),
            task_input_hash=execution.task.input_hash,
            input_refs=execution.input_refs,
            matter_id=str(uuid4()),
            authorized_actor=actor,
            evidence_page_ids=(str(uuid4()),),
        )
        authorization_port = SimpleNamespace(
            resolve_evidence_projection=lambda **_: binding
        )
        projection_source = SimpleNamespace(load_pages=lambda **_: ())
        bridge = WebEvidencePageTaskProjectionPort(
            authorization_port=authorization_port,
            projection_source=projection_source,
        )
        with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "compiled task"):
            bridge.project_pdf_pages(
                run_id=execution.claim.run_id,
                task_id=execution.claim.task_id,
                task_input_hash=execution.task.input_hash,
                input_refs=execution.input_refs,
            )

    def test_staging_receipt_must_match_exact_candidate_request(self):
        page = AgentEvidencePageProjection.build(
            evidence_page_id=str(uuid4()),
            source_file_sha256=digest("registered-pdf"),
            page_number=1,
            extracted_text="candidate",
        )

        class WrongStaging(InMemoryCandidateStaging):
            def stage_review_candidate(self, request):
                valid = super().stage_review_candidate(request)
                return StagedReviewCandidate(
                    **{**valid.__dict__, "source_hash": digest("other-source")}
                )

        with self.assertRaisesRegex(CaseAgentSkillAdapterBlocked, "receipt differs"):
            PdfTextTaskAdapter(
                projection_port=PdfProjectionPort((page,)),
                staging_port=WrongStaging(),
            ).execute(context=context(input_refs=("evidence-page-a",)))


if __name__ == "__main__":
    unittest.main()
