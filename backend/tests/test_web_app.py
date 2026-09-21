from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4
import unittest

from fastapi import Request
from fastapi.testclient import TestClient

from case_api.persistent_identity import AuthenticationMethod, ServerIdentityContext
from case_api.web_common_material_upload import (
    CommonMaterialAdmissionReceipt,
    CommonMaterialUploadReconciliationRequired,
    CommonMaterialUploadReservationReceipt,
    CommonMaterialUploadStatusReceipt,
)
from case_api.web_case_posture import WebCasePostureCompleteReceipt
from case_api.web_case_posture import (
    WebCasePostureBlocked,
    WebCasePostureCommandReceipt,
    WebCasePostureProfile,
    WebCasePostureState,
    WebCasePostureStatus,
)
from case_api.web_material_upload import PostObjectStoreReconciliationRequired
from case_api.web_app import (
    WebAgentCandidateBatchResponse,
    WebAgentCandidateResponse,
    WebDynamicCasePlanDecisionReceipt,
    WebDynamicCasePlanActivationReceipt,
    WebDynamicCasePlanItemResponse,
    WebDynamicCasePlanReferenceResponse,
    WebDynamicCasePlanResponse,
    WebAgentRunResponse,
    WebAgentTaskResponse,
    WebRepresentationProfileResponse,
    WebCaseAgentApprovalResponse,
    WebCaseAgentArtifactResponse,
    WebCaseAgentCompletionReceipt,
    WebCaseAgentCompletionResponse,
    WebCaseAgentControlRunResponse,
    WebCaseAgentCurrentWorkResponse,
    WebCaseAgentDecisionOptionResponse,
    WebCaseAgentDecisionResponse,
    WebApiDependencies,
    WebApiSettings,
    WebMaterialArchiveReceipt,
    WebMaterialUploadStatusResponse,
    WebRequestBlocked,
    WebUploadReceipt,
    WebUploadSlotResponse,
    _validate_archive_receipt,
    _validate_upload_slot,
    _project_money,
    _project_rate,
    create_web_app,
)
from case_api.web_session import CookieDirective, WebSessionGrant
from case_api.web_case_agent_artifacts import (
    WebCaseAgentArtifactReview,
    WebCaseAgentArtifactReviewItem,
    WebCaseAgentArtifactReviewSection,
    WebCaseAgentArtifactSource,
)
from case_api.web_document_draft_delivery import WebDocumentDraftDelivery
from case_api.web_agent_ledger_extraction_review import (
    WebAgentLedgerExtractionBatch,
    WebAgentLedgerExtractionCandidate,
    WebAgentLedgerExtractionConfirmationReceipt,
    WebAgentLedgerExtractionExcerpt,
    WebAgentLedgerExceptionAction,
    WebAgentLedgerExceptionDecisionReceipt,
    WebAgentLedgerExceptionGroup,
    WebAgentLedgerExceptionMember,
    WebAgentLedgerExceptionMemberPage,
    WebAgentLedgerExceptionReasonOption,
    WebAgentLedgerReextractionCohortCapacityExceeded,
    WebAgentLedgerReextractionSourceWindowExceeded,
)
from case_api.web_agent_ledger_exception_followup import (
    WebAgentLedgerExceptionFollowup,
    WebAgentLedgerExceptionFollowupPage,
    WebAgentLedgerExceptionFollowupReceipt,
    WebAgentLedgerExceptionRecoveryReceipt,
    WebFollowupEvidencePageIdPage,
    WebAgentLedgerFollowupAction,
    WebManagedEvidenceSource,
    WebManagedEvidenceSourcePage,
)
from case_kernel.models import Actor, Role
from case_kernel.case_ledger_postgres import CaseLedgerCommandReceipt
from case_kernel.case_work_plan_postgres import CaseLedgerPersistenceBlocked
from case_kernel.calculation_engine import AllocationPolicy
from case_kernel.web_common_material_admission import (
    CommonMaterialAgentStatus,
    CommonMaterialFormat,
    CommonMaterialReviewStatus,
    CommonMaterialRoute,
)


def _identity(*, role: Role = Role.LEAD_LAWYER) -> ServerIdentityContext:
    now = datetime.now(timezone.utc)
    return ServerIdentityContext(
        actor=Actor(actor_id=str(uuid4()), firm_id=str(uuid4()), roles=frozenset({role})),
        session_id=str(uuid4()),
        issuer="https://id.example.test/oidc",
        authentication_method=AuthenticationMethod.OIDC_MFA,
        authenticated_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
    )


class _FakeOidcLogin:
    def __init__(self, grant: WebSessionGrant) -> None:
        self.grant = grant
        self.public_origin = "https://workbench.example.test"
        self.callbacks: list[tuple[tuple[str, str], ...]] = []

    def begin_authorization(self):
        return SimpleNamespace(authorization_url="https://id.example.test/authorize?opaque=1")

    def complete_callback(self, *, parameters):
        values = tuple(parameters)
        self.callbacks.append(values)
        return self.grant


class _FakeSessionAuthority:
    def __init__(self, identity: ServerIdentityContext) -> None:
        self.identity = identity
        self.revoked: list[str] = []

    async def resolve(self, request: Request) -> ServerIdentityContext:
        del request
        return self.identity

    def revoke(self, *, session_id: str) -> None:
        self.revoked.append(session_id)

    @staticmethod
    def clear_cookies():
        return (
            CookieDirective(name="__Host-lawcase_session", value="", max_age=0),
            CookieDirective(name="__Host-lawcase_csrf", value="", httponly=False, max_age=0),
        )


class _FakeMatterStore:
    def __init__(self) -> None:
        self.created: list[tuple[object, Actor, str]] = []
        self.rows: list[dict[str, object]] = []

    def create(self, *, matter, actor, idempotency_key: str):
        self.created.append((matter, actor, idempotency_key))
        self.rows.append(
            {
                "matter_id": matter.matter_id,
                "title": matter.title,
                "stage": "CREATED",
                "version": 1,
                "updated_at": datetime.now(timezone.utc),
                "material_count": 0,
            }
        )
        return SimpleNamespace(
            command_name="CREATE_MATTER",
            idempotency_key=idempotency_key,
            matter_id=matter.matter_id,
            matter_version=1,
            audit_event_id=str(uuid4()),
        )

    def list_accessible(self, *, actor: Actor):
        del actor
        return list(self.rows)


class _FakeCaseLedgerStore:
    def __init__(self) -> None:
        self.fact_id = str(uuid4())
        self.claim_id = str(uuid4())
        self.issue_id = str(uuid4())
        self.transaction_id = str(uuid4())

    def get_case_snapshot(self, *, matter_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            title="周雅丽诉民间借贷纠纷",
            stage="FACT_REVIEW",
            version=3,
            snapshot_hash="a" * 64,
            facts=({
                "fact_id": self.fact_id,
                "original_text": "借款本金已经实际交付。",
                "origin": "LAWYER_ENTERED",
                "status": "CONFIRMED",
                "evidence_count": 1,
                "decision_hash": "b" * 64,
            },),
            claims=({
                "claim_id": self.claim_id,
                "original_claim_text": "请求返还借款本金及利息。",
                "claimed_amount": "100000",
                "currency": "CNY",
                "status": "CONFIRMED_SCOPE",
                "evidence_count": 1,
                "confirmation_hash": "c" * 64,
                "response": {"position": "DISPUTED", "partial_amount": "80000", "currency": "CNY"},
            },),
            issues=({
                "issue_id": self.issue_id,
                "question": "利息应当适用何种期间和上限？",
                "status": "CANDIDATE",
                "claim_ids": (self.claim_id,),
                "confirmed_fact_ids": (self.fact_id,),
            },),
            transactions=({
                "transaction_id": self.transaction_id,
                "local_date": date(2020, 8, 20),
                "date_precision": "DAY",
                "amount": "10000",
                "currency": "CNY",
                "direction": "INBOUND",
                "payer_label": "寒雪青松",
                "payee_label": "周雅丽",
                "channel": "WECHAT",
                "transaction_reference": "微信转账",
                "status": "CONFIRMED",
                "evidence_count": 1,
                "confirmation_hash": "d" * 64,
            },),
            payment_classifications=({
                "classification_id": str(uuid4()),
                "transaction_id": self.transaction_id,
                "origin": "ASSISTANT_ENTRY",
                "nature": "DISBURSEMENT",
                "same_day_sequence": None,
                "status": "APPROVED",
                "evidence_count": 1,
                "allocations": ({"obligation_id": "obligation-main", "amount": "10000", "currency": "CNY"},),
            },),
        )

    def decide_fact(self, **kwargs):
        self.decided = kwargs
        return CaseLedgerCommandReceipt(
            command_name="DECIDE_FACT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=4,
            audit_event_id=str(uuid4()),
            object_type="FACT",
            object_id=kwargs["fact_id"],
        )

    def confirm_claim_scope(self, **kwargs):
        self.claim_confirmation = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CONFIRM_CLAIM_SCOPE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=5,
            audit_event_id=str(uuid4()),
            object_type="CLAIM",
            object_id=kwargs["claim_id"],
        )

    def create_claim_candidate_from_confirmed_facts(self, **kwargs):
        self.claim_candidate = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CREATE_CLAIM_CANDIDATE_FROM_CONFIRMED_FACTS",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=4,
            audit_event_id=str(uuid4()),
            object_type="CLAIM",
            object_id=self.claim_id,
        )

    def set_claim_response(self, **kwargs):
        self.claim_response = kwargs
        return CaseLedgerCommandReceipt(
            command_name="SET_CLAIM_RESPONSE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=6,
            audit_event_id=str(uuid4()),
            object_type="CLAIM_RESPONSE",
            object_id=str(uuid4()),
        )

    def create_dispute_issue_candidate(self, **kwargs):
        self.issue_candidate = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CREATE_DISPUTE_ISSUE_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=6,
            audit_event_id=str(uuid4()),
            object_type="DISPUTE_ISSUE",
            object_id=self.issue_id,
        )

    def confirm_dispute_issue(self, **kwargs):
        self.issue_confirmation = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CONFIRM_DISPUTE_ISSUE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="DISPUTE_ISSUE",
            object_id=kwargs["issue_id"],
        )

    def confirm_transaction(self, **kwargs):
        self.transaction_confirmation = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CONFIRM_TRANSACTION",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=6,
            audit_event_id=str(uuid4()),
            object_type="TRANSACTION",
            object_id=kwargs["transaction_id"],
        )

    def create_payment_classification_candidate(self, **kwargs):
        self.payment_classification_candidate = kwargs
        return CaseLedgerCommandReceipt(
            command_name="CREATE_PAYMENT_CLASSIFICATION_CANDIDATE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="PAYMENT_CLASSIFICATION",
            object_id=str(uuid4()),
        )

    def approve_payment_classification(self, **kwargs):
        self.payment_classification_approval = kwargs
        return CaseLedgerCommandReceipt(
            command_name="APPROVE_PAYMENT_CLASSIFICATION",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=8,
            audit_event_id=str(uuid4()),
            object_type="PAYMENT_CLASSIFICATION",
            object_id=kwargs["classification_id"],
        )


class _FakeLegalStore:
    def __init__(self, *, current_bundle=None, bundle_reconfirmation=None) -> None:
        self.current_bundle = current_bundle
        self.bundle_reconfirmation = bundle_reconfirmation
        self.registration_calls: list[dict[str, object]] = []
        self.legal_event_calls: list[dict[str, object]] = []

    def register_reviewed_capture_snapshot(self, **kwargs):
        self.registration_calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="REGISTER_REVIEWED_CAPTURE_SNAPSHOT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=8,
            audit_event_id=str(uuid4()),
            object_type="LEGAL_SOURCE_SNAPSHOT",
            object_id=str(uuid4()),
        )

    def approve_legal_event(self, **kwargs):
        self.legal_event_calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="APPROVE_CASE_LEGAL_EVENT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=9,
            audit_event_id=str(uuid4()),
            object_type="CASE_LEGAL_EVENT",
            object_id=str(uuid4()),
        )

    def get_legal_review_snapshot(self, *, matter_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            matter_version=6,
            snapshot_hash="e" * 64,
            sources=({
                "snapshot_id": str(uuid4()),
                "source_id": "民间借贷司法解释",
                "publisher": "最高人民法院",
                "authority_level": "JUDICIAL_INTERPRETATION",
                "official_url": "https://www.court.gov.cn/fabu-xiangqing-123.html",
                "provision_locator": "第六条",
                "retrieved_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "content_sha256": "f" * 64,
                "verification_status": "VERIFIED",
                "license_status": "ACTIVE",
                "storage_object_key": "never-returned",
            },),
            rule_versions=({
                "rule_version_id": str(uuid4()),
                "rule_id": "PRIVATE_LENDING_INTEREST_CAP",
                "rule_version": "2020-08-20",
                "issue_key": "利息上限",
                "effective_from": date(2020, 8, 20),
                "effective_to": None,
                "trigger_event_kind": "CLAIM_FILED",
                "formula_kind": "LPR_MULTIPLE",
                "base_annual_rate": "3.85",
                "rate_multiplier": "4",
                "derived_annual_rate": "15.40",
                "status": "APPROVED",
            },),
            legal_events=(),
            fact_bindings=(),
            current_bundle=self.current_bundle,
            bundle_segments=(),
            bundle_reconfirmation=self.bundle_reconfirmation,
        )


class _FakeOfficialSourceCaptureStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.review_calls: list[dict[str, object]] = []
        self.run_id: str | None = None
        self.source_id = "SPC-PRIVATE-LENDING-2020-SECOND-REVISION"
        self.status = "REVIEW_REQUIRED"

    def get_snapshot(self, *, matter_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            matter_version=6,
            runs=(
                ({
                    "run_id": self.run_id,
                    "source_id": self.source_id,
                    "publisher": "最高人民法院",
                    "status": self.status,
                    "authorized_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
                    "retrieved_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
                    "final_url": "https://www.court.gov.cn/zixun/xiangqing/282621.html",
                    "parsed_summary": {"provisions": [{"provision_label": "第二十五条"}]},
                    "failure_code": None,
                },)
                if self.run_id is not None
                else ()
            ),
            reviews=(),
            snapshot_hash="a" * 64,
        )

    def queue_capture(self, **kwargs):
        self.calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="QUEUE_OFFICIAL_SOURCE_CAPTURE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="OFFICIAL_SOURCE_CAPTURE_RUN",
            object_id=str(uuid4()),
        )

    def review_capture(self, **kwargs):
        self.review_calls.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="REVIEW_OFFICIAL_SOURCE_CAPTURE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="OFFICIAL_SOURCE_CAPTURE_REVIEW",
            object_id=str(uuid4()),
        )


class _FakeFormalCalculationStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def get_current_calculation(self, *, matter_id: str, obligation_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            matter_version=6,
            snapshot_hash="1" * 64,
            scenario=None,
            run=None,
        )

    def create_formal_calculation(self, **kwargs):
        self.created.append(kwargs)
        return CaseLedgerCommandReceipt(
            command_name="CREATE_FORMAL_CALCULATION",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="CALCULATION_RUN",
            object_id=str(uuid4()),
        )


class _FakeSubmissionStore:
    def get_submission_snapshot(self, *, matter_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            matter_version=6,
            stage="FINAL_QA",
            work_products=(),
            bundles=(),
            current_bundle=None,
            current_components=(),
            current_export=None,
            snapshot_hash="3" * 64,
        )


class _FakeSubmissionCommandStore(_FakeSubmissionStore):
    def __init__(self) -> None:
        self.bundle_id = str(uuid4())
        self.work_product_id = str(uuid4())
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_submission_snapshot(self, *, matter_id: str, actor: Actor):
        del actor
        return SimpleNamespace(
            matter_id=matter_id,
            matter_version=6,
            stage="READY_TO_EXPORT",
            work_products=({
                "work_product_id": self.work_product_id,
                "document_kind": "DEFENCE_STATEMENT",
                "audience": "COURT_SUBMISSION",
                "media_type": "application/pdf",
                "artifact_sha256": "1" * 64,
                "byte_size": 128,
                "page_count": 2,
                "semantic_text_sha256": "2" * 64,
                "status": "CANDIDATE",
                "approved_at": None,
                "stale_at": None,
                "stale_reason": None,
                "created_at": None,
            },),
            bundles=(),
            current_bundle={"bundle_id": self.bundle_id, "input_hash": "3" * 64},
            current_components=(),
            current_export=None,
            snapshot_hash="4" * 64,
        )

    def approve_work_product(self, **kwargs):
        self.calls.append(("approve", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="APPROVE_SUBMISSION_WORK_PRODUCT",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=7,
            audit_event_id=str(uuid4()),
            object_type="SUBMISSION_WORK_PRODUCT",
            object_id=kwargs["work_product_id"],
        )

    def lock_submission_bundle(self, **kwargs):
        self.calls.append(("lock", kwargs))
        return CaseLedgerCommandReceipt(
            command_name="LOCK_SUBMISSION_BUNDLE",
            idempotency_key=kwargs["idempotency_key"],
            matter_id=kwargs["matter_id"],
            matter_version=8,
            audit_event_id=str(uuid4()),
            object_type="SUBMISSION_BUNDLE",
            object_id=kwargs["bundle_id"],
        )

class _FakeUploadService:
    def __init__(self, *, reconciliation_required: bool = False) -> None:
        self.created: list[dict[str, object]] = []
        self.accepted: list[dict[str, object]] = []
        self.reconciliation_required = reconciliation_required

    def create_slot(self, **kwargs) -> WebUploadSlotResponse:
        self.created.append(kwargs)
        self.last_upload_id = str(uuid4())
        return WebUploadSlotResponse(upload_id=self.last_upload_id, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))

    def read_status(self, **kwargs) -> WebMaterialUploadStatusResponse:
        return WebMaterialUploadStatusResponse(
            operation_id=kwargs["upload_id"], kind="PDF", status="COMPLETED", retry_allowed=False,
            receipt=WebUploadReceipt(
                evidence_file_id=str(uuid4()), display_name="微信转账记录.pdf", content_sha256="a" * 64,
                page_count=3, matter_version=2,
            ),
        )

    async def accept_content(self, **kwargs) -> WebUploadReceipt:
        chunks = kwargs["chunks"]
        content = bytearray()
        async for chunk in chunks:
            content.extend(chunk)
        self.accepted.append({**kwargs, "bytes": bytes(content)})
        if self.reconciliation_required:
            raise PostObjectStoreReconciliationRequired("test-only unknown object-store outcome")
        return WebUploadReceipt(
            evidence_file_id=str(uuid4()),
            display_name="微信转账记录.pdf",
            content_sha256="a" * 64,
            page_count=3,
            matter_version=2,
        )


