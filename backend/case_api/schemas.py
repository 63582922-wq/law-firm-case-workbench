"""Transport schemas. They intentionally reject inputs that do not declare themselves synthetic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator
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
