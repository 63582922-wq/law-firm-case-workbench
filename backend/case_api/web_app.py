"""Same-origin browser API for the self-hosted lawyer workbench.

This is deliberately separate from :mod:`case_api.persistent_app`, whose
loopback and desktop bootstrap contract must never be widened into a browser
deployment.  This composition root accepts only an OIDC authorization-code
login coordinator and opaque cookie session authority; it exposes no bearer
token, CORS, local filesystem, object-store key, or desktop route.

The first Web vertical is intentionally narrow: sign in, list/create a case,
and hand off a PDF upload to a server-owned upload service.  Review, page
preview, manifest locking, and derivative download are added as separate
server-owned routes rather than pretending a browser has direct access to an
original object.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from case_kernel.errors import AuthorizationDenied, IdempotencyConflict, VersionConflict
from case_kernel.case_agent_fact_correction_postgres import FactCorrectionBlocked, PostgresFactCorrectionProposalStore
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.fact_claim_ledger import ClaimResponsePosition, FactStatus
from case_kernel.calculation_engine import AllocationPolicy
from case_kernel.legal_source_postgres import LegalBundleSegmentSelection
from case_kernel.transaction_ledger import ClassificationOrigin, ObligationAllocation, PaymentNature
from case_kernel.legal_rules import LegalEventKind
from case_kernel.models import Actor, Role
from case_kernel.postgres_store import (
    CaseAgentMatterProvisioningBlocked,
    PostgresMatterStore,
)
from case_kernel.request_context import reset_request_id, set_request_id
from case_kernel.workflow import MatterWorkflow

from .persistent_identity import PersistentAuthenticationBlocked, ServerIdentityContext
from .local_managed_acceptance_auth import LocalManagedAcceptanceSessionBootstrap
from .web_evidence_review import WebEvidenceReviewBlocked, WebEvidenceReviewService
from .web_derivative_delivery import WebDerivativeDeliveryBlocked, WebDerivativeDeliveryService
from .web_document_drafts import (
    WebDocumentDraftBlocked,
    WebDocumentDraftRendererBlocked,
    WebDocumentDraftRendererUnknown,
    WebDocumentDraftService,
)
from .web_document_draft_delivery import (
    WebDocumentDraftDeliveryBlocked,
    WebDocumentDraftDeliveryService,
)
from .web_oidc_login import OidcAuthorizationCodeLogin, OidcLoginBlocked
from .web_session import WebSessionAuthority, WebSessionBlocked, WebSessionGrant
from .web_case_agent_artifacts import (
    WebCaseAgentArtifactReview,
    WebCaseAgentArtifactReviewBlocked,
    WebCaseAgentArtifactReviewPort,
)
from .web_case_agent_documents import (
    WebCaseAgentDocumentReview,
    WebCaseAgentDocumentReviewBlocked,
    WebCaseAgentDocumentReviewPort,
)
from .web_common_material_upload import (
    CommonMaterialAdmissionReceipt,
    CommonMaterialUploadReconciliationRequired,
    CommonMaterialUploadReservationReceipt,
    CommonMaterialUploadStatusReceipt,
    WebCommonMaterialUploadBlocked,
)
from .web_case_posture import (
    WebCasePostureBlocked,
    WebCasePostureCommandReceipt,
    WebCasePostureCompleteReceipt,
    WebCasePostureService,
    WebCasePostureState,
)
from .web_agent_ledger_extraction_review import (
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
    WebAgentLedgerExtractionReviewBlocked,
    WebAgentLedgerExtractionReviewPort,
    WebAgentLedgerReextractionCohortCapacityExceeded,
    WebAgentLedgerReextractionSourceWindowExceeded,
)
from .web_agent_ledger_exception_followup import (
    WebAgentLedgerExceptionFollowup,
    WebAgentLedgerExceptionFollowupBlocked,
    WebAgentLedgerExceptionFollowupPage,
    WebAgentLedgerExceptionFollowupPort,
    WebAgentLedgerExceptionFollowupReceipt,
    WebAgentLedgerExceptionRecoveryReceipt,
    WebFollowupEvidencePageIdPage,
    WebAgentLedgerFollowupAction,
    WebManagedEvidenceSource,
    WebManagedEvidenceSourcePage,
    WebManagedEvidenceSourceSelection,
)


__all__ = (
    "WebApiDependencies",
    "WebApiSettings",
    "WebMaterialUploadPort",
    "WebCommonMaterialUploadPort",
    "WebAgentMaterialReviewPort",
    "WebAgentRunResponse",
    "WebAgentCandidateBatchResponse",
    "WebAgentCandidateResponse",
    "WebCaseAgentControlPort",
    "WebCaseAgentControlRunResponse",
    "WebCaseAgentCompletionReceipt",
    "WebCaseAgentCompletionResponse",
    "WebCaseAgentDecisionResponse",
    "WebCaseAgentApprovalResponse",
    "WebCaseAgentArtifactResponse",
    "WebCaseAgentArtifactReviewPort",
    "WebCaseAgentDocumentReviewPort",
    "WebDynamicCasePlanPort",
    "WebDynamicCasePlanResponse",
    "WebDynamicCasePlanItemResponse",
    "WebDynamicCasePlanReferenceResponse",
    "WebDynamicCasePlanDecisionReceipt",
    "WebDynamicCasePlanActivationReceipt",
    "WebAgentLedgerExtractionReviewPort",
    "WebAgentLedgerExceptionFollowupPort",
    "WebPdfPagePreviewPort",
    "WebEvidenceReviewService",
    "WebDerivativeDeliveryService",
    "WebDocumentDraftDeliveryService",
    "WebRequestBlocked",
    "create_web_app",
)


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_SAFE_LOGIN_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,512}$")
_COMMON_MATERIAL_SUFFIXES = frozenset(
    {".docx", ".xlsx", ".pptx", ".rtf", ".txt", ".csv", ".html", ".htm", ".eml", ".jpg", ".jpeg", ".jpe", ".png"}
)
_COMMON_MATERIAL_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/csv",
        "application/rtf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/jpeg",
        "image/png",
        "message/rfc822",
        "text/csv",
        "text/html",
        "text/plain",
        "text/rtf",
    }
)


class WebRequestBlocked(ValueError):
    """A browser request cannot cross this Web API's fixed boundary."""


class WebFeatureUnavailable(WebRequestBlocked):
    """A deployed Web feature has no complete server-owned implementation."""


class WebCalculationBlocked(WebRequestBlocked):
    """The lawyer has not supplied a complete, approval-bound calculation input."""


@dataclass(frozen=True)
class WebApiSettings:
    """Fixed, same-origin navigation and response policy for the Web API.

    Login return locations are server configuration, not request parameters.
    The API is expected to be reverse-proxied below the same HTTPS origin as
    the React workbench, so it intentionally installs no CORS middleware.
    """

    public_origin: str
    post_login_path: str = "/"
    login_failure_path: str = "/?login=failed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_origin", _normalize_https_origin(self.public_origin))
        object.__setattr__(self, "post_login_path", _normalize_fixed_local_path(self.post_login_path))
        object.__setattr__(self, "login_failure_path", _normalize_fixed_local_path(self.login_failure_path, allow_query=True))

    def own_url(self, path: str) -> str:
        return f"{self.public_origin}{path}"


@dataclass(frozen=True)
class WebUploadSlotResponse:
    """Browser-safe upload reservation returned by the material service."""

    upload_id: str
    expires_at: datetime


@dataclass(frozen=True)
class WebUploadReceipt:
    """Browser-safe result of an admitted and ledger-bound PDF upload."""

    evidence_file_id: str
    display_name: str
    content_sha256: str
    page_count: int
    matter_version: int


@dataclass(frozen=True)
class WebMaterialArchiveSlotResponse:
    """Browser-safe reservation for one ZIP material archive."""

    archive_id: str
    expires_at: datetime


@dataclass(frozen=True)
class WebMaterialArchiveReceipt:
    """Receipt for an immutable archive awaiting child-PDF processing."""

    archive_id: str
    display_name: str
    content_sha256: str
    byte_size: int
    entry_count: int
    expanded_byte_size: int
    processing_status: str


@dataclass(frozen=True)
class WebMaterialUploadStatusResponse:
    """Browser-safe status for an existing PDF/ZIP receiving operation."""

    operation_id: str
    kind: str
    status: str
    retry_allowed: bool
    receipt: WebUploadReceipt | WebMaterialArchiveReceipt | None = None


class WebMaterialUploadPort(Protocol):
    """Narrow upload service owned by server composition, never the browser.

    The future implementation persists short-lived slots, streams bytes to
    private staging, invokes a mandatory scanner, persists the private object,
    and creates immutable evidence/page records.  It must never return a
    filesystem path, object key, raw scanner payload, or an original download
    URL.
    """

    def create_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        client_filename: str,
        declared_content_length: int | None,
    ) -> WebUploadSlotResponse: ...

    async def accept_content(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        chunks: Any,
    ) -> WebUploadReceipt: ...

    def read_status(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> WebMaterialUploadStatusResponse: ...


class WebCommonMaterialUploadPort(Protocol):
    """Server-owned admission for supported non-PDF, non-ZIP materials."""

    def create_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        client_filename: str,
        declared_byte_size: int,
        declared_media_type: str,
        idempotency_key: str,
    ) -> CommonMaterialUploadReservationReceipt: ...

    async def accept_content(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
        idempotency_key: str,
        chunks: Any,
    ) -> CommonMaterialAdmissionReceipt: ...

    def read_status(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        upload_id: str,
    ) -> CommonMaterialUploadStatusReceipt: ...


class WebMaterialArchiveUploadPort(Protocol):
    """Narrow browser boundary for ZIP admission; no child is auto-confirmed."""

    def create_slot(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        client_filename: str,
        declared_content_length: int | None,
    ) -> WebMaterialArchiveSlotResponse: ...

    async def accept_content(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        archive_id: str,
        chunks: Any,
    ) -> WebMaterialArchiveReceipt: ...

    def read_status(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        archive_id: str,
    ) -> WebMaterialUploadStatusResponse: ...


class WebPdfPagePreviewPort(Protocol):
    """Server-side renderer for exactly one already-authorized evidence page."""

    def render_page(
        self,
        *,
        actor: Actor,
        matter_id: str,
        evidence_page_id: str,
    ) -> object: ...


@dataclass(frozen=True)
class WebAgentTaskResponse:
    """One browser-safe step in the fixed material-review plan."""

    task_kind: str
    status: str


@dataclass(frozen=True)
class WebAgentRunResponse:
    """Aggregate lawyer-facing run; the service may split it into model batches.

    Hashes, prompts, provider payloads, object locators and worker identifiers do
    not cross this boundary.  ``retry_allowed`` is false when submission outcome
    is unknown, so the browser cannot silently create a second provider request.
    """

    run_id: str
    matter_id: str
    matter_version: int
    scope: str
    status: str
    total_pages: int
    processed_pages: int
    remaining_pages: int
    batch_count: int
    completed_batch_count: int
    candidate_count: int
    tasks: tuple[WebAgentTaskResponse, ...]
    retry_allowed: bool
    failure_state: str | None
    created_at: datetime
    updated_at: datetime
    external_service_notice: str
    representation_profile: "WebRepresentationProfileResponse"


@dataclass(frozen=True)
class WebRepresentationProfileResponse:
    """Confirmed procedural posture used as one input to Agent legal reasoning.

    A posture never maps directly to a fixed material or document template.
    Facts, claims, procedural events and verified current authorities are
    separate, versioned inputs to a later dynamic case plan.
    """

    status: str
    active_proceeding_role: str | None
    proceeding_stage: str | None
    case_type: str | None
    version: int | None


@dataclass(frozen=True)
class WebAgentCandidateResponse:
    """Source-bound candidate that still requires a lawyer decision."""

    candidate_id: str
    evidence_page_id: str
    source_label: str
    page_number: int
    kind: str
    confidence: float
    review_priority: str
    reason_codes: tuple[str, ...]
    supporting_excerpt: str
    duplicate_of_page_id: str | None = None


@dataclass(frozen=True)
class WebAgentCandidateBatchResponse:
    run_id: str
    total_count: int
    items: tuple[WebAgentCandidateResponse, ...]
    next_cursor: str | None
    has_more: bool


class WebAgentMaterialReviewPort(Protocol):
    """Server-owned aggregate Agent workflow exposed to the lawyer workbench.

    The browser authorises the fixed scope ``ALL_CURRENT_EVIDENCE`` only.  This
    port resolves the current registered pages and splits them into bounded
    provider batches; page text, hashes and prompts are never browser input.
    """

    def queue_all_current_evidence(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
        lawyer_confirmed: bool,
    ) -> WebAgentRunResponse: ...

    def representation_profile(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
    ) -> WebRepresentationProfileResponse: ...

    def current_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
    ) -> WebAgentRunResponse | None: ...

    def get_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
    ) -> WebAgentRunResponse: ...

    def candidate_batch(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        limit: int,
        cursor: str | None,
    ) -> WebAgentCandidateBatchResponse: ...


@dataclass(frozen=True)
class WebCaseAgentCurrentWorkResponse:
    """One lawyer-readable description of the work currently in progress."""

    title: str
    detail: str
    status: str


@dataclass(frozen=True)
class WebCaseAgentControlRunResponse:
    """Safe unified Agent projection for the lawyer-facing workbench.

    The projection intentionally excludes task graphs, tool calls, prompts,
    providers, URLs, file paths, hashes and cost controls.  Those remain in
    the server-side control plane and audit trail.
    """

    run_id: str
    matter_id: str
    objective: str
    status: str
    phase_label: str
    progress_completed: int
    progress_total: int
    current_work: WebCaseAgentCurrentWorkResponse | None
    open_decision_count: int
    open_approval_count: int
    artifact_count: int
    status_message: str
    failure_message: str | None
    failure_code: str | None
    version: int
    # The authoritative case snapshot that this run analysed.  The browser
    # needs only the version number—not the snapshot hash—to explain that a
    # later claim, issue or fact change requires a distinct new round instead
    # of silently reusing the old result.
    snapshot_matter_version: int
    created_at: datetime
    updated_at: datetime
    can_pause: bool
    can_resume: bool
    can_cancel: bool
    # Safe lineage marker for the browser. It exposes no plan/task IDs or
    # hashes, but prevents a completed execution run from being mistaken for
    # the analytical source run that is allowed to start it.
    active_plan_execution: bool = False
    required_document_deliverables: tuple[str, ...] = ()
    # Aggregate matter versions also advance when a verified Agent plan is
    # registered or activated.  This server-owned state lets the browser
    # distinguish that downstream lineage from a changed factual/legal input,
    # without revealing plan IDs, task graphs or hashes.
    input_snapshot_status: str = "CURRENT"


@dataclass(frozen=True)
class WebCaseAgentCompletionReceipt:
    """Browser-safe proof that one exact reviewed run version was completed."""

    completion_id: str
    matter_id: str
    run_id: str
    reviewed_run_version: int
    completed_run_version: int
    run_status: str
    verification_status: str
    reviewed_artifact_count: int


@dataclass(frozen=True)
class WebCaseAgentCompletionResponse:
    """Final-review receipt together with the authoritative current run."""

    receipt: WebCaseAgentCompletionReceipt
    run: WebCaseAgentControlRunResponse


@dataclass(frozen=True)
class WebCaseAgentDecisionOptionResponse:
    option_id: str
    label: str
    consequence: str
    requires_note: bool = False


@dataclass(frozen=True)
class WebCaseAgentDecisionResponse:
    decision_id: str
    title: str
    question: str
    options: tuple[WebCaseAgentDecisionOptionResponse, ...]
    allow_note: bool
    blocking: bool
    status: str


@dataclass(frozen=True)
class WebCaseAgentApprovalResponse:
    approval_id: str
    action_label: str
    reason: str
    impact: str
    status: str


@dataclass(frozen=True)
class WebCaseAgentArtifactResponse:
    artifact_id: str
    title: str
    artifact_type: str
    status: str
    review_required: bool
    recovery_review_only: bool = False


class WebCaseAgentControlPort(Protocol):
    """Unified Agent application boundary; the browser supplies only intent.

    This is separate from the historical whole-case material review vertical.
    The application service owns snapshots, planning, task graphs, tools,
    network policy, recovery and audit.  None of those controls are accepted
    from the browser.
    """

    def create_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        objective: str,
        success_criteria: tuple[str, ...],
        constraints: tuple[str, ...],
        requested_deliverables: tuple[str, ...],
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def execute_active_plan(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_matter_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def get_current_run(
        self, *, identity: ServerIdentityContext, matter_id: str
    ) -> WebCaseAgentControlRunResponse | None: ...

    def continue_from_material_review(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def reconcile_active_plan_execution(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        plan_id: str,
        expected_matter_version: int,
        idempotency_key: str,
    ) -> WebCaseAgentControlRunResponse | None: ...

    def get_run(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> WebCaseAgentControlRunResponse: ...

    def pause_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def resume_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def cancel_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def complete_run(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
        document_review_versions: tuple[tuple[str, str], ...] = (),
    ) -> WebCaseAgentCompletionResponse: ...

    def reconcile_completion(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
    ) -> WebCaseAgentCompletionReceipt | None: ...

    def list_decisions(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentDecisionResponse, ...]: ...

    def submit_decision(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        decision_id: str,
        option_id: str | None,
        note: str | None,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def list_approvals(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentApprovalResponse, ...]: ...

    def submit_approval(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        run_id: str,
        approval_id: str,
        approved: bool,
        note: str | None,
        expected_run_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> WebCaseAgentControlRunResponse: ...

    def list_artifacts(
        self, *, identity: ServerIdentityContext, matter_id: str, run_id: str
    ) -> tuple[WebCaseAgentArtifactResponse, ...]: ...


@dataclass(frozen=True)
class WebDynamicCasePlanReferenceResponse:
    """One browser-safe source supporting a proposed case-plan item."""

    source_kind: str
    source_id: str
    label: str
    locator: str | None = None


@dataclass(frozen=True)
class WebDynamicCasePlanItemResponse:
    """A source-bound recommendation, never a preselected fixed workflow."""

    item_id: str
    sequence: int
    category: str
    status: str
    readiness: str
    title: str
    purpose: str
    rationale: str
    risk_if_omitted: str
    prerequisites: tuple[str, ...]
    confidence: float
    review_gate: str
    source_refs: tuple[WebDynamicCasePlanReferenceResponse, ...]
    delivery_target: str | None = None
    deliverable_kind: str | None = None
    required_for_delivery: bool = False


@dataclass(frozen=True)
class WebDynamicCasePlanResponse:
    """The current Agent judgment for this exact version of the case."""

    plan_id: str
    matter_id: str
    generated_matter_version: int
    current_matter_version: int
    status: str
    inputs_current: bool
    stale_reasons: tuple[str, ...]
    generated_at: datetime
    items: tuple[WebDynamicCasePlanItemResponse, ...]
    can_activate: bool = False
    activation_blockers: tuple[str, ...] = ()
    reviewed_item_count: int = 0


@dataclass(frozen=True)
class WebDynamicCasePlanDecisionReceipt:
    """Safe result of one lawyer decision on one explicit candidate item."""

    plan_id: str
    item_id: str
    decision_status: str
    matter_version: int
    requires_replanning: bool = False


@dataclass(frozen=True)
class WebDynamicCasePlanActivationReceipt:
    """The server activated its exact current candidate and advanced the matter."""

    plan_id: str
    status: str
    matter_version: int


class WebDynamicCasePlanPort(Protocol):
    """Optional server-owned dynamic plan service.

    It reasons from the current case snapshot, procedural posture, evidence and
    verified authorities.  The browser cannot submit a workflow, prompt,
    document list or source reference.
    """

    def current_plan(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
    ) -> WebDynamicCasePlanResponse | None: ...

    def decide_item(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        plan_id: str,
        item_id: str,
        expected_version: int,
        idempotency_key: str,
        decision: str,
        reason_code: str,
        readiness_override: str | None,
        required_for_delivery_override: bool | None,
    ) -> WebDynamicCasePlanDecisionReceipt: ...

    def activate_current_plan(
        self,
        *,
        identity: ServerIdentityContext,
        matter_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> WebDynamicCasePlanActivationReceipt: ...


@dataclass(frozen=True)
class WebApiDependencies:
    """Explicit, production-only dependencies for the browser API.

    There is no default memory mode.  Supplying no dependencies creates a
    health-only disabled service; supplying partial dependencies is rejected
    before any case route can be mounted.
    """

    settings: WebApiSettings
    oidc_login: OidcAuthorizationCodeLogin
    session_authority: WebSessionAuthority
    matter_store: PostgresMatterStore
    fact_correction_store: PostgresFactCorrectionProposalStore | None = None
    case_ledger_store: object | None = None
    legal_store: object | None = None
    official_source_capture_store: object | None = None
    formal_calculation_store: object | None = None
    submission_store: object | None = None
    upload_service: WebMaterialUploadPort | None = None
    common_material_upload_service: WebCommonMaterialUploadPort | None = None
    archive_upload_service: WebMaterialArchiveUploadPort | None = None
    page_preview_service: WebPdfPagePreviewPort | None = None
    evidence_review_service: WebEvidenceReviewService | None = None
    derivative_worker: object | None = None
    derivative_delivery_service: WebDerivativeDeliveryService | None = None
    document_draft_service: WebDocumentDraftService | None = None
    document_draft_delivery_service: WebDocumentDraftDeliveryService | None = None
    agent_material_review_service: WebAgentMaterialReviewPort | None = None
    dynamic_case_plan_service: WebDynamicCasePlanPort | None = None
    agent_ledger_extraction_review_service: WebAgentLedgerExtractionReviewPort | None = None
    agent_ledger_exception_followup_service: WebAgentLedgerExceptionFollowupPort | None = None
    case_agent_control_service: WebCaseAgentControlPort | None = None
    case_agent_artifact_review_service: WebCaseAgentArtifactReviewPort | None = None
    case_agent_document_review_service: WebCaseAgentDocumentReviewPort | None = None
    case_posture_service: WebCasePostureService | None = None
    case_agent_runtime_ready: Callable[[str], bool] | None = None
    case_agent_ledger_runtime_ready: Callable[[str], bool] | None = None
    case_agent_document_runtime_ready: Callable[[str], bool] | None = None
    local_managed_acceptance_session_bootstrap: LocalManagedAcceptanceSessionBootstrap | None = None

    def validate(self) -> None:
        if not isinstance(self.settings, WebApiSettings):
            raise ValueError("Web API settings are required")
        if not callable(getattr(self.oidc_login, "begin_authorization", None)) or not callable(
            getattr(self.oidc_login, "complete_callback", None)
        ):
            raise ValueError("Web OIDC login coordinator is invalid")
        if getattr(self.oidc_login, "public_origin", None) != self.settings.public_origin:
            raise ValueError("Web OIDC login origin must match the Web API public origin")
        if not callable(getattr(self.session_authority, "resolve", None)) or not callable(
            getattr(self.session_authority, "revoke", None)
        ) or not callable(getattr(self.session_authority, "clear_cookies", None)):
            raise ValueError("Web session authority is invalid")
        if self.local_managed_acceptance_session_bootstrap is not None and not callable(
            getattr(self.local_managed_acceptance_session_bootstrap, "issue", None)
        ):
            raise ValueError("local managed acceptance session bootstrap is invalid")
        if not callable(getattr(self.matter_store, "create", None)) or not callable(
            getattr(self.matter_store, "list_accessible", None)
        ):
            raise ValueError("Web matter store is invalid")
        if self.fact_correction_store is not None and not all(
            callable(getattr(self.fact_correction_store, method, None))
            for method in ("save", "find_by_key", "read_current", "read_context", "is_available")
        ):
            raise ValueError("Web fact correction store is invalid")
        if self.case_ledger_store is not None and not all(
            callable(getattr(self.case_ledger_store, method, None))
            for method in (
                "get_case_snapshot",
                "decide_fact",
                "confirm_claim_scope",
                "confirm_transaction",
                "create_payment_classification_candidate",
                "approve_payment_classification",
            )
        ):
            raise ValueError("Web case ledger store is invalid")
        if self.legal_store is not None and not callable(getattr(self.legal_store, "get_legal_review_snapshot", None)):
            raise ValueError("Web legal source store is invalid")
        if self.official_source_capture_store is not None and not all(
            callable(getattr(self.official_source_capture_store, method, None))
            for method in ("get_snapshot", "queue_capture", "review_capture")
        ):
            raise ValueError("Web official-source capture store is invalid")
        if self.formal_calculation_store is not None and not all(
            callable(getattr(self.formal_calculation_store, method, None))
            for method in ("get_current_calculation", "create_formal_calculation")
        ):
            raise ValueError("Web formal calculation store is invalid")
        if self.submission_store is not None and not callable(
            getattr(self.submission_store, "get_submission_snapshot", None)
        ):
            raise ValueError("Web submission store is invalid")
        if self.upload_service is not None and (
            not callable(getattr(self.upload_service, "create_slot", None))
            or not callable(getattr(self.upload_service, "accept_content", None))
        ):
            raise ValueError("Web material upload service is invalid")
        if self.common_material_upload_service is not None and not all(
            callable(getattr(self.common_material_upload_service, method, None))
            for method in ("create_slot", "accept_content", "read_status")
        ):
            raise ValueError("Web common material upload service is invalid")
        if self.archive_upload_service is not None and (
            not callable(getattr(self.archive_upload_service, "create_slot", None))
            or not callable(getattr(self.archive_upload_service, "accept_content", None))
        ):
            raise ValueError("Web material archive upload service is invalid")
        if self.page_preview_service is not None and not callable(
            getattr(self.page_preview_service, "render_page", None)
        ):
            raise ValueError("Web PDF page preview service is invalid")
        if self.evidence_review_service is not None and not all(
            callable(getattr(self.evidence_review_service, method, None))
            for method in ("summary", "pages", "create_page_decision_candidate", "confirm_page_decision", "confirm_page_decisions_batch", "stage_agent_page_decision_candidates", "create_annotation_candidate", "confirm_annotation", "lock_manifest", "enqueue_derivative_run")
        ):
            raise ValueError("Web evidence review service is invalid")
        if self.derivative_worker is not None and not callable(getattr(self.derivative_worker, "run", None)):
            raise ValueError("Web evidence derivative worker is invalid")
        if self.derivative_delivery_service is not None and not callable(
            getattr(self.derivative_delivery_service, "download", None)
        ):
            raise ValueError("Web evidence derivative delivery service is invalid")
        if self.document_draft_service is not None and not all(
            callable(getattr(self.document_draft_service, method, None))
            for method in ("snapshot", "generate")
        ):
            raise ValueError("Web document draft service is invalid")
        if self.document_draft_delivery_service is not None and not callable(
            getattr(self.document_draft_delivery_service, "download", None)
        ):
            raise ValueError("Web document draft delivery service is invalid")
        if (self.document_draft_service is None) != (
            self.document_draft_delivery_service is None
        ):
            raise ValueError(
                "Web document draft generation and delivery must be configured together"
            )
        if self.agent_material_review_service is not None and not all(
            callable(getattr(self.agent_material_review_service, method, None))
            for method in ("queue_all_current_evidence", "representation_profile", "current_run", "get_run", "candidate_batch")
        ):
            raise ValueError("Web Agent material review service is invalid")
        if self.dynamic_case_plan_service is not None and not all(
            callable(getattr(self.dynamic_case_plan_service, method, None))
            for method in ("current_plan", "decide_item", "activate_current_plan")
        ):
            raise ValueError("Web dynamic case plan service is invalid")
        if self.agent_ledger_extraction_review_service is not None and not all(
            callable(getattr(self.agent_ledger_extraction_review_service, method, None))
            for method in (
                "is_available",
                "list_batches",
                "confirm_low_risk_batch",
                "list_exception_group_members",
                "decide_exception_group",
            )
        ):
            raise ValueError("Web Agent ledger extraction review service is invalid")
        if self.agent_ledger_exception_followup_service is not None and not all(
            callable(
                getattr(self.agent_ledger_exception_followup_service, method, None)
            )
            for method in (
                "is_available",
                "list_followups",
                "list_eligible_managed_evidence_sources",
                "resolve_followup",
                "recover_exception_followups",
            )
        ):
            raise ValueError("Web Agent ledger exception follow-up service is invalid")
        if self.case_agent_control_service is not None and not all(
            callable(getattr(self.case_agent_control_service, method, None))
            for method in (
                "create_run",
                "execute_active_plan",
                "reconcile_active_plan_execution",
                "get_current_run",
                "get_run",
                "pause_run",
                "resume_run",
                "cancel_run",
                "complete_run",
                "reconcile_completion",
                "list_decisions",
                "submit_decision",
                "list_approvals",
                "submit_approval",
                "list_artifacts",
            )
        ):
            raise ValueError("Web case Agent control service is invalid")
        if self.case_agent_artifact_review_service is not None and not callable(
            getattr(self.case_agent_artifact_review_service, "read_review", None)
        ):
            raise ValueError("Web case Agent artifact review service is invalid")
        if self.case_agent_document_review_service is not None and not all(
            callable(getattr(self.case_agent_document_review_service, method, None))
            for method in ("read_review", "download", "request_revision")
        ):
            raise ValueError("Web case Agent document review service is invalid")
        if self.case_posture_service is not None and not all(
            callable(getattr(self.case_posture_service, method, None))
            for method in (
                "code_options",
                "state",
                "confirm_complete_posture",
                "confirm_party",
                "confirm_proceeding",
                "confirm_position",
                "confirm_engagement",
                "confirm_current_profile",
            )
        ):
            raise ValueError("Web case posture service is invalid")
        if self.case_agent_runtime_ready is not None and not callable(
            self.case_agent_runtime_ready
        ):
            raise ValueError("Web case Agent readiness probe is invalid")
        if self.case_agent_ledger_runtime_ready is not None and not callable(
            self.case_agent_ledger_runtime_ready
        ):
            raise ValueError("Web case Agent ledger readiness probe is invalid")
        if self.case_agent_document_runtime_ready is not None and not callable(
            self.case_agent_document_runtime_ready
        ):
            raise ValueError("Web case Agent document readiness probe is invalid")


class WebCaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=160)

    @field_validator("title")
    @classmethod
    def _title_is_meaningful(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("案件名称不能为空")
        return normalized


class WebFactCorrectionSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_matter_version: int = Field(ge=1)
    expected_revision: int = Field(ge=0, le=998)
    revised_text: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=2000)


class WebFactCorrectionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_matter_version: int = Field(ge=1)


class WebMaterialUploadCreateRequest(BaseModel):
    client_filename: str = Field(min_length=1, max_length=255)
    # Must stay aligned with WebUploadLimits' first production vertical.
    # The server re-counts bytes while streaming; this is only an early UX
    # rejection and never a trusted size assertion.
    content_length: int | None = Field(default=None, ge=1, le=256 * 1024 * 1024)
    content_type: str = Field(default="application/pdf", max_length=128)
    expected_version: int = Field(ge=1)

    @field_validator("client_filename")
    @classmethod
    def _filename_is_not_control_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("材料文件名无效")
        return normalized

    @field_validator("content_type")
    @classmethod
    def _only_pdf_vertical(cls, value: str) -> str:
        if value.strip().lower() != "application/pdf":
            raise ValueError("当前只支持上传 PDF 材料")
        return "application/pdf"


class WebMaterialArchiveCreateRequest(BaseModel):
    client_filename: str = Field(min_length=1, max_length=255)
    content_length: int | None = Field(default=None, ge=1, le=256 * 1024 * 1024)
    content_type: str = Field(default="application/zip", max_length=128)
    expected_version: int = Field(ge=1)

    @field_validator("client_filename")
    @classmethod
    def _archive_filename_is_valid(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("ZIP 材料文件名无效")
        if not normalized.lower().endswith(".zip"):
            raise ValueError("当前只支持 ZIP 材料包")
        return normalized

    @field_validator("content_type")
    @classmethod
    def _only_zip(cls, value: str) -> str:
        if value.strip().lower() != "application/zip":
            raise ValueError("当前只支持上传 ZIP 材料包")
        return "application/zip"


class WebCommonMaterialUploadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_filename: str = Field(min_length=1, max_length=255)
    content_length: int = Field(ge=1, le=100 * 1024 * 1024)
    content_type: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)

    @field_validator("client_filename")
    @classmethod
    def _filename_is_supported(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("材料文件名无效")
        suffix = "." + normalized.rsplit(".", 1)[-1].casefold() if "." in normalized else ""
        if suffix not in _COMMON_MATERIAL_SUFFIXES:
            raise ValueError("当前常见材料入口不支持该文件格式")
        return normalized

    @field_validator("content_type")
    @classmethod
    def _content_type_is_supported(cls, value: str) -> str:
        normalized = value.split(";", 1)[0].strip().lower()
        if normalized not in _COMMON_MATERIAL_CONTENT_TYPES:
            raise ValueError("常见材料的媒体类型不受支持")
        return normalized


class WebCasePosturePartyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    party_kind: str = Field(min_length=1, max_length=64)
    display_label: str = Field(min_length=1, max_length=200)
    party_id: UUID | None = None


class WebCasePostureProceedingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    forum_type: str = Field(min_length=1, max_length=64)
    case_type_code: str = Field(min_length=1, max_length=64)
    procedure_stage: str = Field(min_length=1, max_length=64)
    proceeding_id: UUID | None = None


class WebCasePosturePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    proceeding_id: UUID
    party_id: UUID
    position_code: str = Field(min_length=1, max_length=64)
    position_id: UUID | None = None


class WebCasePostureEngagementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    proceeding_id: UUID
    represented_party_id: UUID
    authority_scope_code: str = Field(min_length=1, max_length=64)
    engagement_state: str = Field(min_length=1, max_length=64)
    engagement_id: UUID | None = None


class WebCasePostureProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    represented_party_id: UUID
    proceeding_id: UUID
    position_id: UUID
    engagement_id: UUID


class WebCasePostureCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    party_kind: str = Field(min_length=1, max_length=64)
    display_label: str = Field(min_length=1, max_length=200)
    forum_type: str = Field(min_length=1, max_length=64)
    case_type_code: str = Field(min_length=1, max_length=64)
    procedure_stage: str = Field(min_length=1, max_length=64)
    position_code: str = Field(min_length=1, max_length=64)
    authority_scope_code: str = Field(min_length=1, max_length=64)
    engagement_state: str = Field(min_length=1, max_length=64)


class WebEvidenceDecisionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    disposition: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=2_000)


class WebFactDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=24)


class WebEvidenceConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class WebClaimResponseRequest(BaseModel):
    """A lawyer's source-bound response to an already confirmed claim.

    The browser may choose a position and already-confirmed facts.  It never
    supplies an approval hash or evidence location: both are derived and
    resolved by the server in the ledger transaction.
    """

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    position: Literal["ADMIT", "PARTIALLY_ADMIT", "DISPUTE", "OUTSIDE_SCOPE"]
    confirmed_fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=200)
    partial_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("confirmed_fact_ids")
    @classmethod
    def _response_fact_ids_are_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("诉请回应不能重复引用同一事实")
        return value

    @model_validator(mode="after")
    def _partial_amount_only_applies_to_partial_admission(self) -> "WebClaimResponseRequest":
        has_amount = self.partial_amount is not None or self.currency is not None
        if self.position == "PARTIALLY_ADMIT":
            if self.partial_amount is None or self.currency is None:
                raise ValueError("部分承认时必须同时填写金额和币种")
        elif has_amount:
            raise ValueError("只有部分承认可以填写回应金额")
        return self


class WebClaimCandidateRequest(BaseModel):
    """Lawyer-authored claim text bound only to server-resolved facts."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    original_claim_text: str = Field(min_length=1, max_length=8_000)
    claimed_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    confirmed_fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=30)

    @field_validator("original_claim_text")
    @classmethod
    def _claim_text_is_usable(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 and character not in "\r\n\t" for character in normalized):
            raise ValueError("诉请候选内容无效")
        return normalized

    @field_validator("confirmed_fact_ids")
    @classmethod
    def _claim_fact_ids_are_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("诉请候选不能重复引用同一事实")
        return value

    @model_validator(mode="after")
    def _claim_amount_and_currency_are_paired(self) -> "WebClaimCandidateRequest":
        if (self.claimed_amount is None) != (self.currency is None):
            raise ValueError("诉请金额和币种必须同时填写或同时留空")
        return self


class WebDisputeIssueCandidateRequest(BaseModel):
    """Lawyer-authored issue bound to current confirmed claims and facts."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=2_000)
    claim_ids: tuple[UUID, ...] = Field(min_length=1, max_length=30)
    confirmed_fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)

    @field_validator("question")
    @classmethod
    def _issue_question_is_usable(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 and character not in "\r\n\t" for character in normalized):
            raise ValueError("争点候选内容无效")
        return normalized

    @field_validator("claim_ids", "confirmed_fact_ids")
    @classmethod
    def _issue_ids_are_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("争点候选不能重复引用同一对象")
        return value


class WebEvidenceBatchConfirmationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    decision_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)

    @field_validator("decision_ids")
    @classmethod
    def _decision_ids_are_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("批量确认不能包含重复决定")
        return value


class WebAgentEvidenceCandidateStagingRequest(BaseModel):
    """Only the run and current version cross the browser boundary."""

    expected_version: int = Field(ge=1)


class WebEvidenceAnnotationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    label: str = Field(min_length=1, max_length=500)


class WebEvidenceLockRequest(BaseModel):
    expected_version: int = Field(ge=1)
    readiness_hash: str = Field(min_length=64, max_length=64)


class WebEvidenceDerivativeRunRequest(BaseModel):
    expected_version: int = Field(ge=1)
    manifest_id: str = Field(min_length=1, max_length=128)


class WebSubmissionWorkProductApprovalRequest(BaseModel):
    expected_version: int = Field(ge=1)


class WebSubmissionBundleLockRequest(BaseModel):
    expected_version: int = Field(ge=1)
    bundle_id: str = Field(min_length=1, max_length=128)


class WebDocumentDraftRequest(BaseModel):
    document_kind: str = Field(pattern=r"^(CASE_REVIEW_MEMO|PAYMENT_LEDGER)$")
    expected_version: int = Field(ge=1)


class WebDocumentDraftApprovalRequest(BaseModel):
    expected_version: int = Field(ge=1)


class WebFormalCalculationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    obligation_id: str = Field(min_length=1, max_length=160)
    start_date: date
    end_date: date
    allocation_policy: str = Field(min_length=1, max_length=64)

    @field_validator("obligation_id")
    @classmethod
    def _obligation_id_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("计算义务编号无效")
        return normalized

    @field_validator("allocation_policy")
    @classmethod
    def _allocation_policy_is_known(cls, value: str) -> str:
        normalized = value.strip()
        try:
            AllocationPolicy(normalized)
        except ValueError:
            raise ValueError("还款抵扣口径无效") from None
        return normalized

    @field_validator("end_date")
    @classmethod
    def _interval_is_non_empty(cls, value: date, info: Any) -> date:
        start = info.data.get("start_date")
        if isinstance(start, date) and value <= start:
            raise ValueError("计算结束日期必须晚于开始日期")
        return value


class WebPaymentClassificationCandidateRequest(BaseModel):
    """The browser chooses legal meaning, never source amounts or evidence."""

    expected_version: int = Field(ge=1)
    obligation_label: str = Field(min_length=1, max_length=160)
    nature: Literal[
        "DISBURSEMENT",
        "REPAYMENT_UNSPECIFIED",
        "INTEREST_PAYMENT",
        "PRINCIPAL_REPAYMENT",
    ]
    same_day_sequence: int | None = Field(default=None, ge=1, le=999)

    @field_validator("obligation_label")
    @classmethod
    def _obligation_label_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("款项归属事项无效")
        return normalized


class WebOfficialSourceCaptureRequest(BaseModel):
    """Browser chooses from a closed server catalogue, never a public URL."""

    expected_version: int = Field(ge=1)
    source_id: Literal[
        "CN-CIVIL-CODE-680",
        "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
        "SPC-PRIVATE-LENDING-2020-FIRST-REVISION",
        "SPC-PRIVATE-LENDING-2015-ORIGINAL",
        "CFETS-LPR-HISTORY",
    ]


class WebOfficialSourceCaptureReviewRequest(BaseModel):
    expected_version: int = Field(ge=1)
    decision: Literal["APPROVE_FOR_REGISTRATION", "REJECT"]
    provision_locator: str = Field(min_length=1, max_length=1_000)

    @field_validator("provision_locator")
    @classmethod
    def _provision_locator_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("依据定位无效")
        return normalized


class WebOfficialSourceCaptureRegistrationRequest(BaseModel):
    expected_version: int = Field(ge=1)


class WebLegalEventConfirmationRequest(BaseModel):
    """A lawyer confirms a date against already-visible evidence pages.

    This is intentionally not a generic rule editor: the browser supplies a
    human decision and selected same-matter pages only.  The server derives
    the approval fingerprint and owns all legal-rule parameters.
    """

    expected_version: int = Field(ge=1)
    event_kind: Literal[
        "CONTRACT_SIGNED",
        "DISBURSEMENT",
        "PAYMENT",
        "DEFAULT",
        "CLAIM_FILED",
        "CASE_ACCEPTED",
        "JUDGMENT",
    ]
    local_date: date
    evidence_page_ids: list[UUID] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _unique_evidence_pages(self) -> "WebLegalEventConfirmationRequest":
        if len(set(self.evidence_page_ids)) != len(self.evidence_page_ids):
            raise ValueError("关键日期不能重复选择同一材料页")
        return self


class WebLegalBundleApprovalRequest(BaseModel):
    """Approve one already-reviewed legal rule for its evidence-bound period.

    The browser selects only a current rule version, its matching confirmed
    trigger event, and the review period end. The server derives the start
    date, issue key, segment identity, audit fingerprint, and applicability
    record from the current matter ledgers.
    """

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    rule_version_id: UUID
    trigger_event_id: UUID
    end_date: date


class WebAgentRunRequest(BaseModel):
    """Explicit lawyer authority for one fixed whole-case material review."""

    expected_version: int = Field(ge=1)
    scope: Literal["ALL_CURRENT_EVIDENCE"]
    material_review_authorized: Literal[True]


class WebCaseAgentRunCreateRequest(BaseModel):
    """One lawyer goal; it cannot smuggle execution controls into the Agent."""

    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=2, max_length=4_000)
    success_criteria: tuple[str, ...] = Field(min_length=1, max_length=30)
    constraints: tuple[str, ...] = Field(default=(), max_length=30)
    requested_deliverables: tuple[
        Literal["CASE_REVIEW_MEMO", "DEFENCE_STATEMENT", "EVIDENCE_CATALOGUE", "SUPPLEMENTARY_EVIDENCE_CHECKLIST", "PAYMENT_LEDGER"], ...
    ] = Field(
        default=("CASE_REVIEW_MEMO", "PAYMENT_LEDGER"),
        min_length=1,
        max_length=5,
    )
    expected_version: int = Field(ge=1)

    @field_validator("objective")
    @classmethod
    def _objective_is_meaningful(cls, value: str) -> str:
        return _normalize_case_agent_business_text(value, "办案目标", 4_000)

    @field_validator("success_criteria")
    @classmethod
    def _criteria_are_meaningful(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_case_agent_text_list(value, "完成标准", maximum_items=30, maximum_length=1_000)

    @field_validator("constraints")
    @classmethod
    def _constraints_are_meaningful(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_case_agent_text_list(
            value, "办案约束", maximum_items=30, maximum_length=1_000, allow_empty=True
        )

    @field_validator("requested_deliverables")
    @classmethod
    def _deliverables_are_canonical(
        cls,
        value: tuple[
            Literal["CASE_REVIEW_MEMO", "DEFENCE_STATEMENT", "EVIDENCE_CATALOGUE", "SUPPLEMENTARY_EVIDENCE_CHECKLIST", "PAYMENT_LEDGER"], ...
        ],
    ) -> tuple[
        Literal["CASE_REVIEW_MEMO", "DEFENCE_STATEMENT", "EVIDENCE_CATALOGUE", "SUPPLEMENTARY_EVIDENCE_CHECKLIST", "PAYMENT_LEDGER"], ...
    ]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("可复核成果类型必须唯一且按服务器目录排序")
        return value


class WebCaseAgentRunCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_version: int = Field(ge=1)


class WebCaseAgentCompletionRequest(WebCaseAgentRunCommandRequest):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_review_versions: dict[str, str] = Field(default_factory=dict, max_length=128)


class WebCaseAgentDocumentRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision_number: int = Field(ge=1, le=999)


class WebDocumentParagraphChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    section_index: int = Field(ge=0, le=499)
    paragraph_index: int = Field(ge=0, le=499)
    expected_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_text: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=1000)
    source_refs: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=100)


