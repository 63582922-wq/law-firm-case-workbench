from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from case_kernel.case_agent_supervisor import (
    AgentEventType,
    AgentRunStatus,
    ArtifactReceipt,
    ExternalSubmissionState,
    ResultStatus,
    TaskResultPayload,
    reduce_agent_event,
)
from case_kernel.case_agent_verifier import (
    ArtifactVerificationRejected,
    FIRST_RELEASE_VERIFIER_POLICY_HASH,
    CanonicalJsonArtifactVerifier,
    CaseAgentRunVerifier,
    ManagedArtifactRead,
    ReviewableDocumentCandidateArtifactVerifier,
    ReviewablePdfArtifactVerifier,
    VerificationOutcome,
    build_first_release_case_agent_run_verifier,
    first_release_verifier_policy_hash,
    _ledger_extraction_authorized_source_refs,
)
from case_kernel.case_agent_document_delivery import (
    build_deterministic_payment_ledger_candidate,
    canonical_document_candidate_bytes,
)
from backend.tests.test_case_agent_document_delivery import (
    CaseAgentDocumentDeliveryTests,
)
from case_kernel.case_agent_worker import (
    AgentWorkerStep,
    CaseAgentWorker,
    DurableVerificationClaim,
    TaskAdapterOutcome,
)
from case_kernel.models import Actor, Role
from case_kernel.qwen_visual_ocr_adapter import AuthorizedVisualOcrBinding
from case_kernel.visual_page_understanding import (
    VisualSourceKind,
    build_visual_page_projection,
    parse_visual_page_candidate,
    visual_page_request_hash,
)
from PIL import Image
from io import BytesIO
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NullObject,
    NumberObject,
    TextStringObject,
)

from backend.tests import test_case_agent_supervisor as supervisor_fixture
from backend.tests.test_case_agent_worker import (
    _Adapter,
    _Compiler,
    _Planner,
    _SnapshotProvider,
)


def digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


class DeterministicPaymentLedgerVerifierTests(unittest.TestCase):
    def _artifact(self, content: bytes) -> ManagedArtifactRead:
        return ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind="REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
            content=content,
            source_input_hash=digest("task"),
            object_receipt_hash=digest("object"),
            media_type="application/json",
        )

    def test_verifier_requires_fixed_columns_and_one_transaction_ref_per_row(self):
        binding = CaseAgentDocumentDeliveryTests().binding("PAYMENT_LEDGER")
        candidate = build_deterministic_payment_ledger_candidate(binding)
        content = canonical_document_candidate_bytes(candidate)
        verifier = ReviewableDocumentCandidateArtifactVerifier()
        verifier.verify(self._artifact(content))

        changed = json.loads(content)
        changed["columns"][2]["label"] = "模型自定义金额"
        with self.assertRaisesRegex(
            ArtifactVerificationRejected,
            "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC",
        ):
            verifier.verify(self._artifact(canonical(changed)))

        changed = json.loads(content)
        changed["rows"][0]["source_refs"] = [
            next(
                source.input_ref
                for source in binding.sources
                if not source.input_ref.startswith("transaction:")
            )
        ]
        with self.assertRaisesRegex(
            ArtifactVerificationRejected,
            "ARTIFACT_PAYMENT_LEDGER_NOT_DETERMINISTIC",
        ):
            verifier.verify(self._artifact(canonical(changed)))


class ReviewablePdfArtifactVerifierTests(unittest.TestCase):
    def _artifact(self, content: bytes) -> ManagedArtifactRead:
        return ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind="REVIEWABLE_DOCUMENT_PDF_PREVIEW",
            content=content,
            source_input_hash=digest("task"),
            object_receipt_hash=digest("object"),
            media_type="application/pdf",
        )

    def _pdf(self, open_action: object) -> bytes:
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=400)
        if open_action == "LOCAL_PAGE_DESTINATION":
            writer.root_object[NameObject("/OpenAction")] = ArrayObject(
                [
                    page.indirect_reference,
                    NameObject("/XYZ"),
                    NullObject(),
                    NullObject(),
                    NumberObject(0),
                ]
            )
        else:
            writer.root_object[NameObject("/OpenAction")] = open_action
        output = BytesIO()
        writer.write(output)
        return output.getvalue()

    def test_allows_valid_local_initial_page_destination(self):
        receipt = ReviewablePdfArtifactVerifier().verify(
            self._artifact(self._pdf("LOCAL_PAGE_DESTINATION"))
        )
        self.assertEqual(receipt.artifact_kind, "REVIEWABLE_DOCUMENT_PDF_PREVIEW")

    def test_rejects_open_action_dictionary_even_when_pdf_is_otherwise_valid(self):
        javascript = DictionaryObject(
            {
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject("app.alert('blocked')"),
            }
        )
        with self.assertRaisesRegex(
            ArtifactVerificationRejected,
            "ARTIFACT_PDF_ACTIVE_CONTENT_REJECTED",
        ):
            ReviewablePdfArtifactVerifier().verify(
                self._artifact(self._pdf(javascript))
            )


