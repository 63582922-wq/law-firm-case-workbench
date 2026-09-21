"""Evidence- and authority-bound dynamic case-work plans.

This module deliberately does not map a party position to a hard-coded list of
materials or documents.  A model may propose a strictly structured candidate,
but code accepts an actionable item only when its exact triggers and sources
exist, are current, belong to the matter, and match the supplied version/hash.
Unsupported ideas remain research/information gaps and can never become a
required court deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Callable
from uuid import UUID, uuid4

from .models import Actor, Role


class CaseWorkPlanBlocked(ValueError):
    """A work-plan candidate is not safe to register or activate."""


class WorkPlanItemKind(str, Enum):
    MATERIAL_REQUEST = "MATERIAL_REQUEST"
    RESEARCH_TASK = "RESEARCH_TASK"
    PROCEDURAL_TASK = "PROCEDURAL_TASK"
    CALCULATION = "CALCULATION"
    DOCUMENT_CANDIDATE = "DOCUMENT_CANDIDATE"
    REVIEW = "REVIEW"
    DEADLINE_RISK = "DEADLINE_RISK"


class WorkPlanReadiness(str, Enum):
    ACTIONABLE = "ACTIONABLE"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


class DeliveryTarget(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INTERNAL_WORK_PRODUCT = "INTERNAL_WORK_PRODUCT"
    CLIENT_DELIVERABLE = "CLIENT_DELIVERABLE"
    COURT_SUBMISSION = "COURT_SUBMISSION"


class ReviewGate(str, Enum):
    LEAD_LAWYER_CONFIRMATION = "LEAD_LAWYER_CONFIRMATION"
    EVIDENCE_REVIEW = "EVIDENCE_REVIEW"
    LEGAL_AUTHORITY_REVIEW = "LEGAL_AUTHORITY_REVIEW"
    PROCEDURE_REVIEW = "PROCEDURE_REVIEW"
    CALCULATION_REVIEW = "CALCULATION_REVIEW"


class WorkPlanSourceType(str, Enum):
    POSTURE_PROFILE = "POSTURE_PROFILE"
    LAWYER_OBJECTIVE = "LAWYER_OBJECTIVE"
    EVIDENCE_PAGE = "EVIDENCE_PAGE"
    EVIDENCE_MANIFEST = "EVIDENCE_MANIFEST"
    CASE_FACT = "CASE_FACT"
    CLAIM = "CLAIM"
    DISPUTE_ISSUE = "DISPUTE_ISSUE"
    TRANSACTION = "TRANSACTION"
    LEGAL_EVENT = "LEGAL_EVENT"
    LEGAL_RULE_VERSION = "LEGAL_RULE_VERSION"
    LEGAL_SOURCE_SNAPSHOT = "LEGAL_SOURCE_SNAPSHOT"
    LEGAL_BUNDLE = "LEGAL_BUNDLE"
    CALCULATION_RUN = "CALCULATION_RUN"
    COURT_PROCEEDING = "COURT_PROCEEDING"
    SERVICE_EVENT = "SERVICE_EVENT"
    PROCEDURAL_DEADLINE = "PROCEDURAL_DEADLINE"
    AGENT_GOAL = "AGENT_GOAL"
    AGENT_TASK_INPUT = "AGENT_TASK_INPUT"


class WorkPlanReferenceUse(str, Enum):
    POSTURE = "POSTURE"
    OBJECTIVE = "OBJECTIVE"
    CLAIM_SCOPE = "CLAIM_SCOPE"
    EVIDENCE = "EVIDENCE"
    FACT = "FACT"
    TRANSACTION = "TRANSACTION"
    COURT_EVENT = "COURT_EVENT"
    SERVICE_EVENT = "SERVICE_EVENT"
    DEADLINE = "DEADLINE"
    LEGAL_AUTHORITY = "LEGAL_AUTHORITY"
    LEGAL_RULE = "LEGAL_RULE"
    CALCULATION = "CALCULATION"
    MATERIAL = "MATERIAL"
    WORK_PLAN = "WORK_PLAN"


@dataclass(frozen=True)
class WorkPlanReference:
    source_type: WorkPlanSourceType
    source_id: str
    source_version: str
    source_hash: str
    use: WorkPlanReferenceUse


@dataclass(frozen=True)
class ResolvedWorkPlanReference:
    matter_id: str
    source_type: WorkPlanSourceType
    source_id: str
    source_version: str
    source_hash: str
    is_current: bool
    is_confirmed: bool
    is_effective: bool = True
    conflict_key: str | None = None


WorkPlanReferenceResolver = Callable[[WorkPlanReference], ResolvedWorkPlanReference | None]


@dataclass(frozen=True)
class PostureProfileRef:
    profile_id: str
    profile_version: int
    profile_hash: str

    def as_reference(self) -> WorkPlanReference:
        return WorkPlanReference(
            WorkPlanSourceType.POSTURE_PROFILE,
            self.profile_id,
            str(self.profile_version),
            self.profile_hash,
            WorkPlanReferenceUse.POSTURE,
        )


@dataclass(frozen=True)
class LawyerObjectiveRef:
    approval_id: str
    approved_matter_version: int
    objective_hash: str

    def as_reference(self) -> WorkPlanReference:
        return WorkPlanReference(
            WorkPlanSourceType.LAWYER_OBJECTIVE,
            self.approval_id,
            str(self.approved_matter_version),
            self.objective_hash,
            WorkPlanReferenceUse.OBJECTIVE,
        )


@dataclass(frozen=True)
class AgentGoalRef:
    """Immutable lawyer-created Agent goal used by a verified graph promotion.

    The goal is not relabelled as an ``approvals`` row.  The candidate plan
    remains subject to the existing lead-lawyer activation gate.
    """

    goal_id: str
    goal_hash: str

    def as_reference(self) -> WorkPlanReference:
        return WorkPlanReference(
            WorkPlanSourceType.AGENT_GOAL,
            self.goal_id,
            "v1",
            self.goal_hash,
            WorkPlanReferenceUse.OBJECTIVE,
        )


@dataclass(frozen=True)
class CaseWorkPlanContext:
    matter_id: str
    matter_version: int
    posture: PostureProfileRef
    confirmed_claim_refs: tuple[WorkPlanReference, ...]
    court_event_refs: tuple[WorkPlanReference, ...]
    service_event_refs: tuple[WorkPlanReference, ...]
    deadline_refs: tuple[WorkPlanReference, ...]
    legal_source_refs: tuple[WorkPlanReference, ...]
    legal_rule_refs: tuple[WorkPlanReference, ...]
    eligible_source_refs: tuple[WorkPlanReference, ...]
    objective: LawyerObjectiveRef | AgentGoalRef
    claim_scope_hash: str
    procedure_context_hash: str
    legal_context_hash: str

    @property
    def all_references(self) -> tuple[WorkPlanReference, ...]:
        return _unique_references(
            (
                self.posture.as_reference(),
                self.objective.as_reference(),
                *self.confirmed_claim_refs,
                *self.court_event_refs,
                *self.service_event_refs,
                *self.deadline_refs,
                *self.legal_source_refs,
                *self.legal_rule_refs,
                *self.eligible_source_refs,
            )
        )


@dataclass(frozen=True)
class CaseWorkPlanItem:
    item_id: str
    sequence: int
    kind: WorkPlanItemKind
    readiness: WorkPlanReadiness
    title: str
    purpose: str
    rationale: str
    prerequisites: tuple[str, ...]
    trigger_refs: tuple[WorkPlanReference, ...]
    source_refs: tuple[WorkPlanReference, ...]
    risk_if_omitted: str
    confidence: float
    review_gate: ReviewGate
    delivery_target: DeliveryTarget = DeliveryTarget.NOT_APPLICABLE
    deliverable_kind: str | None = None
    required_for_delivery: bool = False
    is_primary_document: bool = False


@dataclass(frozen=True)
class CaseWorkPlanCandidate:
    agent_id: str
    agent_version: str
    candidate_input_hash: str
    generated_at: datetime
    context: CaseWorkPlanContext
    items: tuple[CaseWorkPlanItem, ...]


@dataclass(frozen=True)
class ValidatedCaseWorkPlan:
    plan_id: str
    status: str
    context: CaseWorkPlanContext
    items: tuple[CaseWorkPlanItem, ...]
    context_hash: str
    plan_hash: str
    agent_id: str
    agent_version: str
    candidate_input_hash: str
    generated_at: datetime
    confirmed_by: str | None = None
    confirmation_hash: str | None = None

    @property
    def required_court_document_kinds(self) -> tuple[str, ...]:
        return tuple(
            item.deliverable_kind
            for item in self.items
            if item.kind is WorkPlanItemKind.DOCUMENT_CANDIDATE
            and item.readiness is WorkPlanReadiness.ACTIONABLE
            and item.delivery_target is DeliveryTarget.COURT_SUBMISSION
            and item.required_for_delivery
            and item.deliverable_kind is not None
        )

    @property
    def primary_court_document_kind(self) -> str | None:
        primary = tuple(
            item.deliverable_kind
            for item in self.items
            if item.is_primary_document and item.deliverable_kind is not None
        )
        return primary[0] if primary else None


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,119}$")
_MAX_ITEMS = 200
_MAX_REFS_PER_ITEM = 100


def build_case_work_plan_context(
    *,
    matter_id: str,
    matter_version: int,
    posture: PostureProfileRef,
    confirmed_claim_refs: tuple[WorkPlanReference, ...],
    court_event_refs: tuple[WorkPlanReference, ...] = (),
    service_event_refs: tuple[WorkPlanReference, ...] = (),
    deadline_refs: tuple[WorkPlanReference, ...] = (),
    legal_source_refs: tuple[WorkPlanReference, ...] = (),
    legal_rule_refs: tuple[WorkPlanReference, ...] = (),
    eligible_source_refs: tuple[WorkPlanReference, ...] = (),
    objective: LawyerObjectiveRef | AgentGoalRef,
) -> CaseWorkPlanContext:
    """Build exact context hashes from server-owned snapshot references."""

    claim_refs = _unique_references(confirmed_claim_refs)
    court_refs = _unique_references(court_event_refs)
    service_refs = _unique_references(service_event_refs)
    deadlines = _unique_references(deadline_refs)
    legal_sources = _unique_references(legal_source_refs)
    legal_rules = _unique_references(legal_rule_refs)
    eligible_sources = _unique_references(eligible_source_refs)
    return CaseWorkPlanContext(
        matter_id=matter_id,
        matter_version=matter_version,
        posture=posture,
        confirmed_claim_refs=claim_refs,
        court_event_refs=court_refs,
        service_event_refs=service_refs,
        deadline_refs=deadlines,
        legal_source_refs=legal_sources,
        legal_rule_refs=legal_rules,
        eligible_source_refs=eligible_sources,
        objective=objective,
        claim_scope_hash=_hash_payload({"confirmed_claim_refs": _refs_payload(claim_refs)}),
        procedure_context_hash=_hash_payload(
            {
                "court_event_refs": _refs_payload(court_refs),
                "service_event_refs": _refs_payload(service_refs),
                "deadline_refs": _refs_payload(deadlines),
            }
        ),
        legal_context_hash=_hash_payload(
            {
                "legal_source_refs": _refs_payload(legal_sources),
                "legal_rule_refs": _refs_payload(legal_rules),
            }
        ),
    )


def validate_case_work_plan_candidate(
    candidate: CaseWorkPlanCandidate,
    *,
    authoritative_context: CaseWorkPlanContext | None = None,
    resolve_reference: WorkPlanReferenceResolver,
    plan_id: str | None = None,
) -> ValidatedCaseWorkPlan:
    """Validate a model candidate without choosing litigation strategy.

    The function checks provenance, freshness, scope, conflicts, duplicates and
    dependency shape.  It never infers which documents a plaintiff, defendant
    or other represented party should file.
    """

    _require_text(candidate.agent_id, "agent_id", 200)
    _require_text(candidate.agent_version, "agent_version", 100)
    _require_sha256(candidate.candidate_input_hash, "candidate_input_hash")
    if candidate.generated_at.tzinfo is None:
        raise CaseWorkPlanBlocked("generated_at must include a timezone")
    context = candidate.context
    if authoritative_context is not None and context != authoritative_context:
        raise CaseWorkPlanBlocked(
            "model candidate context differs from the server-owned planning snapshot"
        )
    _require_uuid(context.matter_id, "matter_id")
    _require_positive(context.matter_version, "matter_version")
    _validate_context_hashes(context)
    _validate_reference_uses(context)
    resolved_context = tuple(
        _require_resolved_current_reference(
            reference, matter_id=context.matter_id, resolve_reference=resolve_reference
        )
        for reference in context.all_references
    )
    _validate_resolved_conflicts(resolved_context)
    expected_candidate_input_hash = case_work_plan_candidate_input_hash(context)
    if candidate.candidate_input_hash != expected_candidate_input_hash:
        raise CaseWorkPlanBlocked(
            "candidate input hash differs from the server-owned planning snapshot"
        )

    if not candidate.items or len(candidate.items) > _MAX_ITEMS:
        raise CaseWorkPlanBlocked("a work plan requires 1 to 200 items")
    item_ids: set[str] = set()
    sequences: set[int] = set()
    dedupe_keys: set[str] = set()
    required_deliverables: set[str] = set()
    primary_count = 0
    normalized_items: list[CaseWorkPlanItem] = []
    for item in sorted(candidate.items, key=lambda value: value.sequence):
        _require_uuid(item.item_id, "item_id")
        if item.item_id in item_ids:
            raise CaseWorkPlanBlocked("work plan item ids must be unique")
        item_ids.add(item.item_id)
        _require_positive(item.sequence, "item sequence")
        if item.sequence in sequences:
            raise CaseWorkPlanBlocked("work plan item sequence must be unique")
        sequences.add(item.sequence)
        title = _require_text(item.title, "item title", 500)
        purpose = _require_text(item.purpose, "item purpose", 2000)
        rationale = _require_text(item.rationale, "item rationale", 4000)
        risk = _require_text(item.risk_if_omitted, "risk_if_omitted", 2000)
        if not 0 <= item.confidence <= 1:
            raise CaseWorkPlanBlocked("item confidence must be between zero and one")
        triggers = _unique_references(item.trigger_refs)
        sources = _unique_references(item.source_refs)
        if not triggers or len(triggers) > _MAX_REFS_PER_ITEM or len(sources) > _MAX_REFS_PER_ITEM:
            raise CaseWorkPlanBlocked("each item needs bounded trigger references")
        for reference in (*triggers, *sources):
            _require_resolved_current_reference(
                reference, matter_id=context.matter_id, resolve_reference=resolve_reference
            )
        context_keys = {_ref_key(item) for item in context.all_references}
        if any(_ref_key(reference) not in context_keys for reference in triggers):
            raise CaseWorkPlanBlocked("item trigger is outside the confirmed plan context")
        if any(_ref_key(reference) not in context_keys for reference in sources):
            raise CaseWorkPlanBlocked("item source is outside the server planning snapshot")
        if item.readiness is WorkPlanReadiness.ACTIONABLE and not sources:
            raise CaseWorkPlanBlocked("an actionable item requires current source references")
        if item.readiness is not WorkPlanReadiness.ACTIONABLE and (
            item.required_for_delivery or item.is_primary_document
        ):
            raise CaseWorkPlanBlocked(
                "research or information gaps cannot become formal deliverables"
            )
        deliverable = item.deliverable_kind.strip() if item.deliverable_kind else None
        if deliverable is not None and _CODE.fullmatch(deliverable) is None:
            raise CaseWorkPlanBlocked("deliverable_kind must be a stable uppercase code")
        if item.kind is WorkPlanItemKind.DOCUMENT_CANDIDATE:
            if item.delivery_target is DeliveryTarget.NOT_APPLICABLE or deliverable is None:
                raise CaseWorkPlanBlocked("a document candidate needs a target and document kind")
        elif deliverable is not None or item.required_for_delivery or item.is_primary_document:
            raise CaseWorkPlanBlocked("only a document candidate may define a deliverable")
        _validate_item_support(item=replace(item, trigger_refs=triggers, source_refs=sources))
        if item.required_for_delivery:
            if item.delivery_target is not DeliveryTarget.COURT_SUBMISSION:
                raise CaseWorkPlanBlocked("only a court document enters the required submission set")
            assert deliverable is not None
            if deliverable in required_deliverables:
                raise CaseWorkPlanBlocked("required court document kinds must be unique")
            required_deliverables.add(deliverable)
        if item.is_primary_document:
            if not item.required_for_delivery:
                raise CaseWorkPlanBlocked("a primary document must be required for court delivery")
            primary_count += 1
        normalized = replace(
            item,
            title=title,
            purpose=purpose,
            rationale=rationale,
            risk_if_omitted=risk,
            prerequisites=tuple(dict.fromkeys(item.prerequisites)),
            trigger_refs=triggers,
            source_refs=sources,
            deliverable_kind=deliverable,
        )
        dedupe = _item_dedupe_key(normalized)
        if dedupe in dedupe_keys:
            raise CaseWorkPlanBlocked("work plan contains duplicate tasks or deliverables")
        dedupe_keys.add(dedupe)
        normalized_items.append(normalized)
    if primary_count > 1:
        raise CaseWorkPlanBlocked("a plan may identify at most one primary court document")
    _validate_prerequisites(tuple(normalized_items), item_ids)

    context_hash = _context_hash(context)
    plan_hash = _hash_payload(
        {
            "schema_version": "case-work-plan-v1",
            "agent_id": candidate.agent_id.strip(),
            "agent_version": candidate.agent_version.strip(),
            "candidate_input_hash": candidate.candidate_input_hash,
            "generated_at": candidate.generated_at,
            "context_hash": context_hash,
            "items": [_item_payload(item) for item in normalized_items],
        }
    )
    return ValidatedCaseWorkPlan(
        plan_id=plan_id or str(uuid4()),
        status="CANDIDATE",
        context=context,
        items=tuple(normalized_items),
        context_hash=context_hash,
        plan_hash=plan_hash,
        agent_id=candidate.agent_id.strip(),
        agent_version=candidate.agent_version.strip(),
        candidate_input_hash=candidate.candidate_input_hash,
        generated_at=candidate.generated_at,
    )


def activate_case_work_plan(
    plan: ValidatedCaseWorkPlan,
    *,
    actor: Actor,
    confirmation_hash: str,
) -> ValidatedCaseWorkPlan:
    if Role.LEAD_LAWYER not in actor.roles:
        raise PermissionError("only the lead lawyer may activate a case work plan")
    if plan.status != "CANDIDATE":
        raise CaseWorkPlanBlocked("only a candidate work plan may be activated")
    _require_sha256(confirmation_hash, "confirmation_hash")
    if confirmation_hash != plan.plan_hash:
        raise CaseWorkPlanBlocked("work-plan confirmation must bind the exact candidate hash")
    return replace(
        plan,
        status="ACTIVE",
        confirmed_by=actor.actor_id,
        confirmation_hash=confirmation_hash,
    )


def _validate_context_hashes(context: CaseWorkPlanContext) -> None:
    expected = build_case_work_plan_context(
        matter_id=context.matter_id,
        matter_version=context.matter_version,
        posture=context.posture,
        confirmed_claim_refs=context.confirmed_claim_refs,
        court_event_refs=context.court_event_refs,
        service_event_refs=context.service_event_refs,
        deadline_refs=context.deadline_refs,
        legal_source_refs=context.legal_source_refs,
        legal_rule_refs=context.legal_rule_refs,
        eligible_source_refs=context.eligible_source_refs,
        objective=context.objective,
    )
    for name in ("claim_scope_hash", "procedure_context_hash", "legal_context_hash"):
        supplied = getattr(context, name)
        _require_sha256(supplied, name)
        if supplied != getattr(expected, name):
            raise CaseWorkPlanBlocked(f"{name} differs from its exact references")


def _validate_reference_uses(context: CaseWorkPlanContext) -> None:
    if any(
        item.source_type is not WorkPlanSourceType.CLAIM
        or item.use is not WorkPlanReferenceUse.CLAIM_SCOPE
        for item in context.confirmed_claim_refs
    ):
        raise CaseWorkPlanBlocked("claim scope accepts only confirmed claim references")
    for refs, use, label in (
        (context.court_event_refs, WorkPlanReferenceUse.COURT_EVENT, "court event"),
        (context.service_event_refs, WorkPlanReferenceUse.SERVICE_EVENT, "service event"),
        (context.deadline_refs, WorkPlanReferenceUse.DEADLINE, "deadline"),
    ):
        if any(item.use is not use for item in refs):
            raise CaseWorkPlanBlocked(f"{label} context contains an incorrect reference use")
    if any(
        item.source_type is not WorkPlanSourceType.LEGAL_SOURCE_SNAPSHOT
        or item.use is not WorkPlanReferenceUse.LEGAL_AUTHORITY
        for item in context.legal_source_refs
    ):
        raise CaseWorkPlanBlocked("legal sources must be verified official-source references")
    if any(
        item.source_type is not WorkPlanSourceType.LEGAL_RULE_VERSION
        or item.use is not WorkPlanReferenceUse.LEGAL_RULE
        for item in context.legal_rule_refs
    ):
        raise CaseWorkPlanBlocked("legal rules must be approved rule-version references")


def case_work_plan_candidate_input_hash(context: CaseWorkPlanContext) -> str:
    """Derive the exact server planning snapshot a model candidate must bind."""

    return _hash_payload(
        {
            "schema_version": "case-work-plan-candidate-input-v1",
            "context_hash": _context_hash(context),
            "all_references": _refs_payload(context.all_references),
        }
    )


def _validate_item_support(*, item: CaseWorkPlanItem) -> None:
    """Apply capability-specific minimum support without mapping roles to outputs."""

    if item.readiness is not WorkPlanReadiness.ACTIONABLE:
        return
    references = (*item.trigger_refs, *item.source_refs)
    types = {reference.source_type for reference in references}
    uses = {reference.use for reference in references}
    if item.kind is WorkPlanItemKind.DOCUMENT_CANDIDATE and item.delivery_target is DeliveryTarget.COURT_SUBMISSION:
        required_uses = {
            WorkPlanReferenceUse.POSTURE,
            WorkPlanReferenceUse.CLAIM_SCOPE,
            WorkPlanReferenceUse.LEGAL_AUTHORITY,
            WorkPlanReferenceUse.LEGAL_RULE,
        }
        if not required_uses.issubset(uses):
            raise CaseWorkPlanBlocked(
                "an actionable court document requires posture, claim scope and current official law"
            )
        case_support_types = {
            WorkPlanSourceType.EVIDENCE_PAGE,
            WorkPlanSourceType.EVIDENCE_MANIFEST,
            WorkPlanSourceType.CASE_FACT,
            WorkPlanSourceType.DISPUTE_ISSUE,
            WorkPlanSourceType.TRANSACTION,
            WorkPlanSourceType.LEGAL_EVENT,
            WorkPlanSourceType.COURT_PROCEEDING,
            WorkPlanSourceType.SERVICE_EVENT,
        }
        if not types.intersection(case_support_types):
            raise CaseWorkPlanBlocked(
                "an actionable court document requires case evidence, fact or procedure support"
            )
    elif item.kind is WorkPlanItemKind.CALCULATION:
        if WorkPlanReferenceUse.LEGAL_RULE not in uses or not types.intersection(
            {WorkPlanSourceType.TRANSACTION, WorkPlanSourceType.CASE_FACT}
        ):
            raise CaseWorkPlanBlocked(
                "an actionable calculation requires an approved rule and confirmed transaction or fact"
            )
    elif item.kind in {WorkPlanItemKind.PROCEDURAL_TASK, WorkPlanItemKind.DEADLINE_RISK}:
        if not uses.intersection(
            {
                WorkPlanReferenceUse.COURT_EVENT,
                WorkPlanReferenceUse.SERVICE_EVENT,
                WorkPlanReferenceUse.DEADLINE,
            }
        ):
            raise CaseWorkPlanBlocked(
                "an actionable procedure item requires a current court, service or deadline event"
            )


def _require_resolved_current_reference(
    reference: WorkPlanReference,
    *,
    matter_id: str,
    resolve_reference: WorkPlanReferenceResolver,
) -> ResolvedWorkPlanReference:
    _require_uuid(reference.source_id, "reference source_id")
    _require_text(reference.source_version, "reference source_version", 200)
    _require_sha256(reference.source_hash, "reference source_hash")
    resolved = resolve_reference(reference)
    if resolved is None:
        raise CaseWorkPlanBlocked("work-plan reference does not exist")
    if resolved.matter_id != matter_id or resolved.source_type is not reference.source_type:
        raise CaseWorkPlanBlocked("work-plan reference is outside this matter")
    if (
        resolved.source_id != reference.source_id
        or resolved.source_version != reference.source_version
        or resolved.source_hash != reference.source_hash
    ):
        raise CaseWorkPlanBlocked("work-plan reference version or hash changed")
    if not resolved.is_current or not resolved.is_confirmed or not resolved.is_effective:
        raise CaseWorkPlanBlocked("work-plan reference is stale, unconfirmed or not effective")
    return resolved


def _validate_resolved_conflicts(
    references: tuple[ResolvedWorkPlanReference, ...],
) -> None:
    selected: dict[str, tuple[WorkPlanSourceType, str, str]] = {}
    for reference in references:
        if reference.conflict_key is None:
            continue
        _require_text(reference.conflict_key, "reference conflict_key", 200)
        identity = (reference.source_type, reference.source_id, reference.source_version)
        prior = selected.setdefault(reference.conflict_key, identity)
        if prior != identity:
            raise CaseWorkPlanBlocked(
                "planning snapshot contains unresolved conflicting source selections"
            )


def _validate_prerequisites(items: tuple[CaseWorkPlanItem, ...], item_ids: set[str]) -> None:
    edges: dict[str, tuple[str, ...]] = {}
    for item in items:
        for prerequisite in item.prerequisites:
            _require_uuid(prerequisite, "prerequisite item id")
            if prerequisite == item.item_id or prerequisite not in item_ids:
                raise CaseWorkPlanBlocked("item prerequisite is missing or self-referential")
        edges[item.item_id] = item.prerequisites
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise CaseWorkPlanBlocked("work plan prerequisites contain a cycle")
        if item_id in visited:
            return
        visiting.add(item_id)
        for prerequisite in edges[item_id]:
            visit(prerequisite)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in edges:
        visit(item_id)


def _context_hash(context: CaseWorkPlanContext) -> str:
    return _hash_payload(
        {
            "matter_id": context.matter_id,
            "matter_version": context.matter_version,
            "posture": _reference_payload(context.posture.as_reference()),
            "claim_scope_hash": context.claim_scope_hash,
            "procedure_context_hash": context.procedure_context_hash,
            "legal_context_hash": context.legal_context_hash,
            "objective": _reference_payload(context.objective.as_reference()),
            "eligible_source_refs": _refs_payload(context.eligible_source_refs),
        }
    )


def _item_payload(item: CaseWorkPlanItem) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "sequence": item.sequence,
        "kind": item.kind.value,
        "readiness": item.readiness.value,
        "title": item.title,
        "purpose": item.purpose,
        "rationale": item.rationale,
        "prerequisites": item.prerequisites,
        "trigger_refs": _refs_payload(item.trigger_refs),
        "source_refs": _refs_payload(item.source_refs),
        "risk_if_omitted": item.risk_if_omitted,
        "confidence": format(item.confidence, ".8f"),
        "review_gate": item.review_gate.value,
        "delivery_target": item.delivery_target.value,
        "deliverable_kind": item.deliverable_kind,
        "required_for_delivery": item.required_for_delivery,
        "is_primary_document": item.is_primary_document,
    }


def _item_dedupe_key(item: CaseWorkPlanItem) -> str:
    return _hash_payload(
        {
            "kind": item.kind.value,
            "title": " ".join(item.title.casefold().split()),
            "delivery_target": item.delivery_target.value,
            "deliverable_kind": item.deliverable_kind,
            "trigger_refs": _refs_payload(item.trigger_refs),
            "source_refs": _refs_payload(item.source_refs),
        }
    )


def _unique_references(references: tuple[WorkPlanReference, ...]) -> tuple[WorkPlanReference, ...]:
    by_key: dict[tuple[str, str, str, str, str], WorkPlanReference] = {}
    for reference in references:
        by_key[_ref_key(reference)] = reference
    return tuple(by_key[key] for key in sorted(by_key))


def _ref_key(reference: WorkPlanReference) -> tuple[str, str, str, str, str]:
    return (
        reference.source_type.value,
        reference.source_id,
        reference.source_version,
        reference.source_hash,
        reference.use.value,
    )


def _reference_payload(reference: WorkPlanReference) -> dict[str, str]:
    return {
        "source_type": reference.source_type.value,
        "source_id": reference.source_id,
        "source_version": reference.source_version,
        "source_hash": reference.source_hash,
        "use": reference.use.value,
    }


def _refs_payload(references: tuple[WorkPlanReference, ...]) -> list[dict[str, str]]:
    return [_reference_payload(item) for item in _unique_references(references)]


def _hash_payload(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        ).encode("utf-8")
    ).hexdigest()


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseWorkPlanBlocked(f"{label} must be a UUID") from error


def _require_sha256(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value or "") is None:
        raise CaseWorkPlanBlocked(f"{label} must be a lowercase SHA-256")


def _require_positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CaseWorkPlanBlocked(f"{label} must be positive")


def _require_text(value: str, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise CaseWorkPlanBlocked(f"{label} is missing or too long")
    return value.strip()
