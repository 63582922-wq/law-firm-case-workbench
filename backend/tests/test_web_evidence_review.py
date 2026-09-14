from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import unittest

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_evidence_review import WebEvidenceReviewBlocked, WebEvidenceReviewService
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.evidence_manifest import PageDisposition
from case_kernel.evidence_manifest_postgres import PersistentEvidencePageListPage, PersistentEvidenceReviewSummary
from case_kernel.evidence_manifest_postgres import (
    AgentEvidenceDecisionCandidateExclusion,
    AgentEvidenceDecisionCandidateStaging,
)
from case_kernel.models import Actor, Role


def _identity(*, role: Role = Role.LEAD_LAWYER, method: AuthenticationMethod = AuthenticationMethod.OIDC_MFA) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(actor_id=str(uuid4()), firm_id=str(uuid4()), roles=frozenset({role})),
        session_id=str(uuid4()),
        issuer="https://id.example.test/oidc",
        authentication_method=method,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.matter_id = str(uuid4())
        self.page_id = str(uuid4())
        self.decision_id = str(uuid4())
        self.annotation_id = str(uuid4())
        self.manifest_id = str(uuid4())

    def get_evidence_review_summary(self, **kwargs):
        self.calls.append(("summary", kwargs))
        return PersistentEvidenceReviewSummary(
            matter_id=self.matter_id,
            version=7,
            summary_hash="a" * 64,
            manifest_readiness_hash="b" * 64,
            total_pages=1,
            unresolved_page_count=1,
            pending_decision_count=0,
            unresolved_duplicate_count=0,
            original_files=({
                "evidence_file_id": str(uuid4()),
                "original_label": "微信记录.pdf",
                "original_file_sha256": "c" * 64,
                "byte_size": 123,
                "media_type": "application/pdf",
                "page_count": 1,
            },),
            duplicate_groups=(),
            locked_manifest={
                "manifest_id": self.manifest_id,
                "status": "LOCKED",
                "content_hash": "d" * 64,
                "total_pages": 1,
                "included_pages": 1,
                "excluded_pages": 0,
            },
            derivatives=({
                "derivative_id": str(uuid4()),
                "artifact_type": "SUBMISSION_PDF",
                "artifact_sha256": "e" * 64,
                "page_count": 1,
                "status": "VERIFIED",
            },),
            derivative_runs=(),
        )

    def list_evidence_page(self, **kwargs):
        self.calls.append(("pages", kwargs))
        return PersistentEvidencePageListPage(
            matter_id=self.matter_id,
            matter_version=7,
            total_count=1,
            items=({
                "evidence_page_id": self.page_id,
                "evidence_file_id": str(uuid4()),
                "original_label": "微信记录.pdf",
                "page_number": 1,
                "decision": None,
                "pending_decision": None,
                "annotations": ({
                    "annotation_id": self.annotation_id,
                    "purpose": "SUBMISSION_HIGHLIGHT",
                    "x0": "0.1000",
                    "y0": "0.2000",
                    "x1": "0.8000",
                    "y1": "0.9000",
                    "label": "还款记录",
                    "status": "APPROVED",
                },),
            },),
            next_cursor=None,
            has_more=False,
        )

    def _receipt(self, kwargs, command_name, object_type, object_id):
        return CaseLedgerCommandReceipt(
            command_name=command_name,
            idempotency_key=str(kwargs["idempotency_key"]),
            matter_id=str(kwargs["matter_id"]),
            matter_version=8,
            audit_event_id=str(uuid4()),
            object_type=object_type,
            object_id=object_id,
        )

    def create_page_decision_candidate(self, **kwargs):
        self.calls.append(("decision-candidate", kwargs))
        return self._receipt(kwargs, "CREATE_PAGE_DECISION_CANDIDATE", "EVIDENCE_PAGE_DECISION", self.decision_id)

    def approve_page_decision(self, **kwargs):
        self.calls.append(("decision-confirm", kwargs))
        return self._receipt(kwargs, "APPROVE_PAGE_DECISION", "EVIDENCE_PAGE_DECISION", kwargs["decision_id"])

    def approve_page_decisions_batch(self, **kwargs):
        self.calls.append(("decision-batch-confirm", kwargs))
        return self._receipt(kwargs, "APPROVE_PAGE_DECISIONS_BATCH", "EVIDENCE_PAGE_DECISION_BATCH", str(uuid4()))

    def stage_low_risk_agent_page_decision_candidates(self, **kwargs):
        self.calls.append(("agent-decision-candidates", kwargs))
        first_decision, second_decision = str(uuid4()), str(uuid4())
        return AgentEvidenceDecisionCandidateStaging(
            receipt=self._receipt(
                kwargs,
                "STAGE_AGENT_EVIDENCE_PAGE_DECISION_CANDIDATES",
                "EVIDENCE_PAGE_DECISION_CANDIDATE_BATCH",
                str(uuid4()),
            ),
            run_id=kwargs["run_id"],
            decision_ids=(first_decision, second_decision),
            page_ids=(self.page_id, str(uuid4())),
            include_count=1,
            exclude_count=1,
            exclusions=(AgentEvidenceDecisionCandidateExclusion("OCR_REQUIRED", (str(uuid4()),)),),
        )

    def create_annotation_candidate(self, **kwargs):
        self.calls.append(("annotation-candidate", kwargs))
        return self._receipt(kwargs, "CREATE_ANNOTATION_CANDIDATE", "EVIDENCE_ANNOTATION", self.annotation_id)

    def approve_annotation(self, **kwargs):
        self.calls.append(("annotation-confirm", kwargs))
        return self._receipt(kwargs, "APPROVE_ANNOTATION", "EVIDENCE_ANNOTATION", kwargs["annotation_id"])

    def lock_manifest(self, **kwargs):
        self.calls.append(("manifest-lock", kwargs))
        return self._receipt(kwargs, "LOCK_EVIDENCE_MANIFEST", "EVIDENCE_MANIFEST", str(uuid4()))

    def enqueue_derivative_run(self, **kwargs):
        self.calls.append(("derivative-enqueue", kwargs))
        return self._receipt(kwargs, "ENQUEUE_EVIDENCE_DERIVATIVE_RUN", "EVIDENCE_DERIVATIVE_RUN", str(uuid4()))


class WebEvidenceReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeEvidenceStore()
        self.service = WebEvidenceReviewService(evidence_store=self.store)
        self.identity = _identity()

    def test_projection_removes_private_hashes_and_normalizes_decimal_boxes(self) -> None:
        summary = self.service.summary(identity=self.identity, matter_id=self.store.matter_id)
        self.assertTrue(summary["can_batch_confirm_page_decisions"])
        encoded = repr(summary)
        self.assertNotIn("original_file_sha256", encoded)
        self.assertNotIn("artifact_sha256", encoded)
        self.assertNotIn("content_hash", encoded)

        collaborator_summary = self.service.summary(
            identity=_identity(role=Role.COLLABORATING_LAWYER), matter_id=self.store.matter_id
        )
        self.assertFalse(collaborator_summary["can_batch_confirm_page_decisions"])

        pages = self.service.pages(identity=self.identity, matter_id=self.store.matter_id)
        annotation = pages.items[0]["annotations"][0]
        self.assertEqual(annotation["x0"], 0.1)
        self.assertEqual(annotation["y1"], 0.9)

    def test_confirmation_hash_is_server_derived_and_not_browser_input(self) -> None:
        candidate = self.service.create_page_decision_candidate(
            identity=self.identity,
            matter_id=self.store.matter_id,
            evidence_page_id=self.store.page_id,
            expected_version=7,
            disposition="INCLUDE",
            reason="与争议款项相关",
            idempotency_key="decision-candidate-0001",
        )
        self.assertEqual(candidate.object_id, self.store.decision_id)
        confirmed = self.service.confirm_page_decision(
            identity=self.identity,
            matter_id=self.store.matter_id,
            decision_id=self.store.decision_id,
            expected_version=8,
            idempotency_key="decision-confirm-0001",
        )
        self.assertEqual(confirmed.object_id, self.store.decision_id)
        kwargs = self.store.calls[-1][1]
        self.assertRegex(str(kwargs["approval_hash"]), r"^[0-9a-f]{64}$")
        self.assertNotIn("approval_hash", {"expected_version": 8})

    def test_batch_confirmation_is_canonical_server_bound_and_lead_only(self) -> None:
        second_decision_id = str(uuid4())
        receipt = self.service.confirm_page_decisions_batch(
            identity=self.identity,
            matter_id=self.store.matter_id,
            decision_ids=(second_decision_id, self.store.decision_id),
            expected_version=7,
            idempotency_key="decision-batch-confirm-0001",
        )
        self.assertEqual(receipt.object_type, "EVIDENCE_PAGE_DECISION_BATCH")
        kwargs = self.store.calls[-1][1]
        self.assertEqual(kwargs["decision_ids"], tuple(sorted((self.store.decision_id, second_decision_id))))
        self.assertRegex(str(kwargs["batch_hash"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(kwargs["approval_hash"]), r"^[0-9a-f]{64}$")
        self.assertNotEqual(kwargs["batch_hash"], kwargs["approval_hash"])
        with self.assertRaisesRegex(WebEvidenceReviewBlocked, "主办律师"):
            self.service.confirm_page_decisions_batch(
                identity=_identity(role=Role.COLLABORATING_LAWYER),
                matter_id=self.store.matter_id,
                decision_ids=(self.store.decision_id,),
                expected_version=7,
                idempotency_key="decision-batch-confirm-0002",
            )

    def test_batch_confirmation_rejects_duplicate_or_oversized_input_before_store(self) -> None:
        call_count = len(self.store.calls)
        with self.assertRaisesRegex(WebEvidenceReviewBlocked, "重复"):
            self.service.confirm_page_decisions_batch(
                identity=self.identity,
                matter_id=self.store.matter_id,
                decision_ids=(self.store.decision_id, self.store.decision_id),
                expected_version=7,
                idempotency_key="decision-batch-confirm-0003",
            )
        self.assertEqual(len(self.store.calls), call_count)
        with self.assertRaisesRegex(WebEvidenceReviewBlocked, "1 至 100"):
            self.service.confirm_page_decisions_batch(
                identity=self.identity,
                matter_id=self.store.matter_id,
                decision_ids=tuple(str(uuid4()) for _ in range(101)),
                expected_version=7,
                idempotency_key="decision-batch-confirm-0004",
            )
        self.assertEqual(len(self.store.calls), call_count)

    def test_agent_adoption_accepts_only_opaque_run_and_server_returns_candidates(self) -> None:
        run_id = str(uuid4())
        result = self.service.stage_agent_page_decision_candidates(
            identity=self.identity,
            matter_id=self.store.matter_id,
            run_id=run_id,
            expected_version=7,
            idempotency_key="agent-candidate-stage-0001",
        )
        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(result["include_count"], 1)
        self.assertEqual(result["exclude_count"], 1)
        call = self.store.calls[-1]
        self.assertEqual(call[0], "agent-decision-candidates")
        self.assertEqual(
            set(call[1]),
            {"matter_id", "run_id", "actor", "expected_version", "idempotency_key"},
        )
        for forbidden in ("disposition", "confidence", "input_hash", "source_file_sha256"):
            self.assertNotIn(forbidden, call[1])

    def test_non_mfa_or_read_only_roles_cannot_write(self) -> None:
        with self.assertRaises(WebEvidenceReviewBlocked):
            self.service.summary(identity=_identity(method=AuthenticationMethod.OS_BOUND_LOCAL_SESSION), matter_id=self.store.matter_id)
        with self.assertRaises(WebEvidenceReviewBlocked):
            self.service.create_annotation_candidate(
                identity=_identity(role=Role.ASSISTANT),
                matter_id=self.store.matter_id,
                evidence_page_id=self.store.page_id,
                expected_version=7,
                x0=0.1,
                y0=0.1,
                x1=0.9,
                y1=0.9,
                label="红框",
                idempotency_key="annotation-candidate-01",
            )

    def test_lock_uses_server_confirmation_and_current_readiness_hash(self) -> None:
        receipt = self.service.lock_manifest(
            identity=self.identity,
            matter_id=self.store.matter_id,
            expected_version=7,
            readiness_hash="b" * 64,
            idempotency_key="manifest-lock-0001",
        )
        self.assertEqual(receipt.object_type, "EVIDENCE_MANIFEST")
        self.assertRegex(self.store.calls[-1][1]["approval_hash"], r"^[0-9a-f]{64}$")

    def test_derivative_enqueue_reads_manifest_hash_server_side(self) -> None:
        receipt = self.service.enqueue_derivative_run(
            identity=self.identity,
            matter_id=self.store.matter_id,
            manifest_id=self.store.manifest_id,
            expected_version=7,
            idempotency_key="derivative-enqueue-01",
        )
        self.assertEqual(receipt.object_type, "EVIDENCE_DERIVATIVE_RUN")
        kwargs = self.store.calls[-1][1]
        self.assertEqual(kwargs["manifest_content_hash"], "d" * 64)
        self.assertRegex(kwargs["approval_hash"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