class _ArtifactAccess:
    def __init__(self, content_by_id, *, error=None, mutate_lineage=False):
        self.content_by_id = content_by_id
        self.error = error
        self.mutate_lineage = mutate_lineage

    def read_managed_artifact(self, *, artifact, **_):
        if self.error:
            raise self.error
        content = self.content_by_id[artifact.artifact_id]
        return ManagedArtifactRead(
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.artifact_kind,
            content=content,
            source_input_hash=(
                digest("different")
                if self.mutate_lineage
                else artifact.source_input_hash
            ),
            object_receipt_hash=digest("object:" + artifact.artifact_id),
            media_type="application/json",
        )


class _VerificationStore:
    def __init__(self, state, verifier_actor, execution_actor, helper):
        self.state = state
        self.verifier_actor = verifier_actor
        self.execution_actor = execution_actor
        self.helper = helper
        self.events = []
        self.receipts = []
        self.conflict = False

    def start_verification(self, **kwargs):
        self.state = reduce_agent_event(
            self.state,
            self.helper.event(
                self.state.event_version + 1,
                AgentEventType.VERIFICATION_STARTED,
                actor_id=self.verifier_actor.actor_id,
            ),
        )
        self.events.append(AgentEventType.VERIFICATION_STARTED)
        return DurableVerificationClaim(
            verification_attempt_id=str(uuid4()),
            run_id=self.state.run_id,
            event_version=self.state.event_version,
            graph_hash=self.state.graph.graph_hash,
            snapshot_hash=self.state.snapshot.snapshot_hash,
            execution_actor_id=self.execution_actor.actor_id,
            verifier_actor_id=self.verifier_actor.actor_id,
            state=self.state,
        )

    def record_verification_outcome(self, *, receipt, **_):
        if self.conflict:
            raise RuntimeError("version conflict")
        self.receipts.append(receipt)
        self.events.append(
            AgentEventType.VERIFICATION_PASSED
            if receipt.outcome is VerificationOutcome.PASSED
            else AgentEventType.VERIFICATION_FAILED
        )
        return self.state.event_version + 1


class _ExecutionStore:
    def __init__(self, state):
        self.state = state

    def read_projection(self, **_):
        from case_kernel.case_agent_supervisor import decide_next_commands

        return SimpleNamespace(
            state=self.state,
            next_commands=decide_next_commands(self.state),
            checkpoint_verified=True,
        )

    def apply_pending_snapshot_refresh(self, **_):
        return None

    def reap_expired_attempt(self, **_):
        return None

    def reap_expired_planning_attempt(self, **_):
        return None


