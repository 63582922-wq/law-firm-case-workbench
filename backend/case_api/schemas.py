"""Transport schemas. They intentionally reject inputs that do not declare themselves synthetic."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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
