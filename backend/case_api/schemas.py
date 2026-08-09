"""Transport schemas. They intentionally reject inputs that do not declare themselves synthetic."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from uuid import UUID


class SyntheticGuardedModel(BaseModel):
    @staticmethod
    def _assert_synthetic(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} is required")
        return normalized


class CreateMatterRequest(SyntheticGuardedModel):
    matter_id: str = Field(pattern=r"^alpha_[a-z0-9_]{3,80}$")
    title: str = Field(min_length=5, max_length=160)

    @field_validator("title")
    @classmethod
    def title_must_be_synthetic(cls, value: str) -> str:
        value = cls._assert_synthetic(value, "title")
        if not value.startswith("[合成]"):
            raise ValueError("synthetic Alpha titles must start with [合成]")
        return value


class VersionedCommand(SyntheticGuardedModel):
    expected_version: int = Field(ge=1)


class ApprovalRequest(VersionedCommand):
    approval_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,60}$")
    approved_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LockSubmissionRequest(VersionedCommand):
    final_text: str = Field(min_length=12, max_length=20_000)

    @field_validator("final_text")
    @classmethod
    def text_must_be_synthetic(cls, value: str) -> str:
        value = cls._assert_synthetic(value, "final_text")
        if not value.startswith("[SYNTHETIC]"):
            raise ValueError("synthetic Alpha final text must start with [SYNTHETIC]")
        return value


class InvalidateRequest(VersionedCommand):
    change_kind: str = Field(pattern=r"^(SOURCE_CHANGED|FACT_CHANGED|CLAIM_CHANGED|LEGAL_RULE_CHANGED|CALCULATION_CHANGED|DRAFT_CHANGED)$")


class ReceiptResponse(BaseModel):
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    audit_event_id: str


class MatterResponse(BaseModel):
    matter_id: str
    title: str
    stage: str
    version: int
    current_submission_bundle_id: str | None


class HealthResponse(BaseModel):
    service: str
    mode: str
    persistence: str


class CalculationEventRequest(SyntheticGuardedModel):
    event_id: str = Field(pattern=r"^alpha_[a-z0-9_]{3,80}$")
    effective_date: date
    sequence: int = Field(ge=0)
    kind: Literal["DISBURSEMENT", "PAYMENT"]
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: Literal["CNY"]
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    approval_hash: str = Field(min_length=8, max_length=128)


class CalculationRuleSegmentRequest(SyntheticGuardedModel):
    segment_id: str = Field(pattern=r"^alpha_[a-z0-9_]{3,80}$")
    start_date: date
    end_date: date
    annual_rate: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=10)
    source_rule_version: str = Field(pattern=r"^SYNTHETIC-[A-Z0-9_-]{3,80}$")
    applicability_anchor: str = Field(min_length=3, max_length=80)
    approval_hash: str = Field(min_length=8, max_length=128)


class CalculationPreviewRequest(SyntheticGuardedModel):
    scenario_id: str = Field(pattern=r"^alpha_[a-z0-9_]{3,80}$")
    legal_bundle_id: str = Field(pattern=r"^alpha_[a-z0-9_]{3,80}$")
    version: int = Field(ge=1)
    start_date: date
    end_date: date
    events: list[CalculationEventRequest] = Field(min_length=1, max_length=100)
    rule_segments: list[CalculationRuleSegmentRequest] = Field(min_length=1, max_length=100)
    allocation_policy: Literal["INTEREST_THEN_PRINCIPAL", "PRINCIPAL_THEN_INTEREST"]
    approval_hash: str = Field(min_length=8, max_length=128)


class PaymentAllocationResponse(BaseModel):
    payment_event_id: str
    effective_date: date
    payment_amount: Decimal
    allocated_interest: Decimal
    allocated_principal: Decimal
    unapplied_amount: Decimal
    evidence_ids: tuple[str, ...]


class CalculationLineItemResponse(BaseModel):
    period_start: date
    period_end: date
    opening_principal: Decimal
    annual_rate: Decimal
    day_count: int
    accrued_interest: Decimal
    closing_principal: Decimal
    accrued_unpaid_interest: Decimal
    rule_segment_id: str
    source_rule_version: str
    evidence_ids: tuple[str, ...]


class CalculationPreviewResponse(BaseModel):
    run_id: str
    engine_version: str
    legal_bundle_id: str
    legal_bundle_hash: str
    input_hash: str
    output_hash: str
    independent_check_match: bool
    total_interest_accrued: Decimal
    total_interest_paid: Decimal
    remaining_principal: Decimal
    remaining_unpaid_interest: Decimal
    unapplied_payments: Decimal
    line_items: tuple[CalculationLineItemResponse, ...]
    payment_allocations: tuple[PaymentAllocationResponse, ...]


class FactReviewResponse(BaseModel):
    fact_id: str
    original_text: str
    origin: str
    evidence_count: int


class ClaimReviewResponse(BaseModel):
    claim_id: str
    original_claim_text: str
    claimed_amount: Decimal | None
    currency: str | None
    response_position: str
    response_amount: Decimal | None


class IssueReviewResponse(BaseModel):
    issue_id: str
    question: str
    claim_count: int
    fact_count: int


class TransactionReviewResponse(BaseModel):
    event_id: str
    effective_date: date
    kind: str
    amount: Decimal
    currency: str
    payment_application: str
    evidence_ids: tuple[str, ...]


class AlphaReviewResponse(BaseModel):
    mode: Literal["synthetic-alpha-only"]
    fact_snapshot_hash: str
    transaction_snapshot_hash: str
    facts: tuple[FactReviewResponse, ...]
    claims: tuple[ClaimReviewResponse, ...]
    issues: tuple[IssueReviewResponse, ...]
    transactions: tuple[TransactionReviewResponse, ...]
    pending_facts: tuple[AlphaFactCandidateResponse, ...]


class AlphaFactCandidateResponse(BaseModel):
    fact_id: str
    original_text: str
    origin: str
    evidence_count: int


class AlphaFactConfirmationRequest(SyntheticGuardedModel):
    approval_hash: str = Field(min_length=8, max_length=128)


class OriginalEvidenceLinkRequest(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    original_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1)
    region_id: str | None = Field(default=None, max_length=160)
    original_label: str = Field(min_length=1, max_length=240)


class PersistentFactCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    original_text: str = Field(min_length=1, max_length=10_000)
    origin: Literal["PLAINTIFF_PLEADING", "DEFENDANT_STATEMENT", "AGENT_CANDIDATE", "ASSISTANT_ENTRY"]
    evidence_links: list[OriginalEvidenceLinkRequest] = Field(min_length=1, max_length=30)


class PersistentFactDecisionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    status: Literal["CONFIRMED", "DISPUTED", "DENIED", "INVALIDATED"]
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CaseLedgerReceiptResponse(BaseModel):
    command_name: str
    idempotency_key: str
    matter_id: UUID
    matter_version: int
    audit_event_id: UUID
    object_type: str
    object_id: UUID


class PersistentFactResponse(BaseModel):
    fact_id: UUID
    original_text: str
    origin: str
    status: str
    evidence_count: int
    decision_hash: str | None


class PersistentClaimCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    original_claim_text: str = Field(min_length=1, max_length=10_000)
    claimed_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    evidence_links: list[OriginalEvidenceLinkRequest] = Field(min_length=1, max_length=30)

    @field_validator("currency")
    @classmethod
    def claim_currency_matches_amount(cls, value: str | None, info):
        amount = info.data.get("claimed_amount")
        if (amount is None) != (value is None):
            raise ValueError("claimed_amount and currency must be supplied together")
        return value


class PersistentApprovalRequest(BaseModel):
    expected_version: int = Field(ge=1)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PersistentClaimResponseRequest(PersistentApprovalRequest):
    position: Literal["ADMIT", "PARTIALLY_ADMIT", "DISPUTE", "OUTSIDE_SCOPE"]
    confirmed_fact_ids: list[UUID] = Field(min_length=1, max_length=200)
    partial_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class PersistentDisputeIssueCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=2_000)
    claim_ids: list[UUID] = Field(min_length=1, max_length=100)
    confirmed_fact_ids: list[UUID] = Field(min_length=1, max_length=200)


class PersistentTransactionCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    local_date: date | None = None
    date_precision: Literal["EXACT_DATE", "MONTH_ONLY", "YEAR_ONLY", "UNKNOWN"]
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    direction: Literal["OUTGOING", "INCOMING", "UNKNOWN"]
    payer_label: str | None = Field(default=None, max_length=500)
    payee_label: str | None = Field(default=None, max_length=500)
    channel: Literal["WECHAT", "BANK", "CASH", "CHAT_RECORD", "LOAN_INSTRUMENT", "OTHER"]
    transaction_reference: str | None = Field(default=None, max_length=500)
    evidence_links: list[OriginalEvidenceLinkRequest] = Field(min_length=1, max_length=30)


class PersistentConfirmationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    confirmation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PersistentObligationAllocationRequest(BaseModel):
    obligation_id: str = Field(min_length=1, max_length=160)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class PersistentPaymentClassificationCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    origin: Literal["PLAINTIFF_PLEADING", "DEFENDANT_STATEMENT", "AGENT_CANDIDATE", "ASSISTANT_ENTRY"]
    nature: Literal[
        "DISBURSEMENT",
        "REPAYMENT_UNSPECIFIED",
        "INTEREST_PAYMENT",
        "PRINCIPAL_REPAYMENT",
        "REFUND",
        "FEE",
        "UNRELATED",
    ]
    allocations: list[PersistentObligationAllocationRequest] = Field(default_factory=list, max_length=100)
    same_day_sequence: int | None = Field(default=None, ge=1)
    evidence_links: list[OriginalEvidenceLinkRequest] = Field(min_length=1, max_length=30)


class PersistentDuplicateGroupCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    transaction_ids: list[UUID] = Field(min_length=2, max_length=100)


class PersistentDuplicateGroupResolutionRequest(PersistentApprovalRequest):
    same_economic_event: bool
    canonical_transaction_id: UUID | None = None


class PersistentSnapshotFact(PersistentFactResponse):
    decided_by: UUID | None


class PersistentSnapshotClaimResponse(BaseModel):
    claim_response_id: UUID
    position: str
    partial_amount: Decimal | None
    currency: str | None
    confirmed_fact_ids: tuple[UUID, ...]
    approval_hash: str
    approved_by: UUID


class PersistentSnapshotClaim(BaseModel):
    claim_id: UUID
    original_claim_text: str
    claimed_amount: Decimal | None
    currency: str | None
    status: str
    evidence_count: int
    confirmation_hash: str | None
    confirmed_by: UUID | None
    response: PersistentSnapshotClaimResponse | None


class PersistentSnapshotIssue(BaseModel):
    issue_id: UUID
    question: str
    status: str
    claim_ids: tuple[UUID, ...]
    confirmed_fact_ids: tuple[UUID, ...]
    approval_hash: str | None
    approved_by: UUID | None


class PersistentSnapshotTransaction(BaseModel):
    transaction_id: UUID
    local_date: date | None
    date_precision: str
    amount: Decimal
    currency: str
    direction: str
    payer_label: str | None
    payee_label: str | None
    channel: str
    transaction_reference: str | None
    status: str
    evidence_count: int
    confirmation_hash: str | None
    confirmed_by: UUID | None


class PersistentSnapshotAllocation(BaseModel):
    obligation_id: str
    amount: Decimal
    currency: str


class PersistentSnapshotPaymentClassification(BaseModel):
    classification_id: UUID
    transaction_id: UUID
    origin: str
    nature: str
    same_day_sequence: int | None
    status: str
    evidence_count: int
    approval_hash: str | None
    approved_by: UUID | None
    allocations: tuple[PersistentSnapshotAllocation, ...]


class PersistentSnapshotDuplicateGroup(BaseModel):
    duplicate_group_id: UUID
    status: str
    canonical_transaction_id: UUID | None
    transaction_ids: tuple[UUID, ...]
    approval_hash: str | None
    approved_by: UUID | None


class PersistentCaseSnapshotResponse(BaseModel):
    matter_id: UUID
    title: str
    stage: str
    version: int
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts: tuple[PersistentSnapshotFact, ...]
    claims: tuple[PersistentSnapshotClaim, ...]
    issues: tuple[PersistentSnapshotIssue, ...]
    transactions: tuple[PersistentSnapshotTransaction, ...]
    payment_classifications: tuple[PersistentSnapshotPaymentClassification, ...]
    duplicate_groups: tuple[PersistentSnapshotDuplicateGroup, ...]


class PersistentEvidenceOriginalRequest(BaseModel):
    expected_version: int = Field(ge=1)
    original_label: str = Field(min_length=1, max_length=500)
    original_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1, le=2_147_483_648)
    media_type: str = Field(min_length=1, max_length=160)
    page_count: int = Field(ge=1, le=10_000)
    source_scan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    supersedes_file_id: UUID | None = None


class PersistentEvidencePageDecisionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    disposition: Literal["INCLUDE", "EXCLUDE"]
    reason: str = Field(min_length=1, max_length=2_000)


class PersistentEvidenceAnnotationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    x0: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=9)
    y0: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=9)
    x1: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=9)
    y1: Decimal = Field(ge=0, le=1, max_digits=12, decimal_places=9)
    label: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def coordinates_are_ordered(self):
        if not (self.x0 < self.x1 and self.y0 < self.y1):
            raise ValueError("annotation coordinates must form a positive-area rectangle")
        return self


class PersistentEvidenceDuplicateGroupRequest(BaseModel):
    expected_version: int = Field(ge=1)
    evidence_page_ids: list[UUID] = Field(min_length=2, max_length=200)


class PersistentEvidenceDuplicateResolutionRequest(PersistentApprovalRequest):
    same_source_page: bool
    canonical_page_id: UUID | None = None

    @model_validator(mode="after")
    def canonical_page_matches_resolution(self):
        if self.same_source_page and self.canonical_page_id is None:
            raise ValueError("same-source duplicate pages require a canonical page")
        if not self.same_source_page and self.canonical_page_id is not None:
            raise ValueError("distinct pages cannot select a canonical page")
        return self


class PersistentEvidenceDerivativeCandidateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    manifest_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_type: Literal["RELATED_PAGES_PDF", "ANNOTATED_RELATED_PAGES_PDF"]
    storage_object_key: str = Field(pattern=r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.lca$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1, le=10_000)


class PersistentEvidenceDerivativeVerificationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    verification_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PersistentEvidenceDerivativeRunRequest(BaseModel):
    expected_version: int = Field(ge=1)
    manifest_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PersistentEvidenceDerivativeRunClaimRequest(BaseModel):
    expected_version: int = Field(ge=1)
    lease_seconds: int = Field(default=120, ge=30, le=300)


class PersistentEvidenceDerivativeRunLeaseResponse(BaseModel):
    run_id: UUID
    lease_id: UUID
    matter_id: UUID
    manifest_id: UUID
    manifest_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_count: int = Field(ge=1, le=3)
    lease_expires_at: datetime
    matter_version: int = Field(ge=1)


class PersistentEvidenceDerivativeRunCompleteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    lease_id: UUID
    related_derivative_id: UUID
    annotated_derivative_id: UUID


class PersistentEvidenceDerivativeRunHeartbeatRequest(BaseModel):
    lease_id: UUID
    lease_seconds: int = Field(default=120, ge=30, le=300)


class PersistentEvidenceDerivativeRunHeartbeatResponse(BaseModel):
    run_id: UUID
    lease_expires_at: datetime


class PersistentEvidenceDerivativeRunFailureRequest(BaseModel):
    expected_version: int = Field(ge=1)
    lease_id: UUID
    failure_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,79}$")


class PersistentEvidenceDecisionSnapshot(BaseModel):
    decision_id: UUID
    disposition: str
    reason: str
    approval_hash: str
    approved_by: UUID


class PersistentEvidenceAnnotationSnapshot(BaseModel):
    annotation_id: UUID
    purpose: str
    x0: Decimal
    y0: Decimal
    x1: Decimal
    y1: Decimal
    label: str
    status: str
    approval_hash: str | None
    approved_by: UUID | None


class PersistentEvidencePageSnapshot(BaseModel):
    evidence_page_id: UUID
    evidence_file_id: UUID
    page_number: int
    rendered_page_sha256: str | None
    decision: PersistentEvidenceDecisionSnapshot | None
    annotations: tuple[PersistentEvidenceAnnotationSnapshot, ...]


class PersistentEvidenceOriginalSnapshot(BaseModel):
    evidence_file_id: UUID
    original_label: str
    original_file_sha256: str
    byte_size: int
    media_type: str
    page_count: int
    source_scan_fingerprint: str
    supersedes_file_id: UUID | None
    created_at: str


class PersistentEvidenceDuplicateGroupSnapshot(BaseModel):
    duplicate_group_id: UUID
    status: str
    canonical_page_id: UUID | None
    approval_hash: str | None
    approved_by: UUID | None
    evidence_page_ids: tuple[UUID, ...]


class PersistentEvidenceManifestEntrySnapshot(BaseModel):
    evidence_page_id: UUID
    decision_id: UUID
    disposition: str
    derivative_sequence: int | None


class PersistentEvidenceLockedManifestSnapshot(BaseModel):
    manifest_id: UUID
    ledger_version: int
    status: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_pages: int
    included_pages: int
    excluded_pages: int
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: UUID
    entries: tuple[PersistentEvidenceManifestEntrySnapshot, ...]


class PersistentEvidenceDerivativeSnapshot(BaseModel):
    derivative_id: UUID
    manifest_id: UUID
    artifact_type: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int
    status: str
    verification_hash: str | None
    verified_by: UUID | None
    verified_at: str | None


class PersistentEvidenceDerivativeRunSnapshot(BaseModel):
    run_id: UUID
    manifest_id: UUID
    manifest_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_matter_version: int = Field(ge=1)
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]
    attempt_count: int = Field(ge=0, le=3)
    failure_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,79}$")
    related_derivative_id: UUID | None
    annotated_derivative_id: UUID | None
    created_by: UUID
    created_at: str
    updated_at: str
    completed_at: str | None


class PersistentEvidenceSnapshotResponse(BaseModel):
    matter_id: UUID
    version: int
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_files: tuple[PersistentEvidenceOriginalSnapshot, ...]
    pages: tuple[PersistentEvidencePageSnapshot, ...]
    duplicate_groups: tuple[PersistentEvidenceDuplicateGroupSnapshot, ...]
    locked_manifest: PersistentEvidenceLockedManifestSnapshot | None
    derivatives: tuple[PersistentEvidenceDerivativeSnapshot, ...]
    derivative_runs: tuple[PersistentEvidenceDerivativeRunSnapshot, ...]


class PersistentArtifactAccessRequest(BaseModel):
    purpose: Literal["INLINE_PREVIEW", "DOWNLOAD"]


class PersistentArtifactAccessResponse(BaseModel):
    grant_id: UUID
    derivative_id: UUID
    purpose: Literal["INLINE_PREVIEW", "DOWNLOAD"]
    access_token: str = Field(min_length=20, max_length=200)
    expires_at: datetime
