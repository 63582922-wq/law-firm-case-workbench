"""Independent, fail-closed persistent preview API.

The synthetic Alpha application never imports or mounts these routes. Without
explicit dependencies this factory exposes only a disabled health response.
"""

from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from ipaddress import ip_address
from typing import Annotated, Protocol
from urllib.parse import quote
from uuid import UUID
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from case_kernel.case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    PostgresCaseLedgerStore,
)
from case_kernel.stable_pagination import DEFAULT_PAGE_SIZE, StablePaginationBlocked
from case_kernel.artifact_access import (
    ArtifactAccessPurpose,
    EphemeralArtifactAccessBroker,
)
from case_kernel.evidence_refs import EvidenceLink, EvidenceReferenceBlocked
from case_kernel.evidence_manifest import PageDisposition
from case_kernel.evidence_manifest_postgres import (
    PersistentEvidenceSnapshot,
    PostgresEvidenceManifestStore,
)
from case_kernel.errors import AuthorizationDenied, IdempotencyConflict, VersionConflict
from case_kernel.fact_claim_ledger import AssertionOrigin, ClaimResponsePosition, FactStatus
from case_kernel.calculation_engine import AllocationPolicy
from case_kernel.formal_calculation_postgres import (
    PersistentFormalCalculationSnapshot,
    PostgresFormalCalculationStore,
)
from case_kernel.legal_rules import LegalEventKind
from case_kernel.legal_source_postgres import (
    LegalAuthorityLevel,
    LegalBundleSegmentSelection,
    LegalRateFormulaKind,
    PersistentLegalReviewSnapshot,
    PostgresLegalSourceStore,
)
from case_kernel.official_source_capture_postgres import (
    PersistentOfficialSourceCaptureSnapshot,
    PostgresOfficialSourceCaptureStore,
)
from case_kernel.models import Actor, Role
from case_kernel.postgres_store import PostgresMatterStore
from case_kernel.workflow import MatterWorkflow
from case_kernel.local_access_grants import (
    LocalFolderAccessBlocked,
    LocalFolderGrantRegistry,
    LocalSessionProof,
)
from case_kernel.local_intake_authorizations import LocalEvidenceIntakeAuthorizationRegistry
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked
from case_kernel.original_page_access import (
    OriginalPageAccessBlocked,
    OriginalPageAccessBroker,
)
from case_kernel.submission_postgres import (
    PersistentSubmissionSnapshot,
    PostgresSubmissionStore,
    SubmissionComponentSelection,
)
from case_kernel.reviewable_draft_access import (
    ReviewableDraftAccessBlocked,
    ReviewableDraftAccessPurpose,
    ReviewableOfficeDraftAccessBroker,
)
from case_kernel.reviewable_draft_postgres import (
    PersistentReviewableOfficeDraftSnapshot,
    PostgresReviewableDraftStore,
)
from case_kernel.agent_execution_postgres import (
    AgentToolProposal,
    PersistentAgentExecutionSnapshot,
    PostgresAgentExecutionStore,
)
from case_kernel.document_consistency_postgres import (
    PersistentDocumentConsistencySnapshot,
    PostgresDocumentConsistencyReviewStore,
    PersistedDocumentConsistencyFinding,
    ReviewedWorkProduct,
)
from case_kernel.external_request_postgres import (
    ExternalRequestPreflight,
    PersistentExternalRequestSnapshot,
    PostgresExternalRequestStore,
)
from case_kernel.ocr_review_candidate_postgres import PostgresOcrReviewCandidateStore
from case_kernel.submission_access import (
    SubmissionAccessBlocked,
    SubmissionExportAccessBroker,
)
from case_kernel.runtime import RuntimeMode, RuntimeSettings
from case_kernel.request_context import current_request_id, reset_request_id, set_request_id
from case_kernel.transaction_ledger import (
    ClassificationOrigin,
    DatePrecision,
    ObligationAllocation,
    PaymentNature,
    TransactionChannel,
    TransactionDirection,
)

from .persistent_identity import (
    DesktopSessionAuthority,
    PersistentAuthenticationBlocked,
    ServerIdentityContext,
    ServerIdentityResolver,
)
from .schemas import (
    CaseLedgerReceiptResponse,
    DesktopSessionGrantResponse,
    PersistentArtifactAccessRequest,
    PersistentArtifactAccessResponse,
    PersistentLocalFolderGrantRequest,
    PersistentLocalFolderGrantResponse,
    PersistentLocalFolderIntakeSummaryResponse,
    PersistentLocalFolderScanApprovalRequest,
    PersistentLocalFolderScanFilePageResponse,
    PersistentLocalFolderScanRequest,
    PersistentLocalFolderSelectionRequest,
    PersistentLocalFolderSelectionResponse,
    PersistentMatterCreateRequest,
    PersistentMatterCreateResponse,
    PersistentMatterListResponse,
    PersistentOriginalPageAccessRequest,
    PersistentOriginalPageAccessResponse,
    PersistentCaseReviewSummaryResponse,
    PersistentCaseSnapshotResponse,
    PersistentApprovalRequest,
    PersistentClaimCandidateRequest,
    PersistentClaimResponseRequest,
    PersistentConfirmationRequest,
    PersistentDisputeIssueCandidateRequest,
    PersistentDuplicateGroupCandidateRequest,
    PersistentDuplicateGroupResolutionRequest,
    PersistentEvidenceAnnotationRequest,
    PersistentEvidenceDuplicateGroupRequest,
    PersistentEvidenceDuplicateResolutionRequest,
    PersistentEvidenceIntakeClaimRequest,
    PersistentEvidenceIntakeCompleteRequest,
    PersistentEvidenceIntakeFinalizeRequest,
    PersistentEvidenceIntakeHeartbeatRequest,
    PersistentEvidenceIntakeHeartbeatResponse,
    PersistentEvidenceIntakeItemPageResponse,
    PersistentEvidenceIntakeLeaseResponse,
    PersistentEvidenceIntakeReapRequest,
    PersistentEvidenceIntakeRunRequest,
    PersistentEvidenceIntakeSummaryResponse,
    PersistentEvidenceManifestLockRequest,
    PersistentEvidenceDerivativeCandidateRequest,
    PersistentEvidenceDerivativeRunClaimRequest,
    PersistentEvidenceDerivativeRunCompleteRequest,
    PersistentEvidenceDerivativeRunFailureRequest,
    PersistentEvidenceDerivativeRunHeartbeatRequest,
    PersistentEvidenceDerivativeRunHeartbeatResponse,
    PersistentEvidenceDerivativeRunLeaseResponse,
    PersistentEvidenceDerivativeRunRequest,
    PersistentEvidenceDerivativeVerificationRequest,
    PersistentEvidenceOriginalRequest,
    PersistentEvidencePageDecisionRequest,
    PersistentEvidencePageListResponse,
    PersistentEvidenceReviewSummaryResponse,
    PersistentEvidenceSnapshotResponse,
    PersistentFactCandidateRequest,
    PersistentFactDecisionRequest,
    PersistentFactPageResponse,
    PersistentFactResponse,
    PersistentCaseLegalBundleApprovalRequest,
    PersistentCaseLegalEventRequest,
    PersistentCaseLegalFactBindingRequest,
    PersistentFormalCalculationRequest,
    PersistentFormalCalculationSnapshotResponse,
    PersistentLegalRuleVersionRequest,
    PersistentLegalReviewSnapshotResponse,
    PersistentOfficialLegalSourceSnapshotRequest,
    PersistentOfficialSourceCaptureRequest,
    PersistentOfficialSourceCaptureReviewRequest,
    PersistentOfficialSourceCaptureSnapshotResponse,
    PersistentReviewedCaptureRegistrationRequest,
    PersistentSubmissionLockRequest,
    PersistentSubmissionAccessResponse,
    PersistentSubmissionQaRequest,
    PersistentSubmissionSnapshotResponse,
    PersistentSubmissionWorkProductRequest,
    PersistentReviewableOfficeDraftAccessRequest,
    PersistentReviewableOfficeDraftAccessResponse,
    PersistentReviewableOfficeDraftSnapshotResponse,
    PersistentAgentExecutionSnapshotResponse,
    PersistentAgentRunRequest,
    PersistentAgentToolReceiptRequest,
    PersistentDocumentConsistencyFindingRequest,
    PersistentDocumentConsistencyReviewRequest,
    PersistentDocumentConsistencySnapshotResponse,
    PersistentExternalRequestAttemptRequest,
    PersistentExternalRequestPreflightRequest,
    PersistentExternalRequestSnapshotResponse,
    PersistentPaymentClassificationCandidateRequest,
    PersistentTransactionCandidateRequest,
    PersistentTransactionPageResponse,
)


