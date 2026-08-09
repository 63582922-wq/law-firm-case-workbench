"""Stable domain types. These types intentionally contain no AI or HTTP concerns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping
from uuid import uuid4


class Role(str, Enum):
    ASSISTANT = "ASSISTANT"
    COLLABORATING_LAWYER = "COLLABORATING_LAWYER"
    LEAD_LAWYER = "LEAD_LAWYER"
    REVIEWER = "REVIEWER"
    FIRM_ADMIN = "FIRM_ADMIN"
    SYSTEM_WORKER = "SYSTEM_WORKER"


class MatterStage(str, Enum):
    CREATED = "CREATED"
    INGESTING = "INGESTING"
    MATERIAL_REVIEW = "MATERIAL_REVIEW"
    FACT_REVIEW = "FACT_REVIEW"
    CLAIM_REVIEW = "CLAIM_REVIEW"
    LEGAL_REVIEW = "LEGAL_REVIEW"
    CALCULATION_REVIEW = "CALCULATION_REVIEW"
    DRAFT_REVIEW = "DRAFT_REVIEW"
    FINAL_QA = "FINAL_QA"
    READY_TO_EXPORT = "READY_TO_EXPORT"
    EXPORTED = "EXPORTED"
    ARCHIVED = "ARCHIVED"


class SubmissionLifecycle(str, Enum):
    DRAFT = "DRAFT"
    QA_READY = "QA_READY"
    LOCKED = "LOCKED"
    EXPORTED = "EXPORTED"


class SubmissionValidity(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    firm_id: str
    roles: frozenset[Role]


@dataclass
class SubmissionBundle:
    bundle_id: str
    lifecycle: SubmissionLifecycle
    validity: SubmissionValidity
    final_text_hash: str
    approved_by: str
    approved_matter_version: int


@dataclass(frozen=True)
class ApprovalBinding:
    approval_type: str
    object_hash: str
    approved_matter_version: int
    approved_by: str


@dataclass
class Matter:
    matter_id: str
    firm_id: str
    title: str
    stage: MatterStage = MatterStage.CREATED
    version: int = 1
    current_submission_bundle_id: str | None = None
    bundles: dict[str, SubmissionBundle] = field(default_factory=dict)
    approvals: dict[str, ApprovalBinding] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    matter_id: str
    firm_id: str
    actor_id: str
    event_type: str
    input_version: int
    output_version: int
    occurred_at: datetime
    payload: Mapping[str, str]

    @classmethod
    def create(
        cls,
        *,
        matter: Matter,
        actor: Actor,
        event_type: str,
        input_version: int,
        payload: Mapping[str, str],
    ) -> "AuditEvent":
        return cls(
            event_id=f"audit_{uuid4().hex}",
            matter_id=matter.matter_id,
            firm_id=matter.firm_id,
            actor_id=actor.actor_id,
            event_type=event_type,
            input_version=input_version,
            output_version=matter.version,
            occurred_at=datetime.now(timezone.utc),
            payload=dict(payload),
        )


@dataclass(frozen=True)
class CommandReceipt:
    command_name: str
    idempotency_key: str
    matter_id: str
    matter_version: int
    audit_event_id: str