class WebDocumentContentProposalRequest(BaseModel):
    recovery_namespace: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision_number: int = Field(ge=1, le=999)
    changes: list[WebDocumentParagraphChangeRequest] = Field(min_length=1, max_length=50)


class WebDocumentGenerationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recovery_namespace: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_revision_number: int = Field(ge=1, le=999)
    review_note: str = Field(min_length=1, max_length=2000)


class WebCaseAgentActivePlanExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class WebCaseAgentDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_version: int = Field(ge=1)
    option_id: str | None = Field(default=None, min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2_000)

    @field_validator("option_id")
    @classmethod
    def _option_id_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_case_agent_code(value, "决定选项")

    @field_validator("note")
    @classmethod
    def _decision_note_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_case_agent_business_text(value, "决定说明", 2_000)


class WebCaseAgentApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_version: int = Field(ge=1)
    approved: bool
    note: str | None = Field(default=None, max_length=2_000)

    @field_validator("note")
    @classmethod
    def _approval_note_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_case_agent_business_text(value, "审批说明", 2_000)


class WebDynamicCasePlanDecisionRequest(BaseModel):
    """A bounded lawyer decision; arbitrary prompts and prose are forbidden."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    decision: Literal["APPROVE", "MODIFY", "REJECT"]
    reason_code: Literal[
        "VERIFIED_BY_COUNSEL",
        "NOT_APPLICABLE",
        "SUPERSEDED_BY_EVIDENCE",
        "REQUIRES_FURTHER_RESEARCH",
        "PROCEDURAL_POSTURE_CHANGED",
        "INCORRECT_SOURCE_BINDING",
    ]
    readiness_override: Literal["ACTIONABLE", "NEEDS_RESEARCH", "NEEDS_INFORMATION"] | None = None
    required_for_delivery_override: bool | None = None

    @model_validator(mode="after")
    def _overrides_are_only_for_modify(self) -> "WebDynamicCasePlanDecisionRequest":
        if self.decision == "MODIFY" and self.required_for_delivery_override is None and self.readiness_override is None:
            raise ValueError("修改决定必须包含至少一个结构化调整")
        if self.decision != "MODIFY" and (
            self.required_for_delivery_override is not None or self.readiness_override is not None
        ):
            raise ValueError("批准或驳回不得携带修改字段")
        return self


class WebDynamicCasePlanActivationRequest(BaseModel):
    """Only concurrency state crosses the browser boundary; no hash or plan id."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class WebAgentLedgerExtractionConfirmRequest(BaseModel):
    """Only optimistic concurrency state may accompany whole-group confirmation."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class WebAgentLedgerExceptionDecisionRequest(BaseModel):
    """One bounded route over the complete server-owned exception group."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    decision: Literal[
        "REJECT_AS_DUPLICATE",
        "REQUEST_REEXTRACTION",
        "REQUEST_MORE_EVIDENCE",
        "DEFER_WITH_REASON",
    ]
    reason: Literal[
        "DUPLICATE_CONFIRMED",
        "SOURCE_QUALITY_INSUFFICIENT",
        "EXTRACTION_CONFLICT",
        "EVIDENCE_GAP",
        "PARTY_DATE_AMOUNT_UNCLEAR",
        "AWAITING_CLIENT_INPUT",
        "AWAITING_EXTERNAL_RECORD",
        "NEEDS_LEAD_REVIEW",
    ]
    reason_note: str | None = Field(default=None, max_length=500)


class WebAgentLedgerManagedEvidenceSourceRequest(BaseModel):
    """One opaque server-advertised source; never a path, key or hash."""

    model_config = ConfigDict(extra="forbid")
    object_type: Literal["EVIDENCE_FILE", "MATERIAL_OBJECT"]
    object_id: UUID