class PersistentFactLedgerPort(Protocol):
    def create_fact_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def decide_fact(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def list_facts(self, *, matter_id: str, actor: Actor): ...

    def list_fact_page(self, **kwargs): ...

    def list_transaction_page(self, **kwargs): ...

    def get_case_review_summary(self, *, matter_id: str, actor: Actor): ...

    def create_claim_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_claim_scope(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def set_claim_response(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_dispute_issue_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_dispute_issue(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_transaction_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def confirm_transaction(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_payment_classification_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_payment_classification(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_duplicate_group_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def resolve_duplicate_group(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_case_snapshot(self, *, matter_id: str, actor: Actor): ...


class PersistentMatterStorePort(Protocol):
    def create(self, **kwargs): ...

    def list_accessible(self, *, actor: Actor): ...


class PersistentEvidenceManifestPort(Protocol):
    def get_evidence_review_summary(self, *, matter_id: str, actor: Actor): ...

    def list_evidence_page(self, **kwargs): ...

    def create_local_folder_scan_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_local_folder_scan(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_local_folder_intake_summary(self, *, matter_id: str, actor: Actor): ...

    def list_local_folder_scan_file_page(self, **kwargs): ...

    def enqueue_evidence_intake_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def claim_evidence_intake_item(self, **kwargs): ...

    def renew_evidence_intake_item_lease(self, **kwargs): ...

    def complete_evidence_intake_item(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def finalize_evidence_intake_item(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def reap_exhausted_evidence_intake_items(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_current_evidence_intake_summary(self, *, matter_id: str, actor: Actor): ...

    def list_evidence_intake_item_page(self, **kwargs): ...

    def register_original_file(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def register_normalized_original_file(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_page_decision_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_page_decision(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_annotation_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_annotation(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_duplicate_group_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def resolve_duplicate_group(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def lock_manifest(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def register_derivative_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def verify_derivative(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def enqueue_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def claim_derivative_run(self, **kwargs): ...

    def complete_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def renew_derivative_run_lease(self, **kwargs) -> datetime: ...

    def fail_derivative_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_evidence_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentEvidenceSnapshot: ...

    def get_verified_derivative_locator(self, *, matter_id: str, derivative_id: str, actor: Actor): ...

    def get_original_page_locator(self, *, matter_id: str, evidence_page_id: str, actor: Actor): ...


class PersistentFormalCalculationPort(Protocol):
    def create_formal_calculation(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_current_calculation(
        self, *, matter_id: str, obligation_id: str, actor: Actor
    ) -> PersistentFormalCalculationSnapshot: ...


class PersistentLegalSourcePort(Protocol):
    def register_official_source_snapshot(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def register_reviewed_capture_snapshot(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_rule_version(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_legal_event(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_legal_fact_binding(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_case_legal_bundle(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_legal_review_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentLegalReviewSnapshot: ...


class PersistentSubmissionPort(Protocol):
    def register_work_product_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def approve_work_product(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def create_qa_ready_bundle(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def lock_submission_bundle(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_submission_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentSubmissionSnapshot: ...

    def get_verified_export_locator(self, **kwargs): ...


class PersistentReviewableDraftPort(Protocol):
    def approve_reviewable_office_draft_pair(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_reviewable_office_draft_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentReviewableOfficeDraftSnapshot: ...

    def get_reviewable_office_draft_artifact_locator(self, **kwargs): ...


class PersistentAgentExecutionPort(Protocol):
    def plan_agent_run(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def record_tool_execution_receipt(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentAgentExecutionSnapshot: ...


class PersistentDocumentConsistencyPort(Protocol):
    def record_review(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentDocumentConsistencySnapshot: ...


class PersistentExternalRequestPort(Protocol):
    def authorize_external_request(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def record_external_attempt(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def validate_single_page_ocr_execution(self, **kwargs) -> None: ...


class PersistentOcrReviewCandidatePort(Protocol):
    def stage(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentExternalRequestSnapshot: ...


class PersistentOfficialSourceCapturePort(Protocol):
    def queue_capture(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def review_capture(self, **kwargs) -> CaseLedgerCommandReceipt: ...

    def get_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentOfficialSourceCaptureSnapshot: ...


class PersistentRequestBlocked(ValueError):
    pass


class PersistentEvidenceServiceUnavailable(RuntimeError):
    pass


class PersistentCalculationServiceUnavailable(RuntimeError):
    pass


class PersistentLegalSourceServiceUnavailable(RuntimeError):
    pass


class PersistentSubmissionServiceUnavailable(RuntimeError):
    pass


class PersistentReviewableDraftServiceUnavailable(RuntimeError):
    pass


class PersistentAgentExecutionServiceUnavailable(RuntimeError):
    pass


class PersistentDocumentConsistencyServiceUnavailable(RuntimeError):
    pass


class PersistentExternalRequestServiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistentApiDependencies:
    settings: RuntimeSettings
    case_ledger_store: PersistentFactLedgerPort
    identity_resolver: ServerIdentityResolver
    matter_store: PersistentMatterStorePort | None = None
    desktop_session_authority: DesktopSessionAuthority | None = None
    evidence_manifest_store: PersistentEvidenceManifestPort | None = None
    formal_calculation_store: PersistentFormalCalculationPort | None = None
    legal_source_store: PersistentLegalSourcePort | None = None
    official_source_capture_store: PersistentOfficialSourceCapturePort | None = None
    submission_store: PersistentSubmissionPort | None = None
    reviewable_draft_store: PersistentReviewableDraftPort | None = None
    agent_execution_store: PersistentAgentExecutionPort | None = None
    document_consistency_store: PersistentDocumentConsistencyPort | None = None
    external_request_store: PersistentExternalRequestPort | None = None
    submission_access_broker: SubmissionExportAccessBroker | None = None
    reviewable_draft_access_broker: ReviewableOfficeDraftAccessBroker | None = None
    artifact_access_broker: EphemeralArtifactAccessBroker | None = None
    artifact_store: LocalEncryptedArtifactStore | None = None
    local_folder_grants: LocalFolderGrantRegistry | None = None
    local_evidence_intake_authorizations: LocalEvidenceIntakeAuthorizationRegistry | None = None
    original_page_access_broker: OriginalPageAccessBroker | None = None
    native_model_worker: Actor | None = None
    ocr_review_candidate_store: PersistentOcrReviewCandidatePort | None = None

    def validate(self) -> None:
        if self.settings.mode is not RuntimeMode.POSTGRES_INTERNAL_PREVIEW:
            raise ValueError("persistent API requires postgres-internal-preview runtime settings")
        if self.desktop_session_authority is not None and self.identity_resolver is not self.desktop_session_authority:
            raise ValueError("desktop bootstrap and identity resolution must use the same authority")
        if not isinstance(self.case_ledger_store, PostgresCaseLedgerStore):
            # Test doubles must explicitly opt in via the marker; arbitrary
            # objects cannot accidentally become a production persistence port.
            if not getattr(self.case_ledger_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL case ledger store")
        if self.matter_store is not None and not isinstance(self.matter_store, PostgresMatterStore):
            if not getattr(self.matter_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL matter store")
        if self.evidence_manifest_store is not None and not isinstance(
            self.evidence_manifest_store, PostgresEvidenceManifestStore
        ):
            if not getattr(self.evidence_manifest_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL evidence Manifest store")
        if self.formal_calculation_store is not None and not isinstance(
            self.formal_calculation_store, PostgresFormalCalculationStore
        ):
            if not getattr(self.formal_calculation_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL formal calculation store")
        if self.legal_source_store is not None and not isinstance(
            self.legal_source_store, PostgresLegalSourceStore
        ):
            if not getattr(self.legal_source_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL legal source store")
        if self.official_source_capture_store is not None and not isinstance(
            self.official_source_capture_store, PostgresOfficialSourceCaptureStore
        ):
            if not getattr(self.official_source_capture_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL official source capture store")
        if self.submission_store is not None and not isinstance(
            self.submission_store, PostgresSubmissionStore
        ):
            if not getattr(self.submission_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL submission store")
        if self.reviewable_draft_store is not None and not isinstance(
            self.reviewable_draft_store, PostgresReviewableDraftStore
        ):
            if not getattr(self.reviewable_draft_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL reviewable-draft store")
        if self.agent_execution_store is not None and not isinstance(
            self.agent_execution_store, PostgresAgentExecutionStore
        ):
            if not getattr(self.agent_execution_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL Agent execution store")
        if self.document_consistency_store is not None and not isinstance(
            self.document_consistency_store, PostgresDocumentConsistencyReviewStore
        ):
            if not getattr(self.document_consistency_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL document consistency store")
        if self.external_request_store is not None and not isinstance(
            self.external_request_store, PostgresExternalRequestStore
        ):
            if not getattr(self.external_request_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL external-request store")
        if self.ocr_review_candidate_store is not None and not isinstance(
            self.ocr_review_candidate_store, PostgresOcrReviewCandidateStore
        ):
            if not getattr(self.ocr_review_candidate_store, "persistent_test_double", False):
                raise ValueError("persistent API requires the guarded PostgreSQL OCR candidate store")
        if (
            self.artifact_access_broker is not None
            or self.submission_access_broker is not None
            or self.reviewable_draft_access_broker is not None
        ) and self.artifact_store is None:
            raise ValueError("artifact access brokers require the encrypted artifact store")
        if self.artifact_store is not None and (
            self.artifact_access_broker is None
            and self.submission_access_broker is None
            and self.reviewable_draft_access_broker is None
            and self.original_page_access_broker is None
        ):
            raise ValueError("encrypted artifact store requires at least one guarded access broker")
        if self.artifact_access_broker is not None and self.evidence_manifest_store is None:
            raise ValueError("artifact access requires the guarded evidence Manifest store")
        if self.submission_access_broker is not None and self.submission_store is None:
            raise ValueError("submission access requires the guarded submission store")
        if self.reviewable_draft_access_broker is not None and self.reviewable_draft_store is None:
            raise ValueError("reviewable draft access requires the guarded reviewable-draft store")
        if (self.local_folder_grants is None) != (self.original_page_access_broker is None):
            raise ValueError("original-page access requires both the folder grant registry and preview broker")
        if self.original_page_access_broker is not None:
            if self.evidence_manifest_store is None:
                raise ValueError("original-page access requires the guarded evidence Manifest store")
            if self.original_page_access_broker.folder_grants is not self.local_folder_grants:
                raise ValueError("original-page access broker must use the configured folder grant registry")
            if (
                self.original_page_access_broker.artifact_store is not None
                and self.original_page_access_broker.artifact_store is not self.artifact_store
            ):
                raise ValueError("normalized original-page access must use the configured encrypted artifact store")
        if self.local_evidence_intake_authorizations is not None:
            if self.local_folder_grants is None or self.evidence_manifest_store is None:
                raise ValueError("local intake authorization requires folder grants and evidence persistence")
        if self.native_model_worker is not None:
            if self.native_model_worker.roles != frozenset({Role.SYSTEM_WORKER}):
                raise ValueError("native model bridge requires the dedicated system worker role")


def create_persistent_app(
    dependencies: PersistentApiDependencies | None = None,
    *,
    native_parent_api_token: str | None = None,
) -> FastAPI:
    if native_parent_api_token is not None and (
        len(native_parent_api_token) != 64
        or not all(char in "0123456789abcdef" for char in native_parent_api_token)
    ):
        raise ValueError("native desktop parent token is invalid")
    enabled = dependencies is not None
    if dependencies is not None:
        dependencies.validate()
    app = FastAPI(
        title="律所案件 AI 工作台 · 持久化预览 API" if enabled else "律所案件 AI 工作台 · 持久化 API 已禁用",
        version="0.1.0",
        docs_url="/docs" if enabled else None,
        redoc_url=None,
    )
    if enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["tauri://localhost"],
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Accept",
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
            ],
            expose_headers=[
                "Content-Disposition",
                "Content-Length",
                "X-Artifact-SHA256",
                "X-Image-Height",
                "X-Image-Width",
                "X-Request-ID",
            ],
            max_age=600,
        )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = str(uuid4())
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            reset_request_id(token)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        if not enabled:
            return {"service": "persistent-case-api", "mode": "disabled", "persistence": "not-configured"}
        return {
            "service": "persistent-case-api",
            "mode": "postgres-internal-preview",
            "persistence": "configured-not-probed",
            "evidence_manifest": "configured" if dependencies.evidence_manifest_store else "not-configured",
            "formal_calculation": "configured" if dependencies.formal_calculation_store else "not-configured",
            "legal_source": "configured" if dependencies.legal_source_store else "not-configured",
            "official_source_capture": "configured" if dependencies.official_source_capture_store else "not-configured",
            "submission": "configured" if dependencies.submission_store else "not-configured",
            "reviewable_drafts": "configured" if dependencies.reviewable_draft_store else "not-configured",
            "agent_execution": "configured" if dependencies.agent_execution_store else "not-configured",
            "document_consistency": "configured" if dependencies.document_consistency_store else "not-configured",
            "external_request": "configured" if dependencies.external_request_store else "not-configured",
            "artifact_access": "configured" if dependencies.artifact_access_broker else "not-configured",
            "submission_access": "configured" if dependencies.submission_access_broker else "not-configured",
            "reviewable_draft_access": "configured" if dependencies.reviewable_draft_access_broker else "not-configured",
            "original_page_access": "configured" if dependencies.original_page_access_broker else "not-configured",
            "desktop_session": "configured" if dependencies.desktop_session_authority else "not-configured",
        }

    if dependencies is None:
        return app

    async def get_identity(request: Request) -> ServerIdentityContext:
        identity = await dependencies.identity_resolver.resolve(request)
        identity.validate()
        return identity

    def get_idempotency_key(
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> str:
        if idempotency_key is None or not idempotency_key.strip():
            raise PersistentRequestBlocked("Idempotency-Key header is required")
        return idempotency_key.strip()

    def get_evidence_store() -> PersistentEvidenceManifestPort:
        if dependencies.evidence_manifest_store is None:
            raise PersistentEvidenceServiceUnavailable("evidence Manifest persistence is not configured")
        return dependencies.evidence_manifest_store

    def get_matter_workflow() -> MatterWorkflow:
        if dependencies.matter_store is None:
            raise PersistentRequestBlocked("matter creation persistence is not configured")
        return MatterWorkflow(dependencies.matter_store)

    def get_formal_calculation_store() -> PersistentFormalCalculationPort:
        if dependencies.formal_calculation_store is None:
            raise PersistentCalculationServiceUnavailable(
                "formal calculation persistence is not configured"
            )
        return dependencies.formal_calculation_store

    def get_legal_source_store() -> PersistentLegalSourcePort:
        if dependencies.legal_source_store is None:
            raise PersistentLegalSourceServiceUnavailable(
                "official legal source persistence is not configured"
            )
        return dependencies.legal_source_store

    def get_official_source_capture_store() -> PersistentOfficialSourceCapturePort:
        if dependencies.official_source_capture_store is None:
            raise PersistentLegalSourceServiceUnavailable(
                "official source capture persistence is not configured"
            )
        return dependencies.official_source_capture_store

    def get_submission_store() -> PersistentSubmissionPort:
        if dependencies.submission_store is None:
            raise PersistentSubmissionServiceUnavailable(
                "submission persistence is not configured"
            )
        return dependencies.submission_store

    def get_reviewable_draft_store() -> PersistentReviewableDraftPort:
        if dependencies.reviewable_draft_store is None:
            raise PersistentReviewableDraftServiceUnavailable(
                "reviewable Office draft persistence is not configured"
            )
        return dependencies.reviewable_draft_store

    def get_agent_execution_store() -> PersistentAgentExecutionPort:
        if dependencies.agent_execution_store is None:
            raise PersistentAgentExecutionServiceUnavailable(
                "Agent execution persistence is not configured"
            )
        return dependencies.agent_execution_store

    def get_document_consistency_store() -> PersistentDocumentConsistencyPort:
        if dependencies.document_consistency_store is None:
            raise PersistentDocumentConsistencyServiceUnavailable(
                "document consistency persistence is not configured"
            )
        return dependencies.document_consistency_store

    def get_external_request_store() -> PersistentExternalRequestPort:
        if dependencies.external_request_store is None:
            raise PersistentExternalRequestServiceUnavailable(
                "external request persistence is not configured"
            )
        return dependencies.external_request_store

    def require_artifact_services() -> tuple[EphemeralArtifactAccessBroker, LocalEncryptedArtifactStore]:
        if dependencies.artifact_access_broker is None or dependencies.artifact_store is None:
            raise PersistentEvidenceServiceUnavailable("encrypted artifact access is not configured")
        return dependencies.artifact_access_broker, dependencies.artifact_store

    def require_submission_artifact_services() -> tuple[
        SubmissionExportAccessBroker, LocalEncryptedArtifactStore
    ]:
        if dependencies.submission_access_broker is None or dependencies.artifact_store is None:
            raise PersistentSubmissionServiceUnavailable(
                "verified submission export access is not configured"
            )
        return dependencies.submission_access_broker, dependencies.artifact_store

    def require_reviewable_draft_artifact_services() -> tuple[
        ReviewableOfficeDraftAccessBroker, LocalEncryptedArtifactStore
    ]:
        if dependencies.reviewable_draft_access_broker is None or dependencies.artifact_store is None:
            raise PersistentReviewableDraftServiceUnavailable(
                "reviewable Office draft encrypted access is not configured"
            )
        return dependencies.reviewable_draft_access_broker, dependencies.artifact_store

    def require_original_page_services() -> tuple[LocalFolderGrantRegistry, OriginalPageAccessBroker]:
        if dependencies.local_folder_grants is None or dependencies.original_page_access_broker is None:
            raise PersistentEvidenceServiceUnavailable("original-page preview access is not configured")
        return dependencies.local_folder_grants, dependencies.original_page_access_broker

    def require_native_parent(request: Request, session_id: str) -> ServerIdentityContext:
        if native_parent_api_token is None or dependencies.desktop_session_authority is None:
            raise PersistentAuthenticationBlocked("native model bridge is not configured")
        _require_loopback(request)
        if not compare_digest(request.headers.get("authorization", ""), f"Bearer {native_parent_api_token}"):
            # Do not disclose that this private endpoint exists.
            raise KeyError("native-model-bridge")
        return dependencies.desktop_session_authority.resolve_native_session(session_id=session_id)

    @app.exception_handler(PersistentAuthenticationBlocked)
    async def authentication_handler(_: Request, exc: PersistentAuthenticationBlocked):
        del exc
        return _error(
            status.HTTP_401_UNAUTHORIZED,
            "AUTHENTICATION_REQUIRED",
            "登录状态无效或已过期，请重新登录。",
        )

    @app.exception_handler(AuthorizationDenied)
    async def workflow_authorization_handler(_: Request, exc: AuthorizationDenied):
        del exc
        return _error(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED", "你没有新建案件的权限。")

    @app.exception_handler(PermissionError)
    async def permission_handler(_: Request, exc: PermissionError):
        del exc
        return _error(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED", "你没有执行该案件操作的权限。")

    @app.exception_handler(VersionConflict)
    @app.exception_handler(IdempotencyConflict)
    async def conflict_handler(_: Request, exc: VersionConflict | IdempotencyConflict):
        del exc
        return _error(
            status.HTTP_409_CONFLICT,
            "VERSION_OR_IDEMPOTENCY_CONFLICT",
            "案件已发生变化，请刷新后重新确认本次操作。",
        )

    @app.exception_handler(CaseLedgerPersistenceBlocked)
    @app.exception_handler(EvidenceReferenceBlocked)
    @app.exception_handler(StablePaginationBlocked)
    async def ledger_handler(
        _: Request,
        exc: CaseLedgerPersistenceBlocked | EvidenceReferenceBlocked | StablePaginationBlocked,
    ):
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "LEDGER_PRECONDITION_BLOCKED",
            "当前事实、证据或审批状态不满足该操作条件。",
        )

    @app.exception_handler(KeyError)
    async def missing_handler(_: Request, exc: KeyError):
        del exc
        return _error(status.HTTP_404_NOT_FOUND, "OBJECT_NOT_FOUND", "未找到本案中的对应记录。")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "REQUEST_VALIDATION_FAILED",
            "提交内容格式不完整或不符合要求，请检查后重试。",
        )

    @app.exception_handler(PersistentRequestBlocked)
    async def request_handler(_: Request, exc: PersistentRequestBlocked):
        del exc
        return _error(
            status.HTTP_400_BAD_REQUEST,
            "REQUEST_PRECONDITION_BLOCKED",
            "本次操作缺少必要的请求标识，请重新提交。",
        )

    @app.exception_handler(PersistentEvidenceServiceUnavailable)
    async def evidence_service_handler(_: Request, exc: PersistentEvidenceServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EVIDENCE_SERVICE_UNAVAILABLE",
            "证据持久化服务尚未启用，未回退到合成数据。",
        )

    @app.exception_handler(PersistentCalculationServiceUnavailable)
    async def calculation_service_handler(_: Request, exc: PersistentCalculationServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "FORMAL_CALCULATION_SERVICE_UNAVAILABLE",
            "正式利息计算服务尚未启用，未回退到合成测算。",
        )

    @app.exception_handler(PersistentLegalSourceServiceUnavailable)
    async def legal_source_service_handler(_: Request, exc: PersistentLegalSourceServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "LEGAL_SOURCE_SERVICE_UNAVAILABLE",
            "官方法源与案件规则包服务尚未启用，未使用模型记忆或网页摘要替代。",
        )

    @app.exception_handler(PersistentSubmissionServiceUnavailable)
    async def submission_service_handler(_: Request, exc: PersistentSubmissionServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SUBMISSION_SERVICE_UNAVAILABLE",
            "提交材料服务尚未启用，未生成或回退到合成文件。",
        )

    @app.exception_handler(PersistentReviewableDraftServiceUnavailable)
    async def reviewable_draft_service_handler(_: Request, exc: PersistentReviewableDraftServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "REVIEWABLE_DRAFT_SERVICE_UNAVAILABLE",
            "可审阅 Word/Excel 草稿服务尚未启用，系统不会暴露未验证文件。",
        )

    @app.exception_handler(PersistentAgentExecutionServiceUnavailable)
    async def agent_execution_service_handler(_: Request, exc: PersistentAgentExecutionServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AGENT_EXECUTION_SERVICE_UNAVAILABLE",
            "Agent 计划与工具审计服务尚未启用，系统不会回退到内存记录。",
        )

    @app.exception_handler(PersistentDocumentConsistencyServiceUnavailable)
    async def document_consistency_service_handler(_: Request, exc: PersistentDocumentConsistencyServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DOCUMENT_CONSISTENCY_SERVICE_UNAVAILABLE",
            "文书一致性审查服务尚未启用，系统不会把旧报告当作当前有效。",
        )

    @app.exception_handler(PersistentExternalRequestServiceUnavailable)
    async def external_request_service_handler(_: Request, exc: PersistentExternalRequestServiceUnavailable):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EXTERNAL_REQUEST_SERVICE_UNAVAILABLE",
            "外部调用预授权服务尚未启用，系统不会发送或回退到未审计请求。",
        )

    @app.exception_handler(ManagedArtifactBlocked)
    async def managed_artifact_handler(_: Request, exc: ManagedArtifactBlocked):
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ARTIFACT_INTEGRITY_BLOCKED",
            "证据派生件完整性核验未通过，系统已停止读取。",
        )

    @app.exception_handler(LocalFolderAccessBlocked)
    async def local_folder_access_handler(_: Request, exc: LocalFolderAccessBlocked):
        del exc
        return _error(
            status.HTTP_403_FORBIDDEN,
            "LOCAL_FOLDER_ACCESS_DENIED",
            "本地案卷文件夹授权无效、已过期或原件已发生变化。",
        )

    @app.exception_handler(OriginalPageAccessBlocked)
    async def original_page_access_handler(_: Request, exc: OriginalPageAccessBlocked):
        del exc
        return _error(
            status.HTTP_403_FORBIDDEN,
            "ORIGINAL_PAGE_ACCESS_DENIED",
            "原始证据页预览许可无效、已过期或完整性核验未通过。",
        )

    @app.exception_handler(SubmissionAccessBlocked)
    async def submission_access_handler(_: Request, exc: SubmissionAccessBlocked):
        del exc
        return _error(
            status.HTTP_403_FORBIDDEN,
            "SUBMISSION_ACCESS_DENIED",
            "法院提交包下载许可无效、已过期或不属于当前本机会话。",
        )

    @app.exception_handler(ReviewableDraftAccessBlocked)
    async def reviewable_draft_access_handler(_: Request, exc: ReviewableDraftAccessBlocked):
        del exc
        return _error(
            status.HTTP_403_FORBIDDEN,
            "REVIEWABLE_DRAFT_ACCESS_DENIED",
            "可审阅草稿访问许可无效、已过期或不属于当前本机会话。",
        )

    if dependencies.desktop_session_authority is not None:
        @app.post(
            "/v1/desktop-sessions/exchange",
            response_model=DesktopSessionGrantResponse,
            tags=["desktop-session"],
        )
        async def exchange_desktop_session(
            request: Request,
            response: Response,
            x_desktop_bootstrap: Annotated[str | None, Header()] = None,
        ) -> DesktopSessionGrantResponse:
            if x_desktop_bootstrap is None:
                raise PersistentAuthenticationBlocked("desktop bootstrap token is missing")
            grant = dependencies.desktop_session_authority.exchange(
                request=request,
                bootstrap_token=x_desktop_bootstrap,
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return DesktopSessionGrantResponse(
                access_token=grant.access_token,
                session_id=UUID(grant.session_id),
                expires_at=grant.expires_at,
            )

    @app.post(
        "/v1/matters",
        response_model=PersistentMatterCreateResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["matters"],
    )
    async def create_persistent_matter(
        body: PersistentMatterCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> PersistentMatterCreateResponse:
        receipt = get_matter_workflow().create_matter(
            identity.actor,
            matter_id=str(uuid4()),
            title=body.title,
            idempotency_key=idempotency_key,
        )
        return PersistentMatterCreateResponse(
            command_name="CREATE_MATTER",
            idempotency_key=receipt.idempotency_key,
            matter_id=UUID(receipt.matter_id),
            matter_version=receipt.matter_version,
            audit_event_id=UUID(receipt.audit_event_id),
        )

    @app.get(
        "/v1/matters",
        response_model=PersistentMatterListResponse,
        tags=["matters"],
    )
    async def list_persistent_matters(
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentMatterListResponse:
        if dependencies.matter_store is None:
            raise PersistentRequestBlocked("matter listing persistence is not configured")
        return PersistentMatterListResponse.model_validate({
            "matters": dependencies.matter_store.list_accessible(actor=identity.actor),
        })

    @app.get(
        "/v1/matters/{matter_id}/snapshot",
        response_model=PersistentCaseSnapshotResponse,
        tags=["matters"],
    )
    async def get_case_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentCaseSnapshotResponse:
        snapshot = dependencies.case_ledger_store.get_case_snapshot(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentCaseSnapshotResponse.model_validate(snapshot.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/review-summary",
        response_model=PersistentCaseReviewSummaryResponse,
        tags=["matters"],
    )
    async def get_case_review_summary(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentCaseReviewSummaryResponse:
        summary = dependencies.case_ledger_store.get_case_review_summary(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentCaseReviewSummaryResponse.model_validate(summary.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/facts",
        response_model=tuple[PersistentFactResponse, ...],
        tags=["facts"],
    )
    async def list_facts(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> tuple[PersistentFactResponse, ...]:
        facts = dependencies.case_ledger_store.list_facts(matter_id=str(matter_id), actor=identity.actor)
        return tuple(
            PersistentFactResponse(
                fact_id=fact.fact_id,
                original_text=fact.original_text,
                origin=fact.origin.value,
                status=fact.status.value,
                evidence_count=len(fact.evidence_links),
                decision_hash=fact.decision_hash,
            )
            for fact in facts
        )

    @app.get(
        "/v1/matters/{matter_id}/fact-pages",
        response_model=PersistentFactPageResponse,
        tags=["facts"],
    )
    async def list_fact_page(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(min_length=20, max_length=512)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PersistentFactPageResponse:
        page = dependencies.case_ledger_store.list_fact_page(
            matter_id=str(matter_id),
            actor=identity.actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return PersistentFactPageResponse.model_validate(page.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/transaction-pages",
        response_model=PersistentTransactionPageResponse,
        tags=["transactions"],
    )
    async def list_transaction_page(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(min_length=20, max_length=512)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PersistentTransactionPageResponse:
        page = dependencies.case_ledger_store.list_transaction_page(
            matter_id=str(matter_id),
            actor=identity.actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return PersistentTransactionPageResponse.model_validate(page.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/calculations/{obligation_id}/current",
        response_model=PersistentFormalCalculationSnapshotResponse,
        tags=["formal-calculation"],
    )
    async def get_current_formal_calculation(
        matter_id: UUID,
        obligation_id: str,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        calculation_store: Annotated[
            PersistentFormalCalculationPort, Depends(get_formal_calculation_store)
        ],
    ) -> PersistentFormalCalculationSnapshotResponse:
        snapshot = calculation_store.get_current_calculation(
            matter_id=str(matter_id),
            obligation_id=obligation_id,
            actor=identity.actor,
        )
        return PersistentFormalCalculationSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/formal-calculations",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["formal-calculation"],
    )
    async def create_formal_calculation(
        matter_id: UUID,
        body: PersistentFormalCalculationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        calculation_store: Annotated[
            PersistentFormalCalculationPort, Depends(get_formal_calculation_store)
        ],
    ) -> CaseLedgerReceiptResponse:
        receipt = calculation_store.create_formal_calculation(
            matter_id=str(matter_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            obligation_id=body.obligation_id,
            start_date=body.start_date,
            end_date=body.end_date,
            legal_bundle_id=str(body.legal_bundle_id),
            legal_bundle_hash=body.legal_bundle_hash,
            allocation_policy=AllocationPolicy(body.allocation_policy),
            approval_hash=body.approval_hash,
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/official-legal-source-snapshots",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def register_official_legal_source_snapshot(
        matter_id: UUID,
        body: PersistentOfficialLegalSourceSnapshotRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.register_official_source_snapshot(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                source_id=body.source_id,
                publisher=body.publisher,
                authority_level=LegalAuthorityLevel(body.authority_level),
                official_url=body.official_url,
                provision_locator=body.provision_locator,
                retrieved_at=body.retrieved_at,
                content_sha256=body.content_sha256,
                content_media_type=body.content_media_type,
                storage_object_key=body.storage_object_key,
                verification_hash=body.verification_hash,
                license_basis=body.license_basis,
                license_review_hash=body.license_review_hash,
                supersedes_snapshot_id=(
                    str(body.supersedes_snapshot_id) if body.supersedes_snapshot_id else None
                ),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/official-source-captures/{run_id}/register",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def register_reviewed_official_source_capture(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentReviewedCaptureRegistrationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.register_reviewed_capture_snapshot(
                matter_id=str(matter_id),
                run_id=str(run_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                license_basis=body.license_basis,
                license_review_hash=body.license_review_hash,
                registration_hash=body.registration_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/legal-review",
        response_model=PersistentLegalReviewSnapshotResponse,
        tags=["legal-sources"],
    )
    async def get_legal_review_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> PersistentLegalReviewSnapshotResponse:
        snapshot = legal_store.get_legal_review_snapshot(
            matter_id=str(matter_id), actor=identity.actor
        )
        return PersistentLegalReviewSnapshotResponse.model_validate(snapshot.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/official-source-captures",
        response_model=PersistentOfficialSourceCaptureSnapshotResponse,
        tags=["legal-sources"],
    )
    async def get_official_source_capture_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        capture_store: Annotated[
            PersistentOfficialSourceCapturePort, Depends(get_official_source_capture_store)
        ],
    ) -> PersistentOfficialSourceCaptureSnapshotResponse:
        snapshot = capture_store.get_snapshot(matter_id=str(matter_id), actor=identity.actor)
        return PersistentOfficialSourceCaptureSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/official-source-captures",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def queue_official_source_capture(
        matter_id: UUID,
        body: PersistentOfficialSourceCaptureRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        capture_store: Annotated[
            PersistentOfficialSourceCapturePort, Depends(get_official_source_capture_store)
        ],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            capture_store.queue_capture(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                source_id=body.source_id,
                target_url=body.target_url,
                query_sha256=body.query_sha256,
                authorization_hash=body.authorization_hash,
                max_response_bytes=body.max_response_bytes,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/official-source-captures/{run_id}/review",
        response_model=CaseLedgerReceiptResponse,
        tags=["legal-sources"],
    )
    async def review_official_source_capture(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentOfficialSourceCaptureReviewRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        capture_store: Annotated[
            PersistentOfficialSourceCapturePort, Depends(get_official_source_capture_store)
        ],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            capture_store.review_capture(
                matter_id=str(matter_id),
                run_id=str(run_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                decision=body.decision,
                provision_locator=body.provision_locator,
                review_hash=body.review_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/legal-rule-versions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def approve_legal_rule_version(
        matter_id: UUID,
        body: PersistentLegalRuleVersionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.approve_rule_version(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                rule_id=body.rule_id,
                rule_version=body.rule_version,
                issue_key=body.issue_key,
                source_snapshot_id=str(body.source_snapshot_id),
                parameter_source_snapshot_id=(
                    str(body.parameter_source_snapshot_id)
                    if body.parameter_source_snapshot_id
                    else None
                ),
                parameter_evidence_locator=body.parameter_evidence_locator,
                effective_from=body.effective_from,
                effective_to=body.effective_to,
                trigger_event_kind=LegalEventKind(body.trigger_event_kind),
                formula_kind=LegalRateFormulaKind(body.formula_kind),
                base_annual_rate=body.base_annual_rate,
                rate_multiplier=body.rate_multiplier,
                required_fact_keys=tuple(body.required_fact_keys),
                transition_rule_versions=tuple(body.transition_rule_versions),
                conflict_set=body.conflict_set,
                priority=body.priority,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/legal-events",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def approve_case_legal_event(
        matter_id: UUID,
        body: PersistentCaseLegalEventRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.approve_legal_event(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                event_kind=LegalEventKind(body.event_kind),
                local_date=body.local_date,
                evidence_ids=tuple(str(value) for value in body.evidence_ids),
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/legal-fact-bindings",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def approve_case_legal_fact_binding(
        matter_id: UUID,
        body: PersistentCaseLegalFactBindingRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.approve_legal_fact_binding(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                fact_key=body.fact_key,
                fact_id=str(body.fact_id),
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/legal-bundles",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["legal-sources"],
    )
    async def approve_case_legal_bundle(
        matter_id: UUID,
        body: PersistentCaseLegalBundleApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        legal_store: Annotated[PersistentLegalSourcePort, Depends(get_legal_source_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            legal_store.approve_case_legal_bundle(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                segments=tuple(
                    LegalBundleSegmentSelection(
                        segment_id=str(segment.segment_id),
                        issue_key=segment.issue_key,
                        rule_version_id=str(segment.rule_version_id),
                        trigger_event_id=str(segment.trigger_event_id),
                        start_date=segment.start_date,
                        end_date=segment.end_date,
                        applicability_anchor=segment.applicability_anchor,
                    )
                    for segment in body.segments
                ),
                approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/submission-snapshot",
        response_model=PersistentSubmissionSnapshotResponse,
        tags=["submissions"],
    )
    async def get_submission_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> PersistentSubmissionSnapshotResponse:
        snapshot = submission_store.get_submission_snapshot(
            matter_id=str(matter_id), actor=identity.actor
        )
        return PersistentSubmissionSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/submission-work-products",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["submissions"],
    )
    async def register_submission_work_product(
        matter_id: UUID,
        body: PersistentSubmissionWorkProductRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            submission_store.register_work_product_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                document_kind=body.document_kind,
                audience=body.audience,
                media_type=body.media_type,
                storage_object_key=body.storage_object_key,
                artifact_sha256=body.artifact_sha256,
                byte_size=body.byte_size,
                page_count=body.page_count,
                semantic_text_sha256=body.semantic_text_sha256,
                review_input_hash=body.review_input_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/submission-work-products/{work_product_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["submissions"],
    )
    async def approve_submission_work_product(
        matter_id: UUID,
        work_product_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            submission_store.approve_work_product(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                work_product_id=str(work_product_id),
                approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/reviewable-office-drafts",
        response_model=PersistentReviewableOfficeDraftSnapshotResponse,
        tags=["draft-review"],
    )
    async def get_reviewable_office_drafts(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        draft_store: Annotated[PersistentReviewableDraftPort, Depends(get_reviewable_draft_store)],
    ) -> PersistentReviewableOfficeDraftSnapshotResponse:
        snapshot = draft_store.get_reviewable_office_draft_snapshot(
            matter_id=str(matter_id), actor=identity.actor
        )
        return PersistentReviewableOfficeDraftSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/reviewable-office-drafts/{pair_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["draft-review"],
    )
    async def approve_reviewable_office_draft(
        matter_id: UUID,
        pair_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        draft_store: Annotated[PersistentReviewableDraftPort, Depends(get_reviewable_draft_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            draft_store.approve_reviewable_office_draft_pair(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                pair_id=str(pair_id), approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/agent-executions",
        response_model=PersistentAgentExecutionSnapshotResponse,
        tags=["agent-execution"],
    )
    async def get_agent_execution_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        agent_store: Annotated[PersistentAgentExecutionPort, Depends(get_agent_execution_store)],
    ) -> PersistentAgentExecutionSnapshotResponse:
        snapshot = agent_store.get_snapshot(matter_id=str(matter_id), actor=identity.actor)
        return PersistentAgentExecutionSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/agent-executions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["agent-execution"],
    )
    async def plan_agent_execution(
        matter_id: UUID,
        body: PersistentAgentRunRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        agent_store: Annotated[PersistentAgentExecutionPort, Depends(get_agent_execution_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            agent_store.plan_agent_run(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                agent_id=body.agent_id, agent_version=body.agent_version,
                policy_manifest_hash=body.policy_manifest_hash, input_hash=body.input_hash,
                proposals=tuple(
                    AgentToolProposal(
                        sequence=item.sequence, skill_id=item.skill_id, tool_id=item.tool_id,
                        input_hash=item.input_hash, rationale_hash=item.rationale_hash,
                    )
                    for item in body.proposals
                ),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/agent-executions/{proposal_id}/receipts",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["agent-execution"],
    )
    async def record_agent_tool_execution_receipt(
        matter_id: UUID,
        proposal_id: UUID,
        body: PersistentAgentToolReceiptRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        agent_store: Annotated[PersistentAgentExecutionPort, Depends(get_agent_execution_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            agent_store.record_tool_execution_receipt(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                proposal_id=str(proposal_id), status=body.status,
                output_hash=body.output_hash, error_code=body.error_code,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/document-consistency-reviews",
        response_model=PersistentDocumentConsistencySnapshotResponse,
        tags=["document-consistency"],
    )
    async def get_document_consistency_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        consistency_store: Annotated[
            PersistentDocumentConsistencyPort, Depends(get_document_consistency_store)
        ],
    ) -> PersistentDocumentConsistencySnapshotResponse:
        snapshot = consistency_store.get_snapshot(matter_id=str(matter_id), actor=identity.actor)
        return PersistentDocumentConsistencySnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/document-consistency-reviews",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["document-consistency"],
    )
    async def record_document_consistency_review(
        matter_id: UUID,
        body: PersistentDocumentConsistencyReviewRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        consistency_store: Annotated[
            PersistentDocumentConsistencyPort, Depends(get_document_consistency_store)
        ],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            consistency_store.record_review(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                canonical_fields_hash=body.canonical_fields_hash, input_hash=body.input_hash,
                output_hash=body.output_hash,
                documents=tuple(
                    ReviewedWorkProduct(
                        work_product_id=str(item.work_product_id),
                        review_input_hash=item.review_input_hash,
                    ) for item in body.documents
                ),
                findings=tuple(
                    PersistedDocumentConsistencyFinding(
                        finding_id=item.finding_id, work_product_id=str(item.work_product_id),
                        severity=item.severity, code=item.code,
                        field_id_hash=item.field_id_hash,
                        source_refs_hash=item.source_refs_hash,
                    ) for item in body.findings
                ),
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/external-requests",
        response_model=PersistentExternalRequestSnapshotResponse,
        tags=["external-requests"],
    )
    async def get_external_request_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        external_store: Annotated[PersistentExternalRequestPort, Depends(get_external_request_store)],
    ) -> PersistentExternalRequestSnapshotResponse:
        snapshot = external_store.get_snapshot(matter_id=str(matter_id), actor=identity.actor)
        return PersistentExternalRequestSnapshotResponse.model_validate(snapshot.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/external-requests",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["external-requests"],
    )
    async def authorize_external_request(
        matter_id: UUID,
        body: PersistentExternalRequestPreflightRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        external_store: Annotated[PersistentExternalRequestPort, Depends(get_external_request_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            external_store.authorize_external_request(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                preflight=ExternalRequestPreflight(
                    request_kind=body.request_kind, purpose=body.purpose, provider_id=body.provider_id,
                    processor_region=body.processor_region, retention_policy=body.retention_policy,
                    training_policy=body.training_policy, selected_field_ids=tuple(body.selected_field_ids),
                    service_id=body.service_id, call_cap=body.call_cap, cost_currency=body.cost_currency,
                    cost_cap_minor=body.cost_cap_minor,
                    input_hash=body.input_hash, authorization_hash=body.authorization_hash,
                    expires_at=body.expires_at,
                ),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/external-requests/{request_id}/attempts",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["external-requests"],
    )
    async def record_external_request_attempt(
        matter_id: UUID,
        request_id: UUID,
        body: PersistentExternalRequestAttemptRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        external_store: Annotated[PersistentExternalRequestPort, Depends(get_external_request_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            external_store.record_external_attempt(
                matter_id=str(matter_id), actor=identity.actor,
                expected_version=body.expected_version, idempotency_key=idempotency_key,
                request_id=str(request_id), status=body.status,
                provider_request_ref_hash=body.provider_request_ref_hash,
                output_hash=body.output_hash, error_code=body.error_code,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/reviewable-office-drafts/{pair_id}/access",
        response_model=PersistentReviewableOfficeDraftAccessResponse,
        tags=["draft-review"],
    )
    async def issue_reviewable_office_draft_access(
        matter_id: UUID,
        pair_id: UUID,
        body: PersistentReviewableOfficeDraftAccessRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        draft_store: Annotated[PersistentReviewableDraftPort, Depends(get_reviewable_draft_store)],
    ) -> PersistentReviewableOfficeDraftAccessResponse:
        broker, _ = require_reviewable_draft_artifact_services()
        purpose = ReviewableDraftAccessPurpose(body.purpose)
        locator = draft_store.get_reviewable_office_draft_artifact_locator(
            matter_id=str(matter_id), pair_id=str(pair_id), purpose=purpose, actor=identity.actor,
        )
        issued = broker.issue(locator=locator, actor=identity.actor, session=_local_session(identity))
        return PersistentReviewableOfficeDraftAccessResponse(
            grant_id=issued.grant_id, pair_id=issued.pair_id, purpose=issued.purpose.value,
            access_token=issued.access_token, expires_at=issued.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/reviewable-office-drafts/{pair_id}/content",
        response_class=Response,
        tags=["draft-review"],
    )
    async def deliver_reviewable_office_draft(
        matter_id: UUID,
        pair_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        broker, artifact_store = require_reviewable_draft_artifact_services()
        if request.client is None:
            raise PersistentRequestBlocked("local client address is unavailable")
        delivery = broker.deliver(
            access_token=_bearer_token(authorization), actor=identity.actor,
            matter_id=str(matter_id), pair_id=str(pair_id), session=_local_session(identity),
            client_ip=request.client.host, artifact_store=artifact_store,
        )
        disposition = "inline" if delivery.purpose is ReviewableDraftAccessPurpose.REVIEW_PDF else "attachment"
        fallback_name = "reviewable-draft.pdf" if disposition == "inline" else "reviewable-draft-office"
        content_disposition = (
            f'{disposition}; filename="{fallback_name}"; '
            f"filename*=UTF-8''{quote(delivery.file_name)}"
        )
        return Response(
            content=delivery.content, media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": content_disposition,
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.artifact_sha256,
            },
        )

    @app.post(
        "/v1/matters/{matter_id}/submission-bundles/qa-ready",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["submissions"],
    )
    async def create_submission_qa_bundle(
        matter_id: UUID,
        body: PersistentSubmissionQaRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            submission_store.create_qa_ready_bundle(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                selections=tuple(
                    SubmissionComponentSelection(
                        work_product_id=str(item.work_product_id),
                        sequence=item.sequence,
                        court_filename=item.court_filename,
                    )
                    for item in body.selections
                ),
                required_document_kinds=tuple(body.required_document_kinds),
                evidence_manifest_id=str(body.evidence_manifest_id),
                legal_bundle_id=str(body.legal_bundle_id),
                calculation_run_id=str(body.calculation_run_id),
                final_text_approval_id=str(body.final_text_approval_id),
                consistency_review_id=str(body.consistency_review_id),
                consistency_output_hash=body.consistency_output_hash,
                expected_qa_hash=body.expected_qa_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/submission-bundles/{bundle_id}/lock",
        response_model=CaseLedgerReceiptResponse,
        tags=["submissions"],
    )
    async def lock_submission_bundle(
        matter_id: UUID,
        bundle_id: UUID,
        body: PersistentSubmissionLockRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            submission_store.lock_submission_bundle(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                bundle_id=str(bundle_id),
                expected_input_hash=body.expected_input_hash,
                lock_approval_hash=body.lock_approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/submission-exports/{export_id}/access",
        response_model=PersistentSubmissionAccessResponse,
        tags=["submission-access"],
    )
    async def issue_submission_export_access(
        matter_id: UUID,
        export_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        submission_store: Annotated[PersistentSubmissionPort, Depends(get_submission_store)],
    ) -> PersistentSubmissionAccessResponse:
        broker, _ = require_submission_artifact_services()
        locator = submission_store.get_verified_export_locator(
            matter_id=str(matter_id), export_id=str(export_id), actor=identity.actor
        )
        issued = broker.issue(
            locator=locator,
            actor=identity.actor,
            session=_local_session(identity),
        )
        return PersistentSubmissionAccessResponse(
            grant_id=issued.grant_id,
            export_id=issued.export_id,
            access_token=issued.access_token,
            expires_at=issued.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/submission-exports/{export_id}/content",
        response_class=Response,
        tags=["submission-access"],
    )
    async def deliver_submission_export(
        matter_id: UUID,
        export_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        broker, artifact_store = require_submission_artifact_services()
        if request.client is None:
            raise PersistentRequestBlocked("local client address is unavailable")
        delivery = broker.deliver(
            access_token=_bearer_token(authorization),
            actor=identity.actor,
            matter_id=str(matter_id),
            export_id=str(export_id),
            session=_local_session(identity),
            client_ip=request.client.host,
            artifact_store=artifact_store,
        )
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": (
                    "attachment; filename=\"court-submission.zip\"; "
                    "filename*=UTF-8''%E6%B3%95%E9%99%A2%E6%8F%90%E4%BA%A4%E6%9D%90%E6%96%99.zip"
                ),
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.artifact_sha256,
            },
        )

    @app.post(
        "/v1/matters/{matter_id}/facts",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["facts"],
    )
    async def create_fact_candidate(
        matter_id: UUID,
        body: PersistentFactCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.create_fact_candidate(
            matter_id=str(matter_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            original_text=body.original_text,
            origin=AssertionOrigin(body.origin),
            evidence_links=_evidence_links(body.evidence_links),
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/facts/{fact_id}/decision",
        response_model=CaseLedgerReceiptResponse,
        tags=["facts"],
    )
    async def decide_fact(
        matter_id: UUID,
        fact_id: UUID,
        body: PersistentFactDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        receipt = dependencies.case_ledger_store.decide_fact(
            matter_id=str(matter_id),
            fact_id=str(fact_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            status=FactStatus(body.status),
            decision_hash=body.decision_hash,
        )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/claims",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["claims"],
    )
    async def create_claim_candidate(
        matter_id: UUID,
        body: PersistentClaimCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_claim_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                original_claim_text=body.original_claim_text,
                claimed_amount=body.claimed_amount,
                currency=body.currency,
                evidence_links=_evidence_links(body.evidence_links),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/claims/{claim_id}/confirm-scope",
        response_model=CaseLedgerReceiptResponse,
        tags=["claims"],
    )
    async def confirm_claim_scope(
        matter_id: UUID,
        claim_id: UUID,
        body: PersistentConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_claim_scope(
                matter_id=str(matter_id),
                claim_id=str(claim_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                confirmation_hash=body.confirmation_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/claims/{claim_id}/response",
        response_model=CaseLedgerReceiptResponse,
        tags=["claims"],
    )
    async def set_claim_response(
        matter_id: UUID,
        claim_id: UUID,
        body: PersistentClaimResponseRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.set_claim_response(
                matter_id=str(matter_id),
                claim_id=str(claim_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                position=ClaimResponsePosition(body.position),
                confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
                partial_amount=body.partial_amount,
                currency=body.currency,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/issues",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["issues"],
    )
    async def create_dispute_issue_candidate(
        matter_id: UUID,
        body: PersistentDisputeIssueCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_dispute_issue_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                question=body.question,
                claim_ids=tuple(str(value) for value in body.claim_ids),
                confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/issues/{issue_id}/confirm",
        response_model=CaseLedgerReceiptResponse,
        tags=["issues"],
    )
    async def confirm_dispute_issue(
        matter_id: UUID,
        issue_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_dispute_issue(
                matter_id=str(matter_id),
                issue_id=str(issue_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_transaction_candidate(
        matter_id: UUID,
        body: PersistentTransactionCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_transaction_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                local_date=body.local_date,
                date_precision=DatePrecision(body.date_precision),
                amount=body.amount,
                currency=body.currency,
                direction=TransactionDirection(body.direction),
                payer_label=body.payer_label,
                payee_label=body.payee_label,
                channel=TransactionChannel(body.channel),
                transaction_reference=body.transaction_reference,
                evidence_links=_evidence_links(body.evidence_links),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions/{transaction_id}/confirm",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def confirm_transaction(
        matter_id: UUID,
        transaction_id: UUID,
        body: PersistentConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.confirm_transaction(
                matter_id=str(matter_id),
                transaction_id=str(transaction_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                confirmation_hash=body.confirmation_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transactions/{transaction_id}/payment-classifications",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_payment_classification_candidate(
        matter_id: UUID,
        transaction_id: UUID,
        body: PersistentPaymentClassificationCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_payment_classification_candidate(
                matter_id=str(matter_id),
                transaction_id=str(transaction_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                origin=ClassificationOrigin(body.origin),
                nature=PaymentNature(body.nature),
                allocations=tuple(
                    ObligationAllocation(
                        obligation_id=item.obligation_id,
                        amount=item.amount,
                        currency=item.currency,
                    )
                    for item in body.allocations
                ),
                same_day_sequence=body.same_day_sequence,
                evidence_links=_evidence_links(body.evidence_links),
                use_transaction_evidence=body.use_transaction_evidence,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/payment-classifications/{classification_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def approve_payment_classification(
        matter_id: UUID,
        classification_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.approve_payment_classification(
                matter_id=str(matter_id),
                classification_id=str(classification_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transaction-duplicate-groups",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["transactions"],
    )
    async def create_duplicate_group_candidate(
        matter_id: UUID,
        body: PersistentDuplicateGroupCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.create_duplicate_group_candidate(
                matter_id=str(matter_id),
                transaction_ids=tuple(str(value) for value in body.transaction_ids),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/transaction-duplicate-groups/{duplicate_group_id}/resolve",
        response_model=CaseLedgerReceiptResponse,
        tags=["transactions"],
    )
    async def resolve_duplicate_group(
        matter_id: UUID,
        duplicate_group_id: UUID,
        body: PersistentDuplicateGroupResolutionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            dependencies.case_ledger_store.resolve_duplicate_group(
                matter_id=str(matter_id),
                duplicate_group_id=str(duplicate_group_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                same_economic_event=body.same_economic_event,
                canonical_transaction_id=(str(body.canonical_transaction_id) if body.canonical_transaction_id else None),
                approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-snapshot",
        response_model=PersistentEvidenceSnapshotResponse,
        tags=["evidence"],
    )
    async def get_evidence_snapshot(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceSnapshotResponse:
        snapshot = evidence_store.get_evidence_snapshot(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentEvidenceSnapshotResponse.model_validate(snapshot.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/evidence-review-summary",
        response_model=PersistentEvidenceReviewSummaryResponse,
        tags=["evidence"],
    )
    async def get_evidence_review_summary(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceReviewSummaryResponse:
        summary = evidence_store.get_evidence_review_summary(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentEvidenceReviewSummaryResponse.model_validate(summary.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/evidence-pages",
        response_model=PersistentEvidencePageListResponse,
        tags=["evidence"],
    )
    async def list_evidence_page(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
        limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(min_length=20, max_length=512)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PersistentEvidencePageListResponse:
        page = evidence_store.list_evidence_page(
            matter_id=str(matter_id),
            actor=identity.actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return PersistentEvidencePageListResponse.model_validate(page.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/evidence-originals",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def register_evidence_original(
        matter_id: UUID,
        body: PersistentEvidenceOriginalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.register_original_file(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                original_label=body.original_label,
                original_file_sha256=body.original_file_sha256,
                byte_size=body.byte_size,
                media_type=body.media_type,
                page_count=body.page_count,
                source_scan_fingerprint=body.source_scan_fingerprint,
                supersedes_file_id=(str(body.supersedes_file_id) if body.supersedes_file_id else None),
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/decisions",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_page_decision(
        matter_id: UUID,
        evidence_page_id: UUID,
        body: PersistentEvidencePageDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_page_decision_candidate(
                matter_id=str(matter_id),
                evidence_page_id=str(evidence_page_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                disposition=PageDisposition(body.disposition),
                reason=body.reason,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-page-decisions/{decision_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def approve_evidence_page_decision(
        matter_id: UUID,
        decision_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.approve_page_decision(
                matter_id=str(matter_id),
                decision_id=str(decision_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/annotations",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_annotation(
        matter_id: UUID,
        evidence_page_id: UUID,
        body: PersistentEvidenceAnnotationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_annotation_candidate(
                matter_id=str(matter_id),
                evidence_page_id=str(evidence_page_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                x0=float(body.x0),
                y0=float(body.y0),
                x1=float(body.x1),
                y1=float(body.y1),
                label=body.label,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-annotations/{annotation_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def approve_evidence_annotation(
        matter_id: UUID,
        annotation_id: UUID,
        body: PersistentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.approve_annotation(
                matter_id=str(matter_id),
                annotation_id=str(annotation_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-duplicate-groups",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def create_evidence_duplicate_group(
        matter_id: UUID,
        body: PersistentEvidenceDuplicateGroupRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.create_duplicate_group_candidate(
                matter_id=str(matter_id),
                evidence_page_ids=tuple(str(value) for value in body.evidence_page_ids),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-duplicate-groups/{duplicate_group_id}/resolve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def resolve_evidence_duplicate_group(
        matter_id: UUID,
        duplicate_group_id: UUID,
        body: PersistentEvidenceDuplicateResolutionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.resolve_duplicate_group(
                matter_id=str(matter_id),
                duplicate_group_id=str(duplicate_group_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                same_source_page=body.same_source_page,
                canonical_page_id=(str(body.canonical_page_id) if body.canonical_page_id else None),
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-manifests/lock",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence"],
    )
    async def lock_evidence_manifest(
        matter_id: UUID,
        body: PersistentEvidenceManifestLockRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.lock_manifest(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                approval_hash=body.approval_hash,
                readiness_hash=body.readiness_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-manifests/{manifest_id}/derivatives",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence-worker"],
    )
    async def register_evidence_derivative(
        matter_id: UUID,
        manifest_id: UUID,
        body: PersistentEvidenceDerivativeCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.register_derivative_candidate(
                matter_id=str(matter_id),
                manifest_id=str(manifest_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                manifest_content_hash=body.manifest_content_hash,
                artifact_type=body.artifact_type,
                storage_object_key=body.storage_object_key,
                artifact_sha256=body.artifact_sha256,
                page_count=body.page_count,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-manifests/{manifest_id}/derivative-runs",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def enqueue_evidence_derivative_run(
        matter_id: UUID,
        manifest_id: UUID,
        body: PersistentEvidenceDerivativeRunRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.enqueue_derivative_run(
                matter_id=str(matter_id),
                manifest_id=str(manifest_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                manifest_content_hash=body.manifest_content_hash,
                approval_hash=body.approval_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivative-runs/{run_id}/claim",
        response_model=PersistentEvidenceDerivativeRunLeaseResponse,
        tags=["evidence-worker"],
    )
    async def claim_evidence_derivative_run(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceDerivativeRunClaimRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceDerivativeRunLeaseResponse:
        lease = evidence_store.claim_derivative_run(
            matter_id=str(matter_id),
            run_id=str(run_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            lease_seconds=body.lease_seconds,
        )
        return PersistentEvidenceDerivativeRunLeaseResponse.model_validate(lease.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivative-runs/{run_id}/complete",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def complete_evidence_derivative_run(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceDerivativeRunCompleteRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.complete_derivative_run(
                matter_id=str(matter_id),
                run_id=str(run_id),
                lease_id=str(body.lease_id),
                related_derivative_id=str(body.related_derivative_id),
                annotated_derivative_id=str(body.annotated_derivative_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivative-runs/{run_id}/heartbeat",
        response_model=PersistentEvidenceDerivativeRunHeartbeatResponse,
        tags=["evidence-worker"],
    )
    async def heartbeat_evidence_derivative_run(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceDerivativeRunHeartbeatRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceDerivativeRunHeartbeatResponse:
        expires_at = evidence_store.renew_derivative_run_lease(
            matter_id=str(matter_id),
            run_id=str(run_id),
            lease_id=str(body.lease_id),
            actor=identity.actor,
            lease_seconds=body.lease_seconds,
        )
        return PersistentEvidenceDerivativeRunHeartbeatResponse(
            run_id=run_id,
            lease_expires_at=expires_at,
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivative-runs/{run_id}/fail",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def fail_evidence_derivative_run(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceDerivativeRunFailureRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.fail_derivative_run(
                matter_id=str(matter_id),
                run_id=str(run_id),
                lease_id=str(body.lease_id),
                failure_code=body.failure_code,
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/verify",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def verify_evidence_derivative(
        matter_id: UUID,
        derivative_id: UUID,
        body: PersistentEvidenceDerivativeVerificationRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.verify_derivative(
                matter_id=str(matter_id),
                derivative_id=str(derivative_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                verification_hash=body.verification_hash,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/local-folder-selections/inspect",
        response_model=PersistentLocalFolderSelectionResponse,
        tags=["evidence-access"],
    )
    async def inspect_local_folder_selection(
        matter_id: UUID,
        request: Request,
        body: PersistentLocalFolderSelectionRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentLocalFolderSelectionResponse:
        _require_loopback(request)
        folder_grants, _ = require_original_page_services()
        inspection = folder_grants.inspect_selection(
            selected_root=body.selected_root,
            actor=identity.actor,
            matter_id=str(matter_id),
            session=_local_session(identity),
        )
        return PersistentLocalFolderSelectionResponse(
            display_name=inspection.display_name,
            root_fingerprint=inspection.root_fingerprint,
        )

    @app.post(
        "/v1/matters/{matter_id}/local-folder-grants",
        response_model=PersistentLocalFolderGrantResponse,
        tags=["evidence-access"],
    )
    async def issue_local_folder_grant(
        matter_id: UUID,
        request: Request,
        body: PersistentLocalFolderGrantRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
    ) -> PersistentLocalFolderGrantResponse:
        _require_loopback(request)
        folder_grants, _ = require_original_page_services()
        inspection = folder_grants.inspect_selection(
            selected_root=body.selected_root,
            actor=identity.actor,
            matter_id=str(matter_id),
            session=_local_session(identity),
        )
        handle = folder_grants.issue_read_grant(
            selected_root=body.selected_root,
            confirmed_root_fingerprint=body.confirmed_root_fingerprint,
            actor=identity.actor,
            matter_id=str(matter_id),
            session=_local_session(identity),
        )
        return PersistentLocalFolderGrantResponse(
            grant_id=handle.grant_id,
            display_name=inspection.display_name,
            root_fingerprint=handle.root_fingerprint,
            expires_at=handle.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/local-folder-intake",
        response_model=PersistentLocalFolderIntakeSummaryResponse,
        tags=["evidence-access"],
    )
    async def get_local_folder_intake(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentLocalFolderIntakeSummaryResponse:
        summary = evidence_store.get_local_folder_intake_summary(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentLocalFolderIntakeSummaryResponse.model_validate(summary.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/local-folder-scans",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence-access"],
    )
    async def create_local_folder_scan(
        matter_id: UUID,
        request: Request,
        body: PersistentLocalFolderScanRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        _require_loopback(request)
        folder_grants, _ = require_original_page_services()
        manifest = folder_grants.scan_granted_folder(
            grant_id=str(body.folder_grant_id),
            actor=identity.actor,
            matter_id=str(matter_id),
            session=_local_session(identity),
        )
        return _receipt(
            evidence_store.create_local_folder_scan_candidate(
                matter_id=str(matter_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                manifest=manifest,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/local-folder-scans/{scan_id}/files",
        response_model=PersistentLocalFolderScanFilePageResponse,
        tags=["evidence-access"],
    )
    async def list_local_folder_scan_files(
        matter_id: UUID,
        scan_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
        limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(min_length=20, max_length=512)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PersistentLocalFolderScanFilePageResponse:
        page = evidence_store.list_local_folder_scan_file_page(
            matter_id=str(matter_id),
            scan_id=str(scan_id),
            actor=identity.actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return PersistentLocalFolderScanFilePageResponse.model_validate(page.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/local-folder-scans/{scan_id}/approve",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-access"],
    )
    async def approve_local_folder_scan(
        matter_id: UUID,
        scan_id: UUID,
        body: PersistentLocalFolderScanApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.approve_local_folder_scan(
                matter_id=str(matter_id),
                scan_id=str(scan_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
                manifest_hash=body.manifest_hash,
                approval_hash=body.approval_hash,
            )
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-intake-runs/current",
        response_model=PersistentEvidenceIntakeSummaryResponse,
        tags=["evidence-access"],
    )
    async def get_current_evidence_intake_run(
        matter_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceIntakeSummaryResponse:
        summary = evidence_store.get_current_evidence_intake_summary(
            matter_id=str(matter_id),
            actor=identity.actor,
        )
        return PersistentEvidenceIntakeSummaryResponse.model_validate(summary.__dict__)

    @app.get(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/items",
        response_model=PersistentEvidenceIntakeItemPageResponse,
        tags=["evidence-access"],
    )
    async def list_evidence_intake_items(
        matter_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
        limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(min_length=20, max_length=512)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PersistentEvidenceIntakeItemPageResponse:
        page = evidence_store.list_evidence_intake_item_page(
            matter_id=str(matter_id),
            run_id=str(run_id),
            actor=identity.actor,
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return PersistentEvidenceIntakeItemPageResponse.model_validate(page.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs",
        response_model=CaseLedgerReceiptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence-access"],
    )
    async def enqueue_evidence_intake_run(
        matter_id: UUID,
        request: Request,
        body: PersistentEvidenceIntakeRunRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        _require_loopback(request)
        folder_grants, _ = require_original_page_services()
        current_manifest = folder_grants.scan_granted_folder(
            grant_id=str(body.folder_grant_id),
            actor=identity.actor,
            matter_id=str(matter_id),
            session=_local_session(identity),
        )
        if current_manifest.manifest_hash != body.scan_manifest_hash:
            raise LocalFolderAccessBlocked(
                "the case folder changed after approval; rescan and approve the new scope before material intake"
            )
        receipt = evidence_store.enqueue_evidence_intake_run(
            matter_id=str(matter_id),
            scan_id=str(body.scan_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            scan_manifest_hash=body.scan_manifest_hash,
            approval_hash=body.approval_hash,
        )
        if dependencies.local_evidence_intake_authorizations is not None:
            dependencies.local_evidence_intake_authorizations.bind(
                run_id=receipt.object_id,
                matter_id=str(matter_id),
                folder_grant_id=str(body.folder_grant_id),
                grant_actor=identity.actor,
                grant_session=_local_session(identity),
                expected_version=receipt.matter_version,
            )
        return _receipt(receipt)

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/claim",
        response_model=PersistentEvidenceIntakeLeaseResponse,
        tags=["evidence-worker"],
    )
    async def claim_evidence_intake_item(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceIntakeClaimRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceIntakeLeaseResponse:
        lease = evidence_store.claim_evidence_intake_item(
            matter_id=str(matter_id),
            run_id=str(run_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            lease_seconds=body.lease_seconds,
        )
        return PersistentEvidenceIntakeLeaseResponse.model_validate(lease.__dict__)

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/reap-exhausted",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def reap_exhausted_evidence_intake_items(
        matter_id: UUID,
        run_id: UUID,
        body: PersistentEvidenceIntakeReapRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.reap_exhausted_evidence_intake_items(
                matter_id=str(matter_id),
                run_id=str(run_id),
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/items/{item_id}/heartbeat",
        response_model=PersistentEvidenceIntakeHeartbeatResponse,
        tags=["evidence-worker"],
    )
    async def heartbeat_evidence_intake_item(
        matter_id: UUID,
        run_id: UUID,
        item_id: UUID,
        body: PersistentEvidenceIntakeHeartbeatRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentEvidenceIntakeHeartbeatResponse:
        expires_at = evidence_store.renew_evidence_intake_item_lease(
            matter_id=str(matter_id),
            run_id=str(run_id),
            item_id=str(item_id),
            lease_id=str(body.lease_id),
            actor=identity.actor,
            lease_seconds=body.lease_seconds,
        )
        return PersistentEvidenceIntakeHeartbeatResponse(
            run_id=run_id,
            item_id=item_id,
            lease_expires_at=expires_at,
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/items/{item_id}/complete",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def complete_evidence_intake_item(
        matter_id: UUID,
        run_id: UUID,
        item_id: UUID,
        body: PersistentEvidenceIntakeCompleteRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.complete_evidence_intake_item(
                matter_id=str(matter_id),
                run_id=str(run_id),
                item_id=str(item_id),
                lease_id=str(body.lease_id),
                evidence_file_id=str(body.evidence_file_id),
                inspection_hash=body.inspection_hash,
                scanner_name=body.scanner_name,
                scanner_definitions_version=body.scanner_definitions_version,
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-intake-runs/{run_id}/items/{item_id}/finalize",
        response_model=CaseLedgerReceiptResponse,
        tags=["evidence-worker"],
    )
    async def finalize_evidence_intake_item(
        matter_id: UUID,
        run_id: UUID,
        item_id: UUID,
        body: PersistentEvidenceIntakeFinalizeRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> CaseLedgerReceiptResponse:
        return _receipt(
            evidence_store.finalize_evidence_intake_item(
                matter_id=str(matter_id),
                run_id=str(run_id),
                item_id=str(item_id),
                lease_id=str(body.lease_id),
                outcome=body.outcome,
                outcome_code=body.outcome_code,
                inspection_hash=body.inspection_hash,
                scanner_name=body.scanner_name,
                scanner_definitions_version=body.scanner_definitions_version,
                actor=identity.actor,
                expected_version=body.expected_version,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/original-preview/access",
        response_model=PersistentOriginalPageAccessResponse,
        tags=["evidence-access"],
    )
    async def issue_original_page_access(
        matter_id: UUID,
        evidence_page_id: UUID,
        request: Request,
        body: PersistentOriginalPageAccessRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentOriginalPageAccessResponse:
        _require_loopback(request)
        _, broker = require_original_page_services()
        locator = evidence_store.get_original_page_locator(
            matter_id=str(matter_id),
            evidence_page_id=str(evidence_page_id),
            actor=identity.actor,
        )
        issued = broker.issue(
            locator=locator,
            folder_grant_id=str(body.folder_grant_id),
            actor=identity.actor,
            session=_local_session(identity),
        )
        return PersistentOriginalPageAccessResponse(
            grant_id=issued.grant_id,
            evidence_page_id=issued.evidence_page_id,
            access_token=issued.access_token,
            expires_at=issued.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-pages/{evidence_page_id}/original-preview/content",
        response_class=Response,
        tags=["evidence-access"],
    )
    async def deliver_original_page_preview(
        matter_id: UUID,
        evidence_page_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        _require_loopback(request)
        _, broker = require_original_page_services()
        delivery = broker.deliver(
            access_token=_bearer_token(authorization),
            actor=identity.actor,
            matter_id=str(matter_id),
            evidence_page_id=str(evidence_page_id),
            session=_local_session(identity),
            client_ip=request.client.host if request.client else "",
        )
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": f'inline; filename="{delivery.file_name}"',
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.content_sha256,
                "X-Image-Width": str(delivery.width),
                "X-Image-Height": str(delivery.height),
            },
        )

    @app.post(
        "/v1/native-model/matters/{matter_id}/evidence-pages/{evidence_page_id}/content",
        response_class=Response,
        include_in_schema=False,
    )
    async def deliver_native_model_page(
        matter_id: UUID,
        evidence_page_id: UUID,
        request: Request,
        folder_grant_id: UUID,
        desktop_session_id: UUID,
        external_request_id: UUID,
        expected_version: int,
        processor_region: str,
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
        external_store: Annotated[PersistentExternalRequestPort, Depends(get_external_request_store)],
    ) -> Response:
        """Return exactly one verified page to the trusted native parent.

        The route is deliberately invisible to the WebView API surface and
        needs both the process-only parent token and a live OS-bound session.
        It does not accept a file path, arbitrary URL, raw document body or
        arbitrary content type.
        """
        identity = require_native_parent(request, str(desktop_session_id))
        _, broker = require_original_page_services()
        locator = evidence_store.get_original_page_locator(
            matter_id=str(matter_id), evidence_page_id=str(evidence_page_id), actor=identity.actor,
        )
        issued = broker.issue(
            locator=locator,
            folder_grant_id=str(folder_grant_id),
            actor=identity.actor,
            session=_local_session(identity),
        )
        delivery = broker.deliver(
            access_token=issued.access_token,
            actor=identity.actor,
            matter_id=str(matter_id),
            evidence_page_id=str(evidence_page_id),
            session=_local_session(identity),
            client_ip=request.client.host if request.client else "",
        )
        if len(delivery.content) > 20 * 1024 * 1024:
            raise OriginalPageAccessBlocked("the verified evidence page exceeds the native OCR upload limit")
        worker = dependencies.native_model_worker
        if worker is None or worker.firm_id != identity.actor.firm_id:
            raise PersistentAuthenticationBlocked("native OCR worker identity is not configured")
        external_store.validate_single_page_ocr_execution(
            matter_id=str(matter_id),
            actor=worker,
            expected_version=expected_version,
            request_id=str(external_request_id),
            provider_id="qwen",
            processor_region=processor_region,
            evidence_page_id=str(evidence_page_id),
            rendered_page_sha256=delivery.content_sha256,
        )
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": f'attachment; filename="{delivery.file_name}"',
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.content_sha256,
                "X-Image-Width": str(delivery.width),
                "X-Image-Height": str(delivery.height),
            },
        )

    @app.post(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/access",
        response_model=PersistentArtifactAccessResponse,
        tags=["evidence-access"],
    )
    async def issue_evidence_derivative_access(
        matter_id: UUID,
        derivative_id: UUID,
        body: PersistentArtifactAccessRequest,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        evidence_store: Annotated[PersistentEvidenceManifestPort, Depends(get_evidence_store)],
    ) -> PersistentArtifactAccessResponse:
        broker, _ = require_artifact_services()
        locator = evidence_store.get_verified_derivative_locator(
            matter_id=str(matter_id),
            derivative_id=str(derivative_id),
            actor=identity.actor,
        )
        issued = broker.issue(
            locator=locator,
            purpose=ArtifactAccessPurpose(body.purpose),
            actor=identity.actor,
            session=_local_session(identity),
        )
        return PersistentArtifactAccessResponse(
            grant_id=issued.grant_id,
            derivative_id=issued.derivative_id,
            purpose=issued.purpose.value,
            access_token=issued.access_token,
            expires_at=issued.expires_at,
        )

    @app.get(
        "/v1/matters/{matter_id}/evidence-derivatives/{derivative_id}/content",
        response_class=Response,
        tags=["evidence-access"],
    )
    async def deliver_evidence_derivative(
        matter_id: UUID,
        derivative_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(get_identity)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        broker, artifact_store = require_artifact_services()
        access_token = _bearer_token(authorization)
        if request.client is None:
            raise PersistentRequestBlocked("local client address is unavailable")
        delivery = broker.deliver(
            access_token=access_token,
            actor=identity.actor,
            matter_id=str(matter_id),
            derivative_id=str(derivative_id),
            session=_local_session(identity),
            client_ip=request.client.host,
            artifact_store=artifact_store,
        )
        disposition = "inline" if delivery.purpose is ArtifactAccessPurpose.INLINE_PREVIEW else "attachment"
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": f'{disposition}; filename="{delivery.file_name}"',
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.artifact_sha256,
            },
        )

    return app


def _receipt(receipt: CaseLedgerCommandReceipt) -> CaseLedgerReceiptResponse:
    return CaseLedgerReceiptResponse(**receipt.__dict__)


def _local_session(identity: ServerIdentityContext) -> LocalSessionProof:
    return LocalSessionProof(
        session_id=identity.session_id,
        authentication_method=identity.authentication_method.value,
        authenticated_at=identity.authenticated_at,
        expires_at=identity.expires_at,
    )


def _bearer_token(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise PersistentRequestBlocked("artifact delivery requires a bearer token")
    token = value.removeprefix("Bearer ").strip()
    if not token:
        raise PersistentRequestBlocked("artifact delivery requires a bearer token")
    return token


def _require_loopback(request: Request) -> None:
    if request.client is None:
        raise OriginalPageAccessBlocked("original-page access requires a local client address")
    try:
        address = ip_address(request.client.host)
    except ValueError as error:
        raise OriginalPageAccessBlocked("original-page access requires a valid local client address") from error
    if not address.is_loopback:
        raise OriginalPageAccessBlocked("original-page access is restricted to the local device")


def _evidence_links(items) -> tuple[EvidenceLink, ...]:
    return tuple(
        EvidenceLink(
            evidence_id=item.evidence_id,
            original_file_sha256=item.original_file_sha256,
            page_number=item.page_number,
            region_id=item.region_id,
            original_label=item.original_label,
        )
        for item in items
    )


def _error(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": current_request_id(),
        },
    )