class _FakeCommonMaterialUploadService:
    """A browser-boundary fake: source storage details stay deliberately absent."""

    def __init__(
        self,
        *,
        admitted_format: CommonMaterialFormat = CommonMaterialFormat.DOCX,
        agent_status: CommonMaterialAgentStatus = CommonMaterialAgentStatus.AGENT_READY,
        reconciliation_required: bool = False,
    ) -> None:
        self.created: list[dict[str, object]] = []
        self.accepted: list[dict[str, object]] = []
        self.admitted_format = admitted_format
        self.agent_status = agent_status
        self.reconciliation_required = reconciliation_required
        self.upload_id = str(uuid4())
        self.material_object_id = str(uuid4())
        self.evidence_page_id = str(uuid4())

    def _receipt(self) -> CommonMaterialAdmissionReceipt:
        route = (
            CommonMaterialRoute.VISUAL_OCR
            if self.admitted_format in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}
            else CommonMaterialRoute.COMMON_DOCUMENT_READER
        )
        if self.agent_status is CommonMaterialAgentStatus.INGESTED_PENDING_ADAPTER:
            agent_source_ref = None
        elif self.admitted_format in {CommonMaterialFormat.DOCX, CommonMaterialFormat.XLSX}:
            agent_source_ref = f"material-object:{self.material_object_id}"
        else:
            agent_source_ref = f"evidence-page:{self.evidence_page_id}"
        return CommonMaterialAdmissionReceipt(
            material_object_id=self.material_object_id,
            display_name="借款协议.docx" if self.admitted_format is CommonMaterialFormat.DOCX else "转账截图.jpg",
            admitted_format=self.admitted_format,
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if self.admitted_format is CommonMaterialFormat.DOCX
                else "image/jpeg"
            ),
            byte_size=19,
            content_sha256="c" * 64,
            route=route,
            review_status=CommonMaterialReviewStatus.NEEDS_LAWYER_REVIEW,
            agent_status=self.agent_status,
            agent_source_ref=agent_source_ref,
            matter_version=4,
        )

    def create_slot(self, **kwargs) -> CommonMaterialUploadReservationReceipt:
        self.created.append(kwargs)
        return CommonMaterialUploadReservationReceipt(
            upload_id=self.upload_id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    async def accept_content(self, **kwargs) -> CommonMaterialAdmissionReceipt:
        content = bytearray()
        async for chunk in kwargs["chunks"]:
            content.extend(chunk)
        self.accepted.append({**kwargs, "bytes": bytes(content)})
        if self.reconciliation_required:
            raise CommonMaterialUploadReconciliationRequired("test-only unknown admission outcome")
        return self._receipt()

    def read_status(self, **kwargs) -> CommonMaterialUploadStatusReceipt:
        return CommonMaterialUploadStatusReceipt(
            upload_id=kwargs["upload_id"],
            status="COMPLETED",
            retry_allowed=False,
            receipt=self._receipt(),
        )


class _FakeCasePostureService:
    """Route-contract fake with the same role and controlled-code boundaries."""

    def __init__(self, *, current: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.current = current
        self.party_id = str(uuid4())
        self.proceeding_id = str(uuid4())
        self.position_id = str(uuid4())
        self.engagement_id = str(uuid4())
        self.profile_id = str(uuid4())

    @staticmethod
    def code_options() -> dict[str, tuple[str, ...]]:
        return {
            "party_kinds": ("NATURAL_PERSON", "LEGAL_PERSON"),
            "forum_types": ("PEOPLE_COURT", "ARBITRATION_COMMISSION"),
            "case_types": ("CIVIL.PRIVATE_LENDING", "CIVIL.CONTRACT"),
            "procedure_stages": ("FIRST_INSTANCE", "SECOND_INSTANCE"),
            "party_positions": ("PLAINTIFF", "DEFENDANT"),
            "authority_scopes": ("GENERAL_AUTHORITY", "SPECIAL_AUTHORITY"),
            "engagement_states": ("ACTIVE", "TERMINATED"),
        }

    def state(self, **kwargs) -> WebCasePostureState:
        self.calls.append(("state", kwargs))
        identity = kwargs["identity"]
        can_confirm = Role.LEAD_LAWYER in identity.actor.roles
        if not self.current:
            return WebCasePostureState(
                status=WebCasePostureStatus.NOT_CONFIRMED,
                profile=None,
                can_confirm=can_confirm,
            )
        return WebCasePostureState(
            status=WebCasePostureStatus.CURRENT,
            profile=WebCasePostureProfile(
                profile_id=self.profile_id,
                profile_version=5,
                represented_party_id=self.party_id,
                represented_party_display_label="周雅丽",
                represented_party_kind="NATURAL_PERSON",
                proceeding_id=self.proceeding_id,
                forum_type="PEOPLE_COURT",
                position_id=self.position_id,
                engagement_id=self.engagement_id,
                case_type_code="CIVIL.PRIVATE_LENDING",
                procedure_stage="FIRST_INSTANCE",
                represented_position="DEFENDANT",
                authority_scope_code="GENERAL_AUTHORITY",
                engagement_state="ACTIVE",
                confirmed_matter_version=6,
            ),
            can_confirm=can_confirm,
        )

    def _command(self, action: str, object_type: str, object_id: str, **kwargs) -> WebCasePostureCommandReceipt:
        self.calls.append((action, kwargs))
        identity = kwargs["identity"]
        if Role.LEAD_LAWYER not in identity.actor.roles:
            raise WebCasePostureBlocked("只有主办律师可以确认代理档案")
        allowed_by_field = {
            "party_kind": {"NATURAL_PERSON", "LEGAL_PERSON"},
            "forum_type": {"PEOPLE_COURT", "ARBITRATION_COMMISSION"},
            "case_type_code": {"CIVIL.PRIVATE_LENDING", "CIVIL.CONTRACT"},
            "procedure_stage": {"FIRST_INSTANCE", "SECOND_INSTANCE"},
            "position_code": {"PLAINTIFF", "DEFENDANT"},
            "authority_scope_code": {"GENERAL_AUTHORITY", "SPECIAL_AUTHORITY"},
            "engagement_state": {"ACTIVE", "TERMINATED"},
        }
        for field, allowed in allowed_by_field.items():
            if field in kwargs and kwargs[field] not in allowed:
                raise WebCasePostureBlocked(f"{field} is not a controlled option")
        return WebCasePostureCommandReceipt(
            action=action,
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 1,
            object_type=object_type,
            object_id=object_id,
        )

    def confirm_party(self, **kwargs):
        return self._command("CONFIRM_PARTY", "CASE_PARTY", self.party_id, **kwargs)

    def confirm_proceeding(self, **kwargs):
        return self._command("CONFIRM_PROCEEDING", "COURT_PROCEEDING", self.proceeding_id, **kwargs)

    def confirm_position(self, **kwargs):
        return self._command("CONFIRM_POSITION", "COURT_PARTY_POSITION", self.position_id, **kwargs)

    def confirm_engagement(self, **kwargs):
        return self._command("CONFIRM_ENGAGEMENT", "FIRM_ENGAGEMENT", self.engagement_id, **kwargs)

    def confirm_current_profile(self, **kwargs):
        return self._command("CONFIRM_CURRENT_PROFILE", "CASE_POSTURE_PROFILE", self.profile_id, **kwargs)

    def confirm_complete_posture(self, **kwargs):
        self.calls.append(("CONFIRM_COMPLETE_POSTURE", kwargs))
        identity = kwargs["identity"]
        if Role.LEAD_LAWYER not in identity.actor.roles:
            raise WebCasePostureBlocked("只有主办律师可以确认代理档案")
        if kwargs["position_code"] not in {"PLAINTIFF", "DEFENDANT"}:
            raise WebCasePostureBlocked("position_code is not a controlled option")
        return WebCasePostureCompleteReceipt(
            action="CONFIRM_COMPLETE_POSTURE",
            matter_id=kwargs["matter_id"],
            matter_version=kwargs["expected_version"] + 5,
            party_id=self.party_id,
            proceeding_id=self.proceeding_id,
            position_id=self.position_id,
            engagement_id=self.engagement_id,
            profile_id=self.profile_id,
        )


class _FakePagePreviewService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.content = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 32)

    def render_page(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            evidence_page_id=kwargs["evidence_page_id"],
            content_sha256=sha256(self.content).hexdigest(),
            width=4,
            height=5,
            media_type="image/png",
            png_content=self.content,
        )


class _FakeEvidenceReviewService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _receipt(*, command_name: str, object_type: str, object_id: str, matter_id: str) -> CaseLedgerCommandReceipt:
        return CaseLedgerCommandReceipt(
            command_name=command_name,
            idempotency_key="server-only-idempotency",
            matter_id=matter_id,
            matter_version=4,
            audit_event_id=str(uuid4()),
            object_type=object_type,
            object_id=object_id,
        )

    def summary(self, **kwargs):
        self.calls.append(("summary", kwargs))
        return {"matter_id": kwargs["matter_id"], "total_pages": 1, "original_files": ()}

    def pages(self, **kwargs):
        self.calls.append(("pages", kwargs))
        return SimpleNamespace(
            matter_id=kwargs["matter_id"],
            matter_version=3,
            total_count=1,
            items=({
                "evidence_page_id": str(uuid4()),
                "page_number": 1,
            },),
            next_cursor=None,
            has_more=False,
        )

    def create_page_decision_candidate(self, **kwargs):
        self.calls.append(("decision-candidate", kwargs))
        return self._receipt(
            command_name="CREATE_EVIDENCE_PAGE_DECISION_CANDIDATE",
            object_type="EVIDENCE_PAGE_DECISION",
            object_id=str(uuid4()),
            matter_id=kwargs["matter_id"],
        )

    def confirm_page_decision(self, **kwargs):
        self.calls.append(("decision-confirm", kwargs))
        return self._receipt(
            command_name="APPROVE_EVIDENCE_PAGE_DECISION",
            object_type="EVIDENCE_PAGE_DECISION",
            object_id=kwargs["decision_id"],
            matter_id=kwargs["matter_id"],
        )

    def confirm_page_decisions_batch(self, **kwargs):
        self.calls.append(("decision-batch-confirm", kwargs))
        return self._receipt(
            command_name="APPROVE_EVIDENCE_PAGE_DECISIONS_BATCH",
            object_type="EVIDENCE_PAGE_DECISION_BATCH",
            object_id=str(uuid4()),
            matter_id=kwargs["matter_id"],
        )

    def stage_agent_page_decision_candidates(self, **kwargs):
        self.calls.append(("agent-decision-candidates", kwargs))
        decision_id, page_id = str(uuid4()), str(uuid4())
        return {
            "receipt": self._receipt(
                command_name="STAGE_AGENT_EVIDENCE_PAGE_DECISION_CANDIDATES",
                object_type="EVIDENCE_PAGE_DECISION_CANDIDATE_BATCH",
                object_id=str(uuid4()),
                matter_id=kwargs["matter_id"],
            ),
            "run_id": kwargs["run_id"],
            "decision_ids": (decision_id,),
            "page_ids": (page_id,),
            "include_count": 1,
            "exclude_count": 0,
            "excluded": ({"category": "OCR_REQUIRED", "count": 1, "page_ids": (str(uuid4()),)},),
        }

    def create_annotation_candidate(self, **kwargs):
        self.calls.append(("annotation-candidate", kwargs))
        return self._receipt(
            command_name="CREATE_EVIDENCE_ANNOTATION_CANDIDATE",
            object_type="EVIDENCE_ANNOTATION",
            object_id=str(uuid4()),
            matter_id=kwargs["matter_id"],
        )

    def confirm_annotation(self, **kwargs):
        self.calls.append(("annotation-confirm", kwargs))
        return self._receipt(
            command_name="APPROVE_EVIDENCE_ANNOTATION",
            object_type="EVIDENCE_ANNOTATION",
            object_id=kwargs["annotation_id"],
            matter_id=kwargs["matter_id"],
        )

    def lock_manifest(self, **kwargs):
        self.calls.append(("manifest-lock", kwargs))
        return self._receipt(
            command_name="LOCK_EVIDENCE_MANIFEST",
            object_type="EVIDENCE_MANIFEST",
            object_id=str(uuid4()),
            matter_id=kwargs["matter_id"],
        )

    def enqueue_derivative_run(self, **kwargs):
        self.calls.append(("derivative-enqueue", kwargs))
        return self._receipt(
            command_name="ENQUEUE_EVIDENCE_DERIVATIVE_RUN",
            object_type="EVIDENCE_DERIVATIVE_RUN",
            object_id=str(uuid4()),
            matter_id=kwargs["matter_id"],
        )


class _FakeDerivativeWorker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _FakeAgentMaterialReviewService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.case_id = str(uuid4())
        self.run_id = str(uuid4())
        self.page_id = str(uuid4())
        self.candidate_id = str(uuid4())
        now = datetime.now(timezone.utc)
        self.run = WebAgentRunResponse(
            run_id=self.run_id,
            matter_id=self.case_id,
            matter_version=9,
            scope="ALL_CURRENT_EVIDENCE",
            status="NEEDS_REVIEW",
            total_pages=36,
            processed_pages=36,
            remaining_pages=0,
            batch_count=2,
            completed_batch_count=2,
            candidate_count=1,
            tasks=(
                WebAgentTaskResponse(task_kind="MATERIAL_READING", status="COMPLETED"),
                WebAgentTaskResponse(task_kind="EXCEPTION_ROUTING", status="NEEDS_REVIEW"),
            ),
            retry_allowed=True,
            failure_state=None,
            created_at=now,
            updated_at=now,
            external_service_notice="本次所选页文本将发送至律所配置的外部 AI 服务；服务商与保留策略由管理员固定。",
            representation_profile=WebRepresentationProfileResponse(
                status="CONFIRMED",
                active_proceeding_role="DEFENDANT",
                proceeding_stage="FIRST_INSTANCE",
                case_type="PRIVATE_LENDING_DISPUTE",
                version=2,
            ),
        )

    def _scoped_run(self, *, matter_id: str) -> WebAgentRunResponse:
        return self.run if matter_id == self.run.matter_id else WebAgentRunResponse(
            **{**self.run.__dict__, "matter_id": matter_id}
        )

    def queue_all_current_evidence(self, **kwargs):
        self.calls.append(("queue", kwargs))
        return self._scoped_run(matter_id=kwargs["matter_id"])

    def representation_profile(self, **kwargs):
        self.calls.append(("profile", kwargs))
        return self.run.representation_profile

    def current_run(self, **kwargs):
        self.calls.append(("current", kwargs))
        return self._scoped_run(matter_id=kwargs["matter_id"])

    def get_run(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["run_id"] != self.run_id:
            raise KeyError(kwargs["run_id"])
        return self._scoped_run(matter_id=kwargs["matter_id"])

    def candidate_batch(self, **kwargs):
        self.calls.append(("candidates", kwargs))
        return WebAgentCandidateBatchResponse(
            run_id=kwargs["run_id"],
            total_count=1,
            items=(WebAgentCandidateResponse(
                candidate_id=self.candidate_id,
                evidence_page_id=self.page_id,
                source_label="微信交易记录.pdf",
                page_number=17,
                kind="UNCERTAIN",
                confidence=0.42,
                review_priority="HIGH",
                reason_codes=("CONFLICTING_CONTEXT",),
                supporting_excerpt="寒雪青松 2020-08-20 转账 10,000.00元",
            ),),
            next_cursor=None,
            has_more=False,
        )


class _FakeDynamicCasePlanService:
    def __init__(self, *, stale: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.plan_id = str(uuid4())
        self.item_id = str(uuid4())
        now = datetime.now(timezone.utc)
        self.plan = WebDynamicCasePlanResponse(
            plan_id=self.plan_id,
            matter_id=str(uuid4()),
            generated_matter_version=8 if stale else 9,
            current_matter_version=9,
            status="STALE" if stale else "CANDIDATE",
            inputs_current=not stale,
            stale_reasons=("新增证据已进入案件台账",) if stale else (),
            generated_at=now,
            can_activate=not stale,
            activation_blockers=("案件输入已经变化",) if stale else (),
            items=(WebDynamicCasePlanItemResponse(
                item_id=self.item_id,
                sequence=1,
                category="RESEARCH_TASK",
                status="CANDIDATE",
                readiness="NEEDS_RESEARCH",
                title="核验当前适用的利率上限规则",
                purpose="确认本案利息主张的适用规则与期间边界",
                rationale="诉请期间跨越规则变化节点，需以当前有效官方法源核验。",
                risk_if_omitted="可能采用错误的期间规则和利率上限。",
                prerequisites=("CONFIRMED_CLAIM_SCOPE",),
                confidence=0.78,
                review_gate="LEGAL_AUTHORITY_REVIEW",
                source_refs=(WebDynamicCasePlanReferenceResponse(
                    source_kind="CASE_CLAIM",
                    source_id=str(uuid4()),
                    label="原告诉请中的利息主张",
                    locator="诉请第2项",
                ),),
            ),),
        )

    def current_plan(self, **kwargs):
        self.calls.append(("current", kwargs))
        return WebDynamicCasePlanResponse(**{**self.plan.__dict__, "matter_id": kwargs["matter_id"]})

    def decide_item(self, **kwargs):
        self.calls.append(("decision", kwargs))
        return WebDynamicCasePlanDecisionReceipt(
            plan_id=kwargs["plan_id"],
            item_id=kwargs["item_id"],
            decision_status={"APPROVE": "APPROVED", "MODIFY": "CHANGE_REQUESTED", "REJECT": "REJECTED"}[kwargs["decision"]],
            matter_version=kwargs["expected_version"],
            requires_replanning=kwargs["decision"] in {"MODIFY", "REJECT"},
        )

    def activate_current_plan(self, **kwargs):
        self.calls.append(("activate", kwargs))
        return WebDynamicCasePlanActivationReceipt(
            plan_id=self.plan_id,
            status="ACTIVE",
            matter_version=kwargs["expected_version"] + 1,
        )


class _FakeAgentLedgerExtractionReviewService:
    def __init__(self, *, available: bool = True, decision_error=None) -> None:
        self.available = available
        self.decision_error = decision_error
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.batch_id = str(uuid4())
        self.fact_page_id = str(uuid4())
        self.exception_page_id = str(uuid4())
        self.group_id = str(uuid4())
        self.now = datetime.now(timezone.utc)

    def is_available(self, **kwargs):
        self.calls.append(("available", kwargs))
        return self.available

    def list_batches(self, **kwargs):
        self.calls.append(("list", kwargs))
        return (
            WebAgentLedgerExtractionBatch(
                batch_id=self.batch_id,
                matter_id=kwargs["matter_id"],
                status="REVIEW_READY",
                current_matter_version=9,
                source_matter_version=8,
                candidate_count=2,
                low_risk_count=1,
                exception_count=1,
                staged_at=self.now,
                confirmed_at=None,
                can_confirm_low_risk=True,
                exception_review_status="OPEN",
                exception_group_count=1,
                decided_exception_group_count=0,
                exception_groups=(
                    WebAgentLedgerExceptionGroup(
                        group_id=self.group_id,
                        candidate_kind="TRANSACTION",
                        candidate_count=1,
                        summary="1条收付款候选：字段缺失或表述不唯一，需先重新核验原页。",
                        review_reasons=("相关当事人或收付款人不明确。",),
                        source_guidance="来源包含 OCR、非原生文本或复核缺口，需先对照原页。",
                        risk_label="关键信息缺失或表述不唯一",
                        status="OPEN",
                        decision=None,
                        decision_label=None,
                        decision_reason=None,
                        decision_reason_label=None,
                        can_decide=True,
                        allowed_actions=(
                            WebAgentLedgerExceptionAction(
                                code="REQUEST_MORE_EVIDENCE",
                                label="转为补充材料任务",
                                consequence="Agent 会把本组缺口纳入下一版材料清单和办案计划。",
                                requires_note=False,
                                reasons=(
                                    WebAgentLedgerExceptionReasonOption(
                                        code="PARTY_DATE_AMOUNT_UNCLEAR",
                                        label="主体、日期或金额仍不明确",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                low_risk_candidates=(
                    WebAgentLedgerExtractionCandidate(
                        sequence=1,
                        candidate_kind="FACT",
                        summary="借款本金已通过银行转账交付。",
                        confidence=0.995,
                        review_status="LOW_RISK",
                        review_reasons=(),
                        excerpts=(
                            WebAgentLedgerExtractionExcerpt(
                                evidence_page_id=self.fact_page_id,
                                page_number=3,
                                text="转账人民币壹拾万元整",
                            ),
                        ),
                    ),
                ),
                exception_candidates=(
                    WebAgentLedgerExtractionCandidate(
                        sequence=2,
                        candidate_kind="TRANSACTION",
                        summary="2024-02-03 · 100000 CNY · 付款人待核对",
                        confidence=0.82,
                        review_status="EXCEPTION",
                        review_reasons=("相关当事人或收付款人不明确。",),
                        excerpts=(
                            WebAgentLedgerExtractionExcerpt(
                                evidence_page_id=self.exception_page_id,
                                page_number=4,
                                text="付款人名称部分无法识别",
                            ),
                        ),
                    ),
                ),
            ),
        )

    def confirm_low_risk_batch(self, **kwargs):
        self.calls.append(("confirm", kwargs))
        return WebAgentLedgerExtractionConfirmationReceipt(
            batch_id=kwargs["batch_id"],
            matter_version=kwargs["expected_version"] + 1,
            confirmed_fact_count=1,
            confirmed_transaction_count=0,
            confirmed_total_count=1,
        )

    def list_exception_group_members(self, **kwargs):
        self.calls.append(("members", kwargs))
        return WebAgentLedgerExceptionMemberPage(
            group_id=kwargs["group_id"],
            total_count=1,
            offset=0,
            next_offset=None,
            members=(
                WebAgentLedgerExceptionMember(
                    sequence=1,
                    candidate_kind="TRANSACTION",
                    summary="2024-02-03 · 100000 CNY · 付款人待核对",
                    confidence=0.82,
                    review_reasons=("相关当事人或收付款人不明确。",),
                    excerpts=(
                        WebAgentLedgerExtractionExcerpt(
                            evidence_page_id=self.exception_page_id,
                            page_number=4,
                            text="付款人名称部分无法识别",
                        ),
                    ),
                ),
            ),
        )

    def decide_exception_group(self, **kwargs):
        self.calls.append(("decide", kwargs))
        if self.decision_error is not None:
            raise self.decision_error
        return WebAgentLedgerExceptionDecisionReceipt(
            batch_id=kwargs["batch_id"],
            group_id=kwargs["group_id"],
            matter_version=kwargs["expected_version"],
            committed_matter_version=kwargs["expected_version"],
            decision=kwargs["decision"],
            exception_review_status="RESOLVED",
            decided_exception_group_count=1,
            exception_group_count=1,
            batch_resolved=False,
        )


class _FakeAgentLedgerExceptionFollowupService:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.followup_id = str(uuid4())
        self.batch_id = str(uuid4())
        self.group_id = str(uuid4())
        self.page_id = str(uuid4())
        self.source_id = str(uuid4())
        self.now = datetime.now(timezone.utc)

    def is_available(self, **kwargs):
        self.calls.append(("available", kwargs))
        return self.available

    def list_followups(self, **kwargs):
        self.calls.append(("list", kwargs))
        return WebAgentLedgerExceptionFollowupPage(
            total_count=1,
            offset=kwargs["offset"],
            next_offset=None,
            control_health="HEALTHY",
            can_recover=False,
            followups=(
                WebAgentLedgerExceptionFollowup(
                    followup_id=self.followup_id,
                    kind="MORE_EVIDENCE",
                    state="ACTIVE",
                    head_sequence=1,
                    origin_batch_id=self.batch_id,
                    origin_group_id=self.group_id,
                    current_matter_version=9,
                    created_matter_version=8,
                    created_at=self.now,
                    reason="主体、日期或金额仍不明确",
                    reason_note="请当事人提供原始银行流水",
                    candidate_count=2,
                    review_reasons=("相关当事人或收付款人不明确",),
                    evidence_page_count=1,
                    acceptance_requirements=(
                        "至少选择一份在本次补证请求之后新入卷的受管材料。",
                    ),
                    automation_status=None,
                    can_act=True,
                    allowed_actions=(
                        WebAgentLedgerFollowupAction(
                            code="CONFIRM_MORE_EVIDENCE",
                            label="确认新增材料满足本次补证",
                            consequence="服务器只接受本案补证请求之后新入卷的受管材料。",
                            requires_reason=True,
                        ),
                        WebAgentLedgerFollowupAction(
                            code="WITHDRAW",
                            label="撤回本项后续工作",
                            consequence="本项不再阻挡后续计划。",
                            requires_reason=True,
                        ),
                        WebAgentLedgerFollowupAction(
                            code="SUPERSEDE",
                            label="标记已由新情况替代",
                            consequence="Agent 必须基于当前案件重新研判。",
                            requires_reason=True,
                        ),
                    ),
                ),
            ),
        )

    def list_eligible_managed_evidence_sources(self, **kwargs):
        self.calls.append(("sources", kwargs))
        return WebManagedEvidenceSourcePage(
            total_count=1,
            offset=kwargs["offset"],
            next_offset=None,
            sources=(
                WebManagedEvidenceSource(
                    object_type="MATERIAL_OBJECT",
                    object_id=self.source_id,
                    display_label="新增银行流水.xlsx",
                    created_at=self.now,
                ),
            ),
        )

    def list_followup_evidence_page_ids(self, **kwargs):
        self.calls.append(("pages", kwargs))
        return WebFollowupEvidencePageIdPage(
            total_count=1,
            offset=kwargs["offset"],
            next_offset=None,
            evidence_page_ids=(self.page_id,),
        )

    def resolve_followup(self, **kwargs):
        self.calls.append(("resolve", kwargs))
        terminal = {
            "CONFIRM_MORE_EVIDENCE": "SATISFIED",
            "RESUME": "RESUMED",
            "WITHDRAW": "WITHDRAWN",
            "SUPERSEDE": "SUPERSEDED",
        }[kwargs["action"]]
        return WebAgentLedgerExceptionFollowupReceipt(
            followup_id=kwargs["followup_id"],
            action=kwargs["action"],
            terminal_state=terminal,
            matter_version=kwargs["expected_version"] + 1,
        )

    def recover_exception_followups(self, **kwargs):
        self.calls.append(("recover", kwargs))
        return WebAgentLedgerExceptionRecoveryReceipt(
            matter_version=kwargs["expected_matter_version"],
            control_health="HEALTHY",
            recovery_started=True,
        )


class _FakeCaseAgentControlService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.run_id = str(uuid4())
        self.decision_id = str(uuid4())
        self.approval_id = str(uuid4())
        self.artifact_id = str(uuid4())
        self.now = datetime.now(timezone.utc)
        self.completed_versions: dict[str, int] = {}
        self.completion_receipts: dict[
            tuple[str, str, int, str], WebCaseAgentCompletionReceipt
        ] = {}

    def _run(
        self,
        matter_id: str,
        *,
        status: str = "EXECUTING",
        version: int = 4,
        active_plan_execution: bool = False,
    ):
        return WebCaseAgentControlRunResponse(
            run_id=self.run_id, matter_id=matter_id, objective="全面审阅本案并形成可复核的办案成果",
            status=status, phase_label="正在研判", progress_completed=3, progress_total=8,
            current_work=WebCaseAgentCurrentWorkResponse(title="核对争议焦点", detail="结合材料和已确认案情形成候选", status="RUNNING"),
            open_decision_count=1, open_approval_count=1, artifact_count=1,
            status_message="Agent 正在推进；候选不会自动成为正式结论。", failure_message=None,
            failure_code=None,
            version=version, snapshot_matter_version=1,
            created_at=self.now, updated_at=self.now,
            can_pause=status == "EXECUTING", can_resume=status == "PAUSED", can_cancel=status not in {"COMPLETED", "CANCELLED"},
            active_plan_execution=active_plan_execution,
        )

    def create_run(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self._run(kwargs["matter_id"])

    def execute_active_plan(self, **kwargs):
        self.calls.append(("execute_active_plan", kwargs))
        return self._run(kwargs["matter_id"], active_plan_execution=True)

    def reconcile_active_plan_execution(self, **kwargs):
        self.calls.append(("reconcile_active_plan_execution", kwargs))
        if kwargs["idempotency_key"] == "missing-active-plan-execution":
            return None
        return self._run(kwargs["matter_id"], active_plan_execution=True)

    def get_current_run(self, **kwargs):
        self.calls.append(("current", kwargs))
        completed_version = self.completed_versions.get(kwargs["matter_id"])
        return self._run(
            kwargs["matter_id"],
            status="COMPLETED" if completed_version is not None else "EXECUTING",
            version=completed_version or 4,
        )

    def continue_from_material_review(self, **kwargs):
        self.calls.append(("continue_from_material_review", kwargs))
        return self._run(
            kwargs["matter_id"], status="EXECUTING",
            version=kwargs["expected_run_version"] + 1,
        )

    def get_run(self, **kwargs):
        self.calls.append(("get", kwargs))
        completed_version = self.completed_versions.get(kwargs["matter_id"])
        return self._run(
            kwargs["matter_id"],
            status="COMPLETED" if completed_version is not None else "EXECUTING",
            version=completed_version or 4,
        )

    def pause_run(self, **kwargs):
        self.calls.append(("pause", kwargs))
        return self._run(kwargs["matter_id"], status="PAUSED", version=kwargs["expected_run_version"] + 1)

    def resume_run(self, **kwargs):
        self.calls.append(("resume", kwargs))
        return self._run(kwargs["matter_id"], version=kwargs["expected_run_version"] + 1)

    def cancel_run(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return self._run(kwargs["matter_id"], status="CANCELLED", version=kwargs["expected_run_version"] + 1)

    def complete_run(self, **kwargs):
        self.calls.append(("complete", kwargs))
        run = self._run(
            kwargs["matter_id"],
            status="COMPLETED",
            version=kwargs["expected_run_version"] + 1,
        )
        self.completed_versions[kwargs["matter_id"]] = run.version
        receipt = WebCaseAgentCompletionReceipt(
                completion_id=str(uuid4()),
                matter_id=kwargs["matter_id"],
                run_id=self.run_id,
                reviewed_run_version=kwargs["expected_run_version"],
                completed_run_version=kwargs["expected_run_version"] + 1,
                run_status="COMPLETED",
                verification_status="PASSED",
                reviewed_artifact_count=run.artifact_count,
        )
        self.completion_receipts[
            (
                kwargs["matter_id"],
                kwargs["run_id"],
                kwargs["expected_run_version"],
                kwargs["idempotency_key"],
            )
        ] = receipt
        return WebCaseAgentCompletionResponse(receipt=receipt, run=run)

    def reconcile_completion(self, **kwargs):
        self.calls.append(("reconcile_completion", kwargs))
        return self.completion_receipts.get(
            (
                kwargs["matter_id"],
                kwargs["run_id"],
                kwargs["expected_run_version"],
                kwargs["idempotency_key"],
            )
        )

    def list_decisions(self, **kwargs):
        self.calls.append(("decisions", kwargs))
        return (WebCaseAgentDecisionResponse(
            decision_id=self.decision_id, title="确认争议范围", question="是否将反诉纳入本次工作目标？",
            options=(WebCaseAgentDecisionOptionResponse(option_id="INCLUDE", label="纳入", consequence="Agent 将补充反诉材料和交付建议"),),
            allow_note=True, blocking=True, status="OPEN"),)

    def submit_decision(self, **kwargs):
        self.calls.append(("submit_decision", kwargs))
        return self._run(kwargs["matter_id"], version=kwargs["expected_run_version"] + 1)

    def list_approvals(self, **kwargs):
        self.calls.append(("approvals", kwargs))
        return (WebCaseAgentApprovalResponse(
            approval_id=self.approval_id, action_label="联网检索官方法源", reason="本案期间跨越规则变化", impact="仅发送去标识化检索词", status="OPEN"),)

    def submit_approval(self, **kwargs):
        self.calls.append(("submit_approval", kwargs))
        return self._run(kwargs["matter_id"], version=kwargs["expected_run_version"] + 1)

    def list_artifacts(self, **kwargs):
        self.calls.append(("artifacts", kwargs))
        return (WebCaseAgentArtifactResponse(
            artifact_id=self.artifact_id, title="案件初步审阅意见", artifact_type="CASE_REVIEW_MEMO",
            status="READY_FOR_REVIEW", review_required=True),)


class _FakeCaseAgentArtifactReviewService:
    def __init__(
        self,
        artifact_id: str,
        evidence_page_id: str,
        *,
        source_kind: str = "evidence-page",
        source_id: str | None = None,
    ) -> None:
        self.artifact_id = artifact_id
        self.evidence_page_id = evidence_page_id
        self.source_kind = source_kind
        self.source_id = source_id or evidence_page_id
        self.calls: list[dict[str, object]] = []

    def read_review(self, **kwargs):
        self.calls.append(kwargs)
        return WebCaseAgentArtifactReview(
            artifact_id=self.artifact_id,
            artifact_type="CASE_CONTEXT_REVIEW_CANDIDATE",
            title="本案争议与证据缺口",
            review_notice="这是等待律师确认的分析候选。",
            sections=(WebCaseAgentArtifactReviewSection(
                section_id="gaps",
                title="优先核对",
                severity="HIGH",
                items=(WebCaseAgentArtifactReviewItem(
                    item_id="gap-1",
                    title="还款记录仍需核对",
                    detail="付款日期与原告诉请期间存在交叉。",
                    badge="涉及本金抵扣",
                    confidence=0.94,
                    sources=(WebCaseAgentArtifactSource(
                        source_kind=self.source_kind,
                        source_id=self.source_id,
                        label="案件事实" if self.source_kind == "fact" else "证据页",
                        evidence_page_id=self.evidence_page_id,
                    ),),
                ),),
            ),),
        )


class _FakeWebDocumentDraftService:
    def snapshot(self, **kwargs):
        return SimpleNamespace(
            matter_id=kwargs["matter_id"],
            matter_version=3,
            pairs=(),
            snapshot_hash="a" * 64,
        )

    def generate(self, **kwargs):
        del kwargs
        raise AssertionError("this delivery route must not generate a document")


class _FakeWebDocumentDraftDeliveryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def download(self, **kwargs):
        self.calls.append(kwargs)
        pdf = b"%PDF-1.7\nreview\n%%EOF"
        return WebDocumentDraftDelivery(
            file_name="文书审阅候选.pdf",
            ascii_file_name="reviewable-draft.pdf",
            media_type="application/pdf",
            disposition="inline",
            artifact_sha256=sha256(pdf).hexdigest(),
            content=pdf,
        )


def _grant(identity: ServerIdentityContext) -> WebSessionGrant:
    return WebSessionGrant(
        session_id=identity.session_id,
        expires_at=identity.expires_at,
        session_cookie=CookieDirective(name="__Host-lawcase_session", value="A" * 48, max_age=1800),
        csrf_cookie=CookieDirective(name="__Host-lawcase_csrf", value="B" * 48, httponly=False, max_age=1800),
    )


class WebAppTests(unittest.TestCase):
    def test_upload_slot_validation_rejects_invalid_identity_or_expiry(self) -> None:
        with self.assertRaises(WebRequestBlocked):
            _validate_upload_slot(
                WebUploadSlotResponse(
                    upload_id="not-a-uuid",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            )
        with self.assertRaises(WebRequestBlocked):
            _validate_upload_slot(
                WebUploadSlotResponse(
                    upload_id=str(uuid4()),
                    expires_at=datetime.now() + timedelta(minutes=10),
                )
            )

    def test_archive_receipt_validation_rejects_unverified_metadata(self) -> None:
        with self.assertRaises(WebRequestBlocked):
            _validate_archive_receipt(
                WebMaterialArchiveReceipt(
                    archive_id=str(uuid4()),
                    display_name="materials.zip",
                    content_sha256="not-a-sha256",
                    byte_size=10,
                    entry_count=1,
                    expanded_byte_size=20,
                    processing_status="STORED_PENDING_PROCESSING",
                )
            )

    def _client(self, *, reconciliation_required: bool = False, page_preview_service=None, evidence_review_service=None, derivative_worker=None, case_ledger_store=None, legal_store=None, official_source_capture_store=None, formal_calculation_store=None, submission_store=None, document_draft_service=None, document_draft_delivery_service=None, agent_material_review_service=None, dynamic_case_plan_service=None, agent_ledger_extraction_review_service=None, agent_ledger_exception_followup_service=None, case_agent_control_service=None, case_agent_artifact_review_service=None, case_agent_document_review_service=None, case_agent_runtime_ready=None, case_agent_ledger_runtime_ready=None, case_agent_document_runtime_ready=None, common_material_upload_service=None, case_posture_service=None, role: Role = Role.LEAD_LAWYER):
        identity = _identity(role=role)
        session = _FakeSessionAuthority(identity)
        oidc = _FakeOidcLogin(_grant(identity))
        store = _FakeMatterStore()
        uploads = _FakeUploadService(reconciliation_required=reconciliation_required)
        app = create_web_app(
            WebApiDependencies(
                settings=WebApiSettings(public_origin="https://workbench.example.test"),
                oidc_login=oidc,  # type: ignore[arg-type]
                session_authority=session,  # type: ignore[arg-type]
                matter_store=store,  # type: ignore[arg-type]
                fact_correction_store=getattr(self, "fact_correction_store", None),
                case_ledger_store=case_ledger_store,
                legal_store=legal_store,
                official_source_capture_store=official_source_capture_store,
                formal_calculation_store=formal_calculation_store,
                submission_store=submission_store,
                upload_service=uploads,
                common_material_upload_service=common_material_upload_service,
                page_preview_service=page_preview_service,
                evidence_review_service=evidence_review_service,
                derivative_worker=derivative_worker,
                document_draft_service=document_draft_service,
                document_draft_delivery_service=document_draft_delivery_service,
                agent_material_review_service=agent_material_review_service,
                dynamic_case_plan_service=dynamic_case_plan_service,
                agent_ledger_extraction_review_service=agent_ledger_extraction_review_service,
                agent_ledger_exception_followup_service=agent_ledger_exception_followup_service,
                case_agent_control_service=case_agent_control_service,
                case_agent_artifact_review_service=case_agent_artifact_review_service,
                case_agent_document_review_service=case_agent_document_review_service,
                case_posture_service=case_posture_service,
                case_agent_runtime_ready=case_agent_runtime_ready,
                case_agent_ledger_runtime_ready=case_agent_ledger_runtime_ready,
                case_agent_document_runtime_ready=case_agent_document_runtime_ready,
            )
        )
        return TestClient(app), identity, session, oidc, store, uploads

    def test_fact_correction_save_read_and_recovery_routes(self) -> None:
        from case_kernel.case_agent_fact_correction_postgres import FactCorrectionReceipt
        calls = []
        receipt = FactCorrectionReceipt(str(uuid4()), 1, 11)
        def save(**kw):
            calls.append(kw)
            return receipt
        self.fact_correction_store = SimpleNamespace(save=save,is_available=lambda:True,
            read_context=lambda **kw: {"current_matter_version":11,"draft":{"court_ready":False,"stale":False}},
            find_by_key=lambda **kw: receipt, read_current=lambda **kw: {"court_ready":False,"stale":False})
        client, identity, *_ = self._client()
        case_id, candidate_id = str(uuid4()), str(uuid4())
        path = f"/api/v1/cases/{case_id}/fact-corrections/{candidate_id}"
        headers = {"Idempotency-Key":"fact-correction-route-0001"}
        body = dict(expected_matter_version=11,expected_revision=0,revised_text="修改稿",reason="纠正主体")
        response = client.post(path,json=body,headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(calls[0]["actor"],identity.actor)
        self.assertEqual(calls[0]["candidate_id"],candidate_id)
        self.assertFalse(response.json()["court_ready"])
        self.assertEqual(client.get(path).status_code,200)
        recovered = client.get(f"/api/v1/cases/{case_id}/fact-correction-receipt",headers=headers)
        self.assertEqual(recovered.json()["receipt"]["proposal_id"],receipt.proposal_id)
        for change in ({"expected_matter_version":True},{"source_pages":[]},{"actor_id":str(uuid4())}):
            rejected = client.post(path,json={**body,**change},headers=headers)
            self.assertEqual(rejected.status_code,422,rejected.text)
        self.assertEqual(client.post(path,json=body).status_code,422)
        self.assertEqual(len(calls),1)

    def test_fact_correction_submission_and_unknown_receipt(self) -> None:
        calls = []
        case_id, candidate_id, proposal_id = (str(uuid4()) for _ in range(3))
        receipt = CaseLedgerCommandReceipt(command_name="CREATE_FACT_CANDIDATE_FROM_CORRECTION",
            idempotency_key="correction-submit-test-0001",matter_id=case_id,matter_version=12,
            audit_event_id=str(uuid4()),object_type="FACT",object_id=str(uuid4()))
        def submit(**kwargs):
            calls.append(kwargs)
            return receipt
        self.fact_correction_store = SimpleNamespace(save=lambda **kw:None,is_available=lambda:True,
            read_current=lambda **kw:None,read_context=lambda **kw:None,find_by_key=lambda **kw:None,
            submit_for_fact_review=submit,is_submission_available=lambda:True,
            find_submission_by_key=lambda **kw:None)
        client,identity,*_ = self._client()
        path=f"/api/v1/cases/{case_id}/fact-corrections/{candidate_id}/proposals/{proposal_id}/submit"
        recovery=f"/api/v1/cases/{case_id}/fact-correction-submission-receipt"
        headers={"Idempotency-Key":receipt.idempotency_key}
        response=client.post(path,json={"expected_matter_version":11},headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(calls[0]["actor"],identity.actor)
        self.assertEqual(calls[0]["proposal_id"],proposal_id)
        self.assertEqual(response.json()["receipt"]["object_id"],receipt.object_id)
        self.assertFalse(response.json()["court_ready"])
        unknown=client.get(recovery,headers=headers)
        self.assertEqual(unknown.status_code,200)
        self.assertIsNone(unknown.json()["receipt"])
        self.fact_correction_store.find_submission_by_key=lambda **kw:receipt
        self.assertEqual(client.get(recovery,headers=headers).json()["receipt"]["object_id"],receipt.object_id)
        for extra in ({"expected_matter_version":True},{"revised_text":"替换内容"},
                      {"status":"CONFIRMED"},{"actor_id":str(uuid4())},{"evidence_links":[]}):
            self.assertEqual(client.post(path,json={"expected_matter_version":11,**extra},headers=headers).status_code,422)
        self.assertEqual(len(calls),1)
        self.fact_correction_store.read_submission=lambda **kw:dict(current_matter_version=12,
            submission=dict(fact_id=receipt.object_id,proposal_id=proposal_id,status="CANDIDATE"))
        linked=client.get(f"/api/v1/cases/{case_id}/fact-corrections/{candidate_id}/submission")
        self.assertEqual(linked.status_code,200,linked.text)
        self.assertEqual(linked.json()["submission"]["status"],"CANDIDATE")
        self.assertFalse(linked.json()["court_ready"])
        self.fact_correction_store.is_submission_available=lambda:False
        self.assertEqual(client.post(path,json={"expected_matter_version":11},headers=headers).status_code,503)
        self.assertEqual(client.get(recovery,headers=headers).status_code,503)
        self.assertEqual(len(calls),1)

    def test_only_fact_members_expose_exact_correction_identity(self) -> None:
        from dataclasses import replace
        from case_api.web_app import _project_agent_ledger_exception_member
        identifier = str(uuid4())
        member = WebAgentLedgerExceptionMember(sequence=1,candidate_kind="FACT",summary="合成原告诉请陈述",
            confidence=0.7,review_reasons=("主体待核对",),extraction_candidate_id=identifier,
            excerpts=(WebAgentLedgerExtractionExcerpt(evidence_page_id=str(uuid4()),page_number=1,text="合成原文"),))
        self.assertEqual(_project_agent_ledger_exception_member(member)["extraction_candidate_id"],identifier)
        self.assertNotIn("extraction_candidate_id",_project_agent_ledger_exception_member(replace(member,candidate_kind="TRANSACTION")))

    def test_fact_correction_unconfigured_and_conflict(self) -> None:
        from case_kernel.case_agent_fact_correction_postgres import FactCorrectionBlocked
        path = f"/api/v1/cases/{uuid4()}/fact-corrections/{uuid4()}"
        client,*_ = self._client()
        self.assertEqual(client.get(path).status_code,503)
        def blocked(**kw): raise FactCorrectionBlocked("private source identifier")
        self.fact_correction_store=SimpleNamespace(save=blocked,find_by_key=blocked,read_current=blocked,read_context=blocked,is_available=lambda:True)
        client,*_ = self._client()
        response=client.get(path)
        self.assertEqual(response.status_code,409)
        self.assertNotIn("private source identifier",response.text)

    def test_submission_review_projection_is_read_only_and_locator_free(self) -> None:
        client, _, _, _, _, _ = self._client(submission_store=_FakeSubmissionStore())
        response = client.get(f"/api/v1/cases/{uuid4()}/submission-review")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review"]["stage"], "FINAL_QA")
        self.assertFalse(response.json()["review"]["document_drafts_available"])
        self.assertNotIn("storage_object_key", response.text)

    def test_document_draft_delivery_is_same_origin_and_locator_free(self) -> None:
        delivery = _FakeWebDocumentDraftDeliveryService()
        client, identity, _, _, _, _ = self._client(
            document_draft_service=_FakeWebDocumentDraftService(),
            document_draft_delivery_service=delivery,
        )
        case_id, pair_id = str(uuid4()), str(uuid4())
        response = client.get(
            f"/api/v1/cases/{case_id}/document-drafts/{pair_id}/delivery?purpose=REVIEW_PDF"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(response.headers["content-security-policy"], "sandbox")
        self.assertEqual(len(delivery.calls), 1)
        self.assertEqual(delivery.calls[0]["identity"].actor, identity.actor)
        self.assertEqual(delivery.calls[0]["matter_id"], case_id)
        self.assertEqual(delivery.calls[0]["pair_id"], pair_id)
        self.assertNotIn("object_key", response.text)
        invalid = client.get(
            f"/api/v1/cases/{case_id}/document-drafts/{pair_id}/delivery?purpose=ANYTHING"
        )
        self.assertEqual(invalid.status_code, 422)

    def test_submission_approval_and_lock_use_server_bound_candidate_and_input_hash(self) -> None:
        submission = _FakeSubmissionCommandStore()
        client, _, _, _, _, _ = self._client(submission_store=submission)
        case_id = str(uuid4())
        approved = client.post(
            f"/api/v1/cases/{case_id}/submission-work-products/{submission.work_product_id}/approve",
            headers={"Idempotency-Key": "submission-approve-0001"},
            json={"expected_version": 6},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertNotIn("approval_hash", approved.text)
        self.assertNotIn("approval_hash", submission.calls[0][1])
        locked = client.post(
            f"/api/v1/cases/{case_id}/submission-bundles/lock",
            headers={"Idempotency-Key": "submission-lock-0001"},
            json={"expected_version": 7, "bundle_id": submission.bundle_id},
        )
        self.assertEqual(locked.status_code, 200)
        self.assertEqual(submission.calls[1][0], "lock")
        self.assertEqual(submission.calls[1][1]["expected_input_hash"], "3" * 64)

    def test_formal_calculation_uses_server_approved_bundle_and_derives_approval_hash(self) -> None:
        case_id = str(uuid4())
        bundle_id = str(uuid4())
        legal = _FakeLegalStore(current_bundle={"bundle_id": bundle_id, "bundle_hash": "2" * 64})
        calculation = _FakeFormalCalculationStore()
        client, _, _, _, _, _ = self._client(
            case_ledger_store=_FakeCaseLedgerStore(),
            legal_store=legal,
            formal_calculation_store=calculation,
        )
        response = client.post(
            f"/api/v1/cases/{case_id}/formal-calculations",
            headers={"Idempotency-Key": "formal-calculation-12345"},
            json={
                "expected_version": 6,
                "obligation_id": "obligation-main",
                "start_date": "2020-08-20",
                "end_date": "2024-08-20",
                "allocation_policy": "INTEREST_THEN_PRINCIPAL",
            },
        )
        self.assertEqual(response.status_code, 201)
        kwargs = calculation.created[0]
        self.assertEqual(kwargs["legal_bundle_id"], bundle_id)
        self.assertEqual(kwargs["legal_bundle_hash"], "2" * 64)
        self.assertIs(kwargs["allocation_policy"], AllocationPolicy.INTEREST_THEN_PRINCIPAL)
        expected = sha256(
            f"formal-calculation-approval-v1:{case_id}:obligation-main:6:2020-08-20:2024-08-20:INTEREST_THEN_PRINCIPAL:{bundle_id}:{'2' * 64}".encode()
        ).hexdigest()
        self.assertEqual(kwargs["approval_hash"], expected)

    def test_payment_classification_uses_confirmed_transaction_amount_and_server_evidence(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, _, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/transactions/{ledger.transaction_id}/payment-classifications",
            headers={"Idempotency-Key": "payment-classification-001"},
            json={
                "expected_version": 3,
                "obligation_label": "2020年8月借款",
                "nature": "DISBURSEMENT",
                "same_day_sequence": 1,
                "amount": "999999999",
                "currency": "USD",
                "evidence_links": [{"evidence_id": str(uuid4())}],
            },
        )
        self.assertEqual(response.status_code, 201)
        command = ledger.payment_classification_candidate
        self.assertEqual(command["allocations"][0].obligation_id, "2020年8月借款")
        self.assertEqual(command["allocations"][0].amount, Decimal("10000"))
        self.assertEqual(command["allocations"][0].currency, "CNY")
        self.assertEqual(command["evidence_links"], ())
        self.assertTrue(command["use_transaction_evidence"])
        self.assertNotIn("999999999", str(command))

    def test_payment_classification_confirmation_derives_approval_hash(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, _, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        classification_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/payment-classifications/{classification_id}/confirm",
            headers={"Idempotency-Key": "payment-classification-approve-001"},
            json={"expected_version": 3},
        )
        self.assertEqual(response.status_code, 200)
        expected = sha256(
            f"web-payment-classification-v1:{case_id}:{classification_id}:3:APPROVED".encode()
        ).hexdigest()
        self.assertEqual(ledger.payment_classification_approval["approval_hash"], expected)
        self.assertNotIn("approval_hash", response.text)

    def test_formal_calculation_current_projection_is_browser_safe(self) -> None:
        calculation = _FakeFormalCalculationStore()
        client, _, _, _, _, _ = self._client(formal_calculation_store=calculation)
        response = client.get(f"/api/v1/cases/{uuid4()}/calculations/obligation-main/current")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["calculation"]["scenario"], None)
        self.assertNotIn("approval_hash", response.text)

    def test_formal_calculation_stays_blocked_without_approved_bundle(self) -> None:
        client, _, _, _, _, _ = self._client(
            case_ledger_store=_FakeCaseLedgerStore(),
            legal_store=_FakeLegalStore(),
            formal_calculation_store=_FakeFormalCalculationStore(),
        )
        response = client.post(
            f"/api/v1/cases/{uuid4()}/formal-calculations",
            headers={"Idempotency-Key": "formal-calculation-blocked"},
            json={
                "expected_version": 6,
                "obligation_id": "obligation-main",
                "start_date": "2020-08-20",
                "end_date": "2024-08-20",
                "allocation_policy": "INTEREST_THEN_PRINCIPAL",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("formal-calculation-approval", response.text)

    def test_derivative_queue_uses_verified_firm_and_receipt_run_id(self) -> None:
        review = _FakeEvidenceReviewService()
        worker = _FakeDerivativeWorker()
        client, identity, _, _, _, _ = self._client(
            evidence_review_service=review,
            derivative_worker=worker,
        )
        response = client.post(
            f"/api/v1/cases/{uuid4()}/evidence-derivative-runs",
            headers={"Idempotency-Key": "derivative-enqueue-12345"},
            json={"expected_version": 4, "manifest_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 202)
        receipt = response.json()["receipt"]
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(worker.calls[0]["firm_id"], identity.actor.firm_id)
        self.assertEqual(worker.calls[0]["matter_id"], receipt["matter_id"])
        self.assertEqual(worker.calls[0]["run_id"], receipt["object_id"])
        self.assertEqual(worker.calls[0]["expected_version"], receipt["matter_version"])

    def test_disabled_app_exposes_health_only(self) -> None:
        client = TestClient(create_web_app())
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "disabled")
        self.assertEqual(client.get("/setup-status").status_code, 503)
        self.assertEqual(client.get("/api/v1/session").status_code, 404)

    def test_rejects_mismatched_oidc_and_workbench_origin_before_mounting_routes(self) -> None:
        identity = _identity()
        oidc = _FakeOidcLogin(_grant(identity))
        oidc.public_origin = "https://other.example.test"
        with self.assertRaisesRegex(ValueError, "origin"):
            create_web_app(
                WebApiDependencies(
                    settings=WebApiSettings(public_origin="https://workbench.example.test"),
                    oidc_login=oidc,  # type: ignore[arg-type]
                    session_authority=_FakeSessionAuthority(identity),  # type: ignore[arg-type]
                    matter_store=_FakeMatterStore(),  # type: ignore[arg-type]
                )
            )

    def test_same_origin_login_callback_and_safe_session_response(self) -> None:
        client, identity, _, oidc, _, _ = self._client()
        login = client.get("/api/v1/auth/login", follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "https://id.example.test/authorize?opaque=1")
        self.assertNotIn("access-control-allow-origin", login.headers)
        self.assertEqual(login.headers["cache-control"], "no-store, max-age=0")

        callback = client.get("/api/v1/auth/oidc/callback?code=one&state=two&state=two", follow_redirects=False)
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(callback.headers["location"], "https://workbench.example.test/")
        self.assertIn("__Host-lawcase_session=", callback.headers["set-cookie"])
        self.assertIn("__Host-lawcase_csrf=", callback.headers["set-cookie"])
        self.assertEqual(oidc.callbacks, [(('code', 'one'), ('state', 'two'), ('state', 'two'))])

        session = client.get("/api/v1/session")
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(session.json()["actor"]["roles"], ["LEAD_LAWYER"])
        self.assertFalse(session.json()["capabilities"]["can_run_agent"])
        self.assertFalse(session.json()["capabilities"]["can_review_evidence"])
        self.assertNotIn(identity.actor.actor_id, session.text)
        self.assertNotIn(identity.actor.firm_id, session.text)

    def test_agent_capability_only_opens_with_the_aggregate_run_service(self) -> None:
        service = _FakeAgentMaterialReviewService()
        client, _, _, _, _, _ = self._client(agent_material_review_service=service)
        session = client.get("/api/v1/session")
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["capabilities"]["can_run_agent"])

    def test_unified_case_agent_capability_is_independent_and_fail_closed(self) -> None:
        client, _, _, _, _, _ = self._client()
        self.assertFalse(client.get("/api/v1/session").json()["capabilities"]["can_run_case_agent"])
        blocked = client.get(f"/api/v1/cases/{uuid4()}/case-agent-runs/current")
        self.assertEqual(blocked.status_code, 503)
        service = _FakeCaseAgentControlService()
        client, _, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        capabilities = client.get("/api/v1/session").json()["capabilities"]
        self.assertTrue(capabilities["can_run_case_agent"])
        self.assertFalse(capabilities["can_run_agent"])

    def test_unified_case_agent_accepts_business_goal_only_and_projects_progress(self) -> None:
        service = _FakeCaseAgentControlService()
        client, identity, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs",
            headers={"Idempotency-Key": "case-agent-goal-00001"},
            json={
                "objective": "全面审阅本案并形成可复核的办案成果",
                "success_criteria": ["列明事实、争点、证据缺口和下一步交付"],
                "constraints": ["任何法律结论必须由律师确认"],
                "expected_version": 9,
            },
        )
        self.assertEqual(response.status_code, 202)
        run = response.json()["run"]
        self.assertEqual(run["matter_id"], case_id)
        self.assertEqual(run["progress"], {"completed": 3, "total": 8})
        self.assertEqual(run["current_work"]["title"], "核对争议焦点")
        call = service.calls[0]
        self.assertEqual(call[0], "create")
        self.assertEqual(call[1]["identity"], identity)
        self.assertEqual(call[1]["expected_matter_version"], 9)
        self.assertEqual(call[1]["idempotency_key"], "case-agent-goal-00001")
        self.assertEqual(
            call[1]["requested_deliverables"],
            ("CASE_REVIEW_MEMO", "PAYMENT_LEDGER"),
        )
        for forbidden in ("dag", "tool", "provider", "prompt", "url", "path", "hash", "cost"):
            self.assertNotIn(forbidden, response.text.lower())
        rejected = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs",
            headers={"Idempotency-Key": "case-agent-goal-00002"},
            json={"objective": "审阅案件", "success_criteria": ["完成"], "constraints": [], "expected_version": 9, "tool": "shell"},
        )
        self.assertEqual(rejected.status_code, 422)

    def test_material_review_continues_the_same_agent_run_without_browser_source_scope(self) -> None:
        service = _FakeCaseAgentControlService()
        client, identity, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{service.run_id}/continue-analysis",
            headers={"Idempotency-Key": "case-agent-continue-0001"},
            json={"expected_run_version": 4},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["run"]["run_id"], service.run_id)
        call_name, call = service.calls[-1]
        self.assertEqual(call_name, "continue_from_material_review")
        self.assertEqual(call["identity"], identity)
        self.assertEqual(call["matter_id"], case_id)
        self.assertEqual(call["run_id"], service.run_id)
        self.assertEqual(call["expected_run_version"], 4)
        for forbidden in ("candidate", "source", "prompt", "model", "cost", "budget"):
            self.assertNotIn(forbidden, call)
        rejected = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{service.run_id}/continue-analysis",
            headers={"Idempotency-Key": "case-agent-continue-0002"},
            json={"expected_run_version": 4, "candidate_ids": [str(uuid4())]},
        )
        self.assertEqual(rejected.status_code, 422)

    def test_active_plan_execution_is_server_derived_and_requires_runtime(self) -> None:
        service = _FakeCaseAgentControlService()
        case_id = str(uuid4())
        blocked_client, _, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
            case_agent_document_runtime_ready=lambda _firm_id: False,
        )
        blocked_capabilities = blocked_client.get("/api/v1/session").json()[
            "capabilities"
        ]
        self.assertTrue(blocked_capabilities["can_run_case_agent"])
        self.assertFalse(blocked_capabilities["can_execute_active_plan"])
        blocked = blocked_client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/execute-active-plan",
            headers={"Idempotency-Key": "execute-active-plan-0001"},
            json={"expected_version": 12},
        )
        self.assertEqual(blocked.status_code, 503)

        missing_review_client, _, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
            case_agent_document_runtime_ready=lambda _firm_id: True,
        )
        missing_review_capabilities = missing_review_client.get(
            "/api/v1/session"
        ).json()["capabilities"]
        self.assertFalse(missing_review_capabilities["can_execute_active_plan"])
        missing_review = missing_review_client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/execute-active-plan",
            headers={"Idempotency-Key": "execute-active-plan-no-review"},
            json={"expected_version": 12},
        )
        self.assertEqual(missing_review.status_code, 503)

        client, identity, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_document_review_service=SimpleNamespace(
                read_review=lambda **_kwargs: None,
                download=lambda **_kwargs: None,
                request_revision=lambda **_kwargs: None,
            ),
            case_agent_runtime_ready=lambda _firm_id: True,
            case_agent_document_runtime_ready=lambda _firm_id: True,
        )
        enabled_capabilities = client.get("/api/v1/session").json()["capabilities"]
        self.assertTrue(enabled_capabilities["can_execute_active_plan"])
        self.assertTrue(enabled_capabilities["can_complete_case_agent_run"])
        self.assertTrue(enabled_capabilities["can_review_case_agent_documents"])
        response = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/execute-active-plan",
            headers={"Idempotency-Key": "execute-active-plan-0002"},
            json={"expected_version": 12},
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["run"]["active_plan_execution"])
        self.assertEqual(response.json()["run"]["snapshot_matter_version"], 1)
        self.assertEqual(response.json()["run"]["input_snapshot_status"], "CURRENT")
        call_name, call = service.calls[-1]
        self.assertEqual(call_name, "execute_active_plan")
        self.assertEqual(call["identity"], identity)
        self.assertEqual(call["matter_id"], case_id)
        self.assertEqual(call["expected_matter_version"], 12)
        for forbidden in ("plan_id", "plan_hash", "item_id", "deliverable_kind"):
            self.assertNotIn(forbidden, call)
        forged = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/execute-active-plan",
            headers={"Idempotency-Key": "execute-active-plan-0003"},
            json={"expected_version": 12, "plan_id": str(uuid4())},
        )
        self.assertEqual(forged.status_code, 422)

        reconciled = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/active-plan-execution-intent",
            headers={"Idempotency-Key": "execute-active-plan-0002"},
            params={"plan_id": str(uuid4()), "expected_version": 12},
        )
        self.assertEqual(reconciled.status_code, 200)
        self.assertTrue(reconciled.json()["run"]["active_plan_execution"])
        call_name, call = service.calls[-1]
        self.assertEqual(call_name, "reconcile_active_plan_execution")
        self.assertEqual(call["idempotency_key"], "execute-active-plan-0002")
        self.assertEqual(call["expected_matter_version"], 12)

        missing = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/active-plan-execution-intent",
            headers={"Idempotency-Key": "missing-active-plan-execution"},
            params={"plan_id": str(uuid4()), "expected_version": 12},
        )
        self.assertEqual(missing.status_code, 200)
        self.assertIsNone(missing.json()["run"])

    def test_unified_case_agent_lifecycle_inbox_and_artifacts_are_port_owned(self) -> None:
        service = _FakeCaseAgentControlService()
        client, _, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())
        run_id = service.run_id
        self.assertEqual(client.get(f"/api/v1/cases/{case_id}/case-agent-runs/current").status_code, 200)
        paused = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/pause",
            headers={"Idempotency-Key": "case-agent-pause-0001"}, json={"expected_run_version": 4},
        )
        self.assertEqual(paused.json()["run"]["status"], "PAUSED")
        decisions = client.get(f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/decisions").json()["items"]
        self.assertEqual(decisions[0]["status"], "OPEN")
        answered = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/decisions/{service.decision_id}",
            headers={"Idempotency-Key": "case-agent-decision-0001"},
            json={"expected_run_version": 5, "option_id": "INCLUDE", "note": "纳入本次工作"},
        )
        self.assertEqual(answered.status_code, 200)
        approvals = client.get(f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/approvals").json()["items"]
        self.assertIn("联网检索", approvals[0]["action_label"])
        approved = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/approvals/{service.approval_id}",
            headers={"Idempotency-Key": "case-agent-approval-0001"},
            json={"expected_run_version": 6, "approved": True},
        )
        self.assertEqual(approved.status_code, 200)
        artifacts = client.get(f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts").json()["items"]
        self.assertEqual(artifacts[0]["status"], "READY_FOR_REVIEW")
        self.assertTrue(artifacts[0]["review_required"])

    def test_case_agent_final_review_accepts_only_version_and_is_get_verifiable(self) -> None:
        service = _FakeCaseAgentControlService()
        client, identity, _, _, _, _ = self._client(
            case_agent_control_service=service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())
        run_id = service.run_id
        response = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/complete",
            headers={"Idempotency-Key": "case-agent-final-review-0001"},
            json={"expected_run_version": 7},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload["receipt"]),
            {
                "completion_id",
                "matter_id",
                "run_id",
                "reviewed_run_version",
                "completed_run_version",
                "run_status",
                "verification_status",
                "reviewed_artifact_count",
            },
        )
        self.assertEqual(payload["receipt"]["run_status"], "COMPLETED")
        self.assertEqual(payload["receipt"]["verification_status"], "PASSED")
        self.assertEqual(payload["receipt"]["reviewed_run_version"], 7)
        self.assertEqual(payload["receipt"]["completed_run_version"], 8)
        self.assertEqual(payload["run"]["status"], "COMPLETED")
        call_name, call = service.calls[-1]
        self.assertEqual(call_name, "complete")
        self.assertEqual(call["identity"], identity)
        self.assertEqual(call["expected_run_version"], 7)
        self.assertEqual(call["document_review_versions"], ())
        self.assertEqual(
            set(call),
            {
                "identity",
                "matter_id",
                "run_id",
                "expected_run_version",
                "idempotency_key",
                "now",
                "document_review_versions",
            },
        )

        verified = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}"
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(verified.json()["run"]["status"], "COMPLETED")
        self.assertEqual(verified.json()["run"]["version"], 8)

        exact_intent = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/completion-intent",
            headers={"Idempotency-Key": "case-agent-final-review-0001"},
            params={"expected_run_version": 7},
        )
        self.assertEqual(exact_intent.status_code, 200, exact_intent.text)
        self.assertEqual(
            exact_intent.json()["receipt"], payload["receipt"]
        )
        service.completed_versions[case_id] = 10
        advanced_intent = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/completion-intent",
            headers={"Idempotency-Key": "case-agent-final-review-0001"},
            params={"expected_run_version": 7},
        )
        self.assertEqual(
            advanced_intent.json()["receipt"], payload["receipt"]
        )
        wrong_key = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/completion-intent",
            headers={"Idempotency-Key": "case-agent-final-review-other"},
            params={"expected_run_version": 7},
        )
        self.assertEqual(wrong_key.status_code, 200)
        self.assertIsNone(wrong_key.json()["receipt"])

        forged = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/complete",
            headers={"Idempotency-Key": "case-agent-final-review-0002"},
            json={
                "expected_run_version": 7,
                "verification_hash": "0" * 64,
                "artifacts": [],
                "actor": str(uuid4()),
            },
        )
        self.assertEqual(forged.status_code, 422)
        missing_key = client.post(
            f"/api/v1/cases/{case_id}/case-agent-runs/{run_id}/complete",
            json={"expected_run_version": 7},
        )
        self.assertEqual(missing_key.status_code, 422)

    def test_verified_case_agent_artifact_has_a_browser_safe_source_linked_review(self) -> None:
        control = _FakeCaseAgentControlService()
        evidence_page_id = str(uuid4())
        review_service = _FakeCaseAgentArtifactReviewService(
            control.artifact_id,
            evidence_page_id,
        )
        client, identity, _, _, _, _ = self._client(
            case_agent_control_service=control,
            case_agent_artifact_review_service=review_service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())
        response = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{control.run_id}/artifacts/{control.artifact_id}/review"
        )
        self.assertEqual(response.status_code, 200)
        review = response.json()["review"]
        self.assertEqual(review["sections"][0]["severity"], "HIGH")
        self.assertEqual(
            review["sections"][0]["items"][0]["sources"][0]["evidence_page_id"],
            evidence_page_id,
        )
        self.assertNotIn("object_key", response.text)
        self.assertNotIn("prompt", response.text.lower())
        self.assertEqual(review_service.calls[0]["identity"], identity)

    def test_verified_fact_source_may_open_its_server_resolved_evidence_page(self) -> None:
        control = _FakeCaseAgentControlService()
        evidence_page_id = str(uuid4())
        fact_id = str(uuid4())
        review_service = _FakeCaseAgentArtifactReviewService(
            control.artifact_id,
            evidence_page_id,
            source_kind="fact",
            source_id=fact_id,
        )
        client, _, _, _, _, _ = self._client(
            case_agent_control_service=control,
            case_agent_artifact_review_service=review_service,
            case_agent_runtime_ready=lambda _firm_id: True,
        )
        case_id = str(uuid4())

        response = client.get(
            f"/api/v1/cases/{case_id}/case-agent-runs/{control.run_id}/artifacts/{control.artifact_id}/review"
        )

        self.assertEqual(response.status_code, 200)
        source = response.json()["review"]["sections"][0]["items"][0]["sources"][0]
        self.assertEqual(source["source_kind"], "fact")
        self.assertEqual(source["source_id"], fact_id)
        self.assertEqual(source["evidence_page_id"], evidence_page_id)

    def test_dynamic_plan_capability_is_independent_and_projection_is_source_bound(self) -> None:
        service = _FakeDynamicCasePlanService()
        client, _, _, _, _, _ = self._client(dynamic_case_plan_service=service)
        session = client.get("/api/v1/session")
        self.assertTrue(session.json()["capabilities"]["can_review_dynamic_case_plan"])
        self.assertFalse(session.json()["capabilities"]["can_run_agent"])
        case_id = str(uuid4())
        response = client.get(f"/api/v1/cases/{case_id}/dynamic-case-plan")
        self.assertEqual(response.status_code, 200)
        plan = response.json()["plan"]
        self.assertEqual(plan["matter_id"], case_id)
        self.assertEqual(plan["items"][0]["source_counts"]["fact"], 1)
        self.assertEqual(plan["items"][0]["source_counts"]["official_authority"], 0)
        self.assertEqual(plan["items"][0]["sources"][0]["source_kind"], "CASE_CLAIM")
        for forbidden in ("prompt", "source_hash", "candidate_input_hash", "agent_version"):
            self.assertNotIn(forbidden, response.text.lower())

    def test_stale_dynamic_plan_is_explicit_and_cannot_look_current(self) -> None:
        client, _, _, _, _, _ = self._client(dynamic_case_plan_service=_FakeDynamicCasePlanService(stale=True))
        response = client.get(f"/api/v1/cases/{uuid4()}/dynamic-case-plan")
        self.assertEqual(response.status_code, 200)
        plan = response.json()["plan"]
        self.assertEqual(plan["status"], "STALE")
        self.assertFalse(plan["inputs_current"])
        self.assertTrue(plan["stale_reasons"])

    def test_dynamic_plan_item_decision_accepts_only_bounded_structured_changes(self) -> None:
        service = _FakeDynamicCasePlanService()
        client, identity, _, _, _, _ = self._client(dynamic_case_plan_service=service)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/dynamic-case-plans/{service.plan_id}/items/{service.item_id}/decision",
            headers={"Idempotency-Key": "dynamic-plan-decision-0001"},
            json={
                "expected_version": 9,
                "decision": "MODIFY",
                "reason_code": "REQUIRES_FURTHER_RESEARCH",
                "readiness_override": "NEEDS_RESEARCH",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["receipt"]["decision_status"], "CHANGE_REQUESTED")
        self.assertTrue(response.json()["receipt"]["requires_replanning"])
        _, kwargs = service.calls[-1]
        self.assertEqual(kwargs["identity"], identity)
        self.assertEqual(kwargs["expected_version"], 9)
        self.assertEqual(kwargs["idempotency_key"], "dynamic-plan-decision-0001")
        rejected = client.post(
            f"/api/v1/cases/{case_id}/dynamic-case-plans/{service.plan_id}/items/{service.item_id}/decision",
            headers={"Idempotency-Key": "dynamic-plan-decision-0002"},
            json={"expected_version": 9, "decision": "APPROVE", "reason_code": "VERIFIED_BY_COUNSEL", "prompt": "write a defence"},
        )
        self.assertEqual(rejected.status_code, 422)

    def test_dynamic_plan_activation_accepts_only_expected_version_not_browser_hash(self) -> None:
        service = _FakeDynamicCasePlanService()
        client, identity, _, _, _, _ = self._client(dynamic_case_plan_service=service)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/dynamic-case-plan/activate",
            headers={"Idempotency-Key": "dynamic-plan-activate-001"},
            json={"expected_version": 9},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["receipt"]["status"], "ACTIVE")
        self.assertEqual(response.json()["receipt"]["matter_version"], 10)
        _, kwargs = service.calls[-1]
        self.assertEqual(kwargs["identity"], identity)
        self.assertEqual(kwargs["matter_id"], case_id)
        self.assertNotIn("confirmation_hash", kwargs)
        self.assertNotIn("plan_id", kwargs)
        rejected = client.post(
            f"/api/v1/cases/{case_id}/dynamic-case-plan/activate",
            headers={"Idempotency-Key": "dynamic-plan-activate-002"},
            json={"expected_version": 9, "confirmation_hash": "a" * 64},
        )
        self.assertEqual(rejected.status_code, 422)

    def test_dynamic_plan_activation_post_surfaces_authoritative_source_gate(self) -> None:
        service = _FakeDynamicCasePlanService()

        def reject_nonactionable(**kwargs):
            service.calls.append(("activate", kwargs))
            raise CaseLedgerPersistenceBlocked(
                "Agent requested deliverables still need confirmed sources"
            )

        service.activate_current_plan = reject_nonactionable
        client, _, _, _, _, _ = self._client(dynamic_case_plan_service=service)
        response = client.post(
            f"/api/v1/cases/{uuid4()}/dynamic-case-plan/activate",
            headers={"Idempotency-Key": "dynamic-plan-activate-source-gate"},
            json={"expected_version": 9},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "VERSION_OR_IDEMPOTENCY_CONFLICT",
        )

    def test_agent_ledger_batches_are_browser_safe_and_capability_gated(self) -> None:
        service = _FakeAgentLedgerExtractionReviewService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_extraction_review_service=service
        )
        session = client.get("/api/v1/session")
        self.assertTrue(
            session.json()["capabilities"]["can_review_agent_ledger_extractions"]
        )
        self.assertFalse(
            session.json()["capabilities"]["can_run_agent_ledger_extraction"]
        )
        case_id = str(uuid4())
        response = client.get(
            f"/api/v1/cases/{case_id}/agent-ledger-extractions"
        )
        self.assertEqual(response.status_code, 200)
        batch = response.json()["batches"][0]
        self.assertEqual(batch["matter_id"], case_id)
        self.assertEqual(batch["low_risk_count"], 1)
        self.assertEqual(batch["exception_count"], 1)
        self.assertEqual(
            batch["exception_candidates"][0]["excerpts"][0]["evidence_page_id"],
            service.exception_page_id,
        )
        self.assertIn("收付款人不明确", response.text)
        for forbidden in (
            "candidate_hash",
            "object_key",
            "prompt",
            "provider",
            "run_id",
            "candidate_id",
            "reason_code",
        ):
            self.assertNotIn(forbidden, response.text.lower())
        _, kwargs = service.calls[-1]
        self.assertEqual(kwargs, {"identity": identity, "matter_id": case_id})

        unavailable = _FakeAgentLedgerExtractionReviewService(available=False)
        unavailable_client, *_ = self._client(
            agent_ledger_extraction_review_service=unavailable
        )
        self.assertFalse(
            unavailable_client.get("/api/v1/session").json()["capabilities"][
                "can_review_agent_ledger_extractions"
            ]
        )

    def test_ledger_history_review_and_new_extraction_runtime_are_separate(self) -> None:
        service = _FakeAgentLedgerExtractionReviewService()
        base_client, *_ = self._client(
            agent_ledger_extraction_review_service=service,
            case_agent_control_service=_FakeCaseAgentControlService(),
            case_agent_runtime_ready=lambda _firm_id: True,
            case_agent_ledger_runtime_ready=lambda _firm_id: False,
        )
        base_capabilities = base_client.get("/api/v1/session").json()[
            "capabilities"
        ]
        self.assertTrue(base_capabilities["can_run_case_agent"])
        self.assertTrue(
            base_capabilities["can_review_agent_ledger_extractions"]
        )
        self.assertFalse(
            base_capabilities["can_run_agent_ledger_extraction"]
        )

        ledger_client, *_ = self._client(
            agent_ledger_extraction_review_service=service,
            case_agent_control_service=_FakeCaseAgentControlService(),
            case_agent_runtime_ready=lambda _firm_id: True,
            case_agent_ledger_runtime_ready=lambda _firm_id: True,
        )
        ledger_capabilities = ledger_client.get("/api/v1/session").json()[
            "capabilities"
        ]
        self.assertTrue(ledger_capabilities["can_run_case_agent"])
        self.assertTrue(
            ledger_capabilities["can_review_agent_ledger_extractions"]
        )
        self.assertTrue(
            ledger_capabilities["can_run_agent_ledger_extraction"]
        )

    def test_agent_ledger_exception_followups_are_paged_browser_safe_and_capability_gated(self) -> None:
        service = _FakeAgentLedgerExceptionFollowupService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_exception_followup_service=service
        )
        self.assertTrue(
            client.get("/api/v1/session").json()["capabilities"][
                "can_review_agent_ledger_exception_followups"
            ]
        )
        case_id = str(uuid4())
        response = client.get(
            f"/api/v1/cases/{case_id}/agent-ledger-exception-followups?offset=0&limit=20"
        )
        self.assertEqual(response.status_code, 200)
        page = response.json()["page"]
        self.assertEqual(page["total_count"], 1)
        self.assertEqual(page["offset"], 0)
        self.assertEqual(page["control_health"], "HEALTHY")
        self.assertEqual(page["followups"][0]["kind"], "MORE_EVIDENCE")
        for forbidden in (
            "firm_id",
            "actor_id",
            "session_id",
            "run_id",
            "graph_id",
            "object_key",
            "subject_hash",
            "managed_evidence_request_id",
            "origin_exception_decision_id",
        ):
            self.assertNotIn(forbidden, response.text.lower())
        _, kwargs = service.calls[-1]
        self.assertEqual(
            kwargs,
            {
                "identity": identity,
                "matter_id": case_id,
                "offset": 0,
                "limit": 20,
            },
        )

        unavailable = _FakeAgentLedgerExceptionFollowupService(available=False)
        unavailable_client, *_ = self._client(
            agent_ledger_exception_followup_service=unavailable
        )
        self.assertFalse(
            unavailable_client.get("/api/v1/session").json()["capabilities"][
                "can_review_agent_ledger_exception_followups"
            ]
        )

    def test_agent_ledger_followup_sources_and_action_accept_only_advertised_browser_fields(self) -> None:
        service = _FakeAgentLedgerExceptionFollowupService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_exception_followup_service=service
        )
        case_id = str(uuid4())
        sources = client.get(
            f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/{service.followup_id}/eligible-managed-evidence-sources"
        )
        self.assertEqual(sources.status_code, 200)
        source_page = sources.json()["page"]
        self.assertEqual(source_page["total_count"], 1)
        source = source_page["sources"][0]
        self.assertEqual(
            set(source),
            {"object_type", "object_id", "display_label", "created_at"},
        )

        pages = client.get(
            f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/{service.followup_id}/evidence-pages?offset=0&limit=20"
        )
        self.assertEqual(pages.status_code, 200)
        self.assertEqual(pages.json()["page"]["evidence_page_ids"], [service.page_id])

        payload = {
            "expected_version": 9,
            "action": "CONFIRM_MORE_EVIDENCE",
            "reason_note": "已逐页核对新增流水，足以覆盖本次补证要求",
            "managed_evidence_sources": [
                {
                    "object_type": source["object_type"],
                    "object_id": source["object_id"],
                }
            ],
        }
        resolved = client.post(
            f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/{service.followup_id}/action",
            headers={"Idempotency-Key": "ledger-followup-web-action-0001"},
            json=payload,
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["receipt"]["terminal_state"], "SATISFIED")
        _, command = service.calls[-1]
        self.assertEqual(command["identity"], identity)
        self.assertNotIn("firm_id", command)
        self.assertNotIn("managed_evidence_request_id", command)
        self.assertNotIn("run_id", command)
        self.assertNotIn("graph_id", command)

        for forbidden in (
            "firm_id",
            "actor_id",
            "session_id",
            "managed_evidence_request_id",
            "run_id",
            "graph_id",
            "object_key",
            "subject_hash",
        ):
            rejected = client.post(
                f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/{service.followup_id}/action",
                headers={"Idempotency-Key": "ledger-followup-web-action-0002"},
                json={**payload, forbidden: str(uuid4())},
            )
            self.assertEqual(rejected.status_code, 422, forbidden)

    def test_agent_ledger_control_recovery_is_one_server_owned_action(self) -> None:
        service = _FakeAgentLedgerExceptionFollowupService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_exception_followup_service=service
        )
        case_id = str(uuid4())
        recovered = client.post(
            f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/recover-control",
            headers={"Idempotency-Key": "ledger-followup-web-recover-0001"},
            json={"expected_version": 9},
        )
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(
            recovered.json()["receipt"],
            {
                "matter_version": 9,
                "control_health": "HEALTHY",
                "recovery_started": True,
            },
        )
        _, command = service.calls[-1]
        self.assertEqual(command["identity"], identity)
        self.assertEqual(command["matter_id"], case_id)
        self.assertEqual(command["expected_matter_version"], 9)
        for forbidden in ("run_id", "old_run_id", "graph_id", "object_key", "subject_hash"):
            self.assertNotIn(forbidden, command)
            rejected = client.post(
                f"/api/v1/cases/{case_id}/agent-ledger-exception-followups/recover-control",
                headers={"Idempotency-Key": "ledger-followup-web-recover-0002"},
                json={"expected_version": 9, forbidden: str(uuid4())},
            )
            self.assertEqual(rejected.status_code, 422, forbidden)

    def test_agent_ledger_low_risk_confirmation_accepts_only_version(self) -> None:
        service = _FakeAgentLedgerExtractionReviewService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_extraction_review_service=service
        )
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/agent-ledger-extractions/{service.batch_id}/confirm-low-risk",
            headers={"Idempotency-Key": "ledger-extraction-confirm-0001"},
            json={"expected_version": 9},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["receipt"]["matter_version"], 10)
        self.assertEqual(response.json()["receipt"]["confirmed_fact_count"], 1)
        _, kwargs = service.calls[-1]
        self.assertEqual(
            kwargs,
            {
                "identity": identity,
                "matter_id": case_id,
                "batch_id": service.batch_id,
                "expected_version": 9,
                "idempotency_key": "ledger-extraction-confirm-0001",
            },
        )
        for forbidden_body in (
            {"expected_version": 9, "candidates": []},
            {"expected_version": 9, "candidate_hash": "a" * 64},
            {"expected_version": 9, "evidence_page_id": str(uuid4())},
            {"expected_version": 9, "content": "接受 AI"},
        ):
            rejected = client.post(
                f"/api/v1/cases/{case_id}/agent-ledger-extractions/{service.batch_id}/confirm-low-risk",
                headers={"Idempotency-Key": "ledger-extraction-confirm-reject-0001"},
                json=forbidden_body,
            )
            self.assertEqual(rejected.status_code, 422)
        stage = client.post(
            f"/api/v1/cases/{case_id}/agent-ledger-extractions/stage",
            headers={"Idempotency-Key": "ledger-extraction-stage-0001"},
            json={"expected_version": 9},
        )
        self.assertIn(stage.status_code, {404, 405})

    def test_agent_ledger_exception_routes_are_group_only_and_session_bound(self) -> None:
        service = _FakeAgentLedgerExtractionReviewService()
        client, identity, _, _, _, _ = self._client(
            agent_ledger_extraction_review_service=service
        )
        case_id = str(uuid4())
        members = client.get(
            f"/api/v1/cases/{case_id}/agent-ledger-extractions/{service.batch_id}/exception-groups/{service.group_id}/members?offset=0&limit=50"
        )
        self.assertEqual(members.status_code, 200)
        self.assertEqual(members.json()["page"]["total_count"], 1)
        self.assertNotIn("candidate_id", members.text.lower())
        self.assertNotIn("candidate_hash", members.text.lower())
        _, member_kwargs = service.calls[-1]
        self.assertEqual(
            member_kwargs,
            {
                "identity": identity,
                "matter_id": case_id,
                "batch_id": service.batch_id,
                "group_id": service.group_id,
                "offset": 0,
                "limit": 50,
            },
        )

        decided = client.post(
            f"/api/v1/cases/{case_id}/agent-ledger-extractions/{service.batch_id}/exception-groups/{service.group_id}/decision",
            headers={"Idempotency-Key": "ledger-exception-route-0001"},
            json={
                "expected_version": 9,
                "decision": "REQUEST_MORE_EVIDENCE",
                "reason": "PARTY_DATE_AMOUNT_UNCLEAR",
                "reason_note": "请当事人补充原始银行流水",
            },
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["receipt"]["group_id"], service.group_id)
        _, decision_kwargs = service.calls[-1]
        self.assertEqual(decision_kwargs["identity"], identity)
        self.assertEqual(decision_kwargs["idempotency_key"], "ledger-exception-route-0001")
        self.assertNotIn("session_id", decision_kwargs)
        for forbidden_body in (
            {
                "expected_version": 9,
                "decision": "REQUEST_MORE_EVIDENCE",
                "reason": "PARTY_DATE_AMOUNT_UNCLEAR",
                "candidate_ids": [],
            },
            {
                "expected_version": 9,
                "decision": "REQUEST_MORE_EVIDENCE",
                "reason": "PARTY_DATE_AMOUNT_UNCLEAR",
                "candidate_hash": "a" * 64,
            },
            {
                "expected_version": 9,
                "decision": "REQUEST_MORE_EVIDENCE",
                "reason": "PARTY_DATE_AMOUNT_UNCLEAR",
                "session_id": str(uuid4()),
            },
        ):
            rejected = client.post(
                f"/api/v1/cases/{case_id}/agent-ledger-extractions/{service.batch_id}/exception-groups/{service.group_id}/decision",
                headers={"Idempotency-Key": "ledger-exception-route-reject-0001"},
                json=forbidden_body,
            )
            self.assertEqual(rejected.status_code, 422)

    def test_agent_ledger_reextraction_capacity_errors_are_actionable(self) -> None:
        cases = (
            (
                WebAgentLedgerReextractionSourceWindowExceeded("source window"),
                "AGENT_LEDGER_REEXTRACTION_SOURCE_WINDOW_EXCEEDED",
                "64页",
                "拆分",
            ),
            (
                WebAgentLedgerReextractionCohortCapacityExceeded("cohort capacity"),
                "AGENT_LEDGER_REEXTRACTION_COHORT_CAPACITY_EXCEEDED",
                "99组",
                "撤回或替代",
            ),
        )
        for service_error, error_code, limit_text, action_text in cases:
            with self.subTest(error_code=error_code):
                service = _FakeAgentLedgerExtractionReviewService(
                    decision_error=service_error
                )
                client, _, _, _, _, _ = self._client(
                    agent_ledger_extraction_review_service=service
                )
                response = client.post(
                    f"/api/v1/cases/{uuid4()}/agent-ledger-extractions/{service.batch_id}/exception-groups/{service.group_id}/decision",
                    headers={"Idempotency-Key": "ledger-exception-capacity-0001"},
                    json={
                        "expected_version": 9,
                        "decision": "REQUEST_REEXTRACTION",
                        "reason": "SOURCE_QUALITY_INSUFFICIENT",
                    },
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], error_code)
                self.assertIn(limit_text, response.json()["error"]["message"])
                self.assertIn(action_text, response.json()["error"]["message"])

    def test_agent_whole_case_queue_is_explicit_batched_and_browser_safe(self) -> None:
        service = _FakeAgentMaterialReviewService()
        client, identity, _, _, _, _ = self._client(agent_material_review_service=service)
        case_id = str(uuid4())
        queued = client.post(
            f"/api/v1/cases/{case_id}/agent-runs",
            headers={"Idempotency-Key": "agent-whole-case-00001"},
            json={
                "expected_version": 9,
                "scope": "ALL_CURRENT_EVIDENCE",
                "material_review_authorized": True,
            },
        )
        self.assertEqual(queued.status_code, 202)
        run = queued.json()["run"]
        self.assertEqual(run["matter_id"], case_id)
        self.assertEqual(run["progress"]["total_pages"], 36)
        self.assertEqual(run["progress"]["batch_count"], 2)
        self.assertEqual(run["representation_profile"]["active_proceeding_role"], "DEFENDANT")
        queue_call = next(call for call in service.calls if call[0] == "queue")
        self.assertEqual(queue_call[1]["identity"], identity)
        self.assertEqual(queue_call[1]["expected_version"], 9)
        self.assertTrue(queue_call[1]["lawyer_confirmed"])
        for forbidden in ("input_hash", "object_key", "prompt", "provider_payload", "requested_by"):
            self.assertNotIn(forbidden, queued.text.lower())

        current = client.get(f"/api/v1/cases/{case_id}/agent-runs/current")
        self.assertEqual(current.status_code, 200)
        run_id = run["run_id"]
        details = client.get(f"/api/v1/cases/{case_id}/agent-runs/{run_id}")
        self.assertEqual(details.status_code, 200)
        candidates = client.get(f"/api/v1/cases/{case_id}/agent-runs/{run_id}/candidate-batches?limit=100")
        self.assertEqual(candidates.status_code, 200)
        candidate = candidates.json()["candidates"]["items"][0]
        self.assertEqual(candidate["evidence_page_id"], service.page_id)
        self.assertEqual(candidate["kind"], "UNCERTAIN")
        self.assertEqual(candidate["status"], "NEEDS_REVIEW")
        self.assertNotIn("source_file_sha256", candidates.text)

    def test_agent_low_risk_adoption_creates_candidates_without_browser_judgment_fields(self) -> None:
        agent = _FakeAgentMaterialReviewService()
        evidence = _FakeEvidenceReviewService()
        client, _, _, _, _, _ = self._client(
            agent_material_review_service=agent,
            evidence_review_service=evidence,
        )
        case_id, run_id = str(uuid4()), str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/agent-runs/{run_id}/evidence-decision-candidates",
            headers={"Idempotency-Key": "agent-evidence-stage-0001"},
            json={"expected_version": 9},
        )
        self.assertEqual(response.status_code, 201, response.text)
        batch = response.json()["candidate_batch"]
        self.assertEqual(batch["status"], "CANDIDATE")
        self.assertTrue(batch["requires_lead_lawyer_confirmation"])
        self.assertEqual(batch["include_count"], 1)
        _, kwargs = evidence.calls[-1]
        self.assertEqual(
            set(kwargs),
            {"identity", "matter_id", "run_id", "expected_version", "idempotency_key"},
        )
        for forbidden in ("disposition", "confidence", "input_hash", "source_file_sha256", "page_ids"):
            self.assertNotIn(forbidden, kwargs)
            self.assertNotIn(forbidden, response.request.content.decode("utf-8"))

    def test_agent_queue_rejects_implicit_or_page_selected_authority(self) -> None:
        service = _FakeAgentMaterialReviewService()
        client, _, _, _, _, _ = self._client(agent_material_review_service=service)
        case_id = str(uuid4())
        for body in (
            {"expected_version": 9, "scope": "ALL_CURRENT_EVIDENCE"},
            {"expected_version": 9, "scope": "SELECTED_PAGES", "material_review_authorized": True},
            {"expected_version": 9, "scope": "ALL_CURRENT_EVIDENCE", "material_review_authorized": False},
        ):
            response = client.post(
                f"/api/v1/cases/{case_id}/agent-runs",
                headers={"Idempotency-Key": f"agent-invalid-{uuid4()}"},
                json=body,
            )
            self.assertEqual(response.status_code, 422)
        self.assertEqual(service.calls, [])

    def test_representation_profile_supports_dynamic_procedural_role_not_a_defendant_flag(self) -> None:
        service = _FakeAgentMaterialReviewService()
        client, _, _, _, _, _ = self._client(agent_material_review_service=service)
        case_id = str(uuid4())
        response = client.get(f"/api/v1/cases/{case_id}/representation-profile")
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["active_proceeding_role"], "DEFENDANT")
        self.assertEqual(profile["proceeding_stage"], "FIRST_INSTANCE")
        self.assertEqual(profile["case_type"], "PRIVATE_LENDING_DISPUTE")
        self.assertNotIn("is_plaintiff", response.text)
        self.assertNotIn("is_defendant", response.text)

    def test_agent_unknown_provider_outcome_cannot_invite_browser_retry(self) -> None:
        service = _FakeAgentMaterialReviewService()
        service.run = WebAgentRunResponse(
            **{
                **service.run.__dict__,
                "status": "FAILED",
                "processed_pages": 0,
                "remaining_pages": 36,
                "completed_batch_count": 0,
                "candidate_count": 0,
                "retry_allowed": False,
                "failure_state": "PROVIDER_RESULT_UNKNOWN",
            }
        )
        client, _, _, _, _, _ = self._client(agent_material_review_service=service)
        response = client.get(f"/api/v1/cases/{uuid4()}/agent-runs/current")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["run"]["retry_allowed"])
        self.assertEqual(response.json()["run"]["failure_state"], "PROVIDER_RESULT_UNKNOWN")

    def test_configured_setup_status_reports_verticals_without_secrets(self) -> None:
        client, _, _, _, _, _ = self._client()
        status_response = client.get("/setup-status")
        self.assertEqual(status_response.status_code, 200)
        payload = status_response.json()
        self.assertEqual(payload["status"], "PRODUCTION_WEB_CONFIGURED")
        self.assertFalse(payload["derivative_worker"])
        self.assertNotIn("postgres", str(payload).lower())

    def test_create_list_and_stream_pdf_upload_with_browser_safe_receipts(self) -> None:
        client, identity, _, _, store, uploads = self._client()
        created = client.post(
            "/api/v1/cases",
            headers={"Idempotency-Key": "case-create-idempotency-0001"},
            json={"title": "周雅丽诉民间借贷纠纷"},
        )
        self.assertEqual(created.status_code, 201)
        case = created.json()["case"]
        case_id = case["case_id"]
        self.assertEqual(case["material_count"], 0)
        self.assertEqual(store.created[0][1], identity.actor)
        listed_case = client.get("/api/v1/cases").json()["cases"][0]
        self.assertEqual(listed_case["case_id"], case_id)
        self.assertEqual(listed_case["material_count"], 0)

        # The list projection is the durable browser-navigation authority.
        # A page reload cannot rely on the upload component's in-memory state.
        store.rows[0]["material_count"] = 1
        self.assertEqual(
            client.get("/api/v1/cases").json()["cases"][0]["material_count"],
            1,
        )

        slot = client.post(
            f"/api/v1/cases/{case_id}/material-uploads",
            json={
                "client_filename": "微信转账记录.pdf",
                "content_length": 8,
                "content_type": "application/pdf",
                "expected_version": 1,
            },
        )
        self.assertEqual(slot.status_code, 201)
        upload_id = slot.json()["upload"]["upload_id"]
        self.assertEqual(uploads.created[0]["matter_id"], case_id)

        uploaded = client.put(
            f"/api/v1/cases/{case_id}/material-uploads/{upload_id}/content",
            content=b"%PDF-1.7",
            headers={"Content-Type": "application/pdf"},
        )
        self.assertEqual(uploaded.status_code, 200)
        receipt = uploaded.json()["receipt"]
        self.assertEqual(receipt["display_name"], "微信转账记录.pdf")
        self.assertEqual(receipt["scan_status"], "PASSED")
        self.assertNotIn("object_key", uploaded.text)
        self.assertNotIn("path", uploaded.text.lower())
        self.assertEqual(uploads.accepted[0]["bytes"], b"%PDF-1.7")
        status_response = client.get(f"/api/v1/cases/{case_id}/material-uploads/{upload_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"]["state"], "COMPLETED")
        self.assertNotIn("path", status_response.text.lower())

    def test_common_material_dependency_must_implement_the_complete_recoverable_contract(self) -> None:
        identity = _identity()
        with self.assertRaisesRegex(ValueError, "common material upload"):
            create_web_app(
                WebApiDependencies(
                    settings=WebApiSettings(public_origin="https://workbench.example.test"),
                    oidc_login=_FakeOidcLogin(_grant(identity)),  # type: ignore[arg-type]
                    session_authority=_FakeSessionAuthority(identity),  # type: ignore[arg-type]
                    matter_store=_FakeMatterStore(),  # type: ignore[arg-type]
                    common_material_upload_service=object(),
                )
            )

    def test_common_docx_upload_binds_oidc_identity_two_idempotency_keys_and_safe_agent_receipt(self) -> None:
        common_uploads = _FakeCommonMaterialUploadService()
        client, identity, _, _, _, _ = self._client(common_material_upload_service=common_uploads)
        case_id = str(uuid4())
        create_path = f"/api/v1/cases/{case_id}/common-material-uploads"
        payload = {
            "client_filename": "借款协议.docx",
            "content_length": 19,
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "expected_version": 3,
        }

        missing_key = client.post(create_path, json=payload)
        self.assertEqual(missing_key.status_code, 422)
        self.assertEqual(common_uploads.created, [])
        extra_field = client.post(
            create_path,
            headers={"Idempotency-Key": "common-docx-extra-field-0001"},
            json={**payload, "actor_id": str(uuid4())},
        )
        self.assertEqual(extra_field.status_code, 422)
        self.assertEqual(common_uploads.created, [])

        slot = client.post(
            create_path,
            headers={"Idempotency-Key": "common-docx-slot-create-0001"},
            json=payload,
        )
        self.assertEqual(slot.status_code, 201, slot.text)
        self.assertEqual(slot.json()["upload"]["upload_id"], common_uploads.upload_id)
        self.assertEqual(
            common_uploads.created[-1],
            {
                "identity": identity,
                "matter_id": case_id,
                "expected_version": 3,
                "client_filename": "借款协议.docx",
                "declared_byte_size": 19,
                "declared_media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "idempotency_key": "common-docx-slot-create-0001",
            },
        )

        content_path = f"{create_path}/{common_uploads.upload_id}/content"
        missing_content_key = client.put(
            content_path,
            content=b"DOCX-is-binary-bytes",
            headers={"Content-Type": payload["content_type"]},
        )
        self.assertEqual(missing_content_key.status_code, 422)
        self.assertEqual(common_uploads.accepted, [])
        uploaded = client.put(
            content_path,
            content=b"DOCX-is-binary-bytes",
            headers={
                "Content-Type": payload["content_type"],
                "Idempotency-Key": "common-docx-content-put-0001",
            },
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(common_uploads.accepted[-1]["identity"], identity)
        self.assertEqual(common_uploads.accepted[-1]["matter_id"], case_id)
        self.assertEqual(common_uploads.accepted[-1]["idempotency_key"], "common-docx-content-put-0001")
        self.assertEqual(common_uploads.accepted[-1]["bytes"], b"DOCX-is-binary-bytes")

        receipt = uploaded.json()["receipt"]
        self.assertEqual(receipt["admitted_format"], "DOCX")
        self.assertEqual(receipt["agent_status"], "AGENT_READY")
        self.assertEqual(receipt["agent_source_ref"], f"material-object:{common_uploads.material_object_id}")
        self.assertTrue(all(receipt[name] is False for name in (
            "formal_fact", "formal_transaction", "legal_conclusion", "evidence_decision", "court_ready"
        )))
        for forbidden in ("object_key", "storage_object", "source_path", "file_path"):
            self.assertNotIn(forbidden, uploaded.text.lower())

        status_response = client.get(f"{create_path}/{common_uploads.upload_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"]["kind"], "COMMON")
        self.assertEqual(status_response.json()["status"]["receipt"]["agent_status"], "AGENT_READY")
        self.assertNotIn("path", status_response.text.lower())

    def test_common_jpeg_is_bound_to_evidence_page_and_pending_adapter_material_has_no_agent_ref(self) -> None:
        jpeg = _FakeCommonMaterialUploadService(admitted_format=CommonMaterialFormat.JPEG)
        client, _, _, _, _, _ = self._client(common_material_upload_service=jpeg)
        case_id = str(uuid4())
        create_path = f"/api/v1/cases/{case_id}/common-material-uploads"
        jpeg_slot = client.post(
            create_path,
            headers={"Idempotency-Key": "common-jpeg-slot-create-0001"},
            json={
                "client_filename": "付款凭证.jpg",
                "content_length": 19,
                "content_type": "image/jpeg",
                "expected_version": 2,
            },
        )
        self.assertEqual(jpeg_slot.status_code, 201)
        jpeg_upload = client.put(
            f"{create_path}/{jpeg.upload_id}/content",
            content=b"\xff\xd8\xffjpeg-bytes",
            headers={"Content-Type": "image/jpeg", "Idempotency-Key": "common-jpeg-content-put-0001"},
        )
        self.assertEqual(jpeg_upload.status_code, 200, jpeg_upload.text)
        jpeg_receipt = jpeg_upload.json()["receipt"]
        self.assertEqual(jpeg_receipt["route"], "VISUAL_OCR")
        self.assertEqual(jpeg_receipt["agent_status"], "AGENT_READY")
        self.assertEqual(jpeg_receipt["agent_source_ref"], f"evidence-page:{jpeg.evidence_page_id}")

        pending = _FakeCommonMaterialUploadService(
            admitted_format=CommonMaterialFormat.PPTX,
            agent_status=CommonMaterialAgentStatus.INGESTED_PENDING_ADAPTER,
        )
        pending_client, _, _, _, _, _ = self._client(common_material_upload_service=pending)
        pending_slot = pending_client.post(
            create_path,
            headers={"Idempotency-Key": "common-pptx-slot-create-0001"},
            json={
                "client_filename": "庭审演示.pptx",
                "content_length": 19,
                "content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "expected_version": 2,
            },
        )
        self.assertEqual(pending_slot.status_code, 201)
        pending_upload = pending_client.put(
            f"{create_path}/{pending.upload_id}/content",
            content=b"pptx-bytes",
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "Idempotency-Key": "common-pptx-content-put-0001",
            },
        )
        self.assertEqual(pending_upload.status_code, 200, pending_upload.text)
        pending_receipt = pending_upload.json()["receipt"]
        self.assertEqual(pending_receipt["agent_status"], "INGESTED_PENDING_ADAPTER")
        self.assertIsNone(pending_receipt["agent_source_ref"])

    def test_common_material_blocks_unsupported_content_and_never_invites_browser_reupload_after_unknown_outcome(self) -> None:
        unknown = _FakeCommonMaterialUploadService(reconciliation_required=True)
        client, _, _, _, _, _ = self._client(common_material_upload_service=unknown)
        case_id = str(uuid4())
        create_path = f"/api/v1/cases/{case_id}/common-material-uploads"
        unsupported = client.post(
            create_path,
            headers={"Idempotency-Key": "common-invalid-media-type-0001"},
            json={
                "client_filename": "旧格式.doc",
                "content_length": 19,
                "content_type": "application/msword",
                "expected_version": 1,
            },
        )
        self.assertEqual(unsupported.status_code, 422)
        self.assertEqual(unknown.created, [])

        slot = client.post(
            create_path,
            headers={"Idempotency-Key": "common-unknown-slot-create-0001"},
            json={
                "client_filename": "借款协议.docx",
                "content_length": 19,
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "expected_version": 1,
            },
        )
        self.assertEqual(slot.status_code, 201)
        bad_content_type = client.put(
            f"{create_path}/{unknown.upload_id}/content",
            content=b"unexpected",
            headers={"Content-Type": "application/pdf", "Idempotency-Key": "common-wrong-content-type-0001"},
        )
        self.assertEqual(bad_content_type.status_code, 422)
        self.assertEqual(unknown.accepted, [])
        uncertain = client.put(
            f"{create_path}/{unknown.upload_id}/content",
            content=b"DOCX-is-binary-bytes",
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "Idempotency-Key": "common-unknown-content-put-0001",
            },
        )
        self.assertEqual(uncertain.status_code, 409)
        self.assertEqual(uncertain.json()["error"]["code"], "COMMON_MATERIAL_RESULT_UNKNOWN")
        self.assertIn("请勿重复上传", uncertain.json()["error"]["message"])

    def test_case_posture_get_projects_only_lawyer_state_and_closed_options(self) -> None:
        posture = _FakeCasePostureService(current=True)
        client, identity, _, _, _, _ = self._client(case_posture_service=posture)
        case_id = str(uuid4())
        response = client.get(f"/api/v1/cases/{case_id}/case-posture")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(posture.calls[0], ("state", {"identity": identity, "matter_id": case_id}))
        payload = response.json()
        self.assertEqual(payload["posture"]["status"], "CURRENT")
        self.assertTrue(payload["posture"]["can_confirm"])
        self.assertEqual(payload["posture"]["profile"]["represented_position"], "DEFENDANT")
        self.assertEqual(payload["posture"]["profile"]["represented_party_kind"], "NATURAL_PERSON")
        self.assertEqual(payload["posture"]["profile"]["forum_type"], "PEOPLE_COURT")
        self.assertIn("CIVIL.PRIVATE_LENDING", payload["options"]["case_types"])
        self.assertIn("PEOPLE_COURT", payload["options"]["forum_types"])
        for forbidden in ("hash", "firm_id", "actor_id", "session_id", "basis"):
            self.assertNotIn(forbidden, response.text.lower())

        collaborator_posture = _FakeCasePostureService()
        collaborator, _, _, _, _, _ = self._client(
            case_posture_service=collaborator_posture,
            role=Role.COLLABORATING_LAWYER,
        )
        readonly = collaborator.get(f"/api/v1/cases/{case_id}/case-posture")
        self.assertEqual(readonly.status_code, 200)
        self.assertFalse(readonly.json()["posture"]["can_confirm"])
        blocked = collaborator.post(
            f"/api/v1/cases/{case_id}/case-posture/parties",
            headers={"Idempotency-Key": "posture-collaborator-write-0001"},
            json={"expected_version": 1, "party_kind": "NATURAL_PERSON", "display_label": "周雅丽"},
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()["error"]["code"], "CASE_POSTURE_REQUEST_BLOCKED")

    def test_case_posture_five_confirmations_use_structured_fields_idempotency_and_advance_version(self) -> None:
        posture = _FakeCasePostureService()
        client, identity, _, _, _, _ = self._client(case_posture_service=posture)
        case_id = str(uuid4())
        base = f"/api/v1/cases/{case_id}/case-posture"
        operations = (
            (
                "parties",
                "posture-party-confirm-0001",
                {"expected_version": 1, "party_kind": "NATURAL_PERSON", "display_label": "周雅丽"},
                "CONFIRM_PARTY",
                2,
            ),
            (
                "proceedings",
                "posture-proceeding-confirm-0001",
                {
                    "expected_version": 2,
                    "forum_type": "PEOPLE_COURT",
                    "case_type_code": "CIVIL.PRIVATE_LENDING",
                    "procedure_stage": "FIRST_INSTANCE",
                },
                "CONFIRM_PROCEEDING",
                3,
            ),
            (
                "positions",
                "posture-position-confirm-0001",
                {
                    "expected_version": 3,
                    "proceeding_id": posture.proceeding_id,
                    "party_id": posture.party_id,
                    "position_code": "DEFENDANT",
                },
                "CONFIRM_POSITION",
                4,
            ),
            (
                "engagements",
                "posture-engagement-confirm-0001",
                {
                    "expected_version": 4,
                    "proceeding_id": posture.proceeding_id,
                    "represented_party_id": posture.party_id,
                    "authority_scope_code": "GENERAL_AUTHORITY",
                    "engagement_state": "ACTIVE",
                },
                "CONFIRM_ENGAGEMENT",
                5,
            ),
            (
                "profile",
                "posture-profile-confirm-0001",
                {
                    "expected_version": 5,
                    "represented_party_id": posture.party_id,
                    "proceeding_id": posture.proceeding_id,
                    "position_id": posture.position_id,
                    "engagement_id": posture.engagement_id,
                },
                "CONFIRM_CURRENT_PROFILE",
                6,
            ),
        )
        for route, key, body, action, next_version in operations:
            response = client.post(f"{base}/{route}", headers={"Idempotency-Key": key}, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            receipt = response.json()["receipt"]
            self.assertEqual(receipt["action"], action)
            self.assertEqual(receipt["matter_version"], next_version)
            self.assertTrue(receipt["refresh_posture_state"])
            self.assertNotIn("hash", response.text.lower())
            self.assertNotIn("firm_id", response.text.lower())
            self.assertNotIn("actor_id", response.text.lower())

        command_calls = posture.calls
        self.assertEqual([name for name, _ in command_calls], [item[3] for item in operations])
        for (name, kwargs), (_, key, body, _, _) in zip(command_calls, operations):
            self.assertEqual(kwargs["identity"], identity, name)
            self.assertEqual(kwargs["matter_id"], case_id, name)
            self.assertEqual(kwargs["expected_version"], body["expected_version"], name)
            self.assertEqual(kwargs["idempotency_key"], key, name)

    def test_case_posture_complete_confirmation_is_one_lawyer_action(self) -> None:
        posture = _FakeCasePostureService()
        client, identity, _, _, _, _ = self._client(case_posture_service=posture)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/case-posture/confirm",
            headers={"Idempotency-Key": "posture-complete-confirm-0001"},
            json={
                "expected_version": 9,
                "party_kind": "NATURAL_PERSON",
                "display_label": "周雅丽",
                "forum_type": "PEOPLE_COURT",
                "case_type_code": "CIVIL.PRIVATE_LENDING",
                "procedure_stage": "FIRST_INSTANCE",
                "position_code": "DEFENDANT",
                "authority_scope_code": "GENERAL_AUTHORITY",
                "engagement_state": "ACTIVE",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()["receipt"]
        self.assertEqual(receipt["action"], "CONFIRM_COMPLETE_POSTURE")
        self.assertEqual(receipt["matter_version"], 14)
        self.assertEqual(receipt["profile_id"], posture.profile_id)
        self.assertNotIn("hash", response.text.lower())
        name, kwargs = posture.calls[-1]
        self.assertEqual(name, "CONFIRM_COMPLETE_POSTURE")
        self.assertEqual(kwargs["identity"], identity)
        self.assertEqual(kwargs["matter_id"], case_id)
        self.assertEqual(kwargs["idempotency_key"], "posture-complete-confirm-0001")

    def test_case_posture_rejects_browser_extra_fields_missing_key_and_service_rejected_enum(self) -> None:
        posture = _FakeCasePostureService()
        client, _, _, _, _, _ = self._client(case_posture_service=posture)
        case_id = str(uuid4())
        party_path = f"/api/v1/cases/{case_id}/case-posture/parties"
        valid = {"expected_version": 1, "party_kind": "NATURAL_PERSON", "display_label": "周雅丽"}
        missing_key = client.post(party_path, json=valid)
        self.assertEqual(missing_key.status_code, 422)
        self.assertEqual(posture.calls, [])
        extra = client.post(
            party_path,
            headers={"Idempotency-Key": "posture-extra-field-block-0001"},
            json={**valid, "confirmation_hash": "x" * 64},
        )
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(posture.calls, [])
        unknown = client.post(
            party_path,
            headers={"Idempotency-Key": "posture-unknown-enum-block-0001"},
            json={**valid, "party_kind": "UNTRUSTED_BROWSER_CODE"},
        )
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["error"]["code"], "CASE_POSTURE_REQUEST_BLOCKED")
        self.assertEqual(posture.calls[-1][0], "CONFIRM_PARTY")

    def test_create_case_rejects_browser_supplied_agent_identities(self) -> None:
        client, _, _, _, store, _ = self._client()
        response = client.post(
            "/api/v1/cases",
            headers={"Idempotency-Key": "case-create-no-browser-workers-0001"},
            json={
                "title": "浏览器不得指定服务身份",
                "case_agent_execution_actor_id": str(uuid4()),
                "case_agent_verifier_actor_id": str(uuid4()),
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(store.created, [])

    def test_rejects_non_pdf_upload_and_clears_cookies_on_logout(self) -> None:
        client, identity, sessions, _, _, _ = self._client()
        case_id = str(uuid4())
        blocked = client.put(
            f"/api/v1/cases/{case_id}/material-uploads/{uuid4()}/content",
            content=b"not a pdf",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(blocked.status_code, 422)
        logout = client.post("/api/v1/auth/logout")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(sessions.revoked, [identity.session_id])
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])

    def test_unknown_post_object_store_outcome_is_not_reported_as_a_rejected_pdf(self) -> None:
        client, _, _, _, _, _ = self._client(reconciliation_required=True)
        case_id = str(uuid4())
        slot = client.post(
            f"/api/v1/cases/{case_id}/material-uploads",
            json={
                "client_filename": "微信转账记录.pdf",
                "content_length": 8,
                "content_type": "application/pdf",
                "expected_version": 1,
            },
        )
        upload_id = slot.json()["upload"]["upload_id"]
        uncertain = client.put(
            f"/api/v1/cases/{case_id}/material-uploads/{upload_id}/content",
            content=b"%PDF-1.7",
            headers={"Content-Type": "application/pdf"},
        )
        self.assertEqual(uncertain.status_code, 409)
        self.assertEqual(uncertain.json()["error"]["code"], "UPLOAD_RECONCILIATION_REQUIRED")
        self.assertIn("请勿重复上传", uncertain.json()["error"]["message"])

    def test_evidence_page_preview_returns_only_non_cacheable_png_bytes(self) -> None:
        preview = _FakePagePreviewService()
        client, identity, _, _, _, _ = self._client(page_preview_service=preview)
        case_id = str(uuid4())
        page_id = str(uuid4())
        response = client.get(f"/api/v1/cases/{case_id}/evidence-pages/{page_id}/preview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(response.content, preview.content)
        self.assertEqual(preview.calls, [{"actor": identity.actor, "matter_id": case_id, "evidence_page_id": page_id}])
        self.assertNotIn("object_key", response.text)
        self.assertNotIn(".pdf", response.text.lower())

    def test_case_review_returns_real_server_projection_without_internal_objects(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, _, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        response = client.get(f"/api/v1/cases/{case_id}/review")
        self.assertEqual(response.status_code, 200)
        review = response.json()["review"]
        self.assertEqual(review["matter_id"], case_id)
        self.assertEqual(review["facts"][0]["text"], "借款本金已经实际交付。")
        self.assertEqual(review["transactions"][0]["local_date"], "2020-08-20")
        self.assertEqual(review["payment_classifications"][0]["status"], "APPROVED")
        self.assertEqual(
            review["payment_classifications"][0]["allocations"][0]["obligation_label"],
            "obligation-main",
        )
        self.assertNotIn("decision_hash", response.text)
        self.assertNotIn("confirmation_hash", response.text)
        self.assertNotIn("object_key", response.text)

    def test_money_projection_trims_database_scale_but_rejects_real_excess_precision(self) -> None:
        self.assertEqual(_project_money(Decimal("30000.000000")), "30000")
        self.assertEqual(_project_money(Decimal("12.340000")), "12.34")
        with self.assertRaises(WebRequestBlocked):
            _project_money(Decimal("12.34567"))

    def test_rate_projection_keeps_deterministic_rule_precision_separate_from_money(self) -> None:
        self.assertEqual(_project_rate(Decimal("0.000000000000")), "0")
        self.assertEqual(_project_rate("0.123456789012"), "0.123456789012")
        with self.assertRaises(WebRequestBlocked):
            _project_rate("0.1234567890123")

    def test_unconfigured_case_review_is_not_replaced_with_demo_data(self) -> None:
        client, _, _, _, _, _ = self._client()
        response = client.get(f"/api/v1/cases/{uuid4()}/review")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "WEB_FEATURE_NOT_CONFIGURED")

    def test_fact_decision_hash_is_server_derived_and_receipt_is_narrow(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, identity, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/facts/{ledger.fact_id}/decision",
            headers={"Idempotency-Key": "fact-decision-000001"},
            json={"expected_version": 3, "status": "CONFIRMED"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ledger.decided["actor"], identity.actor)
        expected_hash = sha256(f"case-fact-decision-v1:{case_id}:{ledger.fact_id}:3:CONFIRMED".encode("utf-8")).hexdigest()
        self.assertEqual(ledger.decided["decision_hash"], expected_hash)
        self.assertNotIn("decision_hash", response.text)
        self.assertEqual(response.json()["receipt"]["object_id"], ledger.fact_id)

    def test_fact_decision_recovery_does_not_issue_another_decision(self) -> None:
        ledger=_FakeCaseLedgerStore()
        calls=[]
        ledger.find_fact_decision_by_key=lambda **kw:calls.append(kw)
        client,identity,*_=self._client(case_ledger_store=ledger)
        case_id=str(uuid4())
        path=f'/api/v1/cases/{case_id}/facts/{ledger.fact_id}/decision-receipt'
        result=client.get(path,headers={'Idempotency-Key':'recover-original-decision'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertIsNone(result.json()['receipt'])
        self.assertFalse(result.json()['court_ready'])
        self.assertEqual(calls[0]['actor'],identity.actor)
        self.assertEqual(calls[0]['fact_id'],ledger.fact_id)
        self.assertEqual(client.get(path).status_code,422)

    def test_claim_and_transaction_confirmations_are_server_hashed(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, _, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        claim = client.post(
            f"/api/v1/cases/{case_id}/claims/{ledger.claim_id}/confirm-scope",
            headers={"Idempotency-Key": "claim-scope-000001"},
            json={"expected_version": 3},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(ledger.claim_confirmation["confirmation_hash"], sha256(f"case-claim-scope-v1:{case_id}:{ledger.claim_id}:3:CONFIRMED_SCOPE".encode("utf-8")).hexdigest())
        transaction = client.post(
            f"/api/v1/cases/{case_id}/transactions/{ledger.transaction_id}/confirm",
            headers={"Idempotency-Key": "transaction-confirm-01"},
            json={"expected_version": 5},
        )
        self.assertEqual(transaction.status_code, 200)
        self.assertEqual(ledger.transaction_confirmation["confirmation_hash"], sha256(f"case-transaction-v1:{case_id}:{ledger.transaction_id}:5:CONFIRMED".encode("utf-8")).hexdigest())

    def test_claim_response_is_source_bound_and_server_hashed(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, identity, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/claims/{ledger.claim_id}/response",
            headers={"Idempotency-Key": "claim-response-web-01"},
            json={
                "expected_version": 3,
                "position": "DISPUTE",
                "confirmed_fact_ids": [ledger.fact_id],
                "partial_amount": None,
                "currency": None,
                "approval_hash": "browser-cannot-supply-this",
            },
        )
        self.assertEqual(response.status_code, 422)

        response = client.post(
            f"/api/v1/cases/{case_id}/claims/{ledger.claim_id}/response",
            headers={"Idempotency-Key": "claim-response-web-02"},
            json={
                "expected_version": 3,
                "position": "DISPUTE",
                "confirmed_fact_ids": [ledger.fact_id],
                "partial_amount": None,
                "currency": None,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ledger.claim_response["actor"], identity.actor)
        self.assertEqual(ledger.claim_response["confirmed_fact_ids"], (ledger.fact_id,))
        self.assertEqual(ledger.claim_response["position"].value, "DISPUTE")
        expected_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-claim-response-v1",
                    "matter_id": case_id,
                    "claim_id": ledger.claim_id,
                    "expected_version": 3,
                    "position": "DISPUTE",
                    "confirmed_fact_ids": (ledger.fact_id,),
                    "partial_amount": None,
                    "currency": None,
                    "approved_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(ledger.claim_response["approval_hash"], expected_hash)
        self.assertNotIn("approval_hash", response.text)

    def test_claim_candidate_uses_only_server_resolved_confirmed_facts(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, identity, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        response = client.post(
            f"/api/v1/cases/{case_id}/claims/candidates",
            headers={"Idempotency-Key": "claim-candidate-web-01"},
            json={
                "expected_version": 3,
                "original_claim_text": "请求支付已交付货物的剩余价款。",
                "claimed_amount": "100000.00",
                "currency": "CNY",
                "confirmed_fact_ids": [ledger.fact_id],
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ledger.claim_candidate["actor"], identity.actor)
        self.assertEqual(ledger.claim_candidate["confirmed_fact_ids"], (ledger.fact_id,))
        self.assertEqual(ledger.claim_candidate["claimed_amount"], Decimal("100000.00"))
        self.assertNotIn("evidence", response.text)
        self.assertNotIn("hash", response.text)

        forged = client.post(
            f"/api/v1/cases/{case_id}/claims/candidates",
            headers={"Idempotency-Key": "claim-candidate-web-02"},
            json={
                "expected_version": 3,
                "original_claim_text": "请求支付价款。",
                "confirmed_fact_ids": [ledger.fact_id],
                "evidence_links": [{"original_file_sha256": "a" * 64}],
            },
        )
        self.assertEqual(forged.status_code, 422)

    def test_dispute_issue_candidate_and_confirmation_keep_approval_hash_server_owned(self) -> None:
        ledger = _FakeCaseLedgerStore()
        client, identity, _, _, _, _ = self._client(case_ledger_store=ledger)
        case_id = str(uuid4())
        candidate = client.post(
            f"/api/v1/cases/{case_id}/issues/candidates",
            headers={"Idempotency-Key": "issue-candidate-web-01"},
            json={
                "expected_version": 5,
                "question": "案涉货物是否已经完成交付，剩余价款是否到期？",
                "claim_ids": [ledger.claim_id],
                "confirmed_fact_ids": [ledger.fact_id],
            },
        )
        self.assertEqual(candidate.status_code, 201)
        self.assertEqual(ledger.issue_candidate["actor"], identity.actor)
        self.assertEqual(ledger.issue_candidate["claim_ids"], (ledger.claim_id,))
        self.assertEqual(ledger.issue_candidate["confirmed_fact_ids"], (ledger.fact_id,))

        confirmed = client.post(
            f"/api/v1/cases/{case_id}/issues/{ledger.issue_id}/confirm",
            headers={"Idempotency-Key": "issue-confirm-web-01"},
            json={"expected_version": 6},
        )
        self.assertEqual(confirmed.status_code, 200)
        expected_hash = sha256(
            f"case-dispute-issue-v1:{case_id}:{ledger.issue_id}:6:CONFIRMED".encode("utf-8")
        ).hexdigest()
        self.assertEqual(ledger.issue_confirmation["approval_hash"], expected_hash)
        self.assertNotIn("approval_hash", confirmed.text)

    def test_legal_review_returns_official_metadata_without_private_locator(self) -> None:
        client, _, _, _, _, _ = self._client(legal_store=_FakeLegalStore())
        response = client.get(f"/api/v1/cases/{uuid4()}/legal-review")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["review"]
        self.assertEqual(payload["sources"][0]["verification_status"], "VERIFIED")
        self.assertEqual(payload["rule_versions"][0]["derived_annual_rate"], "15.40")
        self.assertNotIn("storage_object_key", response.text)

    def test_legal_review_explains_stale_bundle_without_exposing_old_bundle(self) -> None:
        legal = _FakeLegalStore(
            bundle_reconfirmation={
                "version": 3,
                "status": "STALE",
                "stale_reason": "上游案件事实、证据或交易发生正式变化。",
                "stale_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
                "bundle_hash": "d" * 64,
            }
        )
        client, _, _, _, _, _ = self._client(legal_store=legal)
        response = client.get(f"/api/v1/cases/{uuid4()}/legal-review")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["review"]
        self.assertIsNone(payload["current_bundle"])
        self.assertEqual(payload["bundle_reconfirmation"]["version"], 3)
        self.assertEqual(payload["bundle_reconfirmation"]["reason"], "上游案件事实、证据或交易发生正式变化。")
        self.assertNotIn("bundle_hash", payload["bundle_reconfirmation"])

    def test_official_source_capture_uses_only_the_server_catalogue(self) -> None:
        captures = _FakeOfficialSourceCaptureStore()
        client, identity, _, _, _, _ = self._client(
            official_source_capture_store=captures
        )
        case_id = str(uuid4())

        listed = client.get(f"/api/v1/cases/{case_id}/official-source-captures")
        self.assertEqual(listed.status_code, 200)
        catalogue = listed.json()["catalogue"]
        self.assertIn(
            "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
            {item["source_id"] for item in catalogue},
        )
        self.assertNotIn("target_url", listed.text)
        self.assertNotIn("query", listed.text)

        queued = client.post(
            f"/api/v1/cases/{case_id}/official-source-captures",
            headers={"Idempotency-Key": "official-source-capture-0001"},
            json={
                "expected_version": 6,
                "source_id": "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
                "target_url": "https://attacker.invalid/never-accepted",
            },
        )
        self.assertEqual(queued.status_code, 201, queued.text)
        command = captures.calls[-1]
        self.assertEqual(command["actor"], identity.actor)
        self.assertEqual(
            command["target_url"],
            "https://www.court.gov.cn/zixun/xiangqing/282621.html",
        )
        self.assertEqual(
            command["query_sha256"],
            sha256(
                "民间借贷司法解释 2020年第二次修正 第二十四条至第三十一条 利率保护 过渡规则".encode("utf-8")
            ).hexdigest(),
        )
        self.assertNotIn("target_url", queued.text)
        self.assertNotIn("query_sha256", queued.text)
        self.assertNotIn("authorization_hash", queued.text)

    def test_official_source_review_and_registration_keep_hashes_and_license_data_server_side(self) -> None:
        captures = _FakeOfficialSourceCaptureStore()
        captures.run_id = str(uuid4())
        legal = _FakeLegalStore()
        client, identity, _, _, _, _ = self._client(
            legal_store=legal,
            official_source_capture_store=captures,
        )
        case_id = str(uuid4())

        review = client.post(
            f"/api/v1/cases/{case_id}/official-source-captures/{captures.run_id}/review",
            headers={"Idempotency-Key": "official-source-review-0001"},
            json={
                "expected_version": 6,
                "decision": "APPROVE_FOR_REGISTRATION",
                "provision_locator": "第二十五条、第三十一条",
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(captures.review_calls[-1]["actor"], identity.actor)
        self.assertNotIn("review_hash", review.text)

        registration = client.post(
            f"/api/v1/cases/{case_id}/official-source-captures/{captures.run_id}/register",
            headers={"Idempotency-Key": "official-source-register-0001"},
            json={"expected_version": 7},
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        call = legal.registration_calls[-1]
        self.assertEqual(call["actor"], identity.actor)
        self.assertIn("最高人民法院", call["license_basis"])
        self.assertNotIn("license_review_hash", registration.text)
        self.assertNotIn("registration_hash", registration.text)

    def test_legal_event_confirmation_binds_only_selected_material_pages(self) -> None:
        legal = _FakeLegalStore()
        client, identity, _, _, _, _ = self._client(legal_store=legal)
        case_id = str(uuid4())
        page_id = str(uuid4())

        response = client.post(
            f"/api/v1/cases/{case_id}/legal-events",
            headers={"Idempotency-Key": "legal-event-confirm-0001"},
            json={
                "expected_version": 6,
                "event_kind": "CONTRACT_SIGNED",
                "local_date": "2024-05-08",
                "evidence_page_ids": [page_id],
                "approval_hash": "f" * 64,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        call = legal.legal_event_calls[-1]
        self.assertEqual(call["actor"], identity.actor)
        self.assertEqual(call["evidence_ids"], (page_id,))
        self.assertEqual(call["event_kind"].value, "CONTRACT_SIGNED")
        self.assertNotIn("approval_hash", response.text)
        self.assertNotIn("evidence_page_ids", response.text)

    def test_case_readiness_returns_specific_blockers_without_case_text_projection(self) -> None:
        client, _, _, _, _, _ = self._client(legal_store=_FakeLegalStore(), case_ledger_store=_FakeCaseLedgerStore())
        response = client.get(f"/api/v1/cases/{uuid4()}/readiness")
        self.assertEqual(response.status_code, 200)
        readiness = response.json()["readiness"]
        self.assertEqual(readiness["checks"][0]["status"], "BLOCKED")
        self.assertIn("逐页", readiness["next_action"])
        self.assertNotIn("借款本金已经实际交付", response.text)
        self.assertNotIn("storage_object_key", response.text)

    def test_unconfigured_page_preview_is_a_service_boundary_not_a_synthetic_image(self) -> None:
        client, _, _, _, _, _ = self._client()
        response = client.get(f"/api/v1/cases/{uuid4()}/evidence-pages/{uuid4()}/preview")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "WEB_FEATURE_NOT_CONFIGURED")

    def test_evidence_review_routes_keep_receipts_narrow_and_require_explicit_commands(self) -> None:
        review = _FakeEvidenceReviewService()
        client, _, _, _, _, _ = self._client(evidence_review_service=review)
        case_id = str(uuid4())
        page_id = str(uuid4())
        decision_id = str(uuid4())
        annotation_id = str(uuid4())

        summary = client.get(f"/api/v1/cases/{case_id}/evidence-summary")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["summary"]["matter_id"], case_id)
        pages = client.get(f"/api/v1/cases/{case_id}/evidence-pages?limit=10")
        self.assertEqual(pages.status_code, 200)
        self.assertEqual(pages.json()["matter_version"], 3)

        decision = client.post(
            f"/api/v1/cases/{case_id}/evidence-pages/{page_id}/decisions",
            headers={"Idempotency-Key": "decision-candidate-0001"},
            json={"expected_version": 3, "disposition": "INCLUDE", "reason": "与争议款项相关"},
        )
        self.assertEqual(decision.status_code, 201)
        self.assertNotIn("idempotency_key", decision.text)
        self.assertIn("audit_event_id", decision.json()["receipt"])

        confirmed = client.post(
            f"/api/v1/cases/{case_id}/evidence-page-decisions/{decision_id}/confirm",
            headers={"Idempotency-Key": "decision-confirm-0001"},
            json={"expected_version": 4},
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(review.calls[-1][0], "decision-confirm")

        second_decision_id = str(uuid4())
        batch_confirmed = client.post(
            f"/api/v1/cases/{case_id}/evidence-page-decisions/confirm-batch",
            headers={"Idempotency-Key": "decision-batch-confirm-0001"},
            json={"expected_version": 4, "decision_ids": [decision_id, second_decision_id]},
        )
        self.assertEqual(batch_confirmed.status_code, 200, batch_confirmed.text)
        self.assertEqual(review.calls[-1][0], "decision-batch-confirm")
        self.assertEqual(set(review.calls[-1][1]["decision_ids"]), {decision_id, second_decision_id})
        self.assertNotIn("approval_hash", batch_confirmed.text)

        duplicate_batch = client.post(
            f"/api/v1/cases/{case_id}/evidence-page-decisions/confirm-batch",
            headers={"Idempotency-Key": "decision-batch-confirm-0002"},
            json={"expected_version": 4, "decision_ids": [decision_id, decision_id]},
        )
        self.assertEqual(duplicate_batch.status_code, 422)

        annotation = client.post(
            f"/api/v1/cases/{case_id}/evidence-pages/{page_id}/annotations",
            headers={"Idempotency-Key": "annotation-candidate-01"},
            json={"expected_version": 4, "x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9, "label": "还款记录"},
        )
        self.assertEqual(annotation.status_code, 201)
        confirmed_annotation = client.post(
            f"/api/v1/cases/{case_id}/evidence-annotations/{annotation_id}/confirm",
            headers={"Idempotency-Key": "annotation-confirm-01"},
            json={"expected_version": 5},
        )
        self.assertEqual(confirmed_annotation.status_code, 200)
        self.assertEqual(review.calls[-1][0], "annotation-confirm")

        locked = client.post(
            f"/api/v1/cases/{case_id}/evidence-manifest/lock",
            headers={"Idempotency-Key": "manifest-lock-0001"},
            json={"expected_version": 6, "readiness_hash": "a" * 64},
        )
        self.assertEqual(locked.status_code, 200)
        self.assertEqual(review.calls[-1][0], "manifest-lock")

        queued = client.post(
            f"/api/v1/cases/{case_id}/evidence-derivative-runs",
            headers={"Idempotency-Key": "derivative-enqueue-01"},
            json={"expected_version": 7, "manifest_id": str(uuid4())},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(review.calls[-1][0], "derivative-enqueue")


if __name__ == "__main__":
    unittest.main()