class WebAgentLedgerExceptionFollowupActionRequest(BaseModel):
    """A bounded transition over one server-owned ACTIVE follow-up."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    action: Literal[
        "CONFIRM_MORE_EVIDENCE",
        "RESUME",
        "WITHDRAW",
        "SUPERSEDE",
    ]
    reason_note: str = Field(min_length=1, max_length=500)
    managed_evidence_sources: list[WebAgentLedgerManagedEvidenceSourceRequest] = Field(
        default_factory=list,
        max_length=100,
    )

    @field_validator("reason_note")
    @classmethod
    def _reason_note_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized.encode("utf-8")) > 2_000
            or any(
                ord(character) < 32 and character not in "\n\t"
                for character in normalized
            )
        ):
            raise ValueError("异常后续工作操作说明无效")
        return normalized

    @model_validator(mode="after")
    def _evidence_sources_match_action(
        self,
    ) -> "WebAgentLedgerExceptionFollowupActionRequest":
        refs = [
            (item.object_type, str(item.object_id))
            for item in self.managed_evidence_sources
        ]
        if len(set(refs)) != len(refs):
            raise ValueError("补证材料选择重复")
        if self.action == "CONFIRM_MORE_EVIDENCE" and not refs:
            raise ValueError("确认补证必须选择新增受管材料")
        if self.action != "CONFIRM_MORE_EVIDENCE" and refs:
            raise ValueError("当前动作不能携带补证材料")
        return self


class WebAgentLedgerExceptionRecoveryRequest(BaseModel):
    """Only optimistic concurrency state crosses the recovery boundary."""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


def create_web_app(dependencies: WebApiDependencies | None = None) -> FastAPI:
    """Create a same-origin Web API, disabled until every root dependency exists."""

    # This import stays inside the factory because the upload implementation
    # intentionally imports this module's browser-safe response types.  The
    # special error represents an *unknown* post-object-store outcome: a
    # browser must never treat it as a rejected file and upload another copy.
    from .web_material_upload import PostObjectStoreReconciliationRequired
    from .web_archive_upload import ArchiveObjectStateUnknown
    from .web_case_agent_control import WebCaseAgentControlBlocked
    from case_kernel.web_pdf_page_preview import WebPdfPagePreviewBlocked

    enabled = dependencies is not None
    if dependencies is not None:
        dependencies.validate()
    app = FastAPI(
        title="律所案件工作台 Web API" if enabled else "律所案件工作台 Web API（未配置）",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def _request_security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = str(uuid4())
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["X-Request-ID"] = request_id
        # Route-specific delivery contracts can be stricter than the default
        # application response policy (for example, private no-store PDFs).
        # Never overwrite those contracts after the endpoint has verified and
        # selected its representation.
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store, max-age=0"
        if "Pragma" not in response.headers:
            response.headers["Pragma"] = "no-cache"
        if "X-Content-Type-Options" not in response.headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
        if "Referrer-Policy" not in response.headers:
            response.headers["Referrer-Policy"] = "no-referrer"
        if "X-Frame-Options" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        if enabled:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(PersistentAuthenticationBlocked)
    @app.exception_handler(WebSessionBlocked)
    async def _authentication_error(_: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(status.HTTP_401_UNAUTHORIZED, "AUTHENTICATION_REQUIRED", "登录状态无效或已过期，请重新登录。")

    @app.exception_handler(AuthorizationDenied)
    @app.exception_handler(PermissionError)
    async def _permission_error(_: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(status.HTTP_403_FORBIDDEN, "PERMISSION_DENIED", "你没有执行此操作的权限。")

    @app.exception_handler(VersionConflict)
    @app.exception_handler(IdempotencyConflict)
    @app.exception_handler(CaseLedgerPersistenceBlocked)
    async def _conflict_error(_: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(status.HTTP_409_CONFLICT, "VERSION_OR_IDEMPOTENCY_CONFLICT", "案件已发生变化，请刷新后再提交。")

    @app.exception_handler(PostObjectStoreReconciliationRequired)
    async def _upload_reconciliation_required(_: Request, exc: PostObjectStoreReconciliationRequired) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_409_CONFLICT,
            "UPLOAD_RECONCILIATION_REQUIRED",
            "材料接收结果仍在服务端核验中；请勿重复上传，稍后刷新案件材料记录。",
        )

    @app.exception_handler(CommonMaterialUploadReconciliationRequired)
    async def _common_material_reconciliation_required(
        _: Request, exc: CommonMaterialUploadReconciliationRequired
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_409_CONFLICT,
            "COMMON_MATERIAL_RESULT_UNKNOWN",
            "该材料的服务端接收结果仍待核验；请勿重复上传，稍后读取接收状态。",
        )

    @app.exception_handler(WebCommonMaterialUploadBlocked)
    async def _common_material_upload_blocked(
        _: Request, exc: WebCommonMaterialUploadBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "COMMON_MATERIAL_UPLOAD_BLOCKED",
            "该材料未通过安全接收要求，或当前接收状态不允许继续。",
        )

    @app.exception_handler(WebCasePostureBlocked)
    async def _case_posture_blocked(
        _: Request, exc: WebCasePostureBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "CASE_POSTURE_REQUEST_BLOCKED",
            "案件代理对象、程序阶段或诉讼地位未通过当前状态核验。",
        )

    @app.exception_handler(WebCaseAgentControlBlocked)
    async def _case_agent_control_blocked(
        _: Request, exc: WebCaseAgentControlBlocked
    ) -> JSONResponse:
        return _error(
            status.HTTP_409_CONFLICT,
            "CASE_AGENT_RUN_STATE_BLOCKED",
            str(exc),
        )

    @app.exception_handler(ArchiveObjectStateUnknown)
    async def _archive_reconciliation_required(_: Request, exc: ArchiveObjectStateUnknown) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_409_CONFLICT,
            "ARCHIVE_RECONCILIATION_REQUIRED",
            "ZIP 材料的服务端接收结果仍在核验中；请勿重复上传，先刷新案件材料状态。",
        )

    @app.exception_handler(WebDerivativeDeliveryBlocked)
    async def _derivative_delivery_error(_: Request, exc: WebDerivativeDeliveryBlocked) -> JSONResponse:
        del exc
        return _error(status.HTTP_404_NOT_FOUND, "DERIVATIVE_NOT_AVAILABLE", "该证据 PDF 尚未完成验证或已不可用。")

    @app.exception_handler(WebPdfPagePreviewBlocked)
    async def _page_preview_error(_: Request, exc: WebPdfPagePreviewBlocked) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "EVIDENCE_PAGE_PREVIEW_UNAVAILABLE",
            "无法安全生成该材料页面的预览；原始 PDF 未被发送到浏览器。",
        )

    @app.exception_handler(WebEvidenceReviewBlocked)
    async def _evidence_review_error(_: Request, exc: WebEvidenceReviewBlocked) -> JSONResponse:
        del exc
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "EVIDENCE_REVIEW_REQUEST_INVALID", "证据审阅请求不符合当前案件状态。")

    @app.exception_handler(WebDocumentDraftBlocked)
    async def _document_draft_error(_: Request, exc: WebDocumentDraftBlocked) -> JSONResponse:
        del exc
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "DOCUMENT_DRAFT_BLOCKED", "当前案件的已确认材料不足，暂不能生成文书候选。")

    @app.exception_handler(WebDocumentDraftRendererBlocked)
    async def _document_draft_renderer_blocked(
        _: Request, exc: WebDocumentDraftRendererBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DOCUMENT_DRAFT_RENDERER_UNAVAILABLE",
            "隔离文书渲染服务暂不可用；本次没有生成候选。",
        )

    @app.exception_handler(WebDocumentDraftRendererUnknown)
    async def _document_draft_renderer_unknown(
        _: Request, exc: WebDocumentDraftRendererUnknown
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DOCUMENT_DRAFT_RENDERER_RESULT_UNKNOWN",
            "文书候选生成结果尚未确认；系统未自动重试，请先刷新候选台账。",
        )

    @app.exception_handler(WebDocumentDraftDeliveryBlocked)
    async def _document_draft_delivery_error(
        _: Request, exc: WebDocumentDraftDeliveryBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "DOCUMENT_DRAFT_DELIVERY_UNAVAILABLE",
            "该文书候选当前不能安全提供审阅或下载；请刷新候选台账后再核对。",
        )

    @app.exception_handler(WebCaseAgentArtifactReviewBlocked)
    async def _case_agent_artifact_review_error(
        _: Request, exc: WebCaseAgentArtifactReviewBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "CASE_AGENT_ARTIFACT_REVIEW_UNAVAILABLE",
            "该 Agent 成果尚未通过完整复核、已失效，或当前律师无权查看。",
        )

    @app.exception_handler(WebCaseAgentDocumentReviewBlocked)
    async def _case_agent_document_review_error(
        _: Request, exc: WebCaseAgentDocumentReviewBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "CASE_AGENT_DOCUMENT_REVIEW_UNAVAILABLE",
            "该文书候选尚未通过完整复核、已失效，或当前律师无权查看。",
        )

    @app.exception_handler(WebAgentLedgerReextractionSourceWindowExceeded)
    async def _agent_ledger_reextraction_source_window_error(
        _: Request, exc: WebAgentLedgerReextractionSourceWindowExceeded
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "AGENT_LEDGER_REEXTRACTION_SOURCE_WINDOW_EXCEEDED",
            "该异常组关联的来源页超过单次重新提取上限（64页）。请先按材料范围拆分后重新分流，或选择本组允许的其他处置。",
        )

    @app.exception_handler(WebAgentLedgerReextractionCohortCapacityExceeded)
    async def _agent_ledger_reextraction_cohort_capacity_error(
        _: Request, exc: WebAgentLedgerReextractionCohortCapacityExceeded
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "AGENT_LEDGER_REEXTRACTION_COHORT_CAPACITY_EXCEEDED",
            "本案已达到重新提取任务的来源范围上限（99组）。请先完成、撤回或替代已有重新提取任务，再重试。",
        )

    @app.exception_handler(WebAgentLedgerExtractionReviewBlocked)
    async def _agent_ledger_extraction_review_error(
        _: Request, exc: WebAgentLedgerExtractionReviewBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "AGENT_LEDGER_EXTRACTION_REVIEW_BLOCKED",
            "该材料提取批次已失效、内容不完整，或当前操作不符合整组复核要求。",
        )

    @app.exception_handler(WebAgentLedgerExceptionFollowupBlocked)
    async def _agent_ledger_exception_followup_error(
        _: Request, exc: WebAgentLedgerExceptionFollowupBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "AGENT_LEDGER_EXCEPTION_FOLLOWUP_BLOCKED",
            "该异常后续工作已变化、来源不再符合要求，或当前操作不属于受控动作。",
        )

    @app.exception_handler(FactCorrectionBlocked)
    async def _fact_correction_blocked(_: Request, exc: FactCorrectionBlocked) -> JSONResponse:
        del exc
        return _error(status.HTTP_409_CONFLICT, "FACT_CORRECTION_BLOCKED",
            "当前权限、来源或修改版本已变化。请保留改稿并重新读取，不要直接重复保存。")

    @app.exception_handler(WebFeatureUnavailable)
    async def _feature_unavailable(_: Request, exc: WebFeatureUnavailable) -> JSONResponse:
        del exc
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "WEB_FEATURE_NOT_CONFIGURED", "该办案功能尚未由律所服务端配置。")

    @app.exception_handler(CaseAgentMatterProvisioningBlocked)
    async def _case_agent_principal_unavailable(
        _: Request, exc: CaseAgentMatterProvisioningBlocked
    ) -> JSONResponse:
        del exc
        return _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CASE_AGENT_SERVICE_IDENTITIES_UNAVAILABLE",
            "律所办案 Agent 服务身份尚未完成安全配置，暂不能建立新案件。",
        )

    @app.exception_handler(WebRequestBlocked)
    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "REQUEST_VALIDATION_FAILED", "提交内容不符合要求，请检查后重试。")

    @app.exception_handler(KeyError)
    async def _missing_error(_: Request, exc: KeyError) -> JSONResponse:
        del exc
        return _error(status.HTTP_404_NOT_FOUND, "OBJECT_NOT_FOUND", "未找到相应案件或材料记录。")

    @app.get("/healthz", tags=["system"])
    async def _healthz() -> dict[str, str]:
        if not enabled:
            return {"service": "lawcase-web-api", "mode": "disabled", "persistence": "not-configured"}
        return {
            "service": "lawcase-web-api",
            "mode": "self-hosted-web",
            "persistence": "configured-not-probed",
            "upload_vertical": "configured" if dependencies.upload_service is not None else "not-configured",
            "common_material_upload_vertical": "configured" if dependencies.common_material_upload_service is not None else "not-configured",
            "case_posture_vertical": "configured" if dependencies.case_posture_service is not None else "not-configured",
            "archive_upload_vertical": "configured" if dependencies.archive_upload_service is not None else "not-configured",
            "page_preview_vertical": "configured" if dependencies.page_preview_service is not None else "not-configured",
            "evidence_review_vertical": "configured" if dependencies.evidence_review_service is not None else "not-configured",
            "formal_calculation_vertical": "configured" if dependencies.formal_calculation_store is not None else "not-configured",
            "submission_vertical": "configured" if dependencies.submission_store is not None else "not-configured",
            "derivative_worker_vertical": "configured" if dependencies.derivative_worker is not None else "not-configured",
            "derivative_download_vertical": "configured" if dependencies.derivative_delivery_service is not None else "not-configured",
            "document_draft_vertical": (
                "configured"
                if dependencies.document_draft_service is not None
                and dependencies.document_draft_delivery_service is not None
                else "not-configured"
            ),
            "agent_material_review_vertical": "configured" if dependencies.agent_material_review_service is not None else "not-configured",
            "dynamic_case_plan_vertical": "configured" if dependencies.dynamic_case_plan_service is not None else "not-configured",
            "agent_ledger_extraction_review_vertical": "configured" if dependencies.agent_ledger_extraction_review_service is not None else "not-configured",
            "agent_ledger_exception_followup_vertical": "configured" if dependencies.agent_ledger_exception_followup_service is not None else "not-configured",
            "case_agent_control_vertical": (
                "configured-readiness-gated"
                if dependencies.case_agent_control_service is not None
                else "not-configured"
            ),
            "case_agent_artifact_review_vertical": (
                "configured"
                if dependencies.case_agent_artifact_review_service is not None
                else "not-configured"
            ),
            "case_agent_document_review_vertical": (
                "configured"
                if dependencies.case_agent_document_review_service is not None
                else "not-configured"
            ),
        }

    @app.get("/setup-status", tags=["system"])
    @app.get("/readyz", tags=["system"])
    async def _setup_status() -> Response:
        if not enabled:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "service": "lawcase-web-api",
                    "status": "SETUP_GATED",
                    "case_routes": "DISABLED",
                    "reason": "真实 Web 运行时尚未装配；不会展示或处理案件材料。",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "service": "lawcase-web-api",
                "status": "PRODUCTION_WEB_CONFIGURED",
                "case_routes": "ENABLED",
                "browser": "same-origin-session",
                "material_upload": dependencies.upload_service is not None,
                "common_material_upload": dependencies.common_material_upload_service is not None,
                "case_posture": dependencies.case_posture_service is not None,
                "archive_upload": dependencies.archive_upload_service is not None,
                "page_preview": dependencies.page_preview_service is not None,
                "evidence_review": dependencies.evidence_review_service is not None,
                "formal_calculation": dependencies.formal_calculation_store is not None,
                "submission": dependencies.submission_store is not None,
                "derivative_worker": dependencies.derivative_worker is not None,
                "derivative_download": dependencies.derivative_delivery_service is not None,
                "document_drafts": (
                    dependencies.document_draft_service is not None
                    and dependencies.document_draft_delivery_service is not None
                ),
                "agent_material_review": dependencies.agent_material_review_service is not None,
                "dynamic_case_plan": dependencies.dynamic_case_plan_service is not None,
                "agent_ledger_extraction_review": dependencies.agent_ledger_extraction_review_service is not None,
                "case_agent_control": (
                    "READINESS_GATED"
                    if dependencies.case_agent_control_service is not None
                    else "NOT_CONFIGURED"
                ),
                "case_agent_artifact_review": (
                    dependencies.case_agent_artifact_review_service is not None
                ),
                "case_agent_document_review": (
                    dependencies.case_agent_document_review_service is not None
                ),
                "note": "该接口证明服务装配完成；数据库、对象存储和身份供应商的实时连通性仍须由部署验收记录证明。",
            },
        )

    if dependencies is None:
        return app

    async def _identity(request: Request, response: Response) -> ServerIdentityContext:
        try:
            identity = await dependencies.session_authority.resolve(request)
        except WebSessionBlocked:
            bootstrap = dependencies.local_managed_acceptance_session_bootstrap
            if bootstrap is None:
                raise
            identity, grant = bootstrap.issue(request=request)
            response.set_cookie(**grant.session_cookie.as_response_kwargs())
            response.set_cookie(**grant.csrf_cookie.as_response_kwargs())
        identity.validate()
        return identity

    def _idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise WebRequestBlocked("an idempotency key is required")
        return value

    def _upload_service() -> WebMaterialUploadPort:
        if dependencies.upload_service is None:
            raise WebFeatureUnavailable("material upload is not configured")
        return dependencies.upload_service

    def _fact_correction_store() -> PostgresFactCorrectionProposalStore:
        if dependencies.fact_correction_store is None or not dependencies.fact_correction_store.is_available():
            raise WebFeatureUnavailable("律师事实纠正尚未配置")
        return dependencies.fact_correction_store

    def _fact_correction_submission_store() -> PostgresFactCorrectionProposalStore:
        store = _fact_correction_store()
        if (not callable(getattr(store,"is_submission_available",None))
                or not store.is_submission_available()):
            raise WebFeatureUnavailable("事实修改稿送审尚未部署")
        return store

    def _common_material_upload_service() -> WebCommonMaterialUploadPort:
        if dependencies.common_material_upload_service is None:
            raise WebFeatureUnavailable("常见材料接收尚未由律所服务端配置")
        return dependencies.common_material_upload_service

    def _archive_upload_service() -> WebMaterialArchiveUploadPort:
        if dependencies.archive_upload_service is None:
            raise WebFeatureUnavailable("ZIP 材料接收尚未配置")
        return dependencies.archive_upload_service

    def _case_posture_service() -> WebCasePostureService:
        if dependencies.case_posture_service is None:
            raise WebFeatureUnavailable("案件代理情境尚未由律所服务端配置")
        return dependencies.case_posture_service

    def _page_preview_service() -> WebPdfPagePreviewPort:
        if dependencies.page_preview_service is None:
            raise WebFeatureUnavailable("PDF page preview is not configured")
        return dependencies.page_preview_service

    def _evidence_review_service() -> WebEvidenceReviewService:
        if dependencies.evidence_review_service is None:
            raise WebFeatureUnavailable("evidence review is not configured")
        return dependencies.evidence_review_service

    def _case_ledger_store() -> object:
        if dependencies.case_ledger_store is None:
            raise WebFeatureUnavailable("案件要点台账尚未配置")
        return dependencies.case_ledger_store

    def _legal_store() -> object:
        if dependencies.legal_store is None:
            raise WebFeatureUnavailable("法律依据台账尚未配置")
        return dependencies.legal_store

    def _official_source_capture_store() -> object:
        if dependencies.official_source_capture_store is None:
            raise WebFeatureUnavailable("官方依据核对服务尚未配置")
        return dependencies.official_source_capture_store

    def _formal_calculation_store() -> object:
        if dependencies.formal_calculation_store is None:
            raise WebFeatureUnavailable("确定性利息计算服务尚未配置")
        return dependencies.formal_calculation_store

    def _submission_store() -> object:
        if dependencies.submission_store is None:
            raise WebFeatureUnavailable("应诉材料台账尚未配置")
        return dependencies.submission_store

    def _document_draft_service() -> WebDocumentDraftService:
        if dependencies.document_draft_service is None:
            raise WebFeatureUnavailable("文书生成服务尚未由律所服务端配置")
        return dependencies.document_draft_service

    def _document_draft_delivery_service() -> WebDocumentDraftDeliveryService:
        if dependencies.document_draft_delivery_service is None:
            raise WebFeatureUnavailable("文书审阅与下载服务尚未由律所服务端配置")
        return dependencies.document_draft_delivery_service

    def _agent_material_review_service() -> WebAgentMaterialReviewPort:
        if dependencies.agent_material_review_service is None:
            raise WebFeatureUnavailable("整案材料整理 Agent 尚未由律所服务端配置")
        return dependencies.agent_material_review_service

    def _dynamic_case_plan_service() -> WebDynamicCasePlanPort:
        if dependencies.dynamic_case_plan_service is None:
            raise WebFeatureUnavailable("动态办案计划 Agent 尚未由律所服务端配置")
        return dependencies.dynamic_case_plan_service

    def _agent_ledger_extraction_review_service() -> WebAgentLedgerExtractionReviewPort:
        if dependencies.agent_ledger_extraction_review_service is None:
            raise WebFeatureUnavailable("材料提取批次复核尚未由律所服务端配置")
        return dependencies.agent_ledger_extraction_review_service

    def _agent_ledger_exception_followup_service() -> WebAgentLedgerExceptionFollowupPort:
        if dependencies.agent_ledger_exception_followup_service is None:
            raise WebFeatureUnavailable("异常后续工作服务尚未由律所服务端配置")
        return dependencies.agent_ledger_exception_followup_service

    def _case_agent_control_service() -> WebCaseAgentControlPort:
        if dependencies.case_agent_control_service is None:
            raise WebFeatureUnavailable("统一办案 Agent 尚未由律所服务端配置")
        return dependencies.case_agent_control_service

    def _case_agent_artifact_review_service() -> WebCaseAgentArtifactReviewPort:
        if dependencies.case_agent_artifact_review_service is None:
            raise WebFeatureUnavailable("Agent 成果阅读服务尚未由律所服务端配置")
        return dependencies.case_agent_artifact_review_service

    def _case_agent_document_review_service() -> WebCaseAgentDocumentReviewPort:
        if dependencies.case_agent_document_review_service is None:
            raise WebFeatureUnavailable("Agent 文书阅读与下载服务尚未由律所服务端配置")
        return dependencies.case_agent_document_review_service

    def _case_agent_ready(identity: ServerIdentityContext) -> bool:
        if (
            dependencies.case_agent_control_service is None
            or dependencies.case_agent_runtime_ready is None
        ):
            return False
        try:
            return dependencies.case_agent_runtime_ready(identity.actor.firm_id) is True
        except Exception:
            return False

    def _agent_ledger_extraction_review_ready(
        identity: ServerIdentityContext,
    ) -> bool:
        service = dependencies.agent_ledger_extraction_review_service
        if service is None:
            return False
        try:
            return service.is_available(identity=identity) is True
        except Exception:
            return False

    def _agent_ledger_extraction_runtime_ready(
        identity: ServerIdentityContext,
    ) -> bool:
        probe = dependencies.case_agent_ledger_runtime_ready
        if probe is None:
            return False
        try:
            return probe(identity.actor.firm_id) is True
        except Exception:
            return False

    def _case_agent_document_ready(identity: ServerIdentityContext) -> bool:
        if (
            dependencies.case_agent_control_service is None
            or dependencies.case_agent_document_review_service is None
        ):
            return False
        probe = dependencies.case_agent_document_runtime_ready
        if probe is None:
            return False
        try:
            return probe(identity.actor.firm_id) is True
        except Exception:
            return False

    def _agent_ledger_exception_followup_ready(
        identity: ServerIdentityContext,
    ) -> bool:
        service = dependencies.agent_ledger_exception_followup_service
        if service is None:
            return False
        try:
            return service.is_available(identity=identity) is True
        except Exception:
            return False

    @app.get("/api/v1/session", tags=["session"])
    async def _get_session(identity: Annotated[ServerIdentityContext, Depends(_identity)]) -> dict[str, object]:
        roles = tuple(sorted(role.value for role in identity.actor.roles))
        can_review_agent_ledger_extractions = (
            bool(identity.actor.roles.intersection({
                Role.ASSISTANT,
                Role.COLLABORATING_LAWYER,
                Role.LEAD_LAWYER,
                Role.REVIEWER,
            }))
            and _agent_ledger_extraction_review_ready(identity)
        )
        can_review_agent_ledger_exception_followups = (
            bool(identity.actor.roles.intersection({
                Role.ASSISTANT,
                Role.COLLABORATING_LAWYER,
                Role.LEAD_LAWYER,
                Role.REVIEWER,
            }))
            and _agent_ledger_exception_followup_ready(identity)
        )
        return {
            "authenticated": True,
            "actor": {"roles": roles, "recovery_namespace": sha256(
                f"document-recovery-v1:{identity.actor.firm_id}:{identity.actor.actor_id}".encode("utf-8")
            ).hexdigest()},
            "expires_at": identity.expires_at.isoformat(),
            "capabilities": {
                "can_create_case": Role.LEAD_LAWYER in identity.actor.roles,
                "can_confirm_fact": Role.LEAD_LAWYER in identity.actor.roles,
                "can_run_calculation": Role.LEAD_LAWYER in identity.actor.roles,
                "can_upload_material": bool(
                    identity.actor.roles.intersection({Role.LEAD_LAWYER, Role.COLLABORATING_LAWYER})
                ),
                "can_upload_common_material": (
                    dependencies.common_material_upload_service is not None
                    and bool(
                        identity.actor.roles.intersection(
                            {Role.LEAD_LAWYER, Role.COLLABORATING_LAWYER}
                        )
                    )
                ),
                "can_review_case_posture": dependencies.case_posture_service is not None,
                "can_confirm_case_posture": (
                    dependencies.case_posture_service is not None
                    and Role.LEAD_LAWYER in identity.actor.roles
                ),
                "can_review_evidence": dependencies.evidence_review_service is not None,
                # Do not infer Agent availability from uploads or a model key.
                # This turns true only when a real run service is part of the
                # explicit production composition.
                "can_run_agent": dependencies.agent_material_review_service is not None,
                "can_run_case_agent": _case_agent_ready(identity),
                "can_execute_active_plan": (
                    Role.LEAD_LAWYER in identity.actor.roles
                    and _case_agent_document_ready(identity)
                ),
                "can_complete_case_agent_run": (
                    dependencies.case_agent_control_service is not None
                    and bool(
                        identity.actor.roles.intersection(
                            {Role.LEAD_LAWYER, Role.REVIEWER}
                        )
                    )
                ),
                "can_review_case_agent": (
                    dependencies.case_agent_control_service is not None
                    and (
                        dependencies.case_agent_artifact_review_service is not None
                        or dependencies.case_agent_document_review_service is not None
                    )
                ),
                "can_review_case_agent_documents": (
                    dependencies.case_agent_control_service is not None
                    and dependencies.case_agent_document_review_service is not None
                ),
                "can_review_dynamic_case_plan": dependencies.dynamic_case_plan_service is not None,
                # Historical OPEN batches remain reviewable even when the
                # Worker is offline or lacks the ledger adapter.  Starting a
                # new extraction is a distinct, stricter runtime capability.
                "can_review_agent_ledger_extractions": (
                    can_review_agent_ledger_extractions
                ),
                "can_run_agent_ledger_extraction": (
                    can_review_agent_ledger_extractions
                    and _agent_ledger_extraction_runtime_ready(identity)
                ),
                "can_review_agent_ledger_exception_followups": (
                    can_review_agent_ledger_exception_followups
                ),
                "can_review_facts": dependencies.case_ledger_store is not None,
                "can_review_legal": dependencies.legal_store is not None,
                "can_review_submission": dependencies.submission_store is not None,
                "can_generate_documents": (
                    dependencies.document_draft_service is not None
                    and dependencies.document_draft_delivery_service is not None
                ),
            },
        }

    @app.get("/api/v1/cases", tags=["cases"])
    async def _list_cases(identity: Annotated[ServerIdentityContext, Depends(_identity)]) -> dict[str, object]:
        cases: list[dict[str, object]] = []
        for item in dependencies.matter_store.list_accessible(actor=identity.actor):
            case_id = str(item.get("matter_id", ""))
            try:
                UUID(case_id)
            except (TypeError, ValueError):
                raise WebRequestBlocked("case listing returned an invalid identifier") from None
            material_count = item.get("material_count")
            if (
                isinstance(material_count, bool)
                or not isinstance(material_count, int)
                or material_count < 0
            ):
                raise WebRequestBlocked(
                    "case listing returned an invalid material count"
                )
            cases.append(
                {
                    "case_id": case_id,
                    "title": item.get("title"),
                    "stage": item.get("stage"),
                    "version": item.get("version"),
                    "updated_at": _iso_datetime(item.get("updated_at")),
                    "material_count": material_count,
                }
            )
        return {"cases": cases}

    @app.post("/api/v1/cases", status_code=status.HTTP_201_CREATED, tags=["cases"])
    async def _create_case(
        body: WebCaseCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
    ) -> dict[str, object]:
        receipt = MatterWorkflow(dependencies.matter_store).create_matter(
            identity.actor,
            matter_id=str(uuid4()),
            title=body.title,
            idempotency_key=idempotency_key,
        )
        return {
            "case": {
                "case_id": receipt.matter_id,
                "title": body.title,
                "version": receipt.matter_version,
                "stage": "CREATED",
                "updated_at": None,
                "material_count": 0,
            },
            "receipt": {
                "command_name": receipt.command_name,
                "idempotency_key": receipt.idempotency_key,
                "audit_event_id": receipt.audit_event_id,
            },
        }

    @app.get("/api/v1/cases/{case_id}/case-posture", tags=["cases"])
    async def _get_case_posture(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        return {
            "posture": _project_case_posture_state(
                service.state(identity=identity, matter_id=str(case_id)),
                expected_matter_id=str(case_id),
            ),
            "options": service.code_options(),
        }

    @app.post("/api/v1/cases/{case_id}/case-posture/parties", tags=["cases"])
    async def _confirm_case_posture_party(
        case_id: UUID,
        body: WebCasePosturePartyRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_party(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            party_kind=body.party_kind,
            display_label=body.display_label,
            party_id=None if body.party_id is None else str(body.party_id),
        )
        return {"receipt": _project_case_posture_command(receipt, expected_matter_id=str(case_id))}

    @app.post("/api/v1/cases/{case_id}/case-posture/confirm", tags=["cases"])
    async def _confirm_complete_case_posture(
        case_id: UUID,
        body: WebCasePostureCompleteRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_complete_posture(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            party_kind=body.party_kind,
            display_label=body.display_label,
            forum_type=body.forum_type,
            case_type_code=body.case_type_code,
            procedure_stage=body.procedure_stage,
            position_code=body.position_code,
            authority_scope_code=body.authority_scope_code,
            engagement_state=body.engagement_state,
        )
        return {
            "receipt": _project_case_posture_complete(
                receipt, expected_matter_id=str(case_id)
            )
        }

    @app.post("/api/v1/cases/{case_id}/case-posture/proceedings", tags=["cases"])
    async def _confirm_case_posture_proceeding(
        case_id: UUID,
        body: WebCasePostureProceedingRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_proceeding(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            forum_type=body.forum_type,
            case_type_code=body.case_type_code,
            procedure_stage=body.procedure_stage,
            proceeding_id=None if body.proceeding_id is None else str(body.proceeding_id),
        )
        return {"receipt": _project_case_posture_command(receipt, expected_matter_id=str(case_id))}

    @app.post("/api/v1/cases/{case_id}/case-posture/positions", tags=["cases"])
    async def _confirm_case_posture_position(
        case_id: UUID,
        body: WebCasePosturePositionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_position(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            proceeding_id=str(body.proceeding_id),
            party_id=str(body.party_id),
            position_code=body.position_code,
            position_id=None if body.position_id is None else str(body.position_id),
        )
        return {"receipt": _project_case_posture_command(receipt, expected_matter_id=str(case_id))}

    @app.post("/api/v1/cases/{case_id}/case-posture/engagements", tags=["cases"])
    async def _confirm_case_posture_engagement(
        case_id: UUID,
        body: WebCasePostureEngagementRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_engagement(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            proceeding_id=str(body.proceeding_id),
            represented_party_id=str(body.represented_party_id),
            authority_scope_code=body.authority_scope_code,
            engagement_state=body.engagement_state,
            engagement_id=None if body.engagement_id is None else str(body.engagement_id),
        )
        return {"receipt": _project_case_posture_command(receipt, expected_matter_id=str(case_id))}

    @app.post("/api/v1/cases/{case_id}/case-posture/profile", tags=["cases"])
    async def _confirm_case_posture_profile(
        case_id: UUID,
        body: WebCasePostureProfileRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCasePostureService, Depends(_case_posture_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_current_profile(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            represented_party_id=str(body.represented_party_id),
            proceeding_id=str(body.proceeding_id),
            position_id=str(body.position_id),
            engagement_id=str(body.engagement_id),
        )
        return {"receipt": _project_case_posture_command(receipt, expected_matter_id=str(case_id))}

    @app.post("/api/v1/cases/{case_id}/material-uploads", status_code=status.HTTP_201_CREATED, tags=["materials"])
    async def _create_material_upload(
        case_id: UUID,
        body: WebMaterialUploadCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialUploadPort, Depends(_upload_service)],
    ) -> dict[str, object]:
        slot = service.create_slot(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            client_filename=body.client_filename,
            declared_content_length=body.content_length,
        )
        _validate_upload_slot(slot)
        return {
            "upload": {
                "upload_id": slot.upload_id,
                "expires_at": slot.expires_at.isoformat(),
            }
        }

    @app.put("/api/v1/cases/{case_id}/material-uploads/{upload_id}/content", tags=["materials"])
    async def _put_material_content(
        case_id: UUID,
        upload_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialUploadPort, Depends(_upload_service)],
    ) -> dict[str, object]:
        if _content_type(request) != "application/pdf":
            raise WebRequestBlocked("only PDF content is accepted")
        receipt = await service.accept_content(
            identity=identity,
            matter_id=str(case_id),
            upload_id=str(upload_id),
            chunks=request.stream(),
        )
        _validate_upload_receipt(receipt)
        return {
            "receipt": {
                "evidence_file_id": receipt.evidence_file_id,
                "display_name": receipt.display_name,
                "sha256": receipt.content_sha256,
                "page_count": receipt.page_count,
                "scan_status": "PASSED",
                "matter_version": receipt.matter_version,
            }
        }

    @app.get("/api/v1/cases/{case_id}/material-uploads/{upload_id}", tags=["materials"])
    async def _get_material_upload_status(
        case_id: UUID,
        upload_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialUploadPort, Depends(_upload_service)],
    ) -> dict[str, object]:
        status_receipt = service.read_status(identity=identity, matter_id=str(case_id), upload_id=str(upload_id))
        _validate_status_response(status_receipt, expected_id=str(upload_id), expected_kind="PDF")
        return _project_status_response(status_receipt)

    @app.post(
        "/api/v1/cases/{case_id}/common-material-uploads",
        status_code=status.HTTP_201_CREATED,
        tags=["materials"],
    )
    async def _create_common_material_upload(
        case_id: UUID,
        body: WebCommonMaterialUploadCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCommonMaterialUploadPort, Depends(_common_material_upload_service)],
    ) -> dict[str, object]:
        slot = service.create_slot(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            client_filename=body.client_filename,
            declared_byte_size=body.content_length,
            declared_media_type=body.content_type,
            idempotency_key=idempotency_key,
        )
        _validate_common_material_slot(slot)
        return {
            "upload": {
                "upload_id": slot.upload_id,
                "expires_at": slot.expires_at.isoformat(),
            }
        }

    @app.put(
        "/api/v1/cases/{case_id}/common-material-uploads/{upload_id}/content",
        tags=["materials"],
    )
    async def _put_common_material_content(
        case_id: UUID,
        upload_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCommonMaterialUploadPort, Depends(_common_material_upload_service)],
    ) -> dict[str, object]:
        if _content_type(request) not in _COMMON_MATERIAL_CONTENT_TYPES:
            raise WebRequestBlocked("common material content type is not accepted")
        receipt = await service.accept_content(
            identity=identity,
            matter_id=str(case_id),
            upload_id=str(upload_id),
            idempotency_key=idempotency_key,
            chunks=request.stream(),
        )
        return {"receipt": _project_common_material_receipt(receipt)}

    @app.get(
        "/api/v1/cases/{case_id}/common-material-uploads/{upload_id}",
        tags=["materials"],
    )
    async def _get_common_material_upload_status(
        case_id: UUID,
        upload_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCommonMaterialUploadPort, Depends(_common_material_upload_service)],
    ) -> dict[str, object]:
        receipt = service.read_status(
            identity=identity,
            matter_id=str(case_id),
            upload_id=str(upload_id),
        )
        return {"status": _project_common_material_status(receipt, expected_id=str(upload_id))}

    @app.post("/api/v1/cases/{case_id}/material-archives", status_code=status.HTTP_201_CREATED, tags=["materials"])
    async def _create_material_archive(
        case_id: UUID,
        body: WebMaterialArchiveCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialArchiveUploadPort, Depends(_archive_upload_service)],
    ) -> dict[str, object]:
        slot = service.create_slot(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            client_filename=body.client_filename,
            declared_content_length=body.content_length,
        )
        _validate_archive_slot(slot)
        return {"upload": {"archive_id": slot.archive_id, "expires_at": slot.expires_at.isoformat()}}

    @app.put("/api/v1/cases/{case_id}/material-archives/{archive_id}/content", tags=["materials"])
    async def _put_material_archive_content(
        case_id: UUID,
        archive_id: UUID,
        request: Request,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialArchiveUploadPort, Depends(_archive_upload_service)],
    ) -> dict[str, object]:
        if _content_type(request) != "application/zip":
            raise WebRequestBlocked("only ZIP content is accepted")
        receipt = await service.accept_content(
            identity=identity,
            matter_id=str(case_id),
            archive_id=str(archive_id),
            chunks=request.stream(),
        )
        _validate_archive_receipt(receipt)
        return {
            "receipt": {
                "archive_id": receipt.archive_id,
                "display_name": receipt.display_name,
                "sha256": receipt.content_sha256,
                "byte_size": receipt.byte_size,
                "entry_count": receipt.entry_count,
                "expanded_byte_size": receipt.expanded_byte_size,
                "processing_status": receipt.processing_status,
            }
        }

    @app.get("/api/v1/cases/{case_id}/material-archives/{archive_id}", tags=["materials"])
    async def _get_material_archive_status(
        case_id: UUID,
        archive_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebMaterialArchiveUploadPort, Depends(_archive_upload_service)],
    ) -> dict[str, object]:
        status_receipt = service.read_status(identity=identity, matter_id=str(case_id), archive_id=str(archive_id))
        _validate_status_response(status_receipt, expected_id=str(archive_id), expected_kind="ZIP")
        return _project_status_response(status_receipt)

    @app.get(
        "/api/v1/cases/{case_id}/evidence-pages/{evidence_page_id}/preview",
        tags=["evidence"],
    )
    async def _get_evidence_page_preview(
        case_id: UUID,
        evidence_page_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebPdfPagePreviewPort, Depends(_page_preview_service)],
    ) -> Response:
        # Rendering reads a private object and invokes an external executable;
        # keep that bounded work off the event-loop and return only the
        # renderer's already-validated PNG bytes.
        preview = await asyncio.to_thread(
            service.render_page,
            actor=identity.actor,
            matter_id=str(case_id),
            evidence_page_id=str(evidence_page_id),
        )
        content = _validated_page_preview_png(preview, evidence_page_id=str(evidence_page_id))
        return Response(
            content=content,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Content-Disposition": "inline",
            },
        )

    @app.get("/api/v1/cases/{case_id}/evidence-summary", tags=["evidence"])
    async def _get_evidence_summary(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        return {"summary": service.summary(identity=identity, matter_id=str(case_id))}

    @app.get("/api/v1/cases/{case_id}/evidence-pages", tags=["evidence"])
    async def _list_evidence_pages(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, object]:
        page = service.pages(
            identity=identity,
            matter_id=str(case_id),
            limit=limit,
            cursor=cursor,
            expected_version=expected_version,
        )
        return {
            "matter_id": page.matter_id,
            "matter_version": page.matter_version,
            "total_count": page.total_count,
            "items": page.items,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["case-agent"],
    )
    async def _create_case_agent_run(
        case_id: UUID,
        body: WebCaseAgentRunCreateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        if not _case_agent_ready(identity):
            raise WebFeatureUnavailable(
                "统一办案 Agent 的规划器或执行 Worker 尚未就绪"
            )
        run = service.create_run(
            identity=identity,
            matter_id=str(case_id),
            objective=body.objective,
            success_criteria=body.success_criteria,
            constraints=body.constraints,
            requested_deliverables=body.requested_deliverables,
            expected_matter_version=body.expected_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
        )
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id))}

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/execute-active-plan",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["case-agent"],
    )
    async def _execute_active_case_agent_plan(
        case_id: UUID,
        body: WebCaseAgentActivePlanExecutionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebCaseAgentControlPort, Depends(_case_agent_control_service)
        ],
    ) -> dict[str, object]:
        if not _case_agent_document_ready(identity):
            raise WebFeatureUnavailable(
                "已激活计划执行所需的文书 Worker 尚未就绪"
            )
        run = service.execute_active_plan(
            identity=identity,
            matter_id=str(case_id),
            expected_matter_version=body.expected_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
        )
        return {
            "run": _project_case_agent_run(
                run,
                expected_matter_id=str(case_id),
            )
        }

    @app.get("/api/v1/cases/{case_id}/case-agent-runs/current", tags=["case-agent"])
    async def _get_current_case_agent_run(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        run = service.get_current_run(identity=identity, matter_id=str(case_id))
        return {"run": None if run is None else _project_case_agent_run(run, expected_matter_id=str(case_id))}

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/continue-analysis",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["case-agent"],
    )
    async def _continue_case_agent_analysis(
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentRunCommandRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        if not _case_agent_ready(identity):
            raise WebFeatureUnavailable("统一办案 Agent 的规划器或执行 Worker 尚未就绪")
        run = service.continue_from_material_review(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            expected_run_version=body.expected_run_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
        )
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id), expected_run_id=str(run_id))}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/active-plan-execution-intent",
        tags=["case-agent"],
    )
    async def _reconcile_active_case_agent_plan_execution(
        case_id: UUID,
        plan_id: Annotated[UUID, Query()],
        expected_version: Annotated[int, Query(ge=1)],
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebCaseAgentControlPort, Depends(_case_agent_control_service)
        ],
    ) -> dict[str, object]:
        run = service.reconcile_active_plan_execution(
            identity=identity,
            matter_id=str(case_id),
            plan_id=str(plan_id),
            expected_matter_version=expected_version,
            idempotency_key=idempotency_key,
        )
        return {
            "run": None
            if run is None
            else _project_case_agent_run(run, expected_matter_id=str(case_id))
        }

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/completion-intent",
        tags=["case-agent"],
    )
    async def _reconcile_case_agent_completion(
        case_id: UUID,
        run_id: UUID,
        expected_run_version: Annotated[int, Query(ge=1)],
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebCaseAgentControlPort, Depends(_case_agent_control_service)
        ],
    ) -> dict[str, object]:
        receipt = service.reconcile_completion(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            expected_run_version=expected_run_version,
            idempotency_key=idempotency_key,
        )
        return {
            "receipt": None
            if receipt is None
            else _project_case_agent_completion_receipt(
                receipt,
                expected_matter_id=str(case_id),
                expected_run_id=str(run_id),
                expected_reviewed_version=expected_run_version,
            )
        }

    @app.get("/api/v1/cases/{case_id}/case-agent-runs/{run_id}", tags=["case-agent"])
    async def _get_case_agent_run(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        run = service.get_run(identity=identity, matter_id=str(case_id), run_id=str(run_id))
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id), expected_run_id=str(run_id))}

    def _run_command(
        *,
        command: str,
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentRunCommandRequest,
        identity: ServerIdentityContext,
        service: WebCaseAgentControlPort,
        idempotency_key: str,
    ) -> dict[str, object]:
        method = getattr(service, f"{command}_run")
        run = method(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            expected_run_version=body.expected_run_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
        )
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id), expected_run_id=str(run_id))}

    @app.post("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/pause", tags=["case-agent"])
    async def _pause_case_agent_run(
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentRunCommandRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        return _run_command(command="pause", case_id=case_id, run_id=run_id, body=body, identity=identity, service=service, idempotency_key=idempotency_key)

    @app.post("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/resume", tags=["case-agent"])
    async def _resume_case_agent_run(
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentRunCommandRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        return _run_command(command="resume", case_id=case_id, run_id=run_id, body=body, identity=identity, service=service, idempotency_key=idempotency_key)

    @app.post("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/cancel", tags=["case-agent"])
    async def _cancel_case_agent_run(
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentRunCommandRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        return _run_command(command="cancel", case_id=case_id, run_id=run_id, body=body, identity=identity, service=service, idempotency_key=idempotency_key)

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/complete",
        tags=["case-agent"],
    )
    async def _complete_case_agent_run(
        case_id: UUID,
        run_id: UUID,
        body: WebCaseAgentCompletionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebCaseAgentControlPort, Depends(_case_agent_control_service)
        ],
    ) -> dict[str, object]:
        result = service.complete_run(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            expected_run_version=body.expected_run_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
            document_review_versions=tuple(sorted(body.document_review_versions.items())),
        )
        return _project_case_agent_completion(
            result,
            expected_matter_id=str(case_id),
            expected_run_id=str(run_id),
        )

    @app.get("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/decisions", tags=["case-agent"])
    async def _list_case_agent_decisions(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        items = service.list_decisions(identity=identity, matter_id=str(case_id), run_id=str(run_id))
        return {"items": [_project_case_agent_decision(item) for item in items]}

    @app.post("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/decisions/{decision_id}", tags=["case-agent"])
    async def _submit_case_agent_decision(
        case_id: UUID,
        run_id: UUID,
        decision_id: UUID,
        body: WebCaseAgentDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        run = service.submit_decision(
            identity=identity, matter_id=str(case_id), run_id=str(run_id), decision_id=str(decision_id),
            option_id=body.option_id, note=body.note, expected_run_version=body.expected_run_version,
            idempotency_key=idempotency_key, now=datetime.now().astimezone(),
        )
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id), expected_run_id=str(run_id))}

    @app.get("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/approvals", tags=["case-agent"])
    async def _list_case_agent_approvals(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        items = service.list_approvals(identity=identity, matter_id=str(case_id), run_id=str(run_id))
        return {"items": [_project_case_agent_approval(item) for item in items]}

    @app.post("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/approvals/{approval_id}", tags=["case-agent"])
    async def _submit_case_agent_approval(
        case_id: UUID,
        run_id: UUID,
        approval_id: UUID,
        body: WebCaseAgentApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
    ) -> dict[str, object]:
        run = service.submit_approval(
            identity=identity, matter_id=str(case_id), run_id=str(run_id), approval_id=str(approval_id),
            approved=body.approved, note=body.note, expected_run_version=body.expected_run_version,
            idempotency_key=idempotency_key, now=datetime.now().astimezone(),
        )
        return {"run": _project_case_agent_run(run, expected_matter_id=str(case_id), expected_run_id=str(run_id))}

    @app.get("/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts", tags=["case-agent"])
    async def _list_case_agent_artifacts(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentControlPort, Depends(_case_agent_control_service)],
        response: Response,
        artifact_view: Annotated[
            str | None,
            Header(alias="X-Lawcase-Artifact-View"),
        ] = None,
    ) -> dict[str, object]:
        items = service.list_artifacts(identity=identity, matter_id=str(case_id), run_id=str(run_id))
        # Older browser bundles do not understand the non-promoting recovery
        # marker.  Do not let them render a sealed recovery as an ordinary
        # completed artifact; only the explicit v1 review surface can receive
        # it.  Direct review still has the independent server-side boundary.
        if artifact_view != "sealed-recovery-v1":
            items = tuple(item for item in items if not item.recovery_review_only)
        response.headers["Vary"] = "X-Lawcase-Artifact-View"
        return {"items": [_project_case_agent_artifact(item) for item in items]}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/review",
        tags=["case-agent"],
    )
    async def _read_case_agent_artifact_review(
        case_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebCaseAgentArtifactReviewPort,
            Depends(_case_agent_artifact_review_service),
        ],
    ) -> dict[str, object]:
        review = await asyncio.to_thread(
            service.read_review,
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            artifact_id=str(artifact_id),
        )
        return {"review": _project_case_agent_artifact_review(review)}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-review",
        tags=["case-agent"],
    )
    async def _read_case_agent_document_review(
        case_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebCaseAgentDocumentReviewPort,
            Depends(_case_agent_document_review_service),
        ],
    ) -> dict[str, object]:
        review = await asyncio.to_thread(
            service.read_review,
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            artifact_id=str(artifact_id),
        )
        return {"review": _project_case_agent_document_review(review)}

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-revisions",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["case-agent"],
    )
    async def _request_case_agent_document_revision(
        case_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        body: WebCaseAgentDocumentRevisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebCaseAgentDocumentReviewPort,
            Depends(_case_agent_document_review_service),
        ],
    ) -> dict[str, object]:
        if not _case_agent_document_ready(identity):
            raise WebFeatureUnavailable("文书生成与独立复核 Worker 尚未就绪")
        review = await asyncio.to_thread(
            service.request_revision,
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            artifact_id=str(artifact_id),
            expected_revision_number=body.expected_revision_number,
            idempotency_key=idempotency_key,
        )
        return {"review": _project_case_agent_document_review(review)}

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-content-proposals/{proposal_id}/generation-reviews",
        tags=["case-agent"],
    )
    async def _authorize_document_content_generation(
        case_id: UUID, run_id: UUID, artifact_id: UUID, proposal_id: UUID,
        body: WebDocumentGenerationReviewRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentDocumentReviewPort, Depends(_case_agent_document_review_service)],
    ) -> dict[str, object]:
        from case_kernel.case_agent_document_revisions import CaseAgentDocumentRevisionBlocked
        if body.recovery_namespace is not None and body.recovery_namespace != sha256(
            f"document-recovery-v1:{identity.actor.firm_id}:{identity.actor.actor_id}".encode("utf-8")
        ).hexdigest():
            raise WebRequestBlocked("登录身份已变化，未记录授权；请重新打开文书。")
        authorize = getattr(service, "authorize_content_generation", None)
        if not callable(authorize):
            raise WebFeatureUnavailable("修改复核服务尚未配置，未记录授权。")
        try:
            review_id = await asyncio.to_thread(authorize, identity=identity, matter_id=str(case_id),
                run_id=str(run_id), artifact_id=str(artifact_id), proposal_id=str(proposal_id),
                expected_revision_number=body.expected_revision_number, review_note=body.review_note,
                idempotency_key=idempotency_key)
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebRequestBlocked("生成授权未记录，请核对修改、文书版本和来源。") from error
        return {"review_id": _project_uuid(review_id, "generation review id"),
                "status": "AUTHORIZED_NOT_GENERATED", "court_ready": False}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-content-proposal-status",
        tags=["case-agent"],
    )
    async def _resolve_document_content_proposal(
        case_id: UUID, run_id: UUID, artifact_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentDocumentReviewPort, Depends(_case_agent_document_review_service)],
    ) -> dict[str, object]:
        from case_kernel.case_agent_document_revisions import CaseAgentDocumentRevisionBlocked
        resolve = getattr(service, "resolve_content_proposal", None)
        if not callable(resolve):
            raise WebFeatureUnavailable("保存结果核对尚未配置。")
        try:
            proposal_id = await asyncio.to_thread(
                resolve, identity=identity, matter_id=str(case_id), run_id=str(run_id),
                artifact_id=str(artifact_id), idempotency_key=idempotency_key,
            )
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebRequestBlocked("保存结果尚无法核对，请勿重复提交。") from error
        return {"status": "RECORDED" if proposal_id else "UNCONFIRMED",
                "proposal_id": _project_uuid(proposal_id, "content proposal id") if proposal_id else None,
                "court_ready": False}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-content-proposals",
        tags=["case-agent"],
    )
    async def _list_document_content_proposals(
        case_id: UUID, run_id: UUID, artifact_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentDocumentReviewPort, Depends(_case_agent_document_review_service)],
        after: UUID | None = None,
    ) -> dict[str, object]:
        from case_kernel.case_agent_document_revisions import CaseAgentDocumentRevisionBlocked
        read = getattr(service, "list_content_proposals", None)
        if not callable(read):
            raise WebFeatureUnavailable("修改记录列表尚未配置。")
        try:
            return await asyncio.to_thread(
                read, identity=identity, matter_id=str(case_id), run_id=str(run_id),
                artifact_id=str(artifact_id), after=str(after) if after is not None else None,
            )
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebRequestBlocked("无法读取修改记录，请核对案件、文书和访问权限。") from error

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-content-proposals/{proposal_id}",
        tags=["case-agent"],
    )
    async def _read_document_content_proposal(
        case_id: UUID, run_id: UUID, artifact_id: UUID, proposal_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebCaseAgentDocumentReviewPort, Depends(_case_agent_document_review_service)],
    ) -> dict[str, object]:
        from case_kernel.case_agent_document_revisions import CaseAgentDocumentRevisionBlocked
        read = getattr(service, "read_content_proposal", None)
        if not callable(read):
            raise WebFeatureUnavailable("修改记录读取尚未配置。")
        try:
            return await asyncio.to_thread(
                read, identity=identity, matter_id=str(case_id), run_id=str(run_id),
                artifact_id=str(artifact_id), proposal_id=str(proposal_id),
            )
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebRequestBlocked("无法读取这项修改，请核对案件、文书和访问权限。") from error

    @app.post(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-content-proposals",
        tags=["case-agent"],
    )
    async def _save_document_content_proposal(
        case_id: UUID, run_id: UUID, artifact_id: UUID,
        body: WebDocumentContentProposalRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebCaseAgentDocumentReviewPort, Depends(_case_agent_document_review_service)],
    ) -> dict[str, object]:
        from case_kernel.case_agent_document_revisions import LawyerParagraphChange, CaseAgentDocumentRevisionBlocked
        if body.recovery_namespace is not None and body.recovery_namespace != sha256(
            f"document-recovery-v1:{identity.actor.firm_id}:{identity.actor.actor_id}".encode("utf-8")
        ).hexdigest():
            raise WebRequestBlocked("登录身份已变化，修改未保存；请重新打开文书。")
        save = getattr(service, "save_content_proposal", None)
        if not callable(save):
            raise WebFeatureUnavailable("正文修订服务尚未配置，修改未保存。")
        changes = tuple(LawyerParagraphChange(
            item.section_index, item.paragraph_index, item.expected_text_hash,
            item.replacement_text, item.reason, tuple(item.source_refs),
        ) for item in body.changes)
        try:
            proposal_id = await asyncio.to_thread(
                save, identity=identity, matter_id=str(case_id), run_id=str(run_id),
                artifact_id=str(artifact_id), expected_revision_number=body.expected_revision_number,
                idempotency_key=idempotency_key, changes=changes,
            )
        except CaseAgentDocumentRevisionBlocked as error:
            raise WebRequestBlocked("修改未保存，请核对文书版本、段落和来源后重试。") from error
        return {"proposal_id": _project_uuid(proposal_id, "content proposal id"), "status": "NEEDS_SOURCE_AND_LAWYER_REVIEW", "court_ready": False}

    @app.get(
        "/api/v1/cases/{case_id}/case-agent-runs/{run_id}/artifacts/{artifact_id}/document-files/{file_role}",
        tags=["case-agent"],
    )
    async def _download_case_agent_document(
        case_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        file_role: Literal["editable", "pdf-preview"],
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebCaseAgentDocumentReviewPort,
            Depends(_case_agent_document_review_service),
        ],
        expected_review_version: Annotated[str | None, Header(alias="X-Document-Review-Version", pattern=r"^[a-f0-9]{64}$")] = None,
    ) -> Response:
        delivery = await asyncio.to_thread(
            service.download,
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            artifact_id=str(artifact_id),
            file_role=file_role,
            expected_review_version=expected_review_version,
        )
        if (
            delivery.disposition not in {"inline", "attachment"}
            or (expected_review_version is not None and delivery.review_version != expected_review_version)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", delivery.ascii_file_name)
            or not isinstance(delivery.file_name, str)
            or not 1 <= len(delivery.file_name) <= 120
            or any(character in delivery.file_name for character in "\r\n\x00")
            or not isinstance(delivery.content, bytes)
            or not delivery.content
        ):
            raise WebRequestBlocked("Agent document delivery is invalid")
        content_disposition = (
            f'{delivery.disposition}; filename="{delivery.ascii_file_name}"; '
            f"filename*=UTF-8''{quote(delivery.file_name, safe='')}"
        )
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": content_disposition,
                **({"X-Document-Review-Version": _project_hash(delivery.review_version, "document review version")}
                   if delivery.review_version is not None else {}),
                "Content-Security-Policy": "sandbox",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/cases/{case_id}/agent-runs/current", tags=["agent-material-review"])
    async def _get_current_agent_material_run(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebAgentMaterialReviewPort, Depends(_agent_material_review_service)],
    ) -> dict[str, object]:
        current = service.current_run(identity=identity, matter_id=str(case_id))
        return {"run": None if current is None else _project_agent_run(current)}

    @app.get("/api/v1/cases/{case_id}/representation-profile", tags=["agent-material-review"])
    async def _get_representation_profile(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebAgentMaterialReviewPort, Depends(_agent_material_review_service)],
    ) -> dict[str, object]:
        profile = service.representation_profile(identity=identity, matter_id=str(case_id))
        return {"profile": _project_representation_profile(profile)}

    @app.post(
        "/api/v1/cases/{case_id}/agent-runs",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["agent-material-review"],
    )
    async def _queue_agent_material_run(
        case_id: UUID,
        body: WebAgentRunRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebAgentMaterialReviewPort, Depends(_agent_material_review_service)],
    ) -> dict[str, object]:
        # ``material_review_authorized`` is a one-command lawyer authority,
        # not a stored blanket consent.  The fixed service scope resolves
        # registered pages server-side and may split them into bounded runs.
        profile = service.representation_profile(identity=identity, matter_id=str(case_id))
        _project_representation_profile(profile)
        if profile.status != "CONFIRMED":
            raise WebRequestBlocked("a confirmed representation profile is required before Agent execution")
        run = service.queue_all_current_evidence(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            lawyer_confirmed=True,
        )
        if run.representation_profile != profile:
            raise WebRequestBlocked("Agent run is not bound to the confirmed representation profile")
        return {"run": _project_agent_run(run)}

    @app.get("/api/v1/cases/{case_id}/agent-runs/{run_id}", tags=["agent-material-review"])
    async def _get_agent_material_run(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebAgentMaterialReviewPort, Depends(_agent_material_review_service)],
    ) -> dict[str, object]:
        run = service.get_run(identity=identity, matter_id=str(case_id), run_id=str(run_id))
        return {"run": _project_agent_run(run)}

    @app.get(
        "/api/v1/cases/{case_id}/agent-runs/{run_id}/candidate-batches",
        tags=["agent-material-review"],
    )
    async def _get_agent_material_candidate_batch(
        case_id: UUID,
        run_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebAgentMaterialReviewPort, Depends(_agent_material_review_service)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> dict[str, object]:
        batch = service.candidate_batch(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            limit=limit,
            cursor=cursor,
        )
        return {"candidates": _project_agent_candidate_batch(batch)}

    @app.post(
        "/api/v1/cases/{case_id}/agent-runs/{run_id}/evidence-decision-candidates",
        status_code=status.HTTP_201_CREATED,
        tags=["agent-material-review", "evidence"],
    )
    async def _stage_agent_evidence_decision_candidates(
        case_id: UUID,
        run_id: UUID,
        body: WebAgentEvidenceCandidateStagingRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        result = service.stage_agent_page_decision_candidates(
            identity=identity,
            matter_id=str(case_id),
            run_id=str(run_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        receipt = result["receipt"]
        excluded = result["excluded"]
        return {
            "receipt": _evidence_receipt(receipt),
            "candidate_batch": {
                "run_id": _project_uuid(result["run_id"], "Agent run id"),
                "decision_ids": [
                    _project_uuid(value, "evidence decision id")
                    for value in result["decision_ids"]
                ],
                "page_ids": [
                    _project_uuid(value, "evidence page id")
                    for value in result["page_ids"]
                ],
                "include_count": int(result["include_count"]),
                "exclude_count": int(result["exclude_count"]),
                "excluded": [
                    {
                        "category": _project_text(item["category"], "Agent exclusion category", 80),
                        "count": int(item["count"]),
                        "page_ids": [
                            _project_uuid(value, "Agent excluded evidence page id")
                            for value in item["page_ids"]
                        ],
                    }
                    for item in excluded
                ],
                "status": "CANDIDATE",
                "requires_lead_lawyer_confirmation": True,
            },
        }

    @app.get("/api/v1/cases/{case_id}/dynamic-case-plan", tags=["dynamic-case-plan"])
    async def _get_dynamic_case_plan(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebDynamicCasePlanPort, Depends(_dynamic_case_plan_service)],
    ) -> dict[str, object]:
        plan = service.current_plan(identity=identity, matter_id=str(case_id))
        if plan is None:
            return {"plan": None}
        if plan.matter_id != str(case_id):
            raise WebRequestBlocked("dynamic case plan is not bound to this matter")
        return {"plan": _project_dynamic_case_plan(plan)}

    @app.post(
        "/api/v1/cases/{case_id}/dynamic-case-plans/{plan_id}/items/{item_id}/decision",
        tags=["dynamic-case-plan"],
    )
    async def _decide_dynamic_case_plan_item(
        case_id: UUID,
        plan_id: UUID,
        item_id: UUID,
        body: WebDynamicCasePlanDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebDynamicCasePlanPort, Depends(_dynamic_case_plan_service)],
    ) -> dict[str, object]:
        # Do not pre-read and then write: that creates a TOCTOU window and can
        # block an exact idempotency replay after commit-but-response-loss.
        # The PostgreSQL command authorizes, locks, checks the latest candidate
        # and handles replay in its one transaction.
        receipt = service.decide_item(
            identity=identity,
            matter_id=str(case_id),
            plan_id=str(plan_id),
            item_id=str(item_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            decision=body.decision,
            reason_code=body.reason_code,
            readiness_override=body.readiness_override,
            required_for_delivery_override=body.required_for_delivery_override,
        )
        return {"receipt": _project_dynamic_case_plan_decision(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/dynamic-case-plan/activate",
        tags=["dynamic-case-plan"],
    )
    async def _activate_dynamic_case_plan(
        case_id: UUID,
        body: WebDynamicCasePlanActivationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebDynamicCasePlanPort, Depends(_dynamic_case_plan_service)],
    ) -> dict[str, object]:
        # The browser deliberately supplies neither plan_id nor plan_hash.  The
        # production store selects and locks the latest candidate, derives its
        # confirmation hash, and revalidates profile/Agent promotion/sources
        # and item reviews in the same activation transaction.
        receipt = service.activate_current_plan(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _project_dynamic_case_plan_activation(receipt)}

    @app.get("/api/v1/cases/{case_id}/fact-corrections/{candidate_id}/submission", tags=["agent-ledger-extractions"])
    def _read_fact_correction_submission(
        case_id: UUID, candidate_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_submission_store)],
    ) -> dict[str, object]:
        return {"schema_version":"fact-correction-submission-read-v1","court_ready":False,
                **store.read_submission(actor=identity.actor,matter_id=str(case_id),candidate_id=str(candidate_id))}

    @app.post("/api/v1/cases/{case_id}/fact-corrections/{candidate_id}/proposals/{proposal_id}/submit", tags=["agent-ledger-extractions"])
    def _submit_fact_correction(
        case_id: UUID, candidate_id: UUID, proposal_id: UUID, body: WebFactCorrectionSubmitRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_submission_store)],
    ) -> dict[str, object]:
        receipt = store.submit_for_fact_review(actor=identity.actor,matter_id=str(case_id),
            candidate_id=str(candidate_id),proposal_id=str(proposal_id),
            expected_matter_version=body.expected_matter_version,idempotency_key=idempotency_key)
        return {"schema_version":"fact-correction-submission-response-v1",
                "receipt":asdict(receipt),"court_ready":False}

    @app.get("/api/v1/cases/{case_id}/fact-correction-submission-receipt", tags=["agent-ledger-extractions"])
    def _recover_fact_correction_submission(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_submission_store)],
    ) -> dict[str, object]:
        receipt = store.find_submission_by_key(actor=identity.actor,matter_id=str(case_id),
            idempotency_key=idempotency_key)
        return {"schema_version":"fact-correction-submission-recovery-v1",
                "receipt":asdict(receipt) if receipt else None,"court_ready":False}

    @app.post("/api/v1/cases/{case_id}/fact-corrections/{candidate_id}", tags=["agent-ledger-extractions"])
    def _save_fact_correction(
        case_id: UUID, candidate_id: UUID, body: WebFactCorrectionSaveRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_store)],
    ) -> dict[str, object]:
        receipt = store.save(actor=identity.actor, matter_id=str(case_id), candidate_id=str(candidate_id),
            idempotency_key=idempotency_key, **body.model_dump())
        return {"schema_version": "fact-correction-save-response-v1", "receipt": asdict(receipt), "court_ready": False}

    @app.get("/api/v1/cases/{case_id}/fact-correction-receipt", tags=["agent-ledger-extractions"])
    def _recover_fact_correction_receipt(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_store)],
    ) -> dict[str, object]:
        receipt = store.find_by_key(actor=identity.actor, matter_id=str(case_id), idempotency_key=idempotency_key)
        return {"schema_version": "fact-correction-recovery-response-v1",
                "receipt": asdict(receipt) if receipt else None, "court_ready": False}

    @app.get("/api/v1/cases/{case_id}/fact-corrections/{candidate_id}", tags=["agent-ledger-extractions"])
    def _read_fact_correction(
        case_id: UUID, candidate_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        store: Annotated[PostgresFactCorrectionProposalStore, Depends(_fact_correction_store)],
    ) -> dict[str, object]:
        return {"schema_version": "fact-correction-read-response-v1",
                **store.read_context(actor=identity.actor, matter_id=str(case_id), candidate_id=str(candidate_id))}

    @app.get(
        "/api/v1/cases/{case_id}/agent-ledger-extractions",
        tags=["agent-ledger-extractions"],
    )
    async def _list_agent_ledger_extractions(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebAgentLedgerExtractionReviewPort,
            Depends(_agent_ledger_extraction_review_service),
        ],
    ) -> dict[str, object]:
        batches = service.list_batches(identity=identity, matter_id=str(case_id))
        if not isinstance(batches, tuple) or len(batches) > 100:
            raise WebRequestBlocked("agent ledger extraction batches are invalid")
        return {
            "batches": [
                _project_agent_ledger_extraction_batch(item) for item in batches
            ]
        }

    @app.post(
        "/api/v1/cases/{case_id}/agent-ledger-extractions/{batch_id}/confirm-low-risk",
        tags=["agent-ledger-extractions"],
    )
    async def _confirm_agent_ledger_extraction_low_risk(
        case_id: UUID,
        batch_id: UUID,
        body: WebAgentLedgerExtractionConfirmRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebAgentLedgerExtractionReviewPort,
            Depends(_agent_ledger_extraction_review_service),
        ],
    ) -> dict[str, object]:
        # The browser names one visible immutable batch and its case version.
        # It cannot send a subset, candidate fields, hashes, source pages or
        # model content.  The server re-reads and confirms the complete
        # low-risk lane; exception candidates are never included.
        receipt = service.confirm_low_risk_batch(
            identity=identity,
            matter_id=str(case_id),
            batch_id=str(batch_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return {
            "receipt": _project_agent_ledger_extraction_confirmation(receipt)
        }

    @app.get(
        "/api/v1/cases/{case_id}/agent-ledger-extractions/{batch_id}/exception-groups/{group_id}/members",
        tags=["agent-ledger-extractions"],
    )
    async def _list_agent_ledger_exception_group_members(
        case_id: UUID,
        batch_id: UUID,
        group_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebAgentLedgerExtractionReviewPort,
            Depends(_agent_ledger_extraction_review_service),
        ],
        offset: Annotated[int, Query(ge=0, le=499)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict[str, object]:
        page = service.list_exception_group_members(
            identity=identity,
            matter_id=str(case_id),
            batch_id=str(batch_id),
            group_id=str(group_id),
            offset=offset,
            limit=limit,
        )
        return {"page": _project_agent_ledger_exception_member_page(page)}

    @app.post(
        "/api/v1/cases/{case_id}/agent-ledger-extractions/{batch_id}/exception-groups/{group_id}/decision",
        tags=["agent-ledger-extractions"],
    )
    async def _decide_agent_ledger_exception_group(
        case_id: UUID,
        batch_id: UUID,
        group_id: UUID,
        body: WebAgentLedgerExceptionDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebAgentLedgerExtractionReviewPort,
            Depends(_agent_ledger_extraction_review_service),
        ],
    ) -> dict[str, object]:
        # Group membership, candidate hashes, page ids and the authenticated
        # session are all server-owned.  The browser can only choose one of
        # the bounded routes advertised for this immutable group.
        receipt = service.decide_exception_group(
            identity=identity,
            matter_id=str(case_id),
            batch_id=str(batch_id),
            group_id=str(group_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            decision=body.decision,
            reason=body.reason,
            reason_note=body.reason_note,
        )
        return {"receipt": _project_agent_ledger_exception_decision(receipt)}

    @app.get(
        "/api/v1/cases/{case_id}/agent-ledger-exception-followups",
        tags=["agent-ledger-extractions"],
    )
    async def _list_agent_ledger_exception_followups(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebAgentLedgerExceptionFollowupPort,
            Depends(_agent_ledger_exception_followup_service),
        ],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict[str, object]:
        page = service.list_followups(
            identity=identity,
            matter_id=str(case_id),
            offset=offset,
            limit=limit,
        )
        return {"page": _project_agent_ledger_exception_followup_page(page)}

    @app.post(
        "/api/v1/cases/{case_id}/agent-ledger-exception-followups/recover-control",
        tags=["agent-ledger-extractions"],
    )
    async def _recover_agent_ledger_exception_followups(
        case_id: UUID,
        body: WebAgentLedgerExceptionRecoveryRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebAgentLedgerExceptionFollowupPort,
            Depends(_agent_ledger_exception_followup_service),
        ],
    ) -> dict[str, object]:
        # Recovery is one server-owned operation.  The browser cannot choose
        # either the failed control assignment or the replacement run.
        receipt = service.recover_exception_followups(
            identity=identity,
            matter_id=str(case_id),
            expected_matter_version=body.expected_version,
            idempotency_key=idempotency_key,
            now=datetime.now().astimezone(),
        )
        return {
            "receipt": _project_agent_ledger_exception_recovery_receipt(receipt)
        }

    @app.get(
        "/api/v1/cases/{case_id}/agent-ledger-exception-followups/{followup_id}/eligible-managed-evidence-sources",
        tags=["agent-ledger-extractions"],
    )
    async def _list_agent_ledger_exception_followup_evidence_sources(
        case_id: UUID,
        followup_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebAgentLedgerExceptionFollowupPort,
            Depends(_agent_ledger_exception_followup_service),
        ],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict[str, object]:
        page = service.list_eligible_managed_evidence_sources(
            identity=identity,
            matter_id=str(case_id),
            followup_id=str(followup_id),
            offset=offset,
            limit=limit,
        )
        return {
            "followup_id": str(followup_id),
            "page": _project_agent_ledger_managed_evidence_source_page(page),
        }

    @app.get(
        "/api/v1/cases/{case_id}/agent-ledger-exception-followups/{followup_id}/evidence-pages",
        tags=["agent-ledger-extractions"],
    )
    async def _list_agent_ledger_exception_followup_evidence_pages(
        case_id: UUID,
        followup_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebAgentLedgerExceptionFollowupPort,
            Depends(_agent_ledger_exception_followup_service),
        ],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict[str, object]:
        page = service.list_followup_evidence_page_ids(
            identity=identity,
            matter_id=str(case_id),
            followup_id=str(followup_id),
            offset=offset,
            limit=limit,
        )
        return {
            "followup_id": str(followup_id),
            "page": _project_agent_ledger_followup_evidence_page(page),
        }

    @app.post(
        "/api/v1/cases/{case_id}/agent-ledger-exception-followups/{followup_id}/action",
        tags=["agent-ledger-extractions"],
    )
    async def _resolve_agent_ledger_exception_followup(
        case_id: UUID,
        followup_id: UUID,
        body: WebAgentLedgerExceptionFollowupActionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[
            WebAgentLedgerExceptionFollowupPort,
            Depends(_agent_ledger_exception_followup_service),
        ],
    ) -> dict[str, object]:
        # The browser supplies only a visible follow-up, concurrency state,
        # bounded action/reason and opaque source identities advertised by the
        # GET route.  Session, firm, actor, managed request, hashes, storage
        # keys and all Agent run/graph/task identities remain server-owned.
        receipt = service.resolve_followup(
            identity=identity,
            matter_id=str(case_id),
            followup_id=str(followup_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            action=body.action,
            reason_note=body.reason_note,
            managed_evidence_sources=tuple(
                WebManagedEvidenceSourceSelection(
                    object_type=item.object_type,
                    object_id=str(item.object_id),
                )
                for item in body.managed_evidence_sources
            ),
        )
        return {
            "receipt": _project_agent_ledger_exception_followup_receipt(receipt)
        }

    @app.get("/api/v1/cases/{case_id}/review", tags=["case-review"])
    async def _get_case_review(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        """Return the server-authorized review projection for the lawyer.

        This is read-only. It never infers facts, classifies payments, or
        turns a model candidate into a formal assertion.
        """
        snapshot = ledger.get_case_snapshot(matter_id=str(case_id), actor=identity.actor)
        return {"review": _case_review_projection(snapshot)}

    @app.get("/api/v1/cases/{case_id}/facts/{fact_id}/decision-receipt", tags=["case-review"])
    def _recover_case_fact_decision(
        case_id: UUID, fact_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        if not callable(getattr(ledger,"find_fact_decision_by_key",None)):
            raise WebFeatureUnavailable("事实决定恢复尚未部署")
        receipt=ledger.find_fact_decision_by_key(actor=identity.actor,matter_id=str(case_id),
            fact_id=str(fact_id),idempotency_key=idempotency_key)
        return {"receipt":_evidence_receipt(receipt) if receipt else None,"court_ready":False}

    @app.post("/api/v1/cases/{case_id}/facts/{fact_id}/decision", tags=["case-review"])
    def _decide_case_fact(
        case_id: UUID,
        fact_id: UUID,
        body: WebFactDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        try:
            decision = FactStatus(body.status)
        except ValueError:
            raise WebRequestBlocked("事实决定状态无效") from None
        if decision is FactStatus.CANDIDATE:
            raise WebRequestBlocked("律师确认不能保留为待确认")
        decision_hash = sha256(
            f"case-fact-decision-v1:{case_id}:{fact_id}:{body.expected_version}:{decision.value}".encode("utf-8")
        ).hexdigest()
        receipt = ledger.decide_fact(
            matter_id=str(case_id),
            fact_id=str(fact_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            status=decision,
            decision_hash=decision_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/claims/{claim_id}/confirm-scope", tags=["case-review"])
    async def _confirm_case_claim_scope(
        case_id: UUID,
        claim_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        confirmation_hash = sha256(
            f"case-claim-scope-v1:{case_id}:{claim_id}:{body.expected_version}:CONFIRMED_SCOPE".encode("utf-8")
        ).hexdigest()
        receipt = ledger.confirm_claim_scope(
            matter_id=str(case_id),
            claim_id=str(claim_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            confirmation_hash=confirmation_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/claims/{claim_id}/response", tags=["case-review"])
    async def _set_case_claim_response(
        case_id: UUID,
        claim_id: UUID,
        body: WebClaimResponseRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        confirmed_fact_ids = tuple(sorted(str(value) for value in body.confirmed_fact_ids))
        approval_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-claim-response-v1",
                    "matter_id": str(case_id),
                    "claim_id": str(claim_id),
                    "expected_version": body.expected_version,
                    "position": body.position,
                    "confirmed_fact_ids": confirmed_fact_ids,
                    "partial_amount": str(body.partial_amount) if body.partial_amount is not None else None,
                    "currency": body.currency,
                    "approved_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = ledger.set_claim_response(
            matter_id=str(case_id),
            claim_id=str(claim_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            position=ClaimResponsePosition(body.position),
            confirmed_fact_ids=confirmed_fact_ids,
            partial_amount=body.partial_amount,
            currency=body.currency,
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/claims/candidates",
        status_code=status.HTTP_201_CREATED,
        tags=["case-review"],
    )
    async def _create_case_claim_candidate(
        case_id: UUID,
        body: WebClaimCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        # Evidence identities and hashes are deliberately not accepted from
        # the browser.  The ledger re-resolves every selected confirmed fact
        # and copies immutable source links in the same locked transaction.
        receipt = ledger.create_claim_candidate_from_confirmed_facts(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            original_claim_text=body.original_claim_text,
            claimed_amount=body.claimed_amount,
            currency=body.currency,
            confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/issues/candidates",
        status_code=status.HTTP_201_CREATED,
        tags=["case-review"],
    )
    async def _create_case_dispute_issue_candidate(
        case_id: UUID,
        body: WebDisputeIssueCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        receipt = ledger.create_dispute_issue_candidate(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            question=body.question,
            claim_ids=tuple(str(value) for value in body.claim_ids),
            confirmed_fact_ids=tuple(str(value) for value in body.confirmed_fact_ids),
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/issues/{issue_id}/confirm", tags=["case-review"])
    async def _confirm_case_dispute_issue(
        case_id: UUID,
        issue_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        approval_hash = sha256(
            f"case-dispute-issue-v1:{case_id}:{issue_id}:{body.expected_version}:CONFIRMED".encode("utf-8")
        ).hexdigest()
        receipt = ledger.confirm_dispute_issue(
            matter_id=str(case_id),
            issue_id=str(issue_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/transactions/{transaction_id}/confirm", tags=["case-review"])
    async def _confirm_case_transaction(
        case_id: UUID,
        transaction_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        confirmation_hash = sha256(
            f"case-transaction-v1:{case_id}:{transaction_id}:{body.expected_version}:CONFIRMED".encode("utf-8")
        ).hexdigest()
        receipt = ledger.confirm_transaction(
            matter_id=str(case_id),
            transaction_id=str(transaction_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            confirmation_hash=confirmation_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/transactions/{transaction_id}/payment-classifications",
        status_code=status.HTTP_201_CREATED,
        tags=["case-review"],
    )
    async def _create_payment_classification_candidate(
        case_id: UUID,
        transaction_id: UUID,
        body: WebPaymentClassificationCandidateRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        """Bind a lawyer's classification choice to an already-confirmed source record.

        Amount, currency, and evidence links are resolved under the server-side
        matter lock.  A browser can therefore never turn a typed number or a
        made-up source reference into calculation input.
        """
        snapshot = ledger.get_case_snapshot(matter_id=str(case_id), actor=identity.actor)
        transaction = next(
            (
                item
                for item in getattr(snapshot, "transactions", ())
                if isinstance(item, Mapping) and item.get("transaction_id") == str(transaction_id)
            ),
            None,
        )
        if transaction is None:
            raise WebRequestBlocked("未找到这笔收付款记录")
        if transaction.get("status") != "CONFIRMED":
            raise WebRequestBlocked("请先确认这笔收付款记录，再核对它的款项性质")
        amount = transaction.get("amount")
        currency = transaction.get("currency")
        if amount is None or not isinstance(currency, str) or not currency.strip():
            raise WebRequestBlocked("这笔收付款缺少可核验的金额或币种，暂不能进入金额核对")
        try:
            source_amount = Decimal(str(amount))
        except Exception as error:
            raise WebRequestBlocked("这笔收付款的金额格式无效，暂不能进入金额核对") from error
        receipt = ledger.create_payment_classification_candidate(
            matter_id=str(case_id),
            transaction_id=str(transaction_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            origin=ClassificationOrigin.ASSISTANT_ENTRY,
            nature=PaymentNature(body.nature),
            allocations=(
                ObligationAllocation(
                    obligation_id=body.obligation_label,
                    amount=source_amount,
                    currency=currency.strip().upper(),
                ),
            ),
            same_day_sequence=body.same_day_sequence,
            evidence_links=(),
            use_transaction_evidence=True,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/payment-classifications/{classification_id}/confirm",
        tags=["case-review"],
    )
    async def _approve_payment_classification(
        case_id: UUID,
        classification_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
    ) -> dict[str, object]:
        approval_hash = sha256(
            f"web-payment-classification-v1:{case_id}:{classification_id}:{body.expected_version}:APPROVED".encode("utf-8")
        ).hexdigest()
        receipt = ledger.approve_payment_classification(
            matter_id=str(case_id),
            classification_id=str(classification_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.get("/api/v1/cases/{case_id}/legal-review", tags=["legal-review"])
    async def _get_case_legal_review(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        legal: Annotated[object, Depends(_legal_store)],
    ) -> dict[str, object]:
        snapshot = legal.get_legal_review_snapshot(matter_id=str(case_id), actor=identity.actor)
        return {"review": _legal_review_projection(snapshot)}

    @app.get("/api/v1/cases/{case_id}/official-source-captures", tags=["legal-review"])
    async def _get_official_source_captures(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        capture_store: Annotated[object, Depends(_official_source_capture_store)],
    ) -> dict[str, object]:
        snapshot = capture_store.get_snapshot(
            matter_id=str(case_id), actor=identity.actor
        )
        return {
            "catalogue": _web_official_source_catalogue(),
            "captures": _official_source_capture_projection(snapshot),
        }

    @app.post(
        "/api/v1/cases/{case_id}/official-source-captures",
        status_code=status.HTTP_201_CREATED,
        tags=["legal-review"],
    )
    async def _queue_official_source_capture(
        case_id: UUID,
        body: WebOfficialSourceCaptureRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        capture_store: Annotated[object, Depends(_official_source_capture_store)],
    ) -> dict[str, object]:
        instruction = _web_official_source_instruction(body.source_id)
        query_sha256 = sha256(instruction["query"].encode("utf-8")).hexdigest()
        authorization_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-official-source-capture-v1",
                    "matter_id": str(case_id),
                    "expected_version": body.expected_version,
                    "source_id": body.source_id,
                    "target_url": instruction["target_url"],
                    "query_sha256": query_sha256,
                    "authorized_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = capture_store.queue_capture(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            source_id=body.source_id,
            target_url=instruction["target_url"],
            query_sha256=query_sha256,
            authorization_hash=authorization_hash,
            max_response_bytes=instruction["max_response_bytes"],
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/official-source-captures/{run_id}/review",
        tags=["legal-review"],
    )
    async def _review_official_source_capture(
        case_id: UUID,
        run_id: UUID,
        body: WebOfficialSourceCaptureReviewRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        capture_store: Annotated[object, Depends(_official_source_capture_store)],
    ) -> dict[str, object]:
        review_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-official-source-review-v1",
                    "matter_id": str(case_id),
                    "run_id": str(run_id),
                    "expected_version": body.expected_version,
                    "decision": body.decision,
                    "provision_locator": body.provision_locator,
                    "reviewed_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = capture_store.review_capture(
            matter_id=str(case_id),
            run_id=str(run_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            decision=body.decision,
            provision_locator=body.provision_locator,
            review_hash=review_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/official-source-captures/{run_id}/register",
        status_code=status.HTTP_201_CREATED,
        tags=["legal-review"],
    )
    async def _register_official_source_capture(
        case_id: UUID,
        run_id: UUID,
        body: WebOfficialSourceCaptureRegistrationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        legal: Annotated[object, Depends(_legal_store)],
        capture_store: Annotated[object, Depends(_official_source_capture_store)],
    ) -> dict[str, object]:
        register = getattr(legal, "register_reviewed_capture_snapshot", None)
        if not callable(register):
            raise WebFeatureUnavailable("官方依据登记服务尚未配置")
        source_id = _source_id_for_capture_run(
            snapshot=capture_store.get_snapshot(
                matter_id=str(case_id), actor=identity.actor
            ),
            run_id=str(run_id),
        )
        instruction = _web_official_source_instruction(source_id)
        license_basis = _web_official_source_license_basis(instruction)
        license_review_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-official-source-license-v1",
                    "matter_id": str(case_id),
                    "run_id": str(run_id),
                    "source_id": source_id,
                    "license_basis": license_basis,
                    "reviewed_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        registration_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-official-source-registration-v1",
                    "matter_id": str(case_id),
                    "run_id": str(run_id),
                    "expected_version": body.expected_version,
                    "source_id": source_id,
                    "license_review_hash": license_review_hash,
                    "registered_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = register(
            matter_id=str(case_id),
            run_id=str(run_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            license_basis=license_basis,
            license_review_hash=license_review_hash,
            registration_hash=registration_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/legal-events",
        status_code=status.HTTP_201_CREATED,
        tags=["legal-review"],
    )
    async def _confirm_case_legal_event(
        case_id: UUID,
        body: WebLegalEventConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        legal: Annotated[object, Depends(_legal_store)],
    ) -> dict[str, object]:
        approve_event = getattr(legal, "approve_legal_event", None)
        if not callable(approve_event):
            raise WebFeatureUnavailable("关键日期确认服务尚未配置")
        event_ids = tuple(str(value) for value in body.evidence_page_ids)
        approval_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-legal-event-confirmation-v1",
                    "matter_id": str(case_id),
                    "expected_version": body.expected_version,
                    "event_kind": body.event_kind,
                    "local_date": body.local_date.isoformat(),
                    "evidence_page_ids": event_ids,
                    "confirmed_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = approve_event(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            event_kind=LegalEventKind(body.event_kind),
            local_date=body.local_date,
            evidence_ids=event_ids,
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/legal-bundles/current",
        tags=["legal-review"],
    )
    async def _approve_current_case_legal_bundle(
        case_id: UUID,
        body: WebLegalBundleApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        legal: Annotated[object, Depends(_legal_store)],
    ) -> dict[str, object]:
        approve_bundle = getattr(legal, "approve_case_legal_bundle", None)
        if not callable(approve_bundle):
            raise WebFeatureUnavailable("本案依据确认服务尚未配置")
        snapshot = legal.get_legal_review_snapshot(
            matter_id=str(case_id), actor=identity.actor
        )
        selected_rule = next(
            (
                _legal_row(value)
                for value in tuple(getattr(snapshot, "rule_versions", ()))
                if str(_legal_row(value).get("rule_version_id"))
                == str(body.rule_version_id)
            ),
            None,
        )
        selected_event = next(
            (
                _legal_row(value)
                for value in tuple(getattr(snapshot, "legal_events", ()))
                if str(_legal_row(value).get("legal_event_id"))
                == str(body.trigger_event_id)
            ),
            None,
        )
        if selected_rule is None or selected_event is None:
            raise WebRequestBlocked("请选择本案已核对的依据和关键日期")
        if selected_rule.get("status") != "APPROVED" or selected_event.get("status") != "APPROVED":
            raise WebRequestBlocked("所选依据或关键日期尚未确认")
        event_date_text = _project_optional_date(selected_event.get("local_date"))
        if event_date_text is None:
            raise WebRequestBlocked("所选关键日期尚未确认")
        start_date = date.fromisoformat(event_date_text)
        if body.end_date <= start_date:
            raise WebRequestBlocked("依据适用终点必须晚于已确认的关键日期")
        issue_key = _project_text(selected_rule.get("issue_key"), "规则争点", 255)
        approval_hash = sha256(
            json.dumps(
                {
                    "schema_version": "web-current-legal-bundle-v1",
                    "matter_id": str(case_id),
                    "expected_version": body.expected_version,
                    "rule_version_id": str(body.rule_version_id),
                    "trigger_event_id": str(body.trigger_event_id),
                    "start_date": start_date.isoformat(),
                    "end_date": body.end_date.isoformat(),
                    "approved_by": identity.actor.actor_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = approve_bundle(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            segments=(
                LegalBundleSegmentSelection(
                    segment_id=str(uuid4()),
                    issue_key=issue_key,
                    rule_version_id=str(body.rule_version_id),
                    trigger_event_id=str(body.trigger_event_id),
                    start_date=start_date,
                    end_date=body.end_date,
                    applicability_anchor=(
                        f"律师确认本规则自{start_date.isoformat()}起至"
                        f"{body.end_date.isoformat()}适用于本案。"
                    ),
                ),
            ),
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.get("/api/v1/cases/{case_id}/readiness", tags=["case-review"])
    async def _get_case_readiness(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
        legal: Annotated[object, Depends(_legal_store)],
    ) -> dict[str, object]:
        case_snapshot = ledger.get_case_snapshot(matter_id=str(case_id), actor=identity.actor)
        legal_snapshot = legal.get_legal_review_snapshot(matter_id=str(case_id), actor=identity.actor)
        evidence_summary = None
        if dependencies.evidence_review_service is not None:
            evidence_summary = dependencies.evidence_review_service.summary(identity=identity, matter_id=str(case_id))
        return {"readiness": _case_readiness_projection(case_snapshot, legal_snapshot, evidence_summary)}

    @app.get("/api/v1/cases/{case_id}/calculations/{obligation_id}/current", tags=["calculation"])
    async def _get_current_formal_calculation(
        case_id: UUID,
        obligation_id: str,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        store: Annotated[object, Depends(_formal_calculation_store)],
    ) -> dict[str, object]:
        normalized_obligation_id = _validate_browser_obligation_id(obligation_id)
        snapshot = store.get_current_calculation(
            matter_id=str(case_id),
            obligation_id=normalized_obligation_id,
            actor=identity.actor,
        )
        return {"calculation": _formal_calculation_projection(snapshot)}

    @app.post("/api/v1/cases/{case_id}/formal-calculations", status_code=status.HTTP_201_CREATED, tags=["calculation"])
    async def _create_formal_calculation(
        case_id: UUID,
        body: WebFormalCalculationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        legal: Annotated[object, Depends(_legal_store)],
        ledger: Annotated[object, Depends(_case_ledger_store)],
        store: Annotated[object, Depends(_formal_calculation_store)],
    ) -> dict[str, object]:
        if Role.LEAD_LAWYER not in identity.actor.roles:
            raise AuthorizationDenied("formal calculation requires the lead lawyer role")
        legal_snapshot = legal.get_legal_review_snapshot(matter_id=str(case_id), actor=identity.actor)
        bundle = getattr(legal_snapshot, "current_bundle", None)
        if not isinstance(bundle, Mapping):
            raise WebCalculationBlocked("本案尚未批准可计算的法律规则包")
        bundle_id = bundle.get("bundle_id")
        bundle_hash = bundle.get("bundle_hash")
        if not isinstance(bundle_id, str) or not isinstance(bundle_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_hash):
            raise WebCalculationBlocked("本案法律规则包缺少可核验版本")
        legal_version = getattr(legal_snapshot, "matter_version", None)
        if type(legal_version) is not int or legal_version != body.expected_version:
            raise VersionConflict("legal review version does not match calculation input")
        approved_obligations = _approved_calculation_obligations(
            ledger.get_case_snapshot(matter_id=str(case_id), actor=identity.actor)
        )
        if body.obligation_id.strip() not in approved_obligations:
            raise WebCalculationBlocked("请先在案件要点中确认对应收付款的性质和归属事项")
        policy = AllocationPolicy(body.allocation_policy)
        approval_hash = sha256(
            (
                f"formal-calculation-approval-v1:{case_id}:{body.obligation_id.strip()}:{body.expected_version}:"
                f"{body.start_date.isoformat()}:{body.end_date.isoformat()}:{policy.value}:{bundle_id}:{bundle_hash}"
            ).encode("utf-8")
        ).hexdigest()
        receipt = store.create_formal_calculation(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            obligation_id=body.obligation_id.strip(),
            start_date=body.start_date,
            end_date=body.end_date,
            legal_bundle_id=bundle_id,
            legal_bundle_hash=bundle_hash,
            allocation_policy=policy,
            approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.get("/api/v1/cases/{case_id}/submission-review", tags=["submissions"])
    async def _get_submission_review(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        submission: Annotated[object, Depends(_submission_store)],
    ) -> dict[str, object]:
        snapshot = submission.get_submission_snapshot(matter_id=str(case_id), actor=identity.actor)
        return {
            "review": _submission_review_projection(
                snapshot,
                document_drafts_available=(
                    dependencies.document_draft_service is not None
                    and dependencies.document_draft_delivery_service is not None
                ),
            )
        }

    @app.get("/api/v1/cases/{case_id}/document-drafts", tags=["documents"])
    async def _get_document_drafts(
        case_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[WebDocumentDraftService, Depends(_document_draft_service)],
    ) -> dict[str, object]:
        snapshot = service.snapshot(matter_id=str(case_id), actor=identity.actor)
        pairs = tuple(getattr(snapshot, "pairs", ()))
        return {
            "matter_id": str(case_id),
            "matter_version": int(getattr(snapshot, "matter_version")),
            "snapshot_hash": _project_hash(getattr(snapshot, "snapshot_hash"), "文书候选快照哈希"),
            "pairs": [_document_pair_projection(pair) for pair in pairs],
        }

    @app.post("/api/v1/cases/{case_id}/document-drafts", status_code=status.HTTP_201_CREATED, tags=["documents"])
    async def _create_document_draft(
        case_id: UUID,
        body: WebDocumentDraftRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebDocumentDraftService, Depends(_document_draft_service)],
    ) -> dict[str, object]:
        receipt = service.generate(
            matter_id=str(case_id), actor=identity.actor, expected_version=body.expected_version,
            idempotency_key=idempotency_key, document_kind=body.document_kind,
        )
        return {"receipt": {"pair_id": receipt.pair_id, "matter_version": receipt.matter_version, "document_kind": receipt.document_kind, "review_input_hash": receipt.review_input_hash}}

    @app.get(
        "/api/v1/cases/{case_id}/document-drafts/{pair_id}/delivery",
        response_class=Response,
        tags=["documents"],
    )
    async def _deliver_document_draft(
        case_id: UUID,
        pair_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        service: Annotated[
            WebDocumentDraftDeliveryService, Depends(_document_draft_delivery_service)
        ],
        purpose: Literal["REVIEW_PDF", "DOWNLOAD_EDITABLE"] = Query(
            description="The fixed, server-authorized draft delivery purpose."
        ),
    ) -> Response:
        delivery = await asyncio.to_thread(
            service.download,
            identity=identity,
            matter_id=str(case_id),
            pair_id=str(pair_id),
            purpose=purpose,
        )
        content_disposition = (
            f'{delivery.disposition}; filename="{delivery.ascii_file_name}"; '
            f"filename*=UTF-8''{quote(delivery.file_name)}"
        )
        return Response(
            content=delivery.content,
            media_type=delivery.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": content_disposition,
                "Content-Security-Policy": "sandbox",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": delivery.artifact_sha256,
            },
        )

    @app.post("/api/v1/cases/{case_id}/document-drafts/{pair_id}/approve", tags=["documents"])
    async def _approve_document_draft(
        case_id: UUID,
        pair_id: UUID,
        body: WebDocumentDraftApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebDocumentDraftService, Depends(_document_draft_service)],
    ) -> dict[str, object]:
        if Role.LEAD_LAWYER not in identity.actor.roles and Role.REVIEWER not in identity.actor.roles:
            raise AuthorizationDenied("document draft approval requires a reviewer role")
        snapshot = service.snapshot(matter_id=str(case_id), actor=identity.actor)
        match = next((row for row in tuple(getattr(snapshot, "pairs", ())) if str(row.get("pair_id")) == str(pair_id)), None)
        if not isinstance(match, Mapping) or match.get("status") != "CANDIDATE":
            raise WebDocumentDraftBlocked("文书候选不存在或已经处理")
        approval_hash = match.get("review_input_hash")
        if not isinstance(approval_hash, str):
            raise WebDocumentDraftBlocked("文书候选缺少核验哈希")
        worker = service._reviewable.approve_reviewable_office_draft_pair(
            matter_id=str(case_id), actor=identity.actor, expected_version=body.expected_version,
            idempotency_key=idempotency_key, pair_id=str(pair_id), approval_hash=approval_hash,
        )
        return {"receipt": _evidence_receipt(worker)}

    @app.post(
        "/api/v1/cases/{case_id}/submission-work-products/{work_product_id}/approve",
        tags=["submissions"],
    )
    async def _approve_submission_work_product(
        case_id: UUID,
        work_product_id: UUID,
        body: WebSubmissionWorkProductApprovalRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        submission: Annotated[object, Depends(_submission_store)],
    ) -> dict[str, object]:
        approve = getattr(submission, "approve_work_product", None)
        if not callable(approve):
            raise WebFeatureUnavailable("应诉文书审批服务尚未配置")
        if Role.REVIEWER not in identity.actor.roles and Role.LEAD_LAWYER not in identity.actor.roles:
            raise AuthorizationDenied("submission work-product approval requires a reviewer role")
        # The review binding hash remains server-side.  The browser confirms
        # the exact candidate it saw; it must not copy or invent a lineage
        # hash merely to make an approval request pass.
        receipt = approve(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            work_product_id=str(work_product_id),
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/submission-bundles/lock", tags=["submissions"])
    async def _lock_submission_bundle(
        case_id: UUID,
        body: WebSubmissionBundleLockRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        submission: Annotated[object, Depends(_submission_store)],
    ) -> dict[str, object]:
        lock = getattr(submission, "lock_submission_bundle", None)
        if not callable(lock):
            raise WebFeatureUnavailable("应诉材料包锁定服务尚未配置")
        if Role.LEAD_LAWYER not in identity.actor.roles:
            raise AuthorizationDenied("submission bundle locking requires the lead lawyer role")
        bundle_id = _project_uuid(body.bundle_id, "应诉材料包编号")
        # This is a server-created approval binding.  The browser supplies
        # only the selected opaque bundle identifier and current case version.
        expected_input_hash = _submission_input_hash(submission, identity.actor, str(case_id), bundle_id)
        lock_approval_hash = sha256(
            f"web-submission-lock-v1:{case_id}:{bundle_id}:{body.expected_version}:{expected_input_hash}".encode("utf-8")
        ).hexdigest()
        receipt = lock(
            matter_id=str(case_id),
            actor=identity.actor,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            bundle_id=bundle_id,
            expected_input_hash=expected_input_hash,
            lock_approval_hash=lock_approval_hash,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/evidence-pages/{evidence_page_id}/decisions",
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def _create_evidence_decision_candidate(
        case_id: UUID,
        evidence_page_id: UUID,
        body: WebEvidenceDecisionRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.create_page_decision_candidate(
            identity=identity,
            matter_id=str(case_id),
            evidence_page_id=str(evidence_page_id),
            expected_version=body.expected_version,
            disposition=body.disposition,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/evidence-page-decisions/{decision_id}/confirm",
        tags=["evidence"],
    )
    async def _confirm_evidence_decision(
        case_id: UUID,
        decision_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_page_decision(
            identity=identity,
            matter_id=str(case_id),
            decision_id=str(decision_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/evidence-page-decisions/confirm-batch",
        tags=["evidence"],
    )
    async def _confirm_evidence_decisions_batch(
        case_id: UUID,
        body: WebEvidenceBatchConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_page_decisions_batch(
            identity=identity,
            matter_id=str(case_id),
            decision_ids=tuple(str(item) for item in body.decision_ids),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/evidence-pages/{evidence_page_id}/annotations",
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def _create_evidence_annotation_candidate(
        case_id: UUID,
        evidence_page_id: UUID,
        body: WebEvidenceAnnotationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.create_annotation_candidate(
            identity=identity,
            matter_id=str(case_id),
            evidence_page_id=str(evidence_page_id),
            expected_version=body.expected_version,
            x0=body.x0,
            y0=body.y0,
            x1=body.x1,
            y1=body.y1,
            label=body.label,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post(
        "/api/v1/cases/{case_id}/evidence-annotations/{annotation_id}/confirm",
        tags=["evidence"],
    )
    async def _confirm_evidence_annotation(
        case_id: UUID,
        annotation_id: UUID,
        body: WebEvidenceConfirmationRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.confirm_annotation(
            identity=identity,
            matter_id=str(case_id),
            annotation_id=str(annotation_id),
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/evidence-manifest/lock", tags=["evidence"])
    async def _lock_evidence_manifest(
        case_id: UUID,
        body: WebEvidenceLockRequest,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.lock_manifest(
            identity=identity,
            matter_id=str(case_id),
            expected_version=body.expected_version,
            readiness_hash=body.readiness_hash,
            idempotency_key=idempotency_key,
        )
        return {"receipt": _evidence_receipt(receipt)}

    @app.post("/api/v1/cases/{case_id}/evidence-derivative-runs", status_code=status.HTTP_202_ACCEPTED, tags=["evidence"])
    async def _enqueue_evidence_derivative_run(
        case_id: UUID,
        body: WebEvidenceDerivativeRunRequest,
        background_tasks: BackgroundTasks,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
        idempotency_key: Annotated[str, Depends(_idempotency_key)],
        service: Annotated[WebEvidenceReviewService, Depends(_evidence_review_service)],
    ) -> dict[str, object]:
        receipt = service.enqueue_derivative_run(
            identity=identity,
            matter_id=str(case_id),
            manifest_id=body.manifest_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        if dependencies.derivative_worker is not None:
            # The firm scope comes from the verified server identity.  The
            # browser can supply only the locked manifest id and expected
            # version; it cannot choose a worker tenant.
            background_tasks.add_task(
                dependencies.derivative_worker.run,
                firm_id=identity.actor.firm_id,
                matter_id=str(case_id),
                run_id=receipt.object_id,
                expected_version=receipt.matter_version,
            )
        return {"receipt": _evidence_receipt(receipt)}

    @app.get("/api/v1/cases/{case_id}/evidence-derivatives/{derivative_id}/download", tags=["evidence"])
    async def _download_evidence_derivative(
        case_id: UUID,
        derivative_id: UUID,
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
    ) -> Response:
        service = dependencies.derivative_delivery_service
        if service is None:
            raise WebFeatureUnavailable("证据 PDF 下载服务尚未配置")
        artifact = service.download(
            identity=identity,
            matter_id=str(case_id),
            derivative_id=str(derivative_id),
        )
        safe_name = artifact.file_name.replace('"', "")
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Cache-Control": "no-store, private",
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/auth/login", tags=["authentication"])
    async def _begin_login(request: Request) -> RedirectResponse:
        bootstrap = dependencies.local_managed_acceptance_session_bootstrap
        if bootstrap is not None:
            try:
                _, grant = bootstrap.issue(request=request)
            except (WebSessionBlocked, PersistentAuthenticationBlocked):
                return RedirectResponse(
                    dependencies.settings.own_url(dependencies.settings.login_failure_path),
                    status_code=303,
                )
            response = RedirectResponse(
                dependencies.settings.own_url(dependencies.settings.post_login_path),
                status_code=303,
            )
            response.set_cookie(**grant.session_cookie.as_response_kwargs())
            response.set_cookie(**grant.csrf_cookie.as_response_kwargs())
            return response
        try:
            redirect = dependencies.oidc_login.begin_authorization()
        except OidcLoginBlocked:
            return RedirectResponse(dependencies.settings.own_url(dependencies.settings.login_failure_path), status_code=303)
        location = getattr(redirect, "authorization_url", None)
        if not _valid_external_https_url(location):
            return RedirectResponse(dependencies.settings.own_url(dependencies.settings.login_failure_path), status_code=303)
        return RedirectResponse(location, status_code=303)

    @app.get("/api/v1/auth/oidc/callback", tags=["authentication"])
    async def _complete_login(request: Request) -> RedirectResponse:
        parameters: Sequence[tuple[str, str]] = tuple(request.query_params.multi_items())
        try:
            grant = dependencies.oidc_login.complete_callback(parameters=parameters)
            _validate_session_grant(grant)
        except (OidcLoginBlocked, WebSessionBlocked, PersistentAuthenticationBlocked):
            return RedirectResponse(dependencies.settings.own_url(dependencies.settings.login_failure_path), status_code=303)
        response = RedirectResponse(dependencies.settings.own_url(dependencies.settings.post_login_path), status_code=303)
        response.set_cookie(**grant.session_cookie.as_response_kwargs())
        response.set_cookie(**grant.csrf_cookie.as_response_kwargs())
        return response

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["authentication"])
    async def _logout(
        identity: Annotated[ServerIdentityContext, Depends(_identity)],
    ) -> Response:
        dependencies.session_authority.revoke(session_id=identity.session_id)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        for cookie in dependencies.session_authority.clear_cookies():
            response.set_cookie(**cookie.as_response_kwargs())
        return response

    return app


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _normalize_https_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ValueError("Web API public origin is invalid")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("Web API public origin is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Web API public origin must be a canonical HTTPS origin")
    normalized = f"https://{hostname.lower()}"
    if port is not None and port != 443:
        normalized += f":{port}"
    if value != normalized:
        raise ValueError("Web API public origin must be canonical")
    return normalized


def _normalize_fixed_local_path(value: object, *, allow_query: bool = False) -> str:
    if not isinstance(value, str) or not _SAFE_LOGIN_PATH.fullmatch(value) or value.startswith("//"):
        if not (allow_query and isinstance(value, str)):
            raise ValueError("Web API fixed login path is invalid")
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or (not allow_query and parsed.query)
        or not _SAFE_LOGIN_PATH.fullmatch(parsed.path)
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise ValueError("Web API fixed login path is invalid")
    if allow_query:
        if parsed.query not in {"", "login=failed"}:
            raise ValueError("Web API fixed login path is invalid")
    return value


def _valid_external_https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 4_096:
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _validate_session_grant(grant: object) -> None:
    if not isinstance(grant, WebSessionGrant):
        raise WebSessionBlocked("OIDC Web session grant is invalid")
    try:
        UUID(grant.session_id)
    except (TypeError, ValueError):
        raise WebSessionBlocked("OIDC Web session grant is invalid") from None
    if not isinstance(grant.expires_at, datetime) or grant.expires_at.tzinfo is None:
        raise WebSessionBlocked("OIDC Web session grant is invalid")


def _validate_upload_slot(slot: object) -> None:
    if not isinstance(slot, WebUploadSlotResponse):
        raise WebRequestBlocked("Web material upload slot is invalid")
    try:
        UUID(slot.upload_id)
    except (TypeError, ValueError):
        raise WebRequestBlocked("Web material upload slot is invalid") from None
    if not isinstance(slot.expires_at, datetime) or slot.expires_at.tzinfo is None:
        raise WebRequestBlocked("Web material upload slot is invalid")


def _validate_common_material_slot(slot: object) -> None:
    if (
        not isinstance(slot, CommonMaterialUploadReservationReceipt)
        or not _is_uuid_text(slot.upload_id)
        or not isinstance(slot.expires_at, datetime)
        or slot.expires_at.tzinfo is None
    ):
        raise WebRequestBlocked("Web common material upload slot is invalid")


def _is_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _validate_archive_slot(slot: object) -> None:
    if not isinstance(slot, WebMaterialArchiveSlotResponse) or not _is_uuid_text(slot.archive_id):
        raise WebRequestBlocked("Web material archive slot is invalid")
    if not isinstance(slot.expires_at, datetime) or slot.expires_at.tzinfo is None:
        raise WebRequestBlocked("Web material archive slot is invalid")


def _validate_archive_receipt(receipt: object) -> None:
    if not isinstance(receipt, WebMaterialArchiveReceipt) or not _is_uuid_text(receipt.archive_id):
        raise WebRequestBlocked("Web material archive receipt is invalid")
    if (
        not isinstance(receipt.display_name, str)
        or not receipt.display_name
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.content_sha256)
        or type(receipt.byte_size) is not int
        or receipt.byte_size < 1
        or type(receipt.entry_count) is not int
        or receipt.entry_count < 1
        or type(receipt.expanded_byte_size) is not int
        or receipt.expanded_byte_size < 1
        or receipt.processing_status != "STORED_PENDING_PROCESSING"
    ):
        raise WebRequestBlocked("Web material archive receipt is invalid")


def _validate_status_response(value: object, *, expected_id: str, expected_kind: str) -> None:
    if not isinstance(value, WebMaterialUploadStatusResponse):
        raise WebRequestBlocked("material upload status is invalid")
    if value.operation_id != expected_id or value.kind != expected_kind or value.retry_allowed:
        raise WebRequestBlocked("material upload status is invalid")
    allowed = {"PROCESSING", "COMPLETED", "REJECTED", "EXPIRED", "RECONCILIATION_REQUIRED", "STORED_PENDING_PROCESSING"}
    if value.status not in allowed:
        raise WebRequestBlocked("material upload status is invalid")
    if value.receipt is not None:
        if expected_kind == "PDF":
            _validate_upload_receipt(value.receipt)
        else:
            _validate_archive_receipt(value.receipt)


def _project_status_response(value: WebMaterialUploadStatusResponse) -> dict[str, object]:
    receipt = value.receipt
    if isinstance(receipt, WebUploadReceipt):
        receipt_payload: dict[str, object] | None = {
            "evidence_file_id": receipt.evidence_file_id,
            "display_name": receipt.display_name,
            "sha256": receipt.content_sha256,
            "page_count": receipt.page_count,
            "scan_status": "PASSED",
            "matter_version": receipt.matter_version,
        }
    elif isinstance(receipt, WebMaterialArchiveReceipt):
        receipt_payload = {
            "archive_id": receipt.archive_id,
            "display_name": receipt.display_name,
            "sha256": receipt.content_sha256,
            "byte_size": receipt.byte_size,
            "entry_count": receipt.entry_count,
            "expanded_byte_size": receipt.expanded_byte_size,
            "processing_status": receipt.processing_status,
        }
    else:
        receipt_payload = None
    return {
        "status": {
            "operation_id": value.operation_id,
            "kind": value.kind,
            "state": value.status,
            "retry_allowed": value.retry_allowed,
            "receipt": receipt_payload,
        }
    }


def _project_common_material_receipt(value: object) -> dict[str, object]:
    if not isinstance(value, CommonMaterialAdmissionReceipt):
        raise WebRequestBlocked("common material receipt is invalid")
    admitted_format = str(value.admitted_format)
    route = str(value.route)
    review_status = str(value.review_status)
    agent_status = str(value.agent_status)
    if admitted_format not in {"DOCX", "XLSX", "PPTX", "RTF", "TXT", "CSV", "HTML", "EML", "JPEG", "PNG"}:
        raise WebRequestBlocked("common material receipt is invalid")
    if route not in {"COMMON_DOCUMENT_READER", "VISUAL_OCR"}:
        raise WebRequestBlocked("common material receipt is invalid")
    if review_status != "NEEDS_LAWYER_REVIEW" or agent_status not in {
        "AGENT_READY", "INGESTED_PENDING_ADAPTER"
    }:
        raise WebRequestBlocked("common material receipt is invalid")
    if not _is_uuid_text(value.material_object_id):
        raise WebRequestBlocked("common material receipt is invalid")
    if (
        not isinstance(value.display_name, str)
        or not value.display_name
        or not isinstance(value.media_type, str)
        or not value.media_type
        or type(value.byte_size) is not int
        or value.byte_size < 1
        or not isinstance(value.content_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", value.content_sha256)
        or type(value.matter_version) is not int
        or value.matter_version < 1
    ):
        raise WebRequestBlocked("common material receipt is invalid")
    if any(
        flag is not False
        for flag in (
            value.formal_fact,
            value.formal_transaction,
            value.legal_conclusion,
            value.evidence_decision,
            value.court_ready,
        )
    ):
        raise WebRequestBlocked("common material receipt cannot contain formal conclusions")
    if agent_status == "AGENT_READY":
        if admitted_format in {"DOCX", "XLSX"}:
            expected_ref = f"material-object:{value.material_object_id}"
            if value.agent_source_ref != expected_ref:
                raise WebRequestBlocked("common material Agent binding is invalid")
        elif admitted_format in {"JPEG", "PNG"}:
            if not isinstance(value.agent_source_ref, str) or not value.agent_source_ref.startswith("evidence-page:"):
                raise WebRequestBlocked("common material Agent binding is invalid")
            if not _is_uuid_text(value.agent_source_ref.removeprefix("evidence-page:")):
                raise WebRequestBlocked("common material Agent binding is invalid")
        else:
            raise WebRequestBlocked("common material Agent status is invalid")
    elif value.agent_source_ref is not None:
        raise WebRequestBlocked("common material pending adapter cannot expose an Agent source")
    return {
        "material_object_id": value.material_object_id,
        "display_name": value.display_name,
        "admitted_format": admitted_format,
        "media_type": value.media_type,
        "byte_size": value.byte_size,
        "sha256": value.content_sha256,
        "route": route,
        "review_status": review_status,
        "agent_status": agent_status,
        "agent_source_ref": value.agent_source_ref,
        "matter_version": value.matter_version,
        "formal_fact": False,
        "formal_transaction": False,
        "legal_conclusion": False,
        "evidence_decision": False,
        "court_ready": False,
    }


def _project_common_material_status(
    value: object, *, expected_id: str
) -> dict[str, object]:
    if (
        not isinstance(value, CommonMaterialUploadStatusReceipt)
        or value.upload_id != expected_id
        or value.retry_allowed is not False
        or value.status not in {
            "PROCESSING",
            "COMPLETED",
            "REJECTED",
            "EXPIRED",
            "RECONCILIATION_REQUIRED",
            "ADMISSION_UNAVAILABLE",
        }
    ):
        raise WebRequestBlocked("common material upload status is invalid")
    if value.status == "COMPLETED":
        if value.receipt is None:
            raise WebRequestBlocked("common material upload status is invalid")
        receipt: dict[str, object] | None = _project_common_material_receipt(value.receipt)
    else:
        if value.receipt is not None:
            raise WebRequestBlocked("common material upload status is invalid")
        receipt = None
    return {
        "operation_id": value.upload_id,
        "kind": "COMMON",
        "state": value.status,
        "retry_allowed": False,
        "receipt": receipt,
    }


def _project_case_posture_command(
    value: object, *, expected_matter_id: str
) -> dict[str, object]:
    if (
        not isinstance(value, WebCasePostureCommandReceipt)
        or value.matter_id != expected_matter_id
        or type(value.matter_version) is not int
        or value.matter_version < 1
        or not _is_uuid_text(value.object_id)
        or value.refresh_posture_state is not True
    ):
        raise WebRequestBlocked("case posture command receipt is invalid")
    if value.action not in {
        "CONFIRM_PARTY",
        "CONFIRM_PROCEEDING",
        "CONFIRM_POSITION",
        "CONFIRM_ENGAGEMENT",
        "CONFIRM_CURRENT_PROFILE",
    }:
        raise WebRequestBlocked("case posture command receipt is invalid")
    return {
        "action": value.action,
        "matter_version": value.matter_version,
        "object_type": value.object_type,
        "object_id": value.object_id,
        "refresh_posture_state": True,
    }


def _project_case_posture_complete(
    value: object, *, expected_matter_id: str
) -> dict[str, object]:
    if (
        not isinstance(value, WebCasePostureCompleteReceipt)
        or value.action != "CONFIRM_COMPLETE_POSTURE"
        or value.matter_id != expected_matter_id
        or type(value.matter_version) is not int
        or value.matter_version < 1
        or value.refresh_posture_state is not True
    ):
        raise WebRequestBlocked("complete case posture receipt is invalid")
    ids = {
        "party_id": value.party_id,
        "proceeding_id": value.proceeding_id,
        "position_id": value.position_id,
        "engagement_id": value.engagement_id,
        "profile_id": value.profile_id,
    }
    if any(not _is_uuid_text(identifier) for identifier in ids.values()):
        raise WebRequestBlocked("complete case posture receipt is invalid")
    return {
        "action": value.action,
        "matter_version": value.matter_version,
        **ids,
        "refresh_posture_state": True,
    }


def _project_case_posture_state(
    value: object, *, expected_matter_id: str
) -> dict[str, object]:
    del expected_matter_id  # the service already verifies the profile's matter binding
    if not isinstance(value, WebCasePostureState):
        raise WebRequestBlocked("case posture state is invalid")
    status_value = str(value.status)
    if status_value not in {"NOT_CONFIRMED", "CURRENT", "STALE"}:
        raise WebRequestBlocked("case posture state is invalid")
    if type(value.can_confirm) is not bool:
        raise WebRequestBlocked("case posture state is invalid")
    if value.profile is None:
        if status_value != "NOT_CONFIRMED":
            raise WebRequestBlocked("case posture state is invalid")
        profile: dict[str, object] | None = None
    else:
        profile_value = value.profile
        ids = {
            "profile_id": profile_value.profile_id,
            "represented_party_id": profile_value.represented_party_id,
            "proceeding_id": profile_value.proceeding_id,
            "position_id": profile_value.position_id,
            "engagement_id": profile_value.engagement_id,
        }
        if any(not _is_uuid_text(identifier) for identifier in ids.values()):
            raise WebRequestBlocked("case posture state is invalid")
        if (
            type(profile_value.profile_version) is not int
            or profile_value.profile_version < 1
            or type(profile_value.confirmed_matter_version) is not int
            or profile_value.confirmed_matter_version < 1
            or not isinstance(profile_value.represented_party_display_label, str)
            or not profile_value.represented_party_display_label
            or not isinstance(profile_value.represented_party_kind, str)
            or not profile_value.represented_party_kind
            or not isinstance(profile_value.forum_type, str)
            or not profile_value.forum_type
        ):
            raise WebRequestBlocked("case posture state is invalid")
        profile = {
            **ids,
            "profile_version": profile_value.profile_version,
            "represented_party_display_label": profile_value.represented_party_display_label,
            "represented_party_kind": profile_value.represented_party_kind,
            "case_type_code": profile_value.case_type_code,
            "forum_type": profile_value.forum_type,
            "procedure_stage": profile_value.procedure_stage,
            "represented_position": profile_value.represented_position,
            "authority_scope_code": profile_value.authority_scope_code,
            "engagement_state": profile_value.engagement_state,
            "confirmed_matter_version": profile_value.confirmed_matter_version,
        }
    return {
        "status": status_value,
        "can_confirm": value.can_confirm,
        "profile": profile,
    }


def _project_case_agent_run(
    value: object, *, expected_matter_id: str, expected_run_id: str | None = None
) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentControlRunResponse):
        raise WebRequestBlocked("case Agent run response is invalid")
    run_id = _project_uuid(value.run_id, "case Agent run id")
    matter_id = _project_uuid(value.matter_id, "case Agent matter id")
    if matter_id != expected_matter_id or (expected_run_id is not None and run_id != expected_run_id):
        raise WebRequestBlocked("case Agent run scope is invalid")
    allowed_statuses = {
        "CREATED", "PLANNING", "WAITING_APPROVAL", "EXECUTING", "WAITING_INPUT",
        "RECONCILIATION_REQUIRED", "VERIFYING", "READY_FOR_REVIEW", "COMPLETED",
        "PAUSED", "STALE", "CANCELLED", "FAILED",
    }
    if value.status not in allowed_statuses:
        raise WebRequestBlocked("case Agent run status is invalid")
    required = value.required_document_deliverables
    if (
        not isinstance(required, tuple)
        or any(not isinstance(kind, str) or kind not in {"CASE_REVIEW_MEMO", "DEFENCE_STATEMENT", "EVIDENCE_CATALOGUE", "SUPPLEMENTARY_EVIDENCE_CHECKLIST", "PAYMENT_LEDGER"} for kind in required)
        or len(set(required)) != len(required)
        or (not value.active_plan_execution and required)
    ):
        raise WebRequestBlocked("case Agent document requirements are invalid")
    completed = _project_nonnegative_int(value.progress_completed, "case Agent completed work")
    total = _project_nonnegative_int(value.progress_total, "case Agent total work")
    if completed > total:
        raise WebRequestBlocked("case Agent progress is invalid")
    counts = {
        "open_decision_count": value.open_decision_count,
        "open_approval_count": value.open_approval_count,
        "artifact_count": value.artifact_count,
    }
    for label, count in counts.items():
        counts[label] = _project_nonnegative_int(count, label)
    if type(value.version) is not int or value.version < 1:
        raise WebRequestBlocked("case Agent run version is invalid")
    snapshot_matter_version = _project_positive_int(
        value.snapshot_matter_version, "case Agent snapshot matter version"
    )
    if value.input_snapshot_status not in {
        "CURRENT",
        "PLAN_CANDIDATE_REGISTERED",
        "PLAN_ACTIVE",
        "INPUTS_CHANGED",
    }:
        raise WebRequestBlocked("case Agent input snapshot status is invalid")
    booleans = (
        value.can_pause,
        value.can_resume,
        value.can_cancel,
        value.active_plan_execution,
    )
    if any(type(item) is not bool for item in booleans):
        raise WebRequestBlocked("case Agent action state is invalid")
    current_work = None
    if value.current_work is not None:
        if not isinstance(value.current_work, WebCaseAgentCurrentWorkResponse):
            raise WebRequestBlocked("case Agent current work is invalid")
        current_work = {
            "title": _project_text(value.current_work.title, "case Agent work title", 240),
            "detail": _project_text(value.current_work.detail, "case Agent work detail", 1_000),
            "status": _project_text(value.current_work.status, "case Agent work status", 40),
        }
    failure_message = None if value.failure_message is None else _project_text(
        value.failure_message, "case Agent failure message", 1_000
    )
    failure_code = None if value.failure_code is None else _project_text(
        value.failure_code, "case Agent failure code", 80
    )
    return {
        "run_id": run_id,
        "matter_id": matter_id,
        "objective": _project_text(value.objective, "case Agent objective", 4_000),
        "status": value.status,
        "phase_label": _project_text(value.phase_label, "case Agent phase", 120),
        "progress": {"completed": completed, "total": total},
        "current_work": current_work,
        **counts,
        "status_message": _project_text(value.status_message, "case Agent status message", 1_000),
        "failure_message": failure_message,
        "failure_code": failure_code,
        "version": value.version,
        "snapshot_matter_version": snapshot_matter_version,
        "input_snapshot_status": value.input_snapshot_status,
        "created_at": _project_required_datetime(value.created_at, "case Agent created time"),
        "updated_at": _project_required_datetime(value.updated_at, "case Agent updated time"),
        "actions": {"can_pause": value.can_pause, "can_resume": value.can_resume, "can_cancel": value.can_cancel},
        "active_plan_execution": value.active_plan_execution,
        "required_document_deliverables": list(required),
    }


def _project_case_agent_completion(
    value: object, *, expected_matter_id: str, expected_run_id: str
) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentCompletionResponse):
        raise WebRequestBlocked("case Agent completion response is invalid")
    projected_receipt = _project_case_agent_completion_receipt(
        value.receipt,
        expected_matter_id=expected_matter_id,
        expected_run_id=expected_run_id,
    )
    completed_version = projected_receipt["completed_run_version"]
    artifact_count = projected_receipt["reviewed_artifact_count"]
    projected_run = _project_case_agent_run(
        value.run,
        expected_matter_id=expected_matter_id,
        expected_run_id=expected_run_id,
    )
    if (
        projected_run["status"] != "COMPLETED"
        or projected_run["version"] != completed_version
        or projected_run["artifact_count"] != artifact_count
    ):
        raise WebRequestBlocked("case Agent completion receipt differs from its run")
    return {"receipt": projected_receipt, "run": projected_run}


def _project_case_agent_completion_receipt(
    receipt: object,
    *,
    expected_matter_id: str,
    expected_run_id: str,
    expected_reviewed_version: int | None = None,
) -> dict[str, object]:
    if not isinstance(receipt, WebCaseAgentCompletionReceipt):
        raise WebRequestBlocked("case Agent completion receipt is invalid")
    completion_id = _project_uuid(receipt.completion_id, "case Agent completion id")
    matter_id = _project_uuid(receipt.matter_id, "case Agent completion matter id")
    run_id = _project_uuid(receipt.run_id, "case Agent completion run id")
    if matter_id != expected_matter_id or run_id != expected_run_id:
        raise WebRequestBlocked("case Agent completion scope is invalid")
    reviewed_version = _project_positive_int(
        receipt.reviewed_run_version, "case Agent reviewed run version"
    )
    completed_version = _project_positive_int(
        receipt.completed_run_version, "case Agent completed run version"
    )
    artifact_count = _project_nonnegative_int(
        receipt.reviewed_artifact_count, "case Agent reviewed artifact count"
    )
    if (
        completed_version != reviewed_version + 1
        or (
            expected_reviewed_version is not None
            and reviewed_version != expected_reviewed_version
        )
        or receipt.run_status != "COMPLETED"
        or receipt.verification_status != "PASSED"
    ):
        raise WebRequestBlocked("case Agent completion receipt state is invalid")
    return {
        "completion_id": completion_id,
        "matter_id": matter_id,
        "run_id": run_id,
        "reviewed_run_version": reviewed_version,
        "completed_run_version": completed_version,
        "run_status": receipt.run_status,
        "verification_status": receipt.verification_status,
        "reviewed_artifact_count": artifact_count,
    }


def _project_case_agent_decision(value: object) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentDecisionResponse):
        raise WebRequestBlocked("case Agent decision is invalid")
    if value.status not in {"OPEN", "ANSWERED", "EXPIRED", "CANCELLED"}:
        raise WebRequestBlocked("case Agent decision status is invalid")
    if type(value.allow_note) is not bool or type(value.blocking) is not bool:
        raise WebRequestBlocked("case Agent decision flags are invalid")
    options: list[dict[str, object]] = []
    seen: set[str] = set()
    for option in value.options:
        if not isinstance(option, WebCaseAgentDecisionOptionResponse):
            raise WebRequestBlocked("case Agent decision option is invalid")
        option_id = _normalize_case_agent_code(option.option_id, "decision option")
        if type(option.requires_note) is not bool:
            raise WebRequestBlocked("case Agent decision option note state is invalid")
        if option_id in seen:
            raise WebRequestBlocked("case Agent decision options are invalid")
        seen.add(option_id)
        options.append({
            "option_id": option_id,
            "label": _project_text(option.label, "decision option label", 160),
            "consequence": _project_text(option.consequence, "decision option consequence", 500),
            "requires_note": option.requires_note,
        })
    if not 1 <= len(options) <= 20:
        raise WebRequestBlocked("case Agent decision options are invalid")
    return {
        "decision_id": _project_uuid(value.decision_id, "case Agent decision id"),
        "title": _project_text(value.title, "case Agent decision title", 240),
        "question": _project_text(value.question, "case Agent decision question", 1_000),
        "options": options,
        "allow_note": value.allow_note,
        "blocking": value.blocking,
        "status": value.status,
    }


def _project_case_agent_approval(value: object) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentApprovalResponse) or value.status not in {
        "OPEN", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"
    }:
        raise WebRequestBlocked("case Agent approval is invalid")
    return {
        "approval_id": _project_uuid(value.approval_id, "case Agent approval id"),
        "action_label": _project_text(value.action_label, "case Agent approval action", 240),
        "reason": _project_text(value.reason, "case Agent approval reason", 1_000),
        "impact": _project_text(value.impact, "case Agent approval impact", 1_000),
        "status": value.status,
    }


def _project_case_agent_artifact(value: object) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentArtifactResponse) or value.status not in {
        "CANDIDATE", "READY_FOR_REVIEW", "APPROVED", "SUPERSEDED", "FAILED"
    }:
        raise WebRequestBlocked("case Agent artifact is invalid")
    if type(value.review_required) is not bool:
        raise WebRequestBlocked("case Agent artifact review state is invalid")
    if type(value.recovery_review_only) is not bool:
        raise WebRequestBlocked("case Agent artifact recovery state is invalid")
    return {
        "artifact_id": _project_uuid(value.artifact_id, "case Agent artifact id"),
        "title": _project_text(value.title, "case Agent artifact title", 240),
        "artifact_type": _project_text(value.artifact_type, "case Agent artifact type", 80),
        "status": value.status,
        "review_required": value.review_required,
        "recovery_review_only": value.recovery_review_only,
    }


def _project_case_agent_artifact_review(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentArtifactReview):
        raise WebRequestBlocked("case Agent artifact review is invalid")
    sections: list[dict[str, object]] = []
    if not 1 <= len(value.sections) <= 40:
        raise WebRequestBlocked("case Agent artifact review sections are invalid")
    total_items = 0
    for section in value.sections:
        if section.severity not in {"LOW", "MEDIUM", "HIGH"}:
            raise WebRequestBlocked("case Agent artifact review severity is invalid")
        items: list[dict[str, object]] = []
        for item in section.items:
            total_items += 1
            if total_items > 500:
                raise WebRequestBlocked("case Agent artifact review is oversized")
            sources: list[dict[str, object]] = []
            for source in item.sources:
                source_id = _project_uuid(
                    source.source_id, "case Agent artifact source id"
                )
                evidence_page_id = None
                if source.evidence_page_id is not None:
                    evidence_page_id = _project_uuid(
                        source.evidence_page_id,
                        "case Agent evidence page id",
                    )
                    if source.source_kind == "evidence-page":
                        valid_page_binding = evidence_page_id == source_id
                    else:
                        valid_page_binding = source.source_kind in {
                            "fact",
                            "claim",
                            "issue",
                            "transaction",
                            "review-obligation",
                            "transaction-candidate",
                            "fact-candidate",
                        }
                    if not valid_page_binding:
                        raise WebRequestBlocked(
                            "case Agent evidence source binding is invalid"
                        )
                sources.append(
                    {
                        "source_kind": _project_text(
                            source.source_kind,
                            "case Agent artifact source kind",
                            80,
                        ),
                        "source_id": source_id,
                        "label": _project_text(
                            source.label, "case Agent artifact source label", 120
                        ),
                        "evidence_page_id": evidence_page_id,
                    }
                )
            confidence = item.confidence
            if confidence is not None and (
                type(confidence) not in {float, int}
                or not 0.0 <= float(confidence) <= 1.0
            ):
                raise WebRequestBlocked("case Agent artifact confidence is invalid")
            external_url = item.external_url
            if external_url is not None:
                parsed = urlsplit(external_url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    raise WebRequestBlocked("case Agent artifact public URL is invalid")
            items.append(
                {
                    "item_id": _project_text(
                        item.item_id, "case Agent artifact item id", 200
                    ),
                    "title": _project_text(
                        item.title, "case Agent artifact item title", 500
                    ),
                    "detail": _project_text(
                        item.detail or "未提取到可显示内容。",
                        "case Agent artifact item detail",
                        8_000,
                    ),
                    "badge": (
                        None
                        if item.badge is None
                        else _project_text(
                            item.badge, "case Agent artifact item badge", 240
                        )
                    ),
                    "confidence": (
                        None if confidence is None else float(confidence)
                    ),
                    "sources": sources,
                    "external_url": external_url,
                }
            )
        sections.append(
            {
                "section_id": _project_text(
                    section.section_id, "case Agent artifact section id", 200
                ),
                "title": _project_text(
                    section.title, "case Agent artifact section title", 240
                ),
                "severity": section.severity,
                "items": items,
            }
        )
    return {
        "artifact_id": _project_uuid(
            value.artifact_id, "case Agent artifact review id"
        ),
        "artifact_type": _project_text(
            value.artifact_type, "case Agent artifact review type", 80
        ),
        "title": _project_text(
            value.title, "case Agent artifact review title", 240
        ),
        "review_notice": _project_text(
            value.review_notice, "case Agent artifact review notice", 1_000
        ),
        "sections": sections,
    }


def _project_case_agent_document_review(value: object) -> dict[str, object]:
    if not isinstance(value, WebCaseAgentDocumentReview):
        raise WebRequestBlocked("case Agent document review is invalid")
    if value.output_format not in {"DOCX", "XLSX"}:
        raise WebRequestBlocked("case Agent document format is invalid")
    version_statuses = {
        "CURRENT", "UPDATE_REQUIRED", "GENERATING", "FAILED", "UNKNOWN"
    }
    if (
        value.version_status not in version_statuses
        or type(value.revision_number) is not int
        or not 1 <= value.revision_number <= 1_000
        or type(value.can_request_revision) is not bool
        or type(value.download_ready) is not bool
        or (value.request_status is not None and value.request_status not in {
            "READY", "LEASED", "PASSED", "FAILED", "UNKNOWN"
        })
        or (value.request_id is not None and _project_uuid(
            value.request_id, "document revision request id"
        ) != value.request_id)
        or (value.download_ready != (value.version_status == "CURRENT"))
        or (value.version_status == "CURRENT" and value.can_request_revision)
        or (
            value.can_request_revision
            and value.version_status not in {"UPDATE_REQUIRED", "FAILED", "UNKNOWN"}
        )
    ):
        raise WebRequestBlocked("case Agent document version state is invalid")

    def project_source(source: object) -> dict[str, object]:
        source_ref = _project_text(
            getattr(source, "source_ref", None), "document source ref", 200
        )
        source_kind = _project_text(
            getattr(source, "source_kind", None), "document source kind", 80
        )
        label = _project_text(
            getattr(source, "label", None), "document source label", 240
        )
        return {
            "source_ref": source_ref,
            "source_kind": source_kind,
            "label": label,
        }

    sections: list[dict[str, object]] = []
    columns: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    if not value.download_ready:
        if (
            value.sections
            or value.columns
            or value.rows
            or value.review_pdf_page_count != 0
            or value.total_item_count != 0
            or value.displayed_item_count != 0
            or value.preview_truncated
        ):
            raise WebRequestBlocked("outdated case Agent document exposed content")
    elif (
        type(value.review_pdf_page_count) is not int
        or not 1 <= value.review_pdf_page_count <= 10_000
        or type(value.total_item_count) is not int
        or type(value.displayed_item_count) is not int
        or value.total_item_count < 1
        or not 1 <= value.displayed_item_count <= value.total_item_count
        or type(value.preview_truncated) is not bool
        or value.preview_truncated
        != (value.displayed_item_count < value.total_item_count)
    ):
        raise WebRequestBlocked("case Agent document review counts are invalid")
    elif value.output_format == "DOCX":
        if not value.sections or value.columns or value.rows:
            raise WebRequestBlocked("case Agent DOCX projection is invalid")
        projected_paragraphs = 0
        for section in value.sections:
            paragraphs = []
            for paragraph in section.paragraphs:
                projected_paragraphs += 1
                if projected_paragraphs > 500 or not paragraph.sources:
                    raise WebRequestBlocked("case Agent DOCX preview is oversized")
                paragraphs.append(
                    {
                        "paragraph_id": _project_text(
                            paragraph.paragraph_id, "document paragraph id", 200
                        ),
                        "text": _project_text(
                            paragraph.text, "document paragraph text", 20_000
                        ),
                        "sources": [project_source(item) for item in paragraph.sources],
                    }
                )
            if not paragraphs:
                raise WebRequestBlocked("case Agent DOCX section is empty")
            sections.append(
                {
                    "section_id": _project_text(
                        section.section_id, "document section id", 200
                    ),
                    "heading": _project_text(
                        section.heading, "document section heading", 240
                    ),
                    "paragraphs": paragraphs,
                }
            )
    elif value.download_ready:
        if value.sections or not value.columns or not value.rows:
            raise WebRequestBlocked("case Agent XLSX projection is invalid")
        if len(value.columns) > 200 or len(value.rows) > 250:
            raise WebRequestBlocked("case Agent XLSX preview is oversized")
        columns = [
            {
                "key": _project_text(item.key, "document column key", 80),
                "label": _project_text(item.label, "document column label", 160),
                "value_type": _project_text(
                    item.value_type, "document column type", 20
                ),
            }
            for item in value.columns
        ]
        for row in value.rows:
            if len(row.cells) != len(columns) or not row.sources:
                raise WebRequestBlocked("case Agent XLSX row is invalid")
            cells: list[str | int | float | bool | None] = []
            for cell in row.cells:
                if cell is not None and not isinstance(cell, (str, int, float, bool)):
                    raise WebRequestBlocked("case Agent XLSX cell is invalid")
                if isinstance(cell, str) and len(cell) > 20_000:
                    raise WebRequestBlocked("case Agent XLSX cell is oversized")
                cells.append(cell)
            rows.append(
                {
                    "row_id": _project_text(row.row_id, "document row id", 200),
                    "cells": cells,
                    "sources": [project_source(item) for item in row.sources],
                }
            )
    return {
        "artifact_id": _project_uuid(value.artifact_id, "document artifact id"),
        "title": _project_text(value.title, "document title", 240),
        "deliverable_kind": _project_text(
            value.deliverable_kind, "document deliverable kind", 120
        ),
        "deliverable_label": _project_text(
            value.deliverable_label, "document deliverable label", 240
        ),
        "output_format": value.output_format,
        "review_notice": _project_text(
            value.review_notice, "document review notice", 1_000
        ),
        "version_status": value.version_status,
        "review_version": _project_optional_hash(value.review_version),
        "review_artifact_id": _project_uuid(value.review_artifact_id, "document review artifact") if value.review_artifact_id is not None else None,
        "revision_number": value.revision_number,
        "template_version": _project_text(
            value.template_version, "document template version", 64
        ),
        "installed_template_version": _project_text(
            value.installed_template_version,
            "installed document template version",
            64,
        ),
        "can_request_revision": value.can_request_revision,
        "request_status": value.request_status,
        "request_id": value.request_id,
        "download_ready": value.download_ready,
        "review_pdf_page_count": value.review_pdf_page_count,
        "total_item_count": value.total_item_count,
        "displayed_item_count": value.displayed_item_count,
        "preview_truncated": value.preview_truncated,
        "sections": sections,
        "columns": columns,
        "rows": rows,
    }


def _project_agent_run(value: object) -> dict[str, object]:
    """Validate and return only the lawyer-facing Agent lifecycle fields."""

    if not isinstance(value, WebAgentRunResponse):
        raise WebRequestBlocked("Agent run response is invalid")
    run_id = _project_uuid(value.run_id, "Agent run id")
    matter_id = _project_uuid(value.matter_id, "Agent matter id")
    if value.scope != "ALL_CURRENT_EVIDENCE":
        raise WebRequestBlocked("Agent run scope is invalid")
    allowed_statuses = {"QUEUED", "RUNNING", "NEEDS_REVIEW", "FAILED"}
    if value.status not in allowed_statuses:
        raise WebRequestBlocked("Agent run status is invalid")
    counts = (
        value.total_pages,
        value.processed_pages,
        value.remaining_pages,
        value.batch_count,
        value.completed_batch_count,
        value.candidate_count,
    )
    if any(type(item) is not int or item < 0 or item > 1_000_000 for item in counts):
        raise WebRequestBlocked("Agent run counts are invalid")
    if (
        value.total_pages < 1
        or value.processed_pages + value.remaining_pages != value.total_pages
        or value.batch_count < 1
        or value.completed_batch_count > value.batch_count
    ):
        raise WebRequestBlocked("Agent run progress is invalid")
    if type(value.matter_version) is not int or value.matter_version < 1:
        raise WebRequestBlocked("Agent matter version is invalid")
    if type(value.retry_allowed) is not bool:
        raise WebRequestBlocked("Agent retry state is invalid")
    failure_state = value.failure_state
    if failure_state is not None and failure_state not in {
        "PROVIDER_REJECTED",
        "PROVIDER_RESULT_UNKNOWN",
        "MODEL_RESPONSE_INVALID",
        "INTERNAL_FAILURE",
    }:
        raise WebRequestBlocked("Agent failure state is invalid")
    if failure_state == "PROVIDER_RESULT_UNKNOWN" and value.retry_allowed:
        raise WebRequestBlocked("unknown Agent provider outcome cannot be retryable")
    tasks: list[dict[str, str]] = []
    for task in value.tasks:
        if not isinstance(task, WebAgentTaskResponse):
            raise WebRequestBlocked("Agent task response is invalid")
        task_kind = _project_text(task.task_kind, "Agent task kind", 80)
        task_status = _project_text(task.status, "Agent task status", 40)
        tasks.append({"task_kind": task_kind, "status": task_status})
    if not tasks or len(tasks) > 20:
        raise WebRequestBlocked("Agent task plan is invalid")
    return {
        "run_id": run_id,
        "matter_id": matter_id,
        "matter_version": value.matter_version,
        "scope": value.scope,
        "status": value.status,
        "progress": {
            "total_pages": value.total_pages,
            "processed_pages": value.processed_pages,
            "remaining_pages": value.remaining_pages,
            "batch_count": value.batch_count,
            "completed_batch_count": value.completed_batch_count,
        },
        "candidate_count": value.candidate_count,
        "tasks": tasks,
        "retry_allowed": value.retry_allowed,
        "failure_state": failure_state,
        "created_at": _project_required_datetime(value.created_at, "Agent created time"),
        "updated_at": _project_required_datetime(value.updated_at, "Agent updated time"),
        "external_service_notice": _project_text(
            value.external_service_notice,
            "Agent external service notice",
            500,
        ),
        "representation_profile": _project_representation_profile(value.representation_profile),
    }


def _project_representation_profile(value: object) -> dict[str, object]:
    if not isinstance(value, WebRepresentationProfileResponse):
        raise WebRequestBlocked("representation profile is invalid")
    if value.status not in {"UNCONFIRMED", "CONFIRMED"}:
        raise WebRequestBlocked("representation profile status is invalid")
    role = value.active_proceeding_role
    stage = value.proceeding_stage
    case_type = value.case_type
    version = value.version
    if value.status == "UNCONFIRMED":
        if any(item is not None for item in (role, stage, case_type, version)):
            raise WebRequestBlocked("unconfirmed representation profile cannot project values")
    else:
        allowed_roles = {"PLAINTIFF", "DEFENDANT", "APPELLANT", "APPELLEE", "THIRD_PARTY", "OTHER"}
        if role not in allowed_roles:
            raise WebRequestBlocked("active proceeding role is invalid")
        stage = _project_text(stage, "proceeding stage", 80)
        case_type = _project_text(case_type, "case type", 160)
        if type(version) is not int or version < 1:
            raise WebRequestBlocked("representation profile version is invalid")
    return {
        "status": value.status,
        "active_proceeding_role": role,
        "proceeding_stage": stage,
        "case_type": case_type,
        "version": version,
    }


def _project_agent_candidate_batch(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentCandidateBatchResponse):
        raise WebRequestBlocked("Agent candidate batch is invalid")
    run_id = _project_uuid(value.run_id, "Agent run id")
    if type(value.total_count) is not int or not 0 <= value.total_count <= 1_000_000:
        raise WebRequestBlocked("Agent candidate total is invalid")
    if type(value.has_more) is not bool:
        raise WebRequestBlocked("Agent candidate cursor state is invalid")
    next_cursor = value.next_cursor
    if next_cursor is not None and (
        not isinstance(next_cursor, str)
        or not 1 <= len(next_cursor) <= 1024
        or any(ord(character) < 32 for character in next_cursor)
    ):
        raise WebRequestBlocked("Agent candidate cursor is invalid")
    items = [_project_agent_candidate(item) for item in value.items]
    if len(items) > 100 or value.total_count < len(items):
        raise WebRequestBlocked("Agent candidate batch size is invalid")
    if value.has_more != (next_cursor is not None):
        raise WebRequestBlocked("Agent candidate cursor state is invalid")
    return {
        "run_id": run_id,
        "total_count": value.total_count,
        "items": items,
        "next_cursor": next_cursor,
        "has_more": value.has_more,
    }


def _project_agent_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentCandidateResponse):
        raise WebRequestBlocked("Agent candidate is invalid")
    allowed_kinds = {"RELEVANT_PAGE", "UNRELATED_PAGE", "OCR_REQUIRED", "DUPLICATE_CANDIDATE", "UNCERTAIN"}
    allowed_priorities = {"LOW", "MEDIUM", "HIGH"}
    if value.kind not in allowed_kinds or value.review_priority not in allowed_priorities:
        raise WebRequestBlocked("Agent candidate classification is invalid")
    if not isinstance(value.confidence, (int, float)) or isinstance(value.confidence, bool) or not 0 <= float(value.confidence) <= 1:
        raise WebRequestBlocked("Agent candidate confidence is invalid")
    reasons = [_project_text(reason, "Agent candidate reason", 80) for reason in value.reason_codes]
    if len(reasons) > 20 or len(set(reasons)) != len(reasons):
        raise WebRequestBlocked("Agent candidate reasons are invalid")
    duplicate_of = None if value.duplicate_of_page_id is None else _project_uuid(value.duplicate_of_page_id, "Agent duplicate page id")
    return {
        "candidate_id": _project_uuid(value.candidate_id, "Agent candidate id"),
        "evidence_page_id": _project_uuid(value.evidence_page_id, "Agent evidence page id"),
        "source_label": _project_text(value.source_label, "Agent source label", 500),
        "page_number": _project_positive_int(value.page_number, "Agent page number"),
        "kind": value.kind,
        "confidence": round(float(value.confidence), 4),
        "review_priority": value.review_priority,
        "reason_codes": reasons,
        "supporting_excerpt": _project_text(value.supporting_excerpt, "Agent supporting excerpt", 2_000),
        "duplicate_of_page_id": duplicate_of,
        "status": "NEEDS_REVIEW",
    }


def _project_dynamic_case_plan(value: object) -> dict[str, object]:
    if not isinstance(value, WebDynamicCasePlanResponse):
        raise WebRequestBlocked("dynamic case plan is invalid")
    allowed_statuses = {"CANDIDATE", "ACTIVE", "STALE", "SUPERSEDED"}
    if value.status not in allowed_statuses:
        raise WebRequestBlocked("dynamic case plan status is invalid")
    generated_version = _project_positive_int(value.generated_matter_version, "dynamic case plan generated version")
    current_version = _project_positive_int(value.current_matter_version, "dynamic case plan current version")
    if generated_version > current_version or type(value.inputs_current) is not bool:
        raise WebRequestBlocked("dynamic case plan input state is invalid")
    stale_reasons = [_project_text(reason, "dynamic case plan stale reason", 240) for reason in value.stale_reasons]
    if len(stale_reasons) > 20 or len(set(stale_reasons)) != len(stale_reasons):
        raise WebRequestBlocked("dynamic case plan stale reasons are invalid")
    if value.status == "STALE" and (value.inputs_current or not stale_reasons):
        raise WebRequestBlocked("stale dynamic case plan must explain changed inputs")
    if value.status != "STALE" and (not value.inputs_current or stale_reasons):
        raise WebRequestBlocked("current dynamic case plan cannot carry stale inputs")
    items = [_project_dynamic_case_plan_item(item) for item in value.items]
    if len(items) > 200:
        raise WebRequestBlocked("dynamic case plan has too many items")
    sequences = [item["sequence"] for item in items]
    item_ids = [item["item_id"] for item in items]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences) or len(set(item_ids)) != len(item_ids):
        raise WebRequestBlocked("dynamic case plan items are inconsistent")
    if type(value.can_activate) is not bool:
        raise WebRequestBlocked("dynamic case plan activation state is invalid")
    activation_blockers = [
        _project_text(reason, "dynamic case plan activation blocker", 240)
        for reason in value.activation_blockers
    ]
    if len(activation_blockers) > 20 or len(set(activation_blockers)) != len(activation_blockers):
        raise WebRequestBlocked("dynamic case plan activation blockers are invalid")
    reviewed_item_count = _project_non_negative_int(
        value.reviewed_item_count, "dynamic case plan reviewed item count"
    )
    if reviewed_item_count > len(items):
        raise WebRequestBlocked("dynamic case plan reviewed item count is invalid")
    if value.can_activate and (
        value.status != "CANDIDATE" or not value.inputs_current or activation_blockers
    ):
        raise WebRequestBlocked("dynamic case plan cannot be activated in its current state")
    if not value.can_activate and value.status == "CANDIDATE" and value.inputs_current and not activation_blockers:
        raise WebRequestBlocked("blocked dynamic case plan must explain why activation is unavailable")
    return {
        "plan_id": _project_uuid(value.plan_id, "dynamic case plan id"),
        "matter_id": _project_uuid(value.matter_id, "dynamic case plan matter id"),
        "generated_matter_version": generated_version,
        "current_matter_version": current_version,
        "status": value.status,
        "inputs_current": value.inputs_current,
        "stale_reasons": stale_reasons,
        "generated_at": _project_required_datetime(value.generated_at, "dynamic case plan generated time"),
        "can_activate": value.can_activate,
        "activation_blockers": activation_blockers,
        "reviewed_item_count": reviewed_item_count,
        "items": items,
    }


def _project_dynamic_case_plan_item(value: object) -> dict[str, object]:
    if not isinstance(value, WebDynamicCasePlanItemResponse):
        raise WebRequestBlocked("dynamic case plan item is invalid")
    allowed_categories = {
        "MATERIAL_REQUEST",
        "RESEARCH_TASK",
        "PROCEDURAL_TASK",
        "CALCULATION",
        "DOCUMENT_CANDIDATE",
        "REVIEW",
        "DEADLINE_RISK",
    }
    allowed_statuses = {"CANDIDATE", "APPROVED", "CHANGE_REQUESTED", "REJECTED", "SUPERSEDED"}
    allowed_readiness = {"ACTIONABLE", "NEEDS_RESEARCH", "NEEDS_INFORMATION"}
    allowed_gates = {
        "LEAD_LAWYER_CONFIRMATION",
        "EVIDENCE_REVIEW",
        "LEGAL_AUTHORITY_REVIEW",
        "PROCEDURE_REVIEW",
        "CALCULATION_REVIEW",
    }
    if value.category not in allowed_categories or value.status not in allowed_statuses:
        raise WebRequestBlocked("dynamic case plan item classification is invalid")
    if value.readiness not in allowed_readiness or value.review_gate not in allowed_gates:
        raise WebRequestBlocked("dynamic case plan review state is invalid")
    if not isinstance(value.confidence, (int, float)) or isinstance(value.confidence, bool) or not 0 <= float(value.confidence) <= 1:
        raise WebRequestBlocked("dynamic case plan confidence is invalid")
    prerequisites = [_project_text(item, "dynamic case plan prerequisite", 128) for item in value.prerequisites]
    if len(prerequisites) > 100 or len(set(prerequisites)) != len(prerequisites):
        raise WebRequestBlocked("dynamic case plan prerequisites are invalid")
    sources = [_project_dynamic_case_plan_reference(item) for item in value.source_refs]
    source_keys = [(item["source_kind"], item["source_id"]) for item in sources]
    if len(sources) > 100 or len(set(source_keys)) != len(source_keys):
        raise WebRequestBlocked("dynamic case plan sources are invalid")
    if type(value.required_for_delivery) is not bool:
        raise WebRequestBlocked("dynamic case plan delivery flag is invalid")
    delivery_target = None if value.delivery_target is None else _project_text(value.delivery_target, "delivery target", 80)
    if delivery_target not in {None, "NOT_APPLICABLE", "INTERNAL_WORK_PRODUCT", "CLIENT_DELIVERABLE", "COURT_SUBMISSION"}:
        raise WebRequestBlocked("dynamic case plan delivery target is invalid")
    deliverable_kind = None if value.deliverable_kind is None else _project_text(value.deliverable_kind, "deliverable kind", 120)
    if value.required_for_delivery and (
        value.category != "DOCUMENT_CANDIDATE"
        or value.readiness != "ACTIONABLE"
        or delivery_target != "COURT_SUBMISSION"
        or deliverable_kind is None
    ):
        raise WebRequestBlocked("required deliverable candidate is unsupported")
    source_counts = {kind: 0 for kind in ("fact", "evidence", "procedure", "official_authority")}
    for source in sources:
        bucket = _dynamic_plan_source_bucket(str(source["source_kind"]))
        if bucket is not None:
            source_counts[bucket] += 1
    return {
        "item_id": _project_uuid(value.item_id, "dynamic case plan item id"),
        "sequence": _project_positive_int(value.sequence, "dynamic case plan sequence"),
        "category": value.category,
        "status": value.status,
        "readiness": value.readiness,
        "title": _project_text(value.title, "dynamic case plan item title", 240),
        "purpose": _project_text(value.purpose, "dynamic case plan item purpose", 2_000),
        "rationale": _project_text(value.rationale, "dynamic case plan item rationale", 4_000),
        "risk_if_omitted": _project_text(value.risk_if_omitted, "dynamic case plan item risk", 2_000),
        "prerequisite_count": len(prerequisites),
        "confidence": round(float(value.confidence), 4),
        "review_gate": value.review_gate,
        "sources": sources,
        "source_counts": source_counts,
        "delivery_target": delivery_target,
        "deliverable_kind": deliverable_kind,
        "required_for_delivery": value.required_for_delivery,
    }


def _project_dynamic_case_plan_reference(value: object) -> dict[str, object]:
    if not isinstance(value, WebDynamicCasePlanReferenceResponse):
        raise WebRequestBlocked("dynamic case plan source is invalid")
    allowed_source_kinds = {
        "MATERIAL_OBJECT", "POSTURE_PROFILE", "LAWYER_OBJECTIVE", "EVIDENCE_PAGE", "EVIDENCE_MANIFEST", "CASE_FACT", "CLAIM", "CASE_CLAIM",
        "DISPUTE_ISSUE", "TRANSACTION", "CASE_TRANSACTION", "LEGAL_EVENT", "LEGAL_RULE_VERSION", "APPROVED_LEGAL_RULE", "LEGAL_SOURCE_SNAPSHOT", "LEGAL_BUNDLE",
        "VERIFIED_LEGAL_SOURCE", "CALCULATION_RUN", "COURT_PROCEEDING", "SERVICE_EVENT", "PROCEDURAL_EVENT",
        "PROCEDURAL_DEADLINE", "WORK_PLAN_ITEM", "AGENT_TASK_INPUT", "REVIEW_OBLIGATION", "TRANSACTION_CANDIDATE", "FACT_CANDIDATE",
    }
    if value.source_kind not in allowed_source_kinds:
        raise WebRequestBlocked("dynamic case plan source kind is invalid")
    return {
        "source_kind": value.source_kind,
        "source_id": _project_text(value.source_id, "dynamic case plan source id", 128),
        "label": _project_text(value.label, "dynamic case plan source label", 500),
        "locator": None if value.locator is None else _project_text(value.locator, "dynamic case plan source locator", 500),
    }


def _dynamic_plan_source_bucket(source_kind: str) -> str | None:
    if source_kind in {"CASE_FACT", "CLAIM", "CASE_CLAIM", "DISPUTE_ISSUE", "TRANSACTION", "CASE_TRANSACTION", "LAWYER_OBJECTIVE"}:
        return "fact"
    if source_kind in {"MATERIAL_OBJECT", "EVIDENCE_PAGE", "EVIDENCE_MANIFEST"}:
        return "evidence"
    if source_kind in {"POSTURE_PROFILE", "LEGAL_EVENT", "COURT_PROCEEDING", "SERVICE_EVENT", "PROCEDURAL_EVENT", "PROCEDURAL_DEADLINE"}:
        return "procedure"
    if source_kind in {"VERIFIED_LEGAL_SOURCE", "LEGAL_RULE_VERSION", "APPROVED_LEGAL_RULE", "LEGAL_SOURCE_SNAPSHOT", "LEGAL_BUNDLE"}:
        return "official_authority"
    return None


def _project_dynamic_case_plan_decision(value: object) -> dict[str, object]:
    if not isinstance(value, WebDynamicCasePlanDecisionReceipt):
        raise WebRequestBlocked("dynamic case plan decision receipt is invalid")
    if value.decision_status not in {"APPROVED", "CHANGE_REQUESTED", "REJECTED"}:
        raise WebRequestBlocked("dynamic case plan decision status is invalid")
    if type(value.requires_replanning) is not bool or value.requires_replanning != (
        value.decision_status in {"CHANGE_REQUESTED", "REJECTED"}
    ):
        raise WebRequestBlocked("dynamic case plan replanning state is invalid")
    return {
        "plan_id": _project_uuid(value.plan_id, "dynamic case plan id"),
        "item_id": _project_uuid(value.item_id, "dynamic case plan item id"),
        "decision_status": value.decision_status,
        "matter_version": _project_positive_int(value.matter_version, "dynamic case plan decision version"),
        "requires_replanning": value.requires_replanning,
    }


def _project_dynamic_case_plan_activation(value: object) -> dict[str, object]:
    if not isinstance(value, WebDynamicCasePlanActivationReceipt):
        raise WebRequestBlocked("dynamic case plan activation receipt is invalid")
    if value.status != "ACTIVE":
        raise WebRequestBlocked("dynamic case plan activation status is invalid")
    return {
        "plan_id": _project_uuid(value.plan_id, "dynamic case plan id"),
        "status": value.status,
        "matter_version": _project_positive_int(
            value.matter_version, "dynamic case plan activation version"
        ),
    }


def _project_agent_ledger_exception_followup_page(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionFollowupPage):
        raise WebRequestBlocked("agent ledger exception follow-up page is invalid")
    total_count = _project_non_negative_int(
        value.total_count, "agent ledger exception follow-up count"
    )
    offset = _project_non_negative_int(
        value.offset, "agent ledger exception follow-up offset"
    )
    if offset > total_count:
        raise WebRequestBlocked("agent ledger exception follow-up range is invalid")
    followups = [
        _project_agent_ledger_exception_followup(item) for item in value.followups
    ]
    if len(followups) > 50 or (
        total_count > 0 and (offset >= total_count or not followups)
    ) or (total_count == 0 and (offset != 0 or followups)):
        raise WebRequestBlocked("agent ledger exception follow-up page is incomplete")
    expected_next = offset + len(followups)
    next_offset = value.next_offset
    if next_offset is not None:
        next_offset = _project_non_negative_int(
            next_offset, "agent ledger exception follow-up next offset"
        )
    if next_offset != (expected_next if expected_next < total_count else None):
        raise WebRequestBlocked("agent ledger exception follow-up cursor is invalid")
    if len({item["followup_id"] for item in followups}) != len(followups):
        raise WebRequestBlocked("agent ledger exception follow-up page repeats items")
    control_health = value.control_health
    if control_health not in {None, "HEALTHY", "RECOVERY_REQUIRED"}:
        raise WebRequestBlocked("agent ledger exception control health is invalid")
    if type(value.can_recover) is not bool or value.can_recover and (
        control_health != "RECOVERY_REQUIRED" or total_count == 0
    ):
        raise WebRequestBlocked("agent ledger exception recovery policy is invalid")
    if (total_count == 0) != (control_health is None):
        raise WebRequestBlocked("agent ledger exception control state is incomplete")
    return {
        "total_count": total_count,
        "offset": offset,
        "next_offset": next_offset,
        "control_health": control_health,
        "can_recover": value.can_recover,
        "followups": followups,
    }


def _project_agent_ledger_exception_followup(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionFollowup):
        raise WebRequestBlocked("agent ledger exception follow-up is invalid")
    if value.kind not in {"REEXTRACTION", "MORE_EVIDENCE", "DEFERRED_REVIEW"}:
        raise WebRequestBlocked("agent ledger exception follow-up kind is invalid")
    if value.state != "ACTIVE":
        raise WebRequestBlocked("only active agent ledger exception follow-ups are visible")
    automation_status = value.automation_status
    allowed_automation_statuses = {
        "WAITING_FOR_PLAN",
        "WAITING_FOR_REPLAN",
        "QUEUED",
        "RUNNING",
        "VERIFYING",
        "BLOCKED",
        "RECOVERY_REQUIRED",
    }
    if (
        value.kind == "REEXTRACTION"
        and automation_status not in allowed_automation_statuses
    ) or (value.kind != "REEXTRACTION" and automation_status is not None):
        raise WebRequestBlocked("agent ledger exception automation state is invalid")
    review_reasons = [
        _project_text(item, "agent ledger exception follow-up reason", 240)
        for item in value.review_reasons
    ]
    evidence_page_count = _project_positive_int(
        value.evidence_page_count, "agent ledger exception evidence page count"
    )
    requirements = [
        _project_text(item, "managed evidence acceptance requirement", 300)
        for item in value.acceptance_requirements
    ]
    actions = [
        _project_agent_ledger_followup_action(item) for item in value.allowed_actions
    ]
    if (
        not review_reasons
        or len(review_reasons) > 15
        or len(set(review_reasons)) != len(review_reasons)
        or not actions
        or len(actions) > 3
        or len({item["code"] for item in actions}) != len(actions)
        or (value.kind == "MORE_EVIDENCE") != bool(requirements)
        or type(value.can_act) is not bool
    ):
        raise WebRequestBlocked("agent ledger exception follow-up policy is invalid")
    expected_action_codes = {
        "REEXTRACTION": {"WITHDRAW", "SUPERSEDE"},
        "MORE_EVIDENCE": {
            "CONFIRM_MORE_EVIDENCE",
            "WITHDRAW",
            "SUPERSEDE",
        },
        "DEFERRED_REVIEW": {"RESUME", "WITHDRAW", "SUPERSEDE"},
    }[value.kind]
    if {item["code"] for item in actions} != expected_action_codes:
        raise WebRequestBlocked("agent ledger exception follow-up actions are invalid")
    return {
        "followup_id": _project_uuid(
            value.followup_id, "agent ledger exception follow-up id"
        ),
        "kind": value.kind,
        "state": value.state,
        "head_sequence": _project_positive_int(
            value.head_sequence, "agent ledger exception follow-up sequence"
        ),
        "origin_batch_id": _project_uuid(
            value.origin_batch_id, "agent ledger exception origin batch id"
        ),
        "origin_group_id": _project_uuid(
            value.origin_group_id, "agent ledger exception origin group id"
        ),
        "current_matter_version": _project_positive_int(
            value.current_matter_version,
            "agent ledger exception current matter version",
        ),
        "created_matter_version": _project_positive_int(
            value.created_matter_version,
            "agent ledger exception created matter version",
        ),
        "created_at": _project_required_datetime(
            value.created_at, "agent ledger exception created time"
        ),
        "reason": _project_text(
            value.reason, "agent ledger exception route reason", 240
        ),
        "reason_note": (
            None
            if value.reason_note is None
            else _project_text(
                value.reason_note, "agent ledger exception route note", 500
            )
        ),
        "candidate_count": _project_positive_int(
            value.candidate_count, "agent ledger exception candidate count"
        ),
        "review_reasons": review_reasons,
        "evidence_page_count": evidence_page_count,
        "acceptance_requirements": requirements,
        "automation_status": automation_status,
        "can_act": value.can_act,
        "allowed_actions": actions,
    }


def _project_agent_ledger_followup_action(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerFollowupAction):
        raise WebRequestBlocked("agent ledger exception follow-up action is invalid")
    if type(value.requires_reason) is not bool or value.requires_reason is not True:
        raise WebRequestBlocked("agent ledger exception follow-up reason policy is invalid")
    return {
        "code": _project_text(
            value.code, "agent ledger exception follow-up action code", 40
        ),
        "label": _project_text(
            value.label, "agent ledger exception follow-up action label", 120
        ),
        "consequence": _project_text(
            value.consequence,
            "agent ledger exception follow-up action consequence",
            300,
        ),
        "requires_reason": True,
    }


def _project_agent_ledger_managed_evidence_source(value: object) -> dict[str, object]:
    if not isinstance(value, WebManagedEvidenceSource):
        raise WebRequestBlocked("managed evidence source is invalid")
    if value.object_type not in {"EVIDENCE_FILE", "MATERIAL_OBJECT"}:
        raise WebRequestBlocked("managed evidence source type is invalid")
    return {
        "object_type": value.object_type,
        "object_id": _project_uuid(value.object_id, "managed evidence source id"),
        "display_label": _project_text(
            value.display_label, "managed evidence source label", 500
        ),
        "created_at": _project_required_datetime(
            value.created_at, "managed evidence source created time"
        ),
    }


def _project_agent_ledger_managed_evidence_source_page(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebManagedEvidenceSourcePage):
        raise WebRequestBlocked("managed evidence source page is invalid")
    total_count = _project_non_negative_int(
        value.total_count, "managed evidence source count"
    )
    offset = _project_non_negative_int(value.offset, "managed evidence source offset")
    sources = [
        _project_agent_ledger_managed_evidence_source(item) for item in value.sources
    ]
    expected_next = offset + len(sources)
    next_offset = value.next_offset
    if next_offset is not None:
        next_offset = _project_non_negative_int(
            next_offset, "managed evidence source next offset"
        )
    if (
        offset > total_count
        or len(sources) > 50
        or (total_count > 0 and (offset >= total_count or not sources))
        or (total_count == 0 and (offset != 0 or sources))
        or next_offset != (expected_next if expected_next < total_count else None)
        or len(
            {(item["object_type"], item["object_id"]) for item in sources}
        ) != len(sources)
    ):
        raise WebRequestBlocked("managed evidence source page is inconsistent")
    return {
        "total_count": total_count,
        "offset": offset,
        "next_offset": next_offset,
        "sources": sources,
    }


def _project_agent_ledger_followup_evidence_page(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebFollowupEvidencePageIdPage):
        raise WebRequestBlocked("follow-up evidence page is invalid")
    total_count = _project_positive_int(
        value.total_count, "follow-up evidence page count"
    )
    offset = _project_non_negative_int(value.offset, "follow-up evidence page offset")
    page_ids = [
        _project_uuid(item, "follow-up evidence page id")
        for item in value.evidence_page_ids
    ]
    expected_next = offset + len(page_ids)
    next_offset = value.next_offset
    if next_offset is not None:
        next_offset = _project_non_negative_int(
            next_offset, "follow-up evidence page next offset"
        )
    if (
        offset >= total_count
        or not page_ids
        or len(page_ids) > 50
        or len(set(page_ids)) != len(page_ids)
        or next_offset != (expected_next if expected_next < total_count else None)
    ):
        raise WebRequestBlocked("follow-up evidence page is inconsistent")
    return {
        "total_count": total_count,
        "offset": offset,
        "next_offset": next_offset,
        "evidence_page_ids": page_ids,
    }


def _project_agent_ledger_exception_followup_receipt(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionFollowupReceipt):
        raise WebRequestBlocked("agent ledger exception follow-up receipt is invalid")
    terminal_by_action = {
        "CONFIRM_MORE_EVIDENCE": "SATISFIED",
        "RESUME": "RESUMED",
        "WITHDRAW": "WITHDRAWN",
        "SUPERSEDE": "SUPERSEDED",
    }
    if terminal_by_action.get(value.action) != value.terminal_state:
        raise WebRequestBlocked("agent ledger exception follow-up receipt state is invalid")
    return {
        "followup_id": _project_uuid(
            value.followup_id, "agent ledger exception follow-up id"
        ),
        "action": value.action,
        "terminal_state": value.terminal_state,
        "matter_version": _project_positive_int(
            value.matter_version, "agent ledger exception matter version"
        ),
    }


def _project_agent_ledger_exception_recovery_receipt(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionRecoveryReceipt):
        raise WebRequestBlocked("agent ledger exception recovery receipt is invalid")
    if value.control_health != "HEALTHY" or value.recovery_started is not True:
        raise WebRequestBlocked("agent ledger exception recovery state is invalid")
    return {
        "matter_version": _project_positive_int(
            value.matter_version, "agent ledger exception recovery matter version"
        ),
        "control_health": "HEALTHY",
        "recovery_started": True,
    }


def _project_agent_ledger_extraction_batch(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExtractionBatch):
        raise WebRequestBlocked("agent ledger extraction batch is invalid")
    if value.status not in {
        "REVIEW_READY",
        "EXCEPTIONS_ONLY",
        "EXCEPTIONS_PARTIALLY_RESOLVED",
        "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
        "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL",
        "CONFIRMED",
        "RESOLVED",
        "STALE",
    }:
        raise WebRequestBlocked("agent ledger extraction batch status is invalid")
    current_version = _project_positive_int(
        value.current_matter_version, "agent ledger extraction matter version"
    )
    source_version = _project_positive_int(
        value.source_matter_version, "agent ledger extraction source version"
    )
    candidate_count = _project_non_negative_int(
        value.candidate_count, "agent ledger extraction candidate count"
    )
    low_risk_count = _project_non_negative_int(
        value.low_risk_count, "agent ledger extraction low-risk count"
    )
    exception_count = _project_non_negative_int(
        value.exception_count, "agent ledger extraction exception count"
    )
    low_risk = [
        _project_agent_ledger_extraction_candidate(item)
        for item in value.low_risk_candidates
    ]
    exceptions = [
        _project_agent_ledger_extraction_candidate(item)
        for item in value.exception_candidates
    ]
    if value.exception_review_status not in {
        "NONE",
        "OPEN",
        "PARTIALLY_RESOLVED",
        "RESOLVED",
    }:
        raise WebRequestBlocked("agent ledger exception review status is invalid")
    exception_group_count = _project_non_negative_int(
        value.exception_group_count, "agent ledger exception group count"
    )
    decided_exception_group_count = _project_non_negative_int(
        value.decided_exception_group_count,
        "agent ledger decided exception group count",
    )
    groups = [
        _project_agent_ledger_exception_group(item)
        for item in value.exception_groups
    ]
    if (
        candidate_count > 500
        or low_risk_count + exception_count != candidate_count
        or len(low_risk) != low_risk_count
        or len(exceptions) != exception_count
        or any(item["review_status"] != "LOW_RISK" for item in low_risk)
        or any(item["review_status"] != "EXCEPTION" for item in exceptions)
        or exception_group_count != len(groups)
        or decided_exception_group_count
        != sum(item["status"] == "DECIDED" for item in groups)
        or decided_exception_group_count > exception_group_count
        or sum(int(item["candidate_count"]) for item in groups) != exception_count
    ):
        raise WebRequestBlocked("agent ledger extraction batch counts are invalid")
    if type(value.can_confirm_low_risk) is not bool:
        raise WebRequestBlocked("agent ledger extraction confirmation state is invalid")
    if value.can_confirm_low_risk and not (
        value.status == "REVIEW_READY" and low_risk_count > 0
    ):
        raise WebRequestBlocked("agent ledger extraction confirmation state is inconsistent")
    confirmed_at = (
        None
        if value.confirmed_at is None
        else _project_required_datetime(
            value.confirmed_at, "agent ledger extraction confirmation time"
        )
    )
    low_risk_confirmed = value.status in {
        "CONFIRMED",
        "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
        "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL",
    } or (value.status == "RESOLVED" and low_risk_count > 0)
    if low_risk_confirmed != (confirmed_at is not None):
        raise WebRequestBlocked("agent ledger extraction confirmed state is inconsistent")
    expected_exception_status = (
        "NONE"
        if exception_group_count == 0
        else "OPEN"
        if decided_exception_group_count == 0
        else "PARTIALLY_RESOLVED"
        if decided_exception_group_count < exception_group_count
        else "RESOLVED"
    )
    if value.exception_review_status != expected_exception_status:
        raise WebRequestBlocked("agent ledger exception review counts are inconsistent")
    if (
        (value.status == "CONFIRMED" and exception_count != 0)
        or (value.status == "RESOLVED" and value.exception_review_status != "RESOLVED")
        or (
            value.status in {
                "EXCEPTIONS_ONLY",
                "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
            }
            and value.exception_review_status != "OPEN"
        )
        or (
            value.status in {
                "EXCEPTIONS_PARTIALLY_RESOLVED",
                "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL",
            }
            and value.exception_review_status != "PARTIALLY_RESOLVED"
        )
    ):
        raise WebRequestBlocked("agent ledger extraction exception state is inconsistent")
    return {
        "batch_id": _project_uuid(value.batch_id, "agent ledger extraction batch id"),
        "matter_id": _project_uuid(value.matter_id, "agent ledger extraction matter id"),
        "status": value.status,
        "current_matter_version": current_version,
        "source_matter_version": source_version,
        "candidate_count": candidate_count,
        "low_risk_count": low_risk_count,
        "exception_count": exception_count,
        "staged_at": _project_required_datetime(
            value.staged_at, "agent ledger extraction staged time"
        ),
        "confirmed_at": confirmed_at,
        "can_confirm_low_risk": value.can_confirm_low_risk,
        "exception_review_status": value.exception_review_status,
        "exception_group_count": exception_group_count,
        "decided_exception_group_count": decided_exception_group_count,
        "exception_groups": groups,
        "low_risk_candidates": low_risk,
        "exception_candidates": exceptions,
    }


def _project_agent_ledger_exception_group(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionGroup):
        raise WebRequestBlocked("agent ledger exception group is invalid")
    if value.candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebRequestBlocked("agent ledger exception group kind is invalid")
    if value.status not in {"OPEN", "DECIDED"}:
        raise WebRequestBlocked("agent ledger exception group status is invalid")
    candidate_count = _project_positive_int(
        value.candidate_count, "agent ledger exception candidate count"
    )
    if candidate_count > 500:
        raise WebRequestBlocked("agent ledger exception candidate count is invalid")
    reasons = [
        _project_text(item, "agent ledger exception reason", 240)
        for item in value.review_reasons
    ]
    actions = [_project_agent_ledger_exception_action(item) for item in value.allowed_actions]
    if (
        not reasons
        or len(reasons) > 15
        or len(set(reasons)) != len(reasons)
        or not actions
        or len(actions) > 4
        or len({item["code"] for item in actions}) != len(actions)
        or type(value.can_decide) is not bool
        or (value.status == "DECIDED" and value.can_decide)
    ):
        raise WebRequestBlocked("agent ledger exception group policy is invalid")
    decision = None if value.decision is None else _project_text(
        value.decision, "agent ledger exception decision", 40
    )
    decision_label = None if value.decision_label is None else _project_text(
        value.decision_label, "agent ledger exception decision label", 120
    )
    decision_reason = None if value.decision_reason is None else _project_text(
        value.decision_reason, "agent ledger exception decision reason", 50
    )
    decision_reason_label = (
        None
        if value.decision_reason_label is None
        else _project_text(
            value.decision_reason_label,
            "agent ledger exception decision reason label",
            160,
        )
    )
    if value.status == "OPEN" and any(
        item is not None
        for item in (decision, decision_label, decision_reason, decision_reason_label)
    ):
        raise WebRequestBlocked("open agent ledger exception group has a decision")
    if value.status == "DECIDED" and any(
        item is None
        for item in (decision, decision_label, decision_reason, decision_reason_label)
    ):
        raise WebRequestBlocked("decided agent ledger exception group is incomplete")
    return {
        "group_id": _project_uuid(value.group_id, "agent ledger exception group id"),
        "candidate_kind": value.candidate_kind,
        "candidate_count": candidate_count,
        "summary": _project_text(value.summary, "agent ledger exception summary", 1_000),
        "review_reasons": reasons,
        "source_guidance": _project_text(
            value.source_guidance, "agent ledger exception source guidance", 240
        ),
        "risk_label": _project_text(
            value.risk_label, "agent ledger exception risk label", 160
        ),
        "status": value.status,
        "decision": decision,
        "decision_label": decision_label,
        "decision_reason": decision_reason,
        "decision_reason_label": decision_reason_label,
        "can_decide": value.can_decide,
        "allowed_actions": actions,
    }


def _project_agent_ledger_exception_action(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionAction):
        raise WebRequestBlocked("agent ledger exception action is invalid")
    if type(value.requires_note) is not bool:
        raise WebRequestBlocked("agent ledger exception action note policy is invalid")
    reasons = [
        _project_agent_ledger_exception_reason_option(item) for item in value.reasons
    ]
    if not reasons or len(reasons) > 3 or len({item["code"] for item in reasons}) != len(reasons):
        raise WebRequestBlocked("agent ledger exception action reasons are invalid")
    return {
        "code": _project_text(value.code, "agent ledger exception action code", 40),
        "label": _project_text(value.label, "agent ledger exception action label", 120),
        "consequence": _project_text(
            value.consequence, "agent ledger exception consequence", 300
        ),
        "requires_note": value.requires_note,
        "reasons": reasons,
    }


def _project_agent_ledger_exception_reason_option(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionReasonOption):
        raise WebRequestBlocked("agent ledger exception reason option is invalid")
    return {
        "code": _project_text(value.code, "agent ledger exception reason code", 50),
        "label": _project_text(value.label, "agent ledger exception reason label", 160),
    }


def _project_agent_ledger_extraction_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExtractionCandidate):
        raise WebRequestBlocked("agent ledger extraction candidate is invalid")
    if value.candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebRequestBlocked("agent ledger extraction candidate kind is invalid")
    if value.review_status not in {"LOW_RISK", "EXCEPTION"}:
        raise WebRequestBlocked("agent ledger extraction review status is invalid")
    if (
        not isinstance(value.confidence, (int, float))
        or isinstance(value.confidence, bool)
        or not 0 <= float(value.confidence) <= 1
    ):
        raise WebRequestBlocked("agent ledger extraction confidence is invalid")
    reasons = [
        _project_text(item, "agent ledger extraction review reason", 240)
        for item in value.review_reasons
    ]
    if (
        len(reasons) > 15
        or len(set(reasons)) != len(reasons)
        or (value.review_status == "LOW_RISK" and reasons)
        or (value.review_status == "EXCEPTION" and not reasons)
    ):
        raise WebRequestBlocked("agent ledger extraction review reasons are invalid")
    excerpts = [
        _project_agent_ledger_extraction_excerpt(item) for item in value.excerpts
    ]
    if not excerpts or len(excerpts) > 20:
        raise WebRequestBlocked("agent ledger extraction excerpts are invalid")
    return {
        "sequence": _project_positive_int(
            value.sequence, "agent ledger extraction candidate sequence"
        ),
        "candidate_kind": value.candidate_kind,
        "summary": _project_text(
            value.summary, "agent ledger extraction candidate summary", 2_000
        ),
        "confidence": round(float(value.confidence), 4),
        "review_status": value.review_status,
        "review_reasons": reasons,
        "excerpts": excerpts,
    }


def _project_agent_ledger_extraction_excerpt(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExtractionExcerpt):
        raise WebRequestBlocked("agent ledger extraction excerpt is invalid")
    return {
        "evidence_page_id": _project_uuid(
            value.evidence_page_id, "agent ledger extraction evidence page id"
        ),
        "page_number": _project_positive_int(
            value.page_number, "agent ledger extraction page number"
        ),
        "text": _project_text(value.text, "agent ledger extraction excerpt", 2_000),
    }


def _project_agent_ledger_extraction_confirmation(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExtractionConfirmationReceipt):
        raise WebRequestBlocked("agent ledger extraction confirmation receipt is invalid")
    fact_count = _project_non_negative_int(
        value.confirmed_fact_count, "confirmed fact count"
    )
    transaction_count = _project_non_negative_int(
        value.confirmed_transaction_count, "confirmed transaction count"
    )
    total_count = _project_positive_int(
        value.confirmed_total_count, "confirmed extraction count"
    )
    if fact_count + transaction_count != total_count or total_count > 500:
        raise WebRequestBlocked("agent ledger extraction confirmation counts are invalid")
    return {
        "batch_id": _project_uuid(value.batch_id, "agent ledger extraction batch id"),
        "matter_version": _project_positive_int(
            value.matter_version, "agent ledger extraction matter version"
        ),
        "confirmed_fact_count": fact_count,
        "confirmed_transaction_count": transaction_count,
        "confirmed_total_count": total_count,
    }


def _project_agent_ledger_exception_member_page(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionMemberPage):
        raise WebRequestBlocked("agent ledger exception member page is invalid")
    total_count = _project_positive_int(
        value.total_count, "agent ledger exception total count"
    )
    offset = _project_non_negative_int(value.offset, "agent ledger exception offset")
    if total_count > 500 or offset >= total_count:
        raise WebRequestBlocked("agent ledger exception member range is invalid")
    members = [_project_agent_ledger_exception_member(item) for item in value.members]
    if (
        not members
        or len(members) > 50
        or [item["sequence"] for item in members]
        != list(range(offset + 1, offset + len(members) + 1))
    ):
        raise WebRequestBlocked("agent ledger exception member page is incomplete")
    expected_next = offset + len(members)
    next_offset = value.next_offset
    if next_offset is not None:
        next_offset = _project_non_negative_int(
            next_offset, "agent ledger exception next offset"
        )
    if next_offset != (expected_next if expected_next < total_count else None):
        raise WebRequestBlocked("agent ledger exception member cursor is invalid")
    return {
        "group_id": _project_uuid(value.group_id, "agent ledger exception group id"),
        "total_count": total_count,
        "offset": offset,
        "next_offset": next_offset,
        "members": members,
    }


def _project_agent_ledger_exception_member(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionMember):
        raise WebRequestBlocked("agent ledger exception member is invalid")
    if value.candidate_kind not in {"FACT", "TRANSACTION"}:
        raise WebRequestBlocked("agent ledger exception member kind is invalid")
    if (
        isinstance(value.confidence, bool)
        or not isinstance(value.confidence, (int, float))
        or not 0 <= float(value.confidence) <= 1
    ):
        raise WebRequestBlocked("agent ledger exception member confidence is invalid")
    reasons = [
        _project_text(item, "agent ledger exception member reason", 240)
        for item in value.review_reasons
    ]
    excerpts = [
        _project_agent_ledger_extraction_excerpt(item) for item in value.excerpts
    ]
    if (
        not reasons
        or len(reasons) > 15
        or len(set(reasons)) != len(reasons)
        or not excerpts
        or len(excerpts) > 20
    ):
        raise WebRequestBlocked("agent ledger exception member sources are invalid")
    return {
        **({"extraction_candidate_id": str(UUID(value.extraction_candidate_id))}
           if value.candidate_kind == "FACT" and value.extraction_candidate_id else {}),
        "sequence": _project_positive_int(
            value.sequence, "agent ledger exception member sequence"
        ),
        "candidate_kind": value.candidate_kind,
        "summary": _project_text(
            value.summary, "agent ledger exception member summary", 1_000
        ),
        "confidence": round(float(value.confidence), 4),
        "review_reasons": reasons,
        "excerpts": excerpts,
    }


def _project_agent_ledger_exception_decision(value: object) -> dict[str, object]:
    if not isinstance(value, WebAgentLedgerExceptionDecisionReceipt):
        raise WebRequestBlocked("agent ledger exception decision receipt is invalid")
    if value.decision not in {
        "REJECT_AS_DUPLICATE",
        "REQUEST_REEXTRACTION",
        "REQUEST_MORE_EVIDENCE",
        "DEFER_WITH_REASON",
    } or value.exception_review_status not in {
        "OPEN",
        "PARTIALLY_RESOLVED",
        "RESOLVED",
    }:
        raise WebRequestBlocked("agent ledger exception decision receipt state is invalid")
    group_count = _project_positive_int(
        value.exception_group_count, "agent ledger exception group count"
    )
    decided_count = _project_positive_int(
        value.decided_exception_group_count,
        "agent ledger decided exception group count",
    )
    if (
        group_count > 500
        or decided_count > group_count
        or type(value.batch_resolved) is not bool
        or (value.batch_resolved and value.exception_review_status != "RESOLVED")
    ):
        raise WebRequestBlocked("agent ledger exception decision receipt counts are invalid")
    matter_version = _project_positive_int(
        value.matter_version, "agent ledger exception matter version"
    )
    committed_version = _project_positive_int(
        value.committed_matter_version,
        "agent ledger exception committed matter version",
    )
    if matter_version < committed_version:
        raise WebRequestBlocked("agent ledger exception decision receipt version is invalid")
    return {
        "batch_id": _project_uuid(value.batch_id, "agent ledger extraction batch id"),
        "group_id": _project_uuid(value.group_id, "agent ledger exception group id"),
        "matter_version": matter_version,
        "committed_matter_version": committed_version,
        "decision": value.decision,
        "exception_review_status": value.exception_review_status,
        "decided_exception_group_count": decided_count,
        "exception_group_count": group_count,
        "batch_resolved": value.batch_resolved,
    }


def _project_required_datetime(value: object, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebRequestBlocked(f"{label} is invalid")
    return value.isoformat()


def _validate_upload_receipt(receipt: object) -> None:
    if not isinstance(receipt, WebUploadReceipt):
        raise WebRequestBlocked("Web material upload receipt is invalid")
    for value in (receipt.evidence_file_id,):
        try:
            UUID(value)
        except (TypeError, ValueError):
            raise WebRequestBlocked("Web material upload receipt is invalid") from None
    if (
        not isinstance(receipt.display_name, str)
        or not receipt.display_name
        or not isinstance(receipt.content_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.content_sha256)
        or type(receipt.page_count) is not int
        or receipt.page_count < 1
        or type(receipt.matter_version) is not int
        or receipt.matter_version < 1
    ):
        raise WebRequestBlocked("Web material upload receipt is invalid")


def _evidence_receipt(receipt: object) -> dict[str, object]:
    """Project a ledger receipt without returning browser-held idempotency data."""

    command_name = getattr(receipt, "command_name", None)
    matter_id = getattr(receipt, "matter_id", None)
    matter_version = getattr(receipt, "matter_version", None)
    audit_event_id = getattr(receipt, "audit_event_id", None)
    object_type = getattr(receipt, "object_type", None)
    object_id = getattr(receipt, "object_id", None)
    if (
        not isinstance(command_name, str)
        or not 1 <= len(command_name) <= 128
        or not isinstance(object_type, str)
        or not 1 <= len(object_type) <= 128
        or not isinstance(matter_id, str)
        or not isinstance(audit_event_id, str)
        or not isinstance(object_id, str)
        or type(matter_version) is not int
        or matter_version < 1
    ):
        raise WebRequestBlocked("证据操作回执格式无效")
    try:
        UUID(matter_id)
        UUID(audit_event_id)
        UUID(object_id)
    except (TypeError, ValueError):
        raise WebRequestBlocked("证据操作回执格式无效") from None
    return {
        "command_name": command_name,
        "matter_id": matter_id,
        "matter_version": matter_version,
        "audit_event_id": audit_event_id,
        "object_type": object_type,
        "object_id": object_id,
    }


def _validated_page_preview_png(preview: object, *, evidence_page_id: str) -> bytes:
    """Reject an adapter result unless it is a bounded exact PNG for this page."""

    if getattr(preview, "evidence_page_id", None) != evidence_page_id:
        raise WebRequestBlocked("Web PDF page preview is invalid")
    content = getattr(preview, "png_content", None)
    if not isinstance(content, bytes) or not 24 <= len(content) <= 32 * 1024 * 1024:
        raise WebRequestBlocked("Web PDF page preview is invalid")
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise WebRequestBlocked("Web PDF page preview is invalid")
    content_hash = getattr(preview, "content_sha256", None)
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise WebRequestBlocked("Web PDF page preview is invalid")
    if sha256(content).hexdigest() != content_hash:
        raise WebRequestBlocked("Web PDF page preview is invalid")
    if getattr(preview, "media_type", None) != "image/png":
        raise WebRequestBlocked("Web PDF page preview is invalid")
    width = getattr(preview, "width", None)
    height = getattr(preview, "height", None)
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= 20_000
        or not 1 <= height <= 20_000
        or width * height > 100_000_000
    ):
        raise WebRequestBlocked("Web PDF page preview is invalid")
    return content


def _iso_datetime(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WebRequestBlocked("case listing returned an invalid update time")
    return value.isoformat()


def _content_type(request: Request) -> str:
    values = request.headers.getlist("content-type")
    if len(values) != 1:
        raise WebRequestBlocked("Web material content type is invalid")
    return values[0].split(";", 1)[0].strip().lower()


def _case_review_projection(snapshot: object) -> dict[str, object]:
    """Project the persistent ledger without leaking internal objects."""

    matter_id = getattr(snapshot, "matter_id", None)
    title = getattr(snapshot, "title", None)
    stage = getattr(snapshot, "stage", None)
    version = getattr(snapshot, "version", None)
    snapshot_hash = getattr(snapshot, "snapshot_hash", None)
    if (
        not isinstance(matter_id, str)
        or not isinstance(title, str)
        or not isinstance(stage, str)
        or type(version) is not int
        or version < 1
        or not isinstance(snapshot_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
    ):
        raise WebRequestBlocked("案件要点回执格式无效")

    facts = tuple(getattr(snapshot, "facts", ()))
    claims = tuple(getattr(snapshot, "claims", ()))
    issues = tuple(getattr(snapshot, "issues", ()))
    transactions = tuple(getattr(snapshot, "transactions", ()))
    payment_classifications = tuple(getattr(snapshot, "payment_classifications", ()))
    return {
        "matter_id": matter_id,
        "title": title,
        "stage": stage,
        "version": version,
        "snapshot_hash": snapshot_hash,
        "facts": [_project_fact(item) for item in facts],
        "claims": [_project_claim(item) for item in claims],
        "issues": [_project_issue(item) for item in issues],
        "transactions": [_project_transaction(item) for item in transactions],
        "payment_classifications": [_project_payment_classification(item) for item in payment_classifications],
        "counts": {
            "facts": len(facts),
            "candidate_facts": sum(1 for item in facts if isinstance(item, Mapping) and item.get("status") == "CANDIDATE"),
            "claims": len(claims),
            "issues": len(issues),
            "transactions": len(transactions),
            "payment_classifications": len(payment_classifications),
        },
    }


def _project_fact(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("案件事实回执格式无效")
    return {
        "fact_id": _project_uuid(value.get("fact_id"), "事实编号"),
        "text": _project_text(value.get("original_text"), "事实内容", 8_000),
        "origin": _project_text(value.get("origin"), "事实来源", 80),
        "status": _project_text(value.get("status"), "事实状态", 40),
        "evidence_count": _project_nonnegative_int(value.get("evidence_count"), "事实证据数量"),
        "correction_candidate_id": _project_uuid(value["correction_candidate_id"],"原始纠正候选编号") if value.get("correction_candidate_id") else None,
        "evidence_sources": [
            {"evidence_page_id":_project_uuid(link.get("evidence_id"),"事实证据定位"),
             "label":_project_text(link.get("original_label"),"原件名称",1024),
             "page_number":_project_nonnegative_int(link["page_number"],"原件页码") if link.get("page_number") is not None else None}
            for link in value.get("evidence_links", [])
        ],
    }


def _project_claim(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("诉请回执格式无效")
    response = value.get("response")
    response_payload: dict[str, object] | None = None
    if response is not None:
        if not isinstance(response, Mapping):
            raise WebRequestBlocked("诉请回应回执格式无效")
        response_payload = {
            "position": _project_text(response.get("position"), "诉请回应", 64),
            "partial_amount": _project_money(response.get("partial_amount")),
            "currency": _project_optional_text(response.get("currency"), 12),
        }
    return {
        "claim_id": _project_uuid(value.get("claim_id"), "诉请编号"),
        "text": _project_text(value.get("original_claim_text"), "诉请内容", 8_000),
        "claimed_amount": _project_money(value.get("claimed_amount")),
        "currency": _project_optional_text(value.get("currency"), 12),
        "status": _project_text(value.get("status"), "诉请状态", 40),
        "evidence_count": _project_nonnegative_int(value.get("evidence_count"), "诉请证据数量"),
        "response": response_payload,
    }


def _project_issue(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("争点回执格式无效")
    claim_ids = value.get("claim_ids", ())
    fact_ids = value.get("confirmed_fact_ids", ())
    if not isinstance(claim_ids, (tuple, list)) or not isinstance(fact_ids, (tuple, list)):
        raise WebRequestBlocked("争点关联回执格式无效")
    return {
        "issue_id": _project_uuid(value.get("issue_id"), "争点编号"),
        "question": _project_text(value.get("question"), "争点问题", 2_000),
        "status": _project_text(value.get("status"), "争点状态", 40),
        "claim_ids": [_project_uuid(item, "争点诉请编号") for item in claim_ids],
        "confirmed_fact_ids": [_project_uuid(item, "争点事实编号") for item in fact_ids],
    }


def _project_transaction(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("交易回执格式无效")
    return {
        "transaction_id": _project_uuid(value.get("transaction_id"), "交易编号"),
        "local_date": _project_optional_date(value.get("local_date")),
        "date_precision": _project_optional_text(value.get("date_precision"), 32),
        "amount": _project_money(value.get("amount")),
        "currency": _project_optional_text(value.get("currency"), 12),
        "direction": _project_text(value.get("direction"), "交易方向", 40),
        "payer_label": _project_optional_text(value.get("payer_label"), 255),
        "payee_label": _project_optional_text(value.get("payee_label"), 255),
        "channel": _project_optional_text(value.get("channel"), 64),
        "transaction_reference": _project_optional_text(value.get("transaction_reference"), 255),
        "status": _project_text(value.get("status"), "交易状态", 40),
        "evidence_count": _project_nonnegative_int(value.get("evidence_count"), "交易证据数量"),
    }


def _project_payment_classification(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("款项归属回执格式无效")
    allocations = value.get("allocations", ())
    if not isinstance(allocations, (tuple, list)):
        raise WebRequestBlocked("款项归属明细格式无效")
    return {
        "classification_id": _project_uuid(value.get("classification_id"), "款项归属编号"),
        "transaction_id": _project_uuid(value.get("transaction_id"), "收付款记录编号"),
        "nature": _project_text(value.get("nature"), "款项性质", 64),
        "same_day_sequence": (
            _project_nonnegative_int(value.get("same_day_sequence"), "同日顺序")
            if value.get("same_day_sequence") is not None
            else None
        ),
        "status": _project_text(value.get("status"), "款项归属状态", 40),
        "evidence_count": _project_nonnegative_int(value.get("evidence_count"), "款项归属证据数量"),
        "allocations": [
            {
                "obligation_label": _project_text(item.get("obligation_id"), "归属事项", 160),
                "amount": _project_money(item.get("amount")),
                "currency": _project_optional_text(item.get("currency"), 12),
            }
            for item in allocations
            if isinstance(item, Mapping)
        ],
    }


def _approved_calculation_obligations(snapshot: object) -> frozenset[str]:
    """Return only server-recorded, lawyer-approved calculation inputs."""

    obligations: set[str] = set()
    for classification in getattr(snapshot, "payment_classifications", ()):
        if not isinstance(classification, Mapping) or classification.get("status") != "APPROVED":
            continue
        allocations = classification.get("allocations", ())
        if not isinstance(allocations, (tuple, list)):
            continue
        for allocation in allocations:
            if not isinstance(allocation, Mapping):
                continue
            obligation_id = allocation.get("obligation_id")
            if isinstance(obligation_id, str) and obligation_id.strip():
                obligations.add(obligation_id.strip())
    return frozenset(obligations)


def _project_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise WebRequestBlocked(f"{label}格式无效")
    try:
        UUID(value)
    except (TypeError, ValueError):
        raise WebRequestBlocked(f"{label}格式无效") from None
    return value


def _project_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _normalize_case_agent_business_text(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{label}格式无效")
    if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in normalized):
        raise ValueError(f"{label}格式无效")
    return normalized


def _normalize_case_agent_text_list(
    value: tuple[str, ...],
    label: str,
    *,
    maximum_items: int,
    maximum_length: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if (not allow_empty and not value) or len(value) > maximum_items:
        raise ValueError(f"{label}格式无效")
    normalized = tuple(_normalize_case_agent_business_text(item, label, maximum_length) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label}不能重复")
    return normalized


def _normalize_case_agent_code(value: str, label: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", normalized):
        raise ValueError(f"{label}格式无效")
    return normalized


def _project_optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise WebRequestBlocked("案件投影文本格式无效")
    return value


def _project_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 1_000_000:
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _project_money(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise WebRequestBlocked("案件金额格式无效")
        normalized = value.normalize()
        if normalized.as_tuple().exponent < -4:
            raise WebRequestBlocked("案件金额格式无效")
        text = format(normalized, "f")
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        text = ""
    if not re.fullmatch(r"(?:0|[1-9]\d{0,15})(?:\.\d{1,4})?", text):
        raise WebRequestBlocked("案件金额格式无效")
    return text


def _project_rate(value: object) -> str | None:
    """Project a deterministic rate without applying currency precision.

    Legal-rule storage keeps derived rates at twelve decimal places so that
    downstream calculation remains reproducible.  Treating that value as a
    monetary amount made an otherwise usable legal-review page fail closed.
    The browser still receives a bounded decimal string, never a formula or a
    browser-calculated rate.
    """

    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise WebRequestBlocked("案件年利率格式无效")
        normalized = value.normalize()
        if normalized.as_tuple().exponent < -12:
            raise WebRequestBlocked("案件年利率格式无效")
        text = format(normalized, "f")
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        text = ""
    if not re.fullmatch(r"(?:0|[1-9]\d{0,7})(?:\.\d{1,12})?", text):
        raise WebRequestBlocked("案件年利率格式无效")
    return text


def _project_optional_date(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()
    elif isinstance(value, str):
        text = value
    else:
        raise WebRequestBlocked("案件日期格式无效")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise WebRequestBlocked("案件日期格式无效")
    return text


def _project_optional_hash(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WebRequestBlocked("案件哈希格式无效")
    return value


def _legal_review_projection(snapshot: object) -> dict[str, object]:
    """Expose only the reviewable legal-source metadata needed by the Web UI."""

    matter_id = getattr(snapshot, "matter_id", None)
    matter_version = getattr(snapshot, "matter_version", None)
    snapshot_hash = getattr(snapshot, "snapshot_hash", None)
    if (
        not isinstance(matter_id, str)
        or type(matter_version) is not int
        or matter_version < 1
        or not isinstance(snapshot_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
    ):
        raise WebRequestBlocked("法律依据回执格式无效")

    return {
        "matter_id": _project_uuid(matter_id, "案件编号"),
        "matter_version": matter_version,
        "snapshot_hash": snapshot_hash,
        "sources": [_legal_source_row(row) for row in tuple(getattr(snapshot, "sources", ()))],
        "rule_versions": [_legal_rule_row(row) for row in tuple(getattr(snapshot, "rule_versions", ()))],
        "legal_events": [_legal_event_row(row) for row in tuple(getattr(snapshot, "legal_events", ()))],
        "fact_bindings": [_legal_fact_binding_row(row) for row in tuple(getattr(snapshot, "fact_bindings", ()))],
        "current_bundle": _legal_bundle_row(getattr(snapshot, "current_bundle", None)),
        "bundle_segments": [_legal_segment_row(row) for row in tuple(getattr(snapshot, "bundle_segments", ()))],
        "bundle_reconfirmation": _legal_bundle_reconfirmation_row(
            getattr(snapshot, "bundle_reconfirmation", None)
        ),
    }


_WEB_OFFICIAL_SOURCE_CATALOGUE: dict[str, dict[str, object]] = {
    "CN-CIVIL-CODE-680": {
        "title": "《民法典》借款成立与利息规则",
        "publisher": "最高人民检察院公开法律文本",
        "purpose": "核对借款合同成立、未约定或约定不明利息等基础规则",
        "target_url": "https://www.spp.gov.cn/zdgz/202006/t20200602_463886.shtml",
        "query": "中华人民共和国民法典 第六百七十九条 第六百八十条",
        "max_response_bytes": 4 * 1024 * 1024,
    },
    "SPC-PRIVATE-LENDING-2020-SECOND-REVISION": {
        "title": "民间借贷司法解释（现行修正文本）",
        "publisher": "最高人民法院",
        "purpose": "核对利率保护、预扣利息、逾期利息与过渡规则",
        "target_url": "https://www.court.gov.cn/zixun/xiangqing/282621.html",
        "query": "民间借贷司法解释 2020年第二次修正 第二十四条至第三十一条 利率保护 过渡规则",
        "max_response_bytes": 4 * 1024 * 1024,
    },
    "SPC-PRIVATE-LENDING-2020-FIRST-REVISION": {
        "title": "民间借贷司法解释（2020年第一次修正历史文本）",
        "publisher": "最高人民法院",
        "purpose": "核对历史期间的利率保护与起诉时适用口径",
        "target_url": "https://www.court.gov.cn/zixun/xiangqing/249031.html",
        "query": "民间借贷司法解释 2020年第一次修正 第二十六条 第三十二条",
        "max_response_bytes": 4 * 1024 * 1024,
    },
    "SPC-PRIVATE-LENDING-2015-ORIGINAL": {
        "title": "民间借贷司法解释（2015年原始文本）",
        "publisher": "最高人民法院公报",
        "purpose": "核对已付利息、年利率24%和36%等历史规则",
        "target_url": "https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html",
        "query": "法释2015 18号 第二十六条 第三十一条",
        "max_response_bytes": 4 * 1024 * 1024,
    },
    "CFETS-LPR-HISTORY": {
        "title": "贷款市场报价利率（LPR）历史数据",
        "publisher": "全国银行间同业拆借中心",
        "purpose": "核对利率规则所需的历史一年期LPR数据",
        "target_url": "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN",
        "query": "一年期贷款市场报价利率 历史数据",
        "max_response_bytes": 32 * 1024 * 1024,
    },
}


def _web_official_source_instruction(source_id: str) -> Mapping[str, object]:
    try:
        return _WEB_OFFICIAL_SOURCE_CATALOGUE[source_id]
    except KeyError as error:
        raise WebRequestBlocked("所选官方依据不在本案可用目录中") from error


def _web_official_source_catalogue() -> list[dict[str, object]]:
    return [
        {
            "source_id": source_id,
            "title": _project_text(row["title"], "官方依据名称", 160),
            "publisher": _project_text(row["publisher"], "官方发布机构", 160),
            "purpose": _project_text(row["purpose"], "核对用途", 240),
        }
        for source_id, row in _WEB_OFFICIAL_SOURCE_CATALOGUE.items()
    ]


def _web_official_source_license_basis(instruction: Mapping[str, object]) -> str:
    publisher = _project_text(instruction.get("publisher"), "官方发布机构", 160)
    return (
        f"来源为目录列明的{publisher}公开官方网站原文；"
        "本案仅以律师完成的来源定位核对结果作为办案依据，不替代法律意见或对外发布授权。"
    )


def _source_id_for_capture_run(*, snapshot: object, run_id: str) -> str:
    normalized_run_id = _project_uuid(run_id, "官方依据任务编号")
    for value in tuple(getattr(snapshot, "runs", ())):
        row = _legal_row(value)
        if row.get("run_id") == normalized_run_id:
            return _project_text(row.get("source_id"), "官方依据标识", 160)
    raise WebRequestBlocked("未找到本案官方依据核对任务")


def _official_source_capture_projection(snapshot: object) -> dict[str, object]:
    matter_id = _project_uuid(getattr(snapshot, "matter_id", None), "案件编号")
    matter_version = getattr(snapshot, "matter_version", None)
    if type(matter_version) is not int or matter_version < 1:
        raise WebRequestBlocked("官方依据核对状态格式无效")
    runs: list[dict[str, object]] = []
    for value in tuple(getattr(snapshot, "runs", ())):
        row = _legal_row(value)
        parsed_summary = row.get("parsed_summary")
        provisions: list[str] = []
        if isinstance(parsed_summary, Mapping):
            parsed_provisions = parsed_summary.get("provisions")
            if isinstance(parsed_provisions, (list, tuple)):
                for item in parsed_provisions:
                    if not isinstance(item, Mapping):
                        continue
                    label = item.get("provision_label")
                    if isinstance(label, str) and label.strip() and len(label) <= 120:
                        provisions.append(label.strip())
        runs.append(
            {
                "run_id": _project_uuid(row.get("run_id"), "官方依据任务编号"),
                "source_id": _project_text(row.get("source_id"), "官方依据标识", 160),
                "publisher": _project_text(row.get("publisher"), "官方发布机构", 255),
                "status": _project_text(row.get("status"), "官方依据任务状态", 64),
                "authorized_at": _project_optional_datetime(row.get("authorized_at")),
                "retrieved_at": _project_optional_datetime(row.get("retrieved_at")),
                "official_url": _project_optional_official_url(
                    row.get("final_url")
                ),
                "provisions": provisions,
                "failure_code": _project_optional_text(row.get("failure_code"), 120),
            }
        )
    reviewed_run_ids: list[str] = []
    for value in tuple(getattr(snapshot, "reviews", ())):
        row = _legal_row(value)
        reviewed_run_ids.append(_project_uuid(row.get("run_id"), "官方依据任务编号"))
    return {
        "matter_id": matter_id,
        "matter_version": matter_version,
        "runs": runs,
        "reviewed_run_ids": reviewed_run_ids,
    }


def _legal_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("法律依据记录格式无效")
    return value


def _legal_source_row(value: object) -> dict[str, object]:
    row = _legal_row(value)
    return {
        "snapshot_id": _project_uuid(row.get("snapshot_id"), "法源快照编号"),
        "source_id": _project_text(row.get("source_id"), "法源标识", 160),
        "publisher": _project_text(row.get("publisher"), "法源发布机构", 255),
        "authority_level": _project_text(row.get("authority_level"), "法源层级", 64),
        "official_url": _project_official_url(row.get("official_url")),
        "provision_locator": _project_text(row.get("provision_locator"), "法源定位", 1_000),
        "retrieved_at": _project_optional_datetime(row.get("retrieved_at")),
        "content_sha256": _project_hash(row.get("content_sha256"), "法源内容哈希"),
        "verification_status": _project_text(row.get("verification_status"), "法源核验状态", 64),
        "license_status": _project_text(row.get("license_status"), "法源许可状态", 64),
    }


def _legal_rule_row(value: object) -> dict[str, object]:
    row = _legal_row(value)
    return {
        "rule_version_id": _project_uuid(row.get("rule_version_id"), "规则版本编号"),
        "rule_id": _project_text(row.get("rule_id"), "规则标识", 160),
        "rule_version": _project_text(row.get("rule_version"), "规则版本", 80),
        "issue_key": _project_text(row.get("issue_key"), "规则争点", 255),
        "effective_from": _project_optional_date(row.get("effective_from")),
        "effective_to": _project_optional_date(row.get("effective_to")),
        "trigger_event_kind": _project_text(row.get("trigger_event_kind"), "触发事件", 80),
        "formula_kind": _project_text(row.get("formula_kind"), "计算公式", 80),
        "base_annual_rate": _project_money(row.get("base_annual_rate")),
        "rate_multiplier": _project_money(row.get("rate_multiplier")),
        "derived_annual_rate": _project_rate(row.get("derived_annual_rate")),
        "status": _project_text(row.get("status"), "规则状态", 64),
    }


def _legal_event_row(value: object) -> dict[str, object]:
    row = _legal_row(value)
    evidence_ids = row.get("evidence_ids", ())
    if not isinstance(evidence_ids, (tuple, list)):
        raise WebRequestBlocked("法律事件证据定位格式无效")
    return {
        "legal_event_id": _project_uuid(row.get("legal_event_id"), "法律事件编号"),
        "event_kind": _project_text(row.get("event_kind"), "法律事件类型", 80),
        "local_date": _project_optional_date(row.get("local_date")),
        "evidence_ids": [_project_uuid(item, "法律事件证据编号") for item in evidence_ids],
        "status": _project_text(row.get("status"), "法律事件状态", 64),
    }


def _legal_fact_binding_row(value: object) -> dict[str, object]:
    row = _legal_row(value)
    return {
        "binding_id": _project_uuid(row.get("binding_id"), "法律事实绑定编号"),
        "fact_key": _project_text(row.get("fact_key"), "法律事实键", 160),
        "fact_id": _project_uuid(row.get("fact_id"), "法律事实编号"),
        "status": _project_text(row.get("status"), "法律事实绑定状态", 64),
    }


def _legal_bundle_row(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    row = _legal_row(value)
    return {
        "bundle_id": _project_uuid(row.get("bundle_id"), "法律规则包编号"),
        "version": _project_nonnegative_int(row.get("version"), "法律规则包版本"),
        "bundle_hash": _project_hash(row.get("bundle_hash"), "法律规则包哈希"),
        "approved_at": _project_optional_datetime(row.get("approved_at")),
    }


def _legal_bundle_reconfirmation_row(value: object) -> dict[str, object] | None:
    """Project only the actionable status of a stale case rule bundle.

    The old bundle hash, approvals and segments remain in the protected ledger.
    The browser needs only enough information to explain why a lawyer must
    establish a fresh current bundle before requesting a new analysis.
    """

    if value is None:
        return None
    row = _legal_row(value)
    if row.get("status") != "STALE":
        raise WebRequestBlocked("案件法律规则包状态无效")
    reason = _project_text(row.get("stale_reason"), "法律规则包失效原因", 500)
    return {
        "version": _project_nonnegative_int(row.get("version"), "法律规则包版本"),
        "reason": reason,
        "stale_at": _project_optional_datetime(row.get("stale_at")),
    }


def _legal_segment_row(value: object) -> dict[str, object]:
    row = _legal_row(value)
    return {
        "segment_id": _project_uuid(row.get("segment_id"), "法律规则段编号"),
        "issue_key": _project_text(row.get("issue_key"), "规则段争点", 255),
        "rule_version_id": _project_uuid(row.get("rule_version_id"), "规则段版本编号"),
        "trigger_event_id": _project_uuid(row.get("trigger_event_id"), "规则段触发事件编号"),
        "start_date": _project_optional_date(row.get("start_date")),
        "end_date": _project_optional_date(row.get("end_date")),
        "annual_rate": _project_rate(row.get("annual_rate")),
        "applicability_anchor": _project_text(row.get("applicability_anchor"), "规则段适用锚点", 120),
    }


def _project_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _project_optional_datetime(value: object) -> str | None:
    if value is None:
        return None
    if not hasattr(value, "isoformat") and not isinstance(value, str):
        raise WebRequestBlocked("案件时间格式无效")
    text = value.isoformat() if hasattr(value, "isoformat") else value
    if not isinstance(text, str) or len(text) > 80:
        raise WebRequestBlocked("案件时间格式无效")
    return text


def _project_official_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise WebRequestBlocked("官方法源地址格式无效")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise WebRequestBlocked("官方法源地址格式无效")
    return value


def _case_readiness_projection(case_snapshot: object, legal_snapshot: object, evidence_summary: object | None) -> dict[str, object]:
    facts = tuple(getattr(case_snapshot, "facts", ()))
    claims = tuple(getattr(case_snapshot, "claims", ()))
    issues = tuple(getattr(case_snapshot, "issues", ()))
    transactions = tuple(getattr(case_snapshot, "transactions", ()))
    sources = tuple(getattr(legal_snapshot, "sources", ()))
    rules = tuple(getattr(legal_snapshot, "rule_versions", ()))
    events = tuple(getattr(legal_snapshot, "legal_events", ()))
    current_bundle = getattr(legal_snapshot, "current_bundle", None)
    unresolved_candidates = (
        sum(1 for item in facts if isinstance(item, Mapping) and item.get("status") == "CANDIDATE")
        + sum(1 for item in claims if isinstance(item, Mapping) and item.get("status") == "CANDIDATE")
        + sum(1 for item in issues if isinstance(item, Mapping) and item.get("status") == "CANDIDATE")
        + sum(1 for item in transactions if isinstance(item, Mapping) and item.get("status") == "CANDIDATE")
    )
    confirmed_facts = any(
        isinstance(item, Mapping) and item.get("status") == "CONFIRMED" for item in facts
    )
    confirmed_claims = any(
        isinstance(item, Mapping) and item.get("status") == "CONFIRMED" for item in claims
    )
    confirmed_issues = any(
        isinstance(item, Mapping) and item.get("status") == "CONFIRMED" for item in issues
    )
    confirmed_transactions = any(
        isinstance(item, Mapping) and item.get("status") == "CONFIRMED" for item in transactions
    )
    verified_sources = sum(
        1
        for item in sources
        if isinstance(item, Mapping)
        and item.get("verification_status") == "VERIFIED"
        and item.get("license_status") == "ACTIVE"
    )
    approved_rules = sum(1 for item in rules if isinstance(item, Mapping) and item.get("status") == "APPROVED")
    evidence_locked = bool(
        isinstance(evidence_summary, Mapping)
        and evidence_summary.get("locked_manifest") is not None
        and evidence_summary.get("unresolved_page_count") == 0
        and evidence_summary.get("pending_decision_count") == 0
        and evidence_summary.get("unresolved_duplicate_count") == 0
    )
    checks = [
        _readiness_check("materials", "材料清单已锁定", evidence_locked, "完成逐页纳入/排除、红框确认并锁定证据清单"),
        _readiness_check(
            "case_review",
            "事实、诉请与争点已确认",
            unresolved_candidates == 0 and confirmed_facts and confirmed_claims and confirmed_issues,
            "在“确认案情”中明确本案诉请和争点，并绑定已确认事实",
        ),
        _readiness_check(
            "payment_review",
            "收付款记录已确认",
            unresolved_candidates == 0 and confirmed_transactions,
            "核对本案收付款记录及其款项性质；没有已确认记录时不进行金额核对",
        ),
        _readiness_check("legal_sources", "官方法源和规则已核验", verified_sources > 0 and approved_rules > 0, "登记已核验且许可有效的官方来源和规则版本"),
        _readiness_check("legal_events", "法律事件已由律师确认", bool(events), "确认合同、付款、起诉或受理等本案法律事件"),
        _readiness_check("rule_bundle", "连续规则包已批准", current_bundle is not None, "建立无缺口、无重叠并绑定本案事实的规则包"),
    ]
    first_blocker = next((item["detail"] for item in checks if item["status"] == "BLOCKED"), "前置条件已满足，等待确定性计算服务接入")
    return {
        "matter_id": _project_uuid(getattr(case_snapshot, "matter_id", None), "案件编号"),
        "matter_version": _project_positive_int(getattr(case_snapshot, "version", None), "案件版本"),
        "checks": checks,
        "counts": {
            "facts": len(facts),
            "claims": len(claims),
            "transactions": len(transactions),
            "candidate_items": unresolved_candidates,
            "verified_sources": verified_sources,
            "approved_rules": approved_rules,
        },
        "next_action": first_blocker,
    }


def _readiness_check(key: str, label: str, ready: bool, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "status": "READY" if ready else "BLOCKED", "detail": "已满足" if ready else detail}


def _project_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1 or value > 2**31 - 1:
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _project_non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**31 - 1:
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _validate_browser_obligation_id(value: object) -> str:
    if not isinstance(value, str):
        raise WebRequestBlocked("计算义务编号格式无效")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 160 or "\x00" in normalized or any(ord(character) < 32 for character in normalized):
        raise WebRequestBlocked("计算义务编号格式无效")
    return normalized


def _project_optional_official_url(value: object) -> str | None:
    if value is None:
        return None
    return _project_official_url(value)


def _formal_calculation_projection(snapshot: object) -> dict[str, object]:
    """Expose a deterministic result without approval secrets or storage locators."""

    matter_id = getattr(snapshot, "matter_id", None)
    matter_version = getattr(snapshot, "matter_version", None)
    snapshot_hash = getattr(snapshot, "snapshot_hash", None)
    if (
        not isinstance(matter_id, str)
        or type(matter_version) is not int
        or matter_version < 1
        or not isinstance(snapshot_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
    ):
        raise WebRequestBlocked("利息测算回执格式无效")
    scenario = getattr(snapshot, "scenario", None)
    run = getattr(snapshot, "run", None)
    if scenario is not None and not isinstance(scenario, Mapping):
        raise WebRequestBlocked("利息测算情景格式无效")
    if run is not None and not isinstance(run, Mapping):
        raise WebRequestBlocked("利息测算结果格式无效")
    if (scenario is None) != (run is None):
        raise WebRequestBlocked("利息测算情景与结果不完整")
    return {
        "matter_id": _project_uuid(matter_id, "案件编号"),
        "matter_version": matter_version,
        "snapshot_hash": snapshot_hash,
        "scenario": None if scenario is None else _project_calculation_scenario(scenario),
        "run": None if run is None else _project_calculation_run(run),
    }


def _project_calculation_scenario(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "scenario_id": _project_uuid(value.get("scenario_id"), "计算情景编号"),
        "obligation_id": _validate_browser_obligation_id(value.get("obligation_id")),
        "version": _project_positive_int(value.get("version"), "计算情景版本"),
        "start_date": _project_date_required(value.get("start_date"), "计算开始日期"),
        "end_date": _project_date_required(value.get("end_date"), "计算结束日期"),
        "currency": _project_exact_text(value.get("currency"), "CNY", "计算币种"),
        "allocation_policy": _project_text(value.get("allocation_policy"), "还款抵扣口径", 64),
        "legal_bundle_id": _project_uuid(value.get("legal_bundle_id"), "法律规则包编号"),
        "legal_bundle_hash": _project_hash(value.get("legal_bundle_hash"), "法律规则包哈希"),
        "transaction_snapshot_hash": _project_hash(value.get("transaction_snapshot_hash"), "交易快照哈希"),
        "input_hash": _project_hash(value.get("input_hash"), "计算输入哈希"),
    }


def _project_calculation_run(value: Mapping[str, object]) -> dict[str, object]:
    line_items = value.get("line_items", ())
    payment_allocations = value.get("payment_allocations", ())
    if not isinstance(line_items, (tuple, list)) or not isinstance(payment_allocations, (tuple, list)):
        raise WebRequestBlocked("利息测算明细格式无效")
    return {
        "run_id": _project_uuid(value.get("run_id"), "计算运行编号"),
        "scenario_id": _project_uuid(value.get("scenario_id"), "计算情景编号"),
        "scenario_version": _project_positive_int(value.get("scenario_version"), "计算情景版本"),
        "engine_version": _project_text(value.get("engine_version"), "计算引擎版本", 80),
        "legal_bundle_id": _project_uuid(value.get("legal_bundle_id"), "法律规则包编号"),
        "legal_bundle_hash": _project_hash(value.get("legal_bundle_hash"), "法律规则包哈希"),
        "input_hash": _project_hash(value.get("input_hash"), "计算输入哈希"),
        "output_hash": _project_hash(value.get("output_hash"), "计算输出哈希"),
        "independent_check_hash": _project_hash(value.get("independent_check_hash"), "独立复核哈希"),
        "total_interest_accrued": _project_money(value.get("total_interest_accrued")),
        "total_interest_paid": _project_money(value.get("total_interest_paid")),
        "remaining_principal": _project_money(value.get("remaining_principal")),
        "remaining_unpaid_interest": _project_money(value.get("remaining_unpaid_interest")),
        "unapplied_payments": _project_money(value.get("unapplied_payments")),
        "generated_at": _project_optional_datetime(value.get("generated_at")),
        "line_items": [_project_calculation_line(item) for item in line_items],
        "payment_allocations": [_project_payment_allocation(item) for item in payment_allocations],
    }


def _project_calculation_line(value: object) -> dict[str, object]:
    row = _calculation_row(value)
    return {
        "line_sequence": _project_positive_int(row.get("line_sequence"), "计算明细序号"),
        "period_start": _project_date_required(row.get("period_start"), "计算明细开始日期"),
        "period_end": _project_date_required(row.get("period_end"), "计算明细结束日期"),
        "opening_principal": _project_money(row.get("opening_principal")),
        "annual_rate": _project_money(row.get("annual_rate")),
        "day_count": _project_nonnegative_int(row.get("day_count"), "计算天数"),
        "accrued_interest": _project_money(row.get("accrued_interest")),
        "closing_principal": _project_money(row.get("closing_principal")),
        "accrued_unpaid_interest": _project_money(row.get("accrued_unpaid_interest")),
        "rule_segment_id": _project_uuid(row.get("rule_segment_id"), "规则段编号"),
        "source_rule_version": _project_text(row.get("source_rule_version"), "规则版本", 80),
        "evidence_ids": _project_uuid_list(row.get("evidence_ids"), "计算明细证据编号"),
    }


def _project_payment_allocation(value: object) -> dict[str, object]:
    row = _calculation_row(value)
    return {
        "allocation_sequence": _project_positive_int(row.get("allocation_sequence"), "还款抵扣序号"),
        "payment_event_id": _project_uuid(row.get("payment_event_id"), "还款事件编号"),
        "effective_date": _project_date_required(row.get("effective_date"), "还款日期"),
        "payment_amount": _project_money(row.get("payment_amount")),
        "allocated_interest": _project_money(row.get("allocated_interest")),
        "allocated_principal": _project_money(row.get("allocated_principal")),
        "unapplied_amount": _project_money(row.get("unapplied_amount")),
        "payment_application": _project_text(row.get("payment_application"), "还款用途", 64),
        "evidence_ids": _project_uuid_list(row.get("evidence_ids"), "还款证据编号"),
    }


def _calculation_row(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked("利息测算明细格式无效")
    return value


def _project_uuid_list(value: object, label: str) -> list[str]:
    if not isinstance(value, (tuple, list)):
        raise WebRequestBlocked(f"{label}格式无效")
    return [_project_uuid(item, label) for item in value]


def _project_date_required(value: object, label: str) -> str:
    text = _project_optional_date(value)
    if text is None:
        raise WebRequestBlocked(f"{label}格式无效")
    return text


def _project_exact_text(value: object, expected: str, label: str) -> str:
    if value != expected:
        raise WebRequestBlocked(f"{label}格式无效")
    return expected


def _submission_review_projection(
    snapshot: object, *, document_drafts_available: bool
) -> dict[str, object]:
    matter_id = getattr(snapshot, "matter_id", None)
    matter_version = getattr(snapshot, "matter_version", None)
    stage = getattr(snapshot, "stage", None)
    snapshot_hash = getattr(snapshot, "snapshot_hash", None)
    if (
        not isinstance(matter_id, str)
        or type(matter_version) is not int
        or matter_version < 1
        or not isinstance(stage, str)
        or not isinstance(snapshot_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
    ):
        raise WebRequestBlocked("应诉材料回执格式无效")
    if type(document_drafts_available) is not bool:
        raise WebRequestBlocked("文书候选能力状态无效")
    return {
        "matter_id": _project_uuid(matter_id, "案件编号"),
        "matter_version": matter_version,
        "stage": _project_text(stage, "案件阶段", 80),
        "snapshot_hash": snapshot_hash,
        "work_products": [_submission_work_product_row(value) for value in tuple(getattr(snapshot, "work_products", ()))],
        "bundles": [_submission_bundle_row(value) for value in tuple(getattr(snapshot, "bundles", ()))],
        "current_bundle": _submission_bundle_row(getattr(snapshot, "current_bundle", None)),
        "current_components": [_submission_component_row(value) for value in tuple(getattr(snapshot, "current_components", ()))],
        "current_export": _submission_export_row(getattr(snapshot, "current_export", None)),
        "document_drafts_available": document_drafts_available,
    }


def _submission_input_hash(submission: object, actor: Actor, matter_id: str, bundle_id: str) -> str:
    """Read the exact server-side bundle input hash for a lock command.

    The browser must not submit or display this lineage value.  Keeping the
    lookup here also makes a stale bundle fail through the store's normal
    version/dependency checks instead of accepting a client-supplied hash.
    """

    snapshot = submission.get_submission_snapshot(matter_id=matter_id, actor=actor)
    current_bundle = getattr(snapshot, "current_bundle", None)
    candidates = [current_bundle] if isinstance(current_bundle, Mapping) else []
    candidates.extend(item for item in tuple(getattr(snapshot, "bundles", ())) if isinstance(item, Mapping))
    matching_bundle = next((item for item in candidates if item.get("bundle_id") == bundle_id), None)
    if matching_bundle is None:
        raise WebRequestBlocked("当前案件没有可锁定的应诉材料包")
    value = matching_bundle.get("input_hash")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WebRequestBlocked("应诉材料包缺少可核验输入版本")
    return value


def _submission_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebRequestBlocked(f"{label}格式无效")
    return value


def _submission_work_product_row(value: object) -> dict[str, object]:
    row = _submission_mapping(value, "应诉文书")
    return {
        "work_product_id": _project_uuid(row.get("work_product_id"), "应诉文书编号"),
        "document_kind": _project_text(row.get("document_kind"), "应诉文书类型", 80),
        "audience": _project_text(row.get("audience"), "应诉文书用途", 40),
        "media_type": _project_exact_text(row.get("media_type"), "application/pdf", "应诉文书格式"),
        "artifact_sha256": _project_hash(row.get("artifact_sha256"), "应诉文书哈希"),
        "byte_size": _project_positive_int(row.get("byte_size"), "应诉文书大小"),
        "page_count": _project_positive_int(row.get("page_count"), "应诉文书页数"),
        "semantic_text_sha256": _project_optional_hash(row.get("semantic_text_sha256")),
        "status": _project_text(row.get("status"), "应诉文书状态", 40),
        "approved_at": _project_optional_datetime(row.get("approved_at")),
        "stale_at": _project_optional_datetime(row.get("stale_at")),
        "stale_reason": _project_optional_text(row.get("stale_reason"), 500),
        "created_at": _project_optional_datetime(row.get("created_at")),
    }


def _document_pair_projection(value: object) -> dict[str, object]:
    row = _submission_mapping(value, "文书候选")
    return {
        "pair_id": _project_uuid(row.get("pair_id"), "文书候选编号"),
        "document_kind": _project_text(row.get("document_kind"), "文书候选类型", 80),
        "editable_media_type": _project_text(row.get("editable_media_type"), "可编辑文书格式", 160),
        "editable_sha256": _project_hash(row.get("editable_sha256"), "可编辑文书哈希"),
        "editable_bytes": _project_positive_int(row.get("editable_bytes"), "可编辑文书大小"),
        "review_pdf_sha256": _project_hash(row.get("review_pdf_sha256"), "审阅 PDF 哈希"),
        "review_pdf_bytes": _project_positive_int(row.get("review_pdf_bytes"), "审阅 PDF 大小"),
        "review_pdf_page_count": _project_positive_int(row.get("review_pdf_page_count"), "审阅 PDF 页数"),
        "review_input_hash": _project_hash(row.get("review_input_hash"), "文书候选核验哈希"),
        "status": _project_text(row.get("status"), "文书候选状态", 32),
        "approved_at": _project_optional_datetime(row.get("approved_at")),
        "created_at": _project_optional_datetime(row.get("created_at")),
    }


def _submission_bundle_row(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    row = _submission_mapping(value, "应诉材料包")
    required = row.get("required_document_kinds", ())
    if isinstance(required, str):
        raise WebRequestBlocked("应诉材料包必需文书清单格式无效")
    if not isinstance(required, (tuple, list)) or len(required) > 100:
        raise WebRequestBlocked("应诉材料包必需文书清单格式无效")
    return {
        "bundle_id": _project_uuid(row.get("bundle_id"), "应诉材料包编号"),
        "lifecycle": _project_text(row.get("lifecycle"), "应诉材料包生命周期", 40),
        "validity": _project_text(row.get("validity"), "应诉材料包有效性", 40),
        "final_text_hash": _project_optional_hash(row.get("final_text_hash")),
        "approved_matter_version": _project_positive_int(row.get("approved_matter_version"), "应诉材料包批准版本"),
        "locked_at": _project_optional_datetime(row.get("locked_at")),
        "exported_at": _project_optional_datetime(row.get("exported_at")),
        "created_at": _project_optional_datetime(row.get("created_at")),
        "export_profile": _project_text(row.get("export_profile"), "应诉导出配置", 80),
        "currency": _project_exact_text(row.get("currency"), "CNY", "应诉材料币种"),
        "input_hash": _project_hash(row.get("input_hash"), "应诉材料输入哈希"),
        "required_document_kinds": [_project_text(item, "必需文书类型", 80) for item in required],
        "evidence_manifest_id": _project_uuid(row.get("evidence_manifest_id"), "证据清单编号"),
        "evidence_manifest_hash": _project_hash(row.get("evidence_manifest_hash"), "证据清单哈希"),
        "legal_bundle_id": _project_uuid(row.get("legal_bundle_id"), "法律规则包编号"),
        "legal_bundle_hash": _project_hash(row.get("legal_bundle_hash"), "法律规则包哈希"),
        "calculation_run_id": _project_uuid(row.get("calculation_run_id"), "利息测算运行编号"),
        "calculation_output_hash": _project_hash(row.get("calculation_output_hash"), "利息测算输出哈希"),
        "final_text_approval_id": _project_uuid(row.get("final_text_approval_id"), "最终文本批准编号"),
        "qa_hash": _project_hash(row.get("qa_hash"), "应诉质量核对哈希"),
        "qa_approved_at": _project_optional_datetime(row.get("qa_approved_at")),
    }


def _submission_component_row(value: object) -> dict[str, object]:
    row = _submission_mapping(value, "应诉材料包组成")
    return {
        "work_product_id": _project_uuid(row.get("work_product_id"), "组成文书编号"),
        "sequence": _project_positive_int(row.get("sequence"), "组成顺序"),
        "document_kind": _project_text(row.get("document_kind"), "组成文书类型", 80),
        "court_filename": _project_text(row.get("court_filename"), "法院文件名", 255),
        "media_type": _project_exact_text(row.get("media_type"), "application/pdf", "组成文书格式"),
        "artifact_sha256": _project_hash(row.get("artifact_sha256"), "组成文书哈希"),
        "byte_size": _project_positive_int(row.get("byte_size"), "组成文书大小"),
    }


def _submission_export_row(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    row = _submission_mapping(value, "法院提交导出")
    return {
        "export_id": _project_uuid(row.get("export_id"), "法院导出编号"),
        "bundle_id": _project_uuid(row.get("bundle_id"), "导出材料包编号"),
        "input_hash": _project_hash(row.get("input_hash"), "导出输入哈希"),
        "court_zip_sha256": _project_hash(row.get("court_zip_sha256"), "法院压缩包哈希"),
        "court_zip_bytes": _project_positive_int(row.get("court_zip_bytes"), "法院压缩包大小"),
        "internal_manifest_sha256": _project_hash(row.get("internal_manifest_sha256"), "内部清单哈希"),
        "component_count": _project_positive_int(row.get("component_count"), "法院导出组成数量"),
        "verification_hash": _project_hash(row.get("verification_hash"), "法院导出核验哈希"),
        "verified_at": _project_optional_datetime(row.get("verified_at")),
        "created_at": _project_optional_datetime(row.get("created_at")),
    }