class CaseAgentRunVerifierTests(unittest.TestCase):
    def setUp(self):
        self.helper = supervisor_fixture.CaseAgentSupervisorTests(methodName="runTest")
        self.helper.setUp()
        self.execution_actor = Actor(
            str(uuid4()), self.helper.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.verifier_actor = Actor(
            str(uuid4()), self.helper.firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        self.now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)

    def test_ledger_source_binding_includes_only_direct_visual_dependencies(self):
        visual = SimpleNamespace(
            skill=SimpleNamespace(tool_id="understand_visual_page"),
            input_refs=("evidence-page:visual",),
        )
        unrelated = SimpleNamespace(
            skill=SimpleNamespace(tool_id="parse_office_document"),
            input_refs=("material-object:office",),
        )
        task = SimpleNamespace(
            input_refs=("evidence-page:native",),
            dependency_ids=("visual", "unrelated"),
        )
        self.assertEqual(
            _ledger_extraction_authorized_source_refs(
                task_spec=task,
                task_specs={"visual": visual, "unrelated": unrelated},
            ),
            frozenset(("evidence-page:native", "evidence-page:visual")),
        )

    def _state(self, *, artifacts=True):
        task = self.helper.local_task()
        if artifacts:
            task = replace(
                task,
                capability=replace(
                    task.capability, writes_managed_derivatives=False
                ),
            )
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.start_ready_task(state, sequence=3)
        payload = canonical(
            {
                "schema_version": "agent-pdf-text-candidate-v1",
                "task_input_hash": task.input_hash,
                "source_hash": digest("source"),
                "review_status": "NEEDS_LAWYER_REVIEW",
                "formal_fact": False,
                "formal_transaction": False,
                "legal_conclusion": False,
                "evidence_decision": False,
                "pages": [
                    {
                        "input_ref": "evidence-page-a",
                        "evidence_page_id": str(uuid4()),
                        "source_file_sha256": digest("source-file"),
                        "page_number": 1,
                        "extracted_text": "候选文字",
                        "extracted_text_sha256": digest("候选文字"),
                    }
                ],
            }
        )
        artifact = ArtifactReceipt(
            artifact_id=str(uuid4()),
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            content_hash=digest(payload),
            byte_size=len(payload),
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        receipt = self.helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.SUCCEEDED,
            artifacts=(artifact,) if artifacts else (),
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                4,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(receipt),
            ),
        )
        self.assertEqual(state.status, AgentRunStatus.VERIFYING)
        return state, artifact, payload

    def _verifier(self, access, *, registered=True):
        format_verifier = CanonicalJsonArtifactVerifier(
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-pdf-text-candidate-v1",),
            payload_kind="PDF_TEXT_REVIEW_CANDIDATE",
        )
        return CaseAgentRunVerifier(
            verifier_id="case-run-verifier",
            verifier_version="1.0.0",
            artifact_access=access,
            artifact_verifiers=(
                {format_verifier.artifact_kind: format_verifier}
                if registered
                else {}
            ),
            clock=lambda: self.now,
        )

    def _public_research_payload(self, **changes):
        query = {
            "question_id": str(uuid4()),
            "purpose": "LEGAL_AUTHORITY_DISCOVERY",
            "query_hash": digest("minimized-public-query"),
            "public_terms": ["民间借贷", "LPR四倍", "利息上限"],
        }
        lead = {
            "lead_id": digest("official-lead")[:32],
            "title": "法律法规数据库检索结果",
            "url": "https://flk.npc.gov.cn/detail.html?id=public",
            "snippet": "仅作公开研究线索，需要律师核对原文和时间效力。",
            "published_on_candidate": "2025-01-01",
            "authority_class": "PRIMARY_LEGISLATION",
            "official_source_id": "NATIONAL_LAWS_DATABASE",
            "publisher": "国家法律法规数据库",
            "official_domain": True,
            "prompt_injection_signals": [],
            "status": "PUBLIC_RESEARCH_LEAD",
        }
        payload = {
            "schema_version": "agent-public-research-leads-candidate-v1",
            "task_input_hash": digest("research-task-input"),
            "source_hash": digest("research-source"),
            "review_status": "NEEDS_LAWYER_REVIEW",
            "formal_fact": False,
            "formal_transaction": False,
            "legal_conclusion": False,
            "evidence_decision": False,
            "legal_effect_confirmed": False,
            "query": query,
            "provider_id": "brave_web_search",
            "external_request_id": str(uuid4()),
            "leads": [lead],
        }
        payload.update(changes)
        return payload

    def _public_research_verifier(self):
        return CanonicalJsonArtifactVerifier(
            artifact_kind="PUBLIC_RESEARCH_LEADS_CANDIDATE",
            allowed_schema_versions=(
                "agent-public-research-leads-candidate-v1",
            ),
            payload_kind="PUBLIC_RESEARCH_LEADS_CANDIDATE",
        )

    def _managed_public_research(self, payload, *, content=None):
        encoded = canonical(payload) if content is None else content
        return ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind="PUBLIC_RESEARCH_LEADS_CANDIDATE",
            content=encoded,
            source_input_hash=payload["task_input_hash"],
            object_receipt_hash=digest("public-research-object"),
            media_type="application/json",
        )

    def _visual_payload(self):
        output = BytesIO()
        Image.new("RGB", (40, 30), "white").save(output, "PNG")
        content = output.getvalue()
        matter_id = str(uuid4())
        page_id = str(uuid4())
        projection = build_visual_page_projection(
            matter_id=matter_id,
            evidence_page_id=page_id,
            page_number=1,
            source_kind=VisualSourceKind.NATIVE_IMAGE,
            source_file_sha256=digest(content),
            source_page_sha256=digest(content),
            source_media_type="image/png",
            source_bytes=content,
        )
        task_hash = digest("visual-task")
        external_request_id = str(uuid4())
        binding = AuthorizedVisualOcrBinding.build(
            run_id=str(uuid4()), task_id=str(uuid4()), attempt_id=str(uuid4()),
            task_input_hash=task_hash, firm_id=str(uuid4()), matter_id=matter_id,
            matter_version=1, input_refs=(f"evidence-page:{page_id}",),
            external_request_id=external_request_id,
            processor_region="cn-beijing", workspace_id="ws-legal-prod",
            projections=(projection,),
        )
        raw = canonical(
            {
                "schema_version": "visual-page-understanding-v1",
                "request_hash": visual_page_request_hash(projection),
                "matter_id": matter_id,
                "evidence_page_id": page_id,
                "source_file_sha256": projection.source_file_sha256,
                "source_page_sha256": projection.source_page_sha256,
                "rendered_page_sha256": projection.rendered_page_sha256,
                "projection_hash": projection.projection_hash,
                "provider_id": "qwen", "model_id": "qwen3.5-ocr",
                "text_blocks": [{
                    "block_id": "b1", "kind": "TEXT", "text": "候选文字",
                    "region": {"x": .1, "y": .1, "width": .4, "height": .2},
                    "confidence": .9,
                }],
                "tables": [], "fields": [], "quality_risks": [],
            }
        )
        candidate = parse_visual_page_candidate(
            raw, projection=projection, expected_provider_id="qwen",
            expected_model_id="qwen3.5-ocr",
            provider_request_ref_hash=digest("provider-ref"),
        )
        region = {"x": .1, "y": .1, "width": .4, "height": .2}
        page = {
            "input_ref": f"evidence-page:{page_id}", "matter_id": matter_id,
            "evidence_page_id": page_id, "page_number": 1,
            "source_kind": "NATIVE_IMAGE",
            "source_file_sha256": projection.source_file_sha256,
            "source_page_sha256": projection.source_page_sha256,
            "rendered_page_sha256": projection.rendered_page_sha256,
            "projection_hash": projection.projection_hash,
            "parser_id": projection.parser_id,
            "parser_version": projection.parser_version,
            "orientation_applied": projection.orientation_applied,
            "source_format": projection.source_format,
            "had_transparency": projection.had_transparency,
            "request_hash": visual_page_request_hash(projection),
            "width": projection.width, "height": projection.height,
            "media_type": "image/png", "provider_id": "qwen",
            "model_id": "qwen3.5-ocr",
            "provider_request_ref_hash": candidate.provider_request_ref_hash,
            "candidate_hash": candidate.candidate_hash,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "text_blocks": [{
                "block_id": "b1", "kind": "TEXT", "text": "候选文字",
                "region": region, "confidence": .9,
            }],
            "tables": [], "fields": [], "quality_risks": [],
        }
        source_hash = digest(canonical({
            "schema_version": "agent-visual-page-source-set-v1",
            "task_input_hash": task_hash, "binding_hash": binding.binding_hash,
            "external_request_id": external_request_id,
            "pages": [{key: page[key] for key in (
                "input_ref", "evidence_page_id", "page_number", "source_kind",
                "source_file_sha256", "source_page_sha256",
                "rendered_page_sha256", "projection_hash", "width", "height",
                "media_type",
            )}],
        }))
        return {
            "schema_version": "agent-visual-page-candidate-bundle-v1",
            "task_input_hash": task_hash, "source_hash": source_hash,
            "binding_hash": binding.binding_hash,
            "review_status": "NEEDS_LAWYER_REVIEW", "formal_fact": False,
            "formal_transaction": False, "legal_conclusion": False,
            "evidence_decision": False, "authenticity_confirmed": False,
            "provenance": {
                "run_id": binding.run_id, "task_id": binding.task_id,
                "attempt_id": binding.attempt_id, "firm_id": binding.firm_id,
                "matter_id": binding.matter_id,
                "matter_version": binding.matter_version,
                "input_refs": list(binding.input_refs),
                "external_request_id": binding.external_request_id,
                "processor_region": binding.processor_region,
                "workspace_id_hash": digest("ws-legal-prod"),
            },
            "external_request_id": external_request_id,
            "provider": {"provider_id": "qwen", "model_id": "qwen3.5-ocr",
                "provider_version": "1.0.0", "processor_region": "cn-beijing",
                "service_id": "qwen-visual-ocr", "network_capable": True},
            "pages": [page],
        }

    def _visual_verifier(self):
        return CanonicalJsonArtifactVerifier(
            artifact_kind="VISUAL_PAGE_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-visual-page-candidate-bundle-v1",),
            payload_kind="VISUAL_PAGE_REVIEW_CANDIDATE",
        )

    def _managed_visual(self, payload):
        return ManagedArtifactRead(
            artifact_id=str(uuid4()), artifact_kind="VISUAL_PAGE_REVIEW_CANDIDATE",
            content=canonical(payload), source_input_hash=payload["task_input_hash"],
            object_receipt_hash=digest("visual-object"), media_type="application/json",
        )

    def test_first_release_builder_is_the_single_policy_identity(self):
        verifier = build_first_release_case_agent_run_verifier(
            artifact_access=_ArtifactAccess({}),
            clock=lambda: self.now,
        )
        self.assertEqual(verifier.policy_hash, FIRST_RELEASE_VERIFIER_POLICY_HASH)
        self.assertEqual(
            first_release_verifier_policy_hash(),
            FIRST_RELEASE_VERIFIER_POLICY_HASH,
        )
        self.assertIn(
            "PUBLIC_RESEARCH_LEADS_CANDIDATE",
            verifier._artifact_verifiers,
        )
        self.assertIn("VISUAL_PAGE_REVIEW_CANDIDATE", verifier._artifact_verifiers)

    def test_visual_verifier_accepts_strict_review_only_candidate(self):
        payload = self._visual_payload()
        receipt = self._visual_verifier().verify(self._managed_visual(payload))
        self.assertEqual(receipt.artifact_kind, "VISUAL_PAGE_REVIEW_CANDIDATE")
        self.assertEqual(
            receipt.declared_external_request_id, payload["external_request_id"]
        )

    def test_visual_verifier_rejects_unknown_formal_and_hash_misbinding(self):
        base = self._visual_payload()
        mutations = []
        value = json.loads(canonical(base)); value["browser_path"] = "/tmp/a.png"; mutations.append(value)
        value = json.loads(canonical(base)); value["formal_fact"] = True; mutations.append(value)
        value = json.loads(canonical(base)); value["pages"][0]["request_hash"] = digest("wrong"); mutations.append(value)
        value = json.loads(canonical(base)); value["pages"][0]["projection_hash"] = digest("wrong"); mutations.append(value)
        value = json.loads(canonical(base)); value["pages"][0]["candidate_hash"] = digest("wrong"); mutations.append(value)
        value = json.loads(canonical(base)); value["pages"][0]["width"] = 20001; mutations.append(value)
        value = json.loads(canonical(base)); value["provenance"]["matter_version"] = 2; mutations.append(value)
        value = json.loads(canonical(base)); value["binding_hash"] = digest("wrong"); mutations.append(value)
        for payload in mutations:
            with self.subTest(keys=tuple(payload)):
                with self.assertRaisesRegex(Exception, "ARTIFACT_(REVIEW|VISUAL)_"):
                    self._visual_verifier().verify(self._managed_visual(payload))

    def test_visual_verifier_rejects_non_finite_nested_numbers(self):
        for field, value in (("confidence", float("nan")), ("x", float("inf"))):
            payload = self._visual_payload()
            block = payload["pages"][0]["text_blocks"][0]
            if field == "confidence":
                block["confidence"] = value
            else:
                block["region"][field] = value
            artifact = self._managed_visual(payload)
            # Construct non-standard JSON bytes directly: the JSON decoder's
            # parse_constant hook must reject these before hash validation.
            artifact = replace(
                artifact,
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            with self.subTest(field=field):
                with self.assertRaisesRegex(Exception, "ARTIFACT_JSON_INVALID"):
                    self._visual_verifier().verify(artifact)

    def test_public_research_verifier_accepts_only_review_leads(self):
        payload = self._public_research_payload()
        receipt = self._public_research_verifier().verify(
            self._managed_public_research(payload)
        )
        self.assertEqual(
            receipt.declared_source_input_hash, payload["task_input_hash"]
        )

    def test_public_research_verifier_rejects_noncanonical_and_unknown_fields(self):
        payload = self._public_research_payload()
        noncanonical = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        with self.assertRaisesRegex(Exception, "ARTIFACT_JSON_NOT_CANONICAL"):
            self._public_research_verifier().verify(
                self._managed_public_research(payload, content=noncanonical)
            )
        for target in (payload, payload["query"], payload["leads"][0]):
            mutated = json.loads(canonical(payload))
            if target is payload:
                mutated["model_claim"] = "law"
            elif target is payload["query"]:
                mutated["query"]["raw_question"] = "private"
            else:
                mutated["leads"][0]["provider_score"] = 1
            with self.subTest(fields=sorted(target)):
                with self.assertRaisesRegex(
                    Exception, "ARTIFACT_PUBLIC_RESEARCH_.*INVALID"
                ):
                    self._public_research_verifier().verify(
                        self._managed_public_research(mutated)
                    )

    def test_public_research_verifier_rejects_formal_or_legal_effect_claims(self):
        for field in (
            "formal_fact",
            "formal_transaction",
            "legal_conclusion",
            "evidence_decision",
            "legal_effect_confirmed",
        ):
            payload = self._public_research_payload(**{field: True})
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    Exception,
                    "ARTIFACT_(REVIEW_CONTRACT|PUBLIC_RESEARCH_CONTRACT)_INVALID",
                ):
                    self._public_research_verifier().verify(
                        self._managed_public_research(payload)
                    )

    def test_public_research_verifier_validates_query_provider_request_and_count(self):
        base = self._public_research_payload()
        mutations = []
        query = dict(base["query"])
        query["public_terms"] = ["民间借贷", "13800000000"]
        mutations.append({"query": query})
        query = dict(base["query"])
        query["query_hash"] = "not-a-sha"
        mutations.append({"query": query})
        mutations.extend(
            (
                {"provider_id": "browser-selected-provider"},
                {"external_request_id": "request-from-browser"},
                {"leads": []},
                {"leads": base["leads"] * 21},
            )
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                payload = self._public_research_payload(**mutation)
                with self.assertRaisesRegex(
                    Exception, "ARTIFACT_PUBLIC_RESEARCH_.*INVALID"
                ):
                    self._public_research_verifier().verify(
                        self._managed_public_research(payload)
                    )

    def test_public_research_verifier_validates_url_authority_status_and_signals(self):
        base = self._public_research_payload()
        lead_mutations = (
            {"url": "http://flk.npc.gov.cn/detail.html"},
            {"url": "https://user:password@flk.npc.gov.cn/detail.html"},
            {"url": "https://flk.npc.gov.cn/detail.html#model-prompt"},
            {"url": "https://flk.npc.gov.cn:443/detail.html"},
            {"url": "https://flk.npc.gov.cn/?token=secret"},
            {"authority_class": "MODEL_ASSUMED_AUTHORITY"},
            {"official_source_id": "SUPREME_PEOPLES_COURT"},
            {"publisher": "模型声称的发布者"},
            {"official_domain": False},
            {"status": "LEGAL_AUTHORITY_CONFIRMED"},
            {"prompt_injection_signals": ["NEW_UNREVIEWED_SIGNAL"]},
            {
                "prompt_injection_signals": [
                    "IGNORE_PRIOR_INSTRUCTIONS",
                    "IGNORE_PRIOR_INSTRUCTIONS",
                ]
            },
        )
        for mutation in lead_mutations:
            lead = {**base["leads"][0], **mutation}
            payload = self._public_research_payload(leads=[lead])
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(
                    Exception, "ARTIFACT_PUBLIC_RESEARCH_.*INVALID"
                ):
                    self._public_research_verifier().verify(
                        self._managed_public_research(payload)
                    )

    def test_public_research_verifier_allows_enumerated_prompt_injection_as_data(self):
        base = self._public_research_payload()
        lead = {
            **base["leads"][0],
            "prompt_injection_signals": [
                "IGNORE_PRIOR_INSTRUCTIONS",
                "TOOL_OR_COMMAND_REQUEST",
            ],
        }
        payload = self._public_research_payload(leads=[lead])
        receipt = self._public_research_verifier().verify(
            self._managed_public_research(payload)
        )
        self.assertEqual(
            receipt.artifact_kind, "PUBLIC_RESEARCH_LEADS_CANDIDATE"
        )

    def test_first_release_run_verifier_accepts_network_research_candidate(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        state = self.helper.start_ready_task(state, sequence=4)
        payload = self._public_research_payload(
            task_input_hash=task.input_hash
        )
        encoded = canonical(payload)
        artifact = ArtifactReceipt(
            artifact_id=str(uuid4()),
            artifact_kind="PUBLIC_RESEARCH_LEADS_CANDIDATE",
            content_hash=digest(encoded),
            byte_size=len(encoded),
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        result = self.helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.SUCCEEDED,
            external_state=ExternalSubmissionState.SUBMITTED,
            external_request_id=payload["external_request_id"],
            artifacts=(artifact,),
            external_calls=1,
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                5,
                # Approval and task start already consumed sequences 3 and 4.
                # The final result is the next durable event.
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(result),
            ),
        )
        receipt = build_first_release_case_agent_run_verifier(
            artifact_access=_ArtifactAccess({artifact.artifact_id: encoded}),
            clock=lambda: self.now,
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        self.assertEqual(receipt.outcome, VerificationOutcome.PASSED)

    def test_first_release_run_verifier_rejects_research_request_misbinding(self):
        task = self.helper.external_task()
        state = self.helper.state_with_graph(self.helper.graph((task,)))
        state = self.helper.approve(state, task.task_id, sequence=3)
        state = self.helper.start_ready_task(state, sequence=4)
        payload = self._public_research_payload(
            task_input_hash=task.input_hash
        )
        encoded = canonical(payload)
        artifact = ArtifactReceipt(
            artifact_id=str(uuid4()),
            artifact_kind="PUBLIC_RESEARCH_LEADS_CANDIDATE",
            content_hash=digest(encoded),
            byte_size=len(encoded),
            source_input_hash=task.input_hash,
            managed_derivative=False,
        )
        result = self.helper.result_receipt(
            state,
            task_id=task.task_id,
            status=ResultStatus.SUCCEEDED,
            external_state=ExternalSubmissionState.SUBMITTED,
            external_request_id=str(uuid4()),
            artifacts=(artifact,),
            external_calls=1,
        )
        state = reduce_agent_event(
            state,
            self.helper.event(
                5,
                AgentEventType.TASK_RESULT_RECORDED,
                TaskResultPayload(result),
            ),
        )
        receipt = build_first_release_case_agent_run_verifier(
            artifact_access=_ArtifactAccess({artifact.artifact_id: encoded}),
            clock=lambda: self.now,
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
        self.assertEqual(
            receipt.error_code, "ARTIFACT_FORMAT_RECEIPT_MISMATCH"
        )

    def test_policy_hash_binds_json_schema_and_payload_contract(self):
        access = _ArtifactAccess({})
        base = CanonicalJsonArtifactVerifier(
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-pdf-text-candidate-v1",),
            payload_kind="PDF_TEXT_REVIEW_CANDIDATE",
        )
        generic = CanonicalJsonArtifactVerifier(
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-pdf-text-candidate-v1",),
        )
        changed_schema = CanonicalJsonArtifactVerifier(
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-pdf-text-candidate-v2",),
            payload_kind="PDF_TEXT_REVIEW_CANDIDATE",
        )
        hashes = {
            CaseAgentRunVerifier(
                verifier_id="case-run-verifier",
                verifier_version="1.0.0",
                artifact_access=access,
                artifact_verifiers={item.artifact_kind: item},
            ).policy_hash
            for item in (base, generic, changed_schema)
        }
        self.assertEqual(len(hashes), 3)

    def test_first_release_format_verifier_rejects_schema_only_model_json(self):
        state, artifact, _payload = self._state()
        ungrounded = canonical(
            {
                "schema_version": "agent-pdf-text-candidate-v1",
                "task_input_hash": state.graph.tasks[0].input_hash,
                "source_hash": digest("source"),
                "review_status": "NEEDS_LAWYER_REVIEW",
                "formal_fact": True,
                "formal_transaction": False,
                "legal_conclusion": False,
                "evidence_decision": False,
                "pages": [],
            }
        )
        broken_artifact = replace(
            artifact,
            content_hash=digest(ungrounded),
            byte_size=len(ungrounded),
        )
        runtime = state.tasks[0]
        broken_final = replace(runtime.receipts[-1], artifacts=(broken_artifact,))
        broken = replace(
            state,
            tasks=(replace(runtime, receipts=(broken_final,)),),
            artifacts=(broken_artifact,),
        )
        receipt = build_first_release_case_agent_run_verifier(
            artifact_access=_ArtifactAccess(
                {broken_artifact.artifact_id: ungrounded}
            ),
            clock=lambda: self.now,
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=broken,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
        self.assertEqual(
            receipt.error_code, "ARTIFACT_REVIEW_CONTRACT_INVALID"
        )

    def test_canonical_json_verifier_rejects_duplicate_keys(self):
        verifier = CanonicalJsonArtifactVerifier(
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            allowed_schema_versions=("agent-pdf-text-candidate-v1",),
        )
        content = (
            b'{"schema_version":"agent-pdf-text-candidate-v1",'
            b'"schema_version":"agent-pdf-text-candidate-v1"}'
        )
        managed = ManagedArtifactRead(
            artifact_id=str(uuid4()),
            artifact_kind="PDF_TEXT_REVIEW_CANDIDATE",
            content=content,
            source_input_hash=digest("input"),
            object_receipt_hash=digest("object"),
            media_type="application/json",
        )
        with self.assertRaisesRegex(
            Exception, "ARTIFACT_JSON_INVALID"
        ):
            verifier.verify(managed)

    def test_same_actor_is_rejected_by_worker_constructor(self):
        state, artifact, payload = self._state()
        task = state.graph.tasks[0]
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        with self.assertRaisesRegex(ValueError, "cannot serve as its own"):
            CaseAgentWorker(
                worker_id="worker-1",
                actor=self.execution_actor,
                store=_ExecutionStore(state),
                snapshot_provider=_SnapshotProvider(SimpleNamespace()),
                planner=_Planner(),
                planner_compiler=_Compiler(object()),
                adapters={task.skill.tool_id: adapter},
                verifier=self._verifier(_ArtifactAccess({artifact.artifact_id: payload})),
                verifier_actor=self.execution_actor,
                verifier_store=object(),
            )

    def test_missing_artifact_verifier_fails_closed(self):
        state, artifact, payload = self._state()
        receipt = self._verifier(
            _ArtifactAccess({artifact.artifact_id: payload}), registered=False
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
        self.assertEqual(receipt.error_code, "ARTIFACT_VERIFIER_NOT_REGISTERED")

    def test_hash_or_lineage_mismatch_fails(self):
        state, artifact, payload = self._state()
        for access in (
            _ArtifactAccess({artifact.artifact_id: payload + b" "}),
            _ArtifactAccess(
                {artifact.artifact_id: payload}, mutate_lineage=True
            ),
        ):
            receipt = self._verifier(access).verify(
                verification_attempt_id=str(uuid4()),
                state=state,
                verifier_actor_id=self.verifier_actor.actor_id,
                execution_actor_id=self.execution_actor.actor_id,
            )
            self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
            self.assertEqual(receipt.error_code, "ARTIFACT_LINEAGE_MISMATCH")

    def test_receipt_rebinds_to_current_task_and_artifact_manifest(self):
        state, artifact, payload = self._state()
        receipt = self._verifier(
            _ArtifactAccess({artifact.artifact_id: payload})
        ).verify(
            verification_attempt_id=str(uuid4()),
            state=state,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        receipt.validate_against_state(state)
        with self.assertRaisesRegex(ValueError, "current Agent run"):
            receipt.validate_against_state(
                replace(
                    state,
                    snapshot=replace(
                        state.snapshot, snapshot_hash=digest("changed snapshot")
                    ),
                )
            )
        bad_lineage = replace(
            receipt.artifact_lineage[0], task_id=str(uuid4())
        )
        forged = replace(receipt, artifact_lineage=(bad_lineage,))
        # Rehashing is intentionally not available to callers; even a forged
        # dataclass cannot pass the canonical receipt check.
        with self.assertRaisesRegex(ValueError, "hash"):
            forged.validate_against_state(state)

    def test_unknown_receipt_blocks_verification(self):
        state, artifact, payload = self._state()
        runtime = state.tasks[0]
        unknown = replace(
            runtime.receipts[-1],
            status=ResultStatus.UNKNOWN,
            output_hash=None,
            external_submission_state=ExternalSubmissionState.UNKNOWN,
            external_request_id="provider-unknown",
            artifacts=(),
        )
        broken = replace(
            state,
            tasks=(replace(runtime, receipts=(unknown,)),),
            artifacts=(),
        )
        receipt = self._verifier(_ArtifactAccess({})).verify(
            verification_attempt_id=str(uuid4()),
            state=broken,
            verifier_actor_id=self.verifier_actor.actor_id,
            execution_actor_id=self.execution_actor.actor_id,
        )
        self.assertEqual(receipt.outcome, VerificationOutcome.FAILED)
        self.assertEqual(receipt.error_code, "TASK_FINAL_RECEIPT_BINDING_MISMATCH")

    def _worker(self, state, artifact, payload, verifier):
        task = state.graph.tasks[0]
        adapter = _Adapter(
            self.helper.adapters[task.skill.tool_id],
            outcome=TaskAdapterOutcome(
                ResultStatus.SUCCEEDED,
                ExternalSubmissionState.NOT_APPLICABLE,
                digest("output"),
                None,
                None,
                1,
                0,
                0,
            ),
        )
        verification_store = _VerificationStore(
            state, self.verifier_actor, self.execution_actor, self.helper
        )
        worker = CaseAgentWorker(
            worker_id="worker-1",
            actor=self.execution_actor,
            store=_ExecutionStore(state),
            snapshot_provider=_SnapshotProvider(SimpleNamespace()),
            planner=_Planner(),
            planner_compiler=_Compiler(object()),
            adapters={task.skill.tool_id: adapter},
            verifier=verifier,
            verifier_actor=self.verifier_actor,
            verifier_store=verification_store,
        )
        return worker, verification_store

    def test_worker_persists_started_then_passed(self):
        state, artifact, payload = self._state()
        worker, store = self._worker(
            state,
            artifact,
            payload,
            self._verifier(_ArtifactAccess({artifact.artifact_id: payload})),
        )
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.VERIFICATION_PASSED)
        self.assertEqual(
            store.events,
            [AgentEventType.VERIFICATION_STARTED, AgentEventType.VERIFICATION_PASSED],
        )
        self.assertEqual(len(store.receipts), 1)

    def test_worker_persists_started_then_known_failed(self):
        state, artifact, payload = self._state()
        worker, store = self._worker(
            state,
            artifact,
            payload,
            self._verifier(_ArtifactAccess({artifact.artifact_id: payload}), registered=False),
        )
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.VERIFICATION_FAILED)
        self.assertEqual(store.receipts[0].outcome, VerificationOutcome.FAILED)

    def test_infrastructure_crash_keeps_started_without_terminal_result(self):
        state, artifact, payload = self._state()
        worker, store = self._worker(
            state,
            artifact,
            payload,
            self._verifier(
                _ArtifactAccess(
                    {artifact.artifact_id: payload},
                    error=TimeoutError("storage unavailable"),
                )
            ),
        )
        result = worker.process_run_once(
            matter_id=self.helper.matter_id, run_id=self.helper.run_id
        )
        self.assertEqual(result.step, AgentWorkerStep.VERIFICATION_REQUIRED)
        self.assertEqual(store.events, [AgentEventType.VERIFICATION_STARTED])
        self.assertEqual(store.receipts, [])

    def test_version_conflict_does_not_claim_completion(self):
        state, artifact, payload = self._state()
        worker, store = self._worker(
            state,
            artifact,
            payload,
            self._verifier(_ArtifactAccess({artifact.artifact_id: payload})),
        )
        store.conflict = True
        with self.assertRaisesRegex(RuntimeError, "version conflict"):
            worker.process_run_once(
                matter_id=self.helper.matter_id, run_id=self.helper.run_id
            )
        self.assertEqual(store.events, [AgentEventType.VERIFICATION_STARTED])
        self.assertEqual(store.receipts, [])


if __name__ == "__main__":
    unittest.main()
