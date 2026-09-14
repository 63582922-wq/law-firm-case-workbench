"""Authoritative, server-only planning snapshots for the case Agent.

The planner must not assemble its own view of a matter by calling several
repositories independently.  That can mix facts from one matter version with
evidence or legal authority from another.  This module therefore defines one
atomic repository contract and a pure provider that turns the returned,
metadata-only projection into :class:`CasePlanningSnapshot`.

The repository implementation is expected to read the existing case ledger,
evidence ledger, confirmed posture profile, active dynamic work plan and
reviewed legal/procedure ledgers in one PostgreSQL ``REPEATABLE READ``
transaction.  The opening and closing snapshot fences are both verified
against the exact snapshot captured when the Agent run was created.

No type in this module has a field for document text, a filesystem path, an
object-store key, a URL or an executable command.  The model receives only
opaque server-owned identifiers, versions, hashes, status and bounded status
signals.  A Skill is exposed only when an actual registered adapter instance
is injected with an explicit input-type capability declaration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Protocol
from uuid import UUID

from .case_agent_planner import (
    CasePlannerBlocked,
    CasePlanningSignal,
    CasePlanningSnapshot,
    PlanningInputKind,
    PlanningInputRef,
    PlanningInputStatus,
    PlanningSignalCategory,
    ReextractionPlanningObligation,
)
from .case_agent_supervisor import (
    AgentRunState,
    AgentSupervisorBlocked,
    CaseSnapshotRef,
    RuntimeAdapterManifest,
)
from .models import Actor, Role


class CasePlanningProjectionBlocked(RuntimeError):
    """The authoritative projection is unavailable, stale or not executable."""


class PlanningProjectionObjectType(StrEnum):
    """Stable server object types accepted by the planning projection.

    These are persistence entities, not model-selected capabilities.  Their
    opaque prefixes remain stable because task materialization resolves them
    through server-side bindings.
    """

    MATERIAL_OBJECT = "MATERIAL_OBJECT"
    EVIDENCE_PAGE = "EVIDENCE_PAGE"
    CASE_FACT = "CASE_FACT"
    CASE_CLAIM = "CASE_CLAIM"
    DISPUTE_ISSUE = "DISPUTE_ISSUE"
    CASE_TRANSACTION = "CASE_TRANSACTION"
    POSTURE_PROFILE = "POSTURE_PROFILE"
    WORK_PLAN_ITEM = "WORK_PLAN_ITEM"
    VERIFIED_LEGAL_SOURCE = "VERIFIED_LEGAL_SOURCE"
    APPROVED_LEGAL_RULE = "APPROVED_LEGAL_RULE"
    PROCEDURAL_EVENT = "PROCEDURAL_EVENT"
    REVIEW_OBLIGATION = "REVIEW_OBLIGATION"
    TRANSACTION_CANDIDATE = "TRANSACTION_CANDIDATE"
    FACT_CANDIDATE = "FACT_CANDIDATE"


class ProjectionSectionState(StrEnum):
    """Whether an authoritative optional ledger was read in the transaction."""

    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    NOT_CONFIGURED = "NOT_CONFIGURED"


_REF_PREFIX: dict[PlanningProjectionObjectType, str] = {
    PlanningProjectionObjectType.MATERIAL_OBJECT: "material-object",
    PlanningProjectionObjectType.EVIDENCE_PAGE: "evidence-page",
    PlanningProjectionObjectType.CASE_FACT: "fact",
    PlanningProjectionObjectType.CASE_CLAIM: "claim",
    PlanningProjectionObjectType.DISPUTE_ISSUE: "issue",
    PlanningProjectionObjectType.CASE_TRANSACTION: "transaction",
    PlanningProjectionObjectType.POSTURE_PROFILE: "posture-profile",
    PlanningProjectionObjectType.WORK_PLAN_ITEM: "work-plan-item",
    PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE: "legal-source",
    PlanningProjectionObjectType.APPROVED_LEGAL_RULE: "legal-rule",
    PlanningProjectionObjectType.PROCEDURAL_EVENT: "legal-event",
    PlanningProjectionObjectType.REVIEW_OBLIGATION: "review-obligation",
    PlanningProjectionObjectType.TRANSACTION_CANDIDATE: "transaction-candidate",
    PlanningProjectionObjectType.FACT_CANDIDATE: "fact-candidate",
}


# PlanningInputKind is a bounded capability category rather than a persistence
# entity enum.  Prefixes above preserve the exact source entity where a claim
# or issue has to share a capability category with an existing planner kind.
_INPUT_KIND: dict[PlanningProjectionObjectType, PlanningInputKind] = {
    PlanningProjectionObjectType.MATERIAL_OBJECT: PlanningInputKind.MATERIAL,
    PlanningProjectionObjectType.EVIDENCE_PAGE: PlanningInputKind.EVIDENCE_PAGE,
    PlanningProjectionObjectType.CASE_FACT: PlanningInputKind.CONFIRMED_FACT,
    PlanningProjectionObjectType.CASE_CLAIM: PlanningInputKind.WORK_PLAN_ITEM,
    PlanningProjectionObjectType.DISPUTE_ISSUE: PlanningInputKind.LEGAL_GAP,
    PlanningProjectionObjectType.CASE_TRANSACTION: PlanningInputKind.CONFIRMED_TRANSACTION,
    PlanningProjectionObjectType.POSTURE_PROFILE: PlanningInputKind.PROCEDURAL_EVENT,
    PlanningProjectionObjectType.WORK_PLAN_ITEM: PlanningInputKind.WORK_PLAN_ITEM,
    PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE: PlanningInputKind.VERIFIED_SOURCE,
    PlanningProjectionObjectType.APPROVED_LEGAL_RULE: PlanningInputKind.VERIFIED_SOURCE,
    PlanningProjectionObjectType.PROCEDURAL_EVENT: PlanningInputKind.PROCEDURAL_EVENT,
    PlanningProjectionObjectType.REVIEW_OBLIGATION: PlanningInputKind.LEGAL_GAP,
    PlanningProjectionObjectType.TRANSACTION_CANDIDATE: PlanningInputKind.LEGAL_GAP,
    PlanningProjectionObjectType.FACT_CANDIDATE: PlanningInputKind.LEGAL_GAP,
}


_NEUTRAL_OBJECT_TYPES = frozenset(
    {
        PlanningProjectionObjectType.MATERIAL_OBJECT,
        PlanningProjectionObjectType.EVIDENCE_PAGE,
    }
)


@dataclass(frozen=True)
class AuthoritativePlanningObject:
    """One current metadata-only object returned by the atomic repository."""

    object_type: PlanningProjectionObjectType
    object_id: str
    object_version: str
    content_hash: str
    status: PlanningInputStatus
    source_media_type: str | None = None
    extraction_complete: bool | None = None

    @property
    def ref_id(self) -> str:
        return planning_object_ref_id(self.object_type, self.object_id)

    def validate(self) -> None:
        _uuid(self.object_id, "planning object_id")
        _code(self.object_version, "planning object_version")
        _hash(self.content_hash, "planning object content_hash")
        if not isinstance(self.object_type, PlanningProjectionObjectType):
            raise CasePlanningProjectionBlocked("planning object type is invalid")
        if not isinstance(self.status, PlanningInputStatus):
            raise CasePlanningProjectionBlocked("planning object status is invalid")
        if self.extraction_complete is not None and (
            type(self.extraction_complete) is not bool
            or self.object_type is not PlanningProjectionObjectType.EVIDENCE_PAGE
        ):
            raise CasePlanningProjectionBlocked("extraction coverage is only valid for evidence pages")
        if self.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE:
            if self.source_media_type not in {
                "application/pdf",
                "image/jpeg",
                "image/png",
            }:
                raise CasePlanningProjectionBlocked(
                    "evidence-page planning object media type is invalid"
                )
        elif self.source_media_type is not None:
            raise CasePlanningProjectionBlocked(
                "non-evidence planning object cannot declare source media type"
            )


@dataclass(frozen=True)
class ConfirmedPostureProjection:
    """Bounded non-PII fields from the current lawyer-confirmed posture."""

    profile_id: str
    profile_version: str
    profile_hash: str
    effective_status: str
    case_type_code: str
    procedure_stage: str
    represented_position: str
    authority_scope_code: str
    engagement_state: str

    def validate(self) -> None:
        _uuid(self.profile_id, "posture profile_id")
        _code(self.profile_version, "posture profile_version")
        _hash(self.profile_hash, "posture profile_hash")
        if self.effective_status != "CURRENT":
            raise CasePlanningProjectionBlocked(
                "planning may use only the current confirmed posture profile"
            )
        for label, value in (
            ("case_type_code", self.case_type_code),
            ("procedure_stage", self.procedure_stage),
            ("represented_position", self.represented_position),
            ("authority_scope_code", self.authority_scope_code),
            ("engagement_state", self.engagement_state),
        ):
            _code(value, label)


@dataclass(frozen=True)
class ActiveDynamicWorkPlanProjection:
    """The active plan identity and bounded readiness counts, never its prose."""

    plan_id: str
    plan_version: str
    plan_hash: str
    bound_matter_version: int
    item_ids: tuple[str, ...]
    actionable_count: int
    needs_information_count: int
    needs_research_count: int

    def validate(self) -> None:
        _uuid(self.plan_id, "work plan_id")
        _code(self.plan_version, "work plan_version")
        _hash(self.plan_hash, "work plan_hash")
        if self.bound_matter_version < 1:
            raise CasePlanningProjectionBlocked("work plan matter version is invalid")
        if not self.item_ids or len(self.item_ids) > 500:
            raise CasePlanningProjectionBlocked("active work plan has an invalid item set")
        if len(self.item_ids) != len(set(self.item_ids)):
            raise CasePlanningProjectionBlocked("active work plan item ids are duplicated")
        for item_id in self.item_ids:
            _uuid(item_id, "work plan item_id")
        counts = (
            self.actionable_count,
            self.needs_information_count,
            self.needs_research_count,
        )
        if any(value < 0 for value in counts) or sum(counts) != len(self.item_ids):
            raise CasePlanningProjectionBlocked(
                "work plan readiness counts differ from its item set"
            )


@dataclass(frozen=True)
class GovernedLawyerPlanningSignal:
    """A current structured lawyer correction, rejection or explicit gap.

    The repository may project this only from a governed lawyer-decision
    ledger.  ``summary`` is bounded case data for planning, not an executable
    instruction.  The decision hash is embedded in the resulting planning
    signal identity so any lawyer revision produces a different planning hash.
    """

    signal_id: str
    signal_version: str
    decision_hash: str
    category: PlanningSignalCategory
    code: str
    status: PlanningInputStatus
    summary: str
    source_ref_ids: tuple[str, ...]

    def validate(self, *, authorized_refs: frozenset[str]) -> None:
        _uuid(self.signal_id, "lawyer planning signal_id")
        _code(self.signal_version, "lawyer planning signal_version")
        _hash(self.decision_hash, "lawyer planning decision_hash")
        if not isinstance(self.category, PlanningSignalCategory):
            raise CasePlanningProjectionBlocked(
                "lawyer planning signal category is invalid"
            )
        _code(self.code, "lawyer planning signal code")
        if not self.code.startswith("LAWYER_"):
            raise CasePlanningProjectionBlocked(
                "governed lawyer planning signal code must identify its provenance"
            )
        if self.status not in {
            PlanningInputStatus.CONFIRMED,
            PlanningInputStatus.DISPUTED,
            PlanningInputStatus.OPEN,
            PlanningInputStatus.BLOCKED,
        }:
            raise CasePlanningProjectionBlocked(
                "governed lawyer signal must be a decided correction, rejection or gap"
            )
        _text(self.summary, "lawyer planning signal summary", 1_000)
        if not self.source_ref_ids or len(self.source_ref_ids) > 100:
            raise CasePlanningProjectionBlocked(
                "lawyer planning signal requires bounded authoritative sources"
            )
        if len(self.source_ref_ids) != len(set(self.source_ref_ids)):
            raise CasePlanningProjectionBlocked(
                "lawyer planning signal source references are duplicated"
            )
        for ref_id in self.source_ref_ids:
            _ref(ref_id, "lawyer planning signal source reference")
        if not set(self.source_ref_ids).issubset(authorized_refs):
            raise CasePlanningProjectionBlocked(
                "lawyer planning signal cites an object outside this projection"
            )


@dataclass(frozen=True)
class AuthoritativeCasePlanningProjection:
    """One metadata-only result from a single authoritative read transaction."""

    firm_id: str
    matter_id: str
    opening_case_snapshot: CaseSnapshotRef
    closing_case_snapshot: CaseSnapshotRef
    objects: tuple[AuthoritativePlanningObject, ...]
    posture_state: ProjectionSectionState
    posture: ConfirmedPostureProjection | None
    work_plan_state: ProjectionSectionState
    active_work_plan: ActiveDynamicWorkPlanProjection | None
    legal_state: ProjectionSectionState
    procedure_state: ProjectionSectionState
    lawyer_signals: tuple[GovernedLawyerPlanningSignal, ...]
    reextraction_obligations: tuple[ReextractionPlanningObligation, ...]
    projection_hash: str

    @classmethod
    def build(
        cls,
        *,
        firm_id: str,
        matter_id: str,
        opening_case_snapshot: CaseSnapshotRef,
        closing_case_snapshot: CaseSnapshotRef,
        objects: Iterable[AuthoritativePlanningObject],
        posture_state: ProjectionSectionState,
        posture: ConfirmedPostureProjection | None,
        work_plan_state: ProjectionSectionState,
        active_work_plan: ActiveDynamicWorkPlanProjection | None,
        legal_state: ProjectionSectionState,
        procedure_state: ProjectionSectionState,
        lawyer_signals: Iterable[GovernedLawyerPlanningSignal] = (),
        reextraction_obligations: Iterable[ReextractionPlanningObligation] = (),
    ) -> "AuthoritativeCasePlanningProjection":
        object_items = tuple(sorted(tuple(objects), key=lambda item: item.ref_id))
        signal_items = tuple(
            sorted(tuple(lawyer_signals), key=lambda item: (item.signal_id, item.signal_version))
        )
        obligations = tuple(
            sorted(
                tuple(reextraction_obligations),
                key=lambda item: item.obligation_id,
            )
        )
        payload = _projection_payload(
            firm_id=firm_id,
            matter_id=matter_id,
            opening_case_snapshot=opening_case_snapshot,
            closing_case_snapshot=closing_case_snapshot,
            objects=object_items,
            posture_state=posture_state,
            posture=posture,
            work_plan_state=work_plan_state,
            active_work_plan=active_work_plan,
            legal_state=legal_state,
            procedure_state=procedure_state,
            lawyer_signals=signal_items,
            reextraction_obligations=obligations,
        )
        result = cls(
            firm_id=firm_id,
            matter_id=matter_id,
            opening_case_snapshot=opening_case_snapshot,
            closing_case_snapshot=closing_case_snapshot,
            objects=object_items,
            posture_state=posture_state,
            posture=posture,
            work_plan_state=work_plan_state,
            active_work_plan=active_work_plan,
            legal_state=legal_state,
            procedure_state=procedure_state,
            lawyer_signals=signal_items,
            reextraction_obligations=obligations,
            projection_hash=_canonical_hash(payload),
        )
        result._validate_shape()
        return result

    def validate(self) -> None:
        self._validate_shape()
        expected = _canonical_hash(
            _projection_payload(
                firm_id=self.firm_id,
                matter_id=self.matter_id,
                opening_case_snapshot=self.opening_case_snapshot,
                closing_case_snapshot=self.closing_case_snapshot,
                objects=self.objects,
                posture_state=self.posture_state,
                posture=self.posture,
                work_plan_state=self.work_plan_state,
                active_work_plan=self.active_work_plan,
                legal_state=self.legal_state,
                procedure_state=self.procedure_state,
                lawyer_signals=self.lawyer_signals,
                reextraction_obligations=self.reextraction_obligations,
            )
        )
        if self.projection_hash != expected:
            raise CasePlanningProjectionBlocked(
                "authoritative planning projection hash differs from its contents"
            )

    def _validate_shape(self) -> None:
        _uuid(self.firm_id, "projection firm_id")
        _uuid(self.matter_id, "projection matter_id")
        try:
            self.opening_case_snapshot.validate()
            self.closing_case_snapshot.validate()
        except AgentSupervisorBlocked as error:
            raise CasePlanningProjectionBlocked("case snapshot fence is invalid") from error
        if (
            self.opening_case_snapshot.matter_id != self.matter_id
            or self.closing_case_snapshot.matter_id != self.matter_id
        ):
            raise CasePlanningProjectionBlocked("snapshot fence belongs to another matter")
        if len(self.objects) > 10_000:
            raise CasePlanningProjectionBlocked("repository projection is unreasonably large")
        refs: set[str] = set()
        for item in self.objects:
            item.validate()
            if item.ref_id in refs:
                raise CasePlanningProjectionBlocked("planning projection has duplicate objects")
            refs.add(item.ref_id)
        if tuple(sorted(self.objects, key=lambda item: item.ref_id)) != self.objects:
            raise CasePlanningProjectionBlocked("planning projection objects are not canonical")
        for state in (
            self.posture_state,
            self.work_plan_state,
            self.legal_state,
            self.procedure_state,
        ):
            if not isinstance(state, ProjectionSectionState):
                raise CasePlanningProjectionBlocked("projection section state is invalid")
        self._validate_posture(refs)
        self._validate_work_plan(refs)
        self._validate_legal_and_procedure_sections()
        self._validate_lawyer_signals(frozenset(refs))
        self._validate_reextraction_obligations(frozenset(refs))
        _hash(self.projection_hash, "projection_hash")

    def _validate_posture(self, refs: set[str]) -> None:
        posture_objects = tuple(
            item
            for item in self.objects
            if item.object_type is PlanningProjectionObjectType.POSTURE_PROFILE
        )
        if self.posture_state is ProjectionSectionState.AVAILABLE:
            if self.posture is None or len(posture_objects) != 1:
                raise CasePlanningProjectionBlocked(
                    "available posture section requires exactly one current profile"
                )
            self.posture.validate()
            expected_ref = planning_object_ref_id(
                PlanningProjectionObjectType.POSTURE_PROFILE, self.posture.profile_id
            )
            if expected_ref not in refs:
                raise CasePlanningProjectionBlocked("current posture profile object is missing")
            item = posture_objects[0]
            if (
                item.object_id != self.posture.profile_id
                or item.object_version != self.posture.profile_version
                or item.content_hash != self.posture.profile_hash
                or item.status is not PlanningInputStatus.CONFIRMED
            ):
                raise CasePlanningProjectionBlocked(
                    "posture profile metadata differs from its authoritative object"
                )
        elif self.posture is not None or posture_objects:
            raise CasePlanningProjectionBlocked(
                "empty or unconfigured posture section cannot contain a profile"
            )

    def _validate_work_plan(self, refs: set[str]) -> None:
        plan_objects = tuple(
            item
            for item in self.objects
            if item.object_type is PlanningProjectionObjectType.WORK_PLAN_ITEM
        )
        if self.work_plan_state is ProjectionSectionState.AVAILABLE:
            if self.active_work_plan is None:
                raise CasePlanningProjectionBlocked(
                    "available work-plan section requires an active plan"
                )
            self.active_work_plan.validate()
            if self.active_work_plan.bound_matter_version != self.opening_case_snapshot.matter_version:
                raise CasePlanningProjectionBlocked(
                    "active work plan is stale against the planning snapshot"
                )
            expected = {
                planning_object_ref_id(PlanningProjectionObjectType.WORK_PLAN_ITEM, item_id)
                for item_id in self.active_work_plan.item_ids
            }
            actual = {item.ref_id for item in plan_objects}
            if expected != actual or not expected.issubset(refs):
                raise CasePlanningProjectionBlocked(
                    "active work plan item set differs from its authoritative objects"
                )
        elif self.active_work_plan is not None or plan_objects:
            raise CasePlanningProjectionBlocked(
                "empty or unconfigured work-plan section cannot contain active items"
            )

    def _validate_legal_and_procedure_sections(self) -> None:
        sources = tuple(
            item
            for item in self.objects
            if item.object_type is PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE
        )
        events = tuple(
            item
            for item in self.objects
            if item.object_type is PlanningProjectionObjectType.PROCEDURAL_EVENT
        )
        _validate_optional_section(
            name="legal",
            state=self.legal_state,
            items=sources,
            allowed_statuses=frozenset(
                {PlanningInputStatus.CONFIRMED, PlanningInputStatus.LOCKED}
            ),
        )
        _validate_optional_section(
            name="procedure",
            state=self.procedure_state,
            items=events,
            allowed_statuses=frozenset({PlanningInputStatus.CONFIRMED}),
        )

    def _validate_lawyer_signals(self, authorized_refs: frozenset[str]) -> None:
        if len(self.lawyer_signals) > 100:
            raise CasePlanningProjectionBlocked(
                "authoritative projection has too many lawyer decision signals"
            )
        identities: set[tuple[str, str]] = set()
        for item in self.lawyer_signals:
            if not isinstance(item, GovernedLawyerPlanningSignal):
                raise CasePlanningProjectionBlocked(
                    "authoritative lawyer decision signal is invalid"
                )
            item.validate(authorized_refs=authorized_refs)
            identity = (item.signal_id, item.signal_version)
            if identity in identities:
                raise CasePlanningProjectionBlocked(
                    "authoritative lawyer decision signal is duplicated"
                )
            identities.add(identity)
        if (
            tuple(
                sorted(
                    self.lawyer_signals,
                    key=lambda item: (item.signal_id, item.signal_version),
                )
            )
            != self.lawyer_signals
        ):
            raise CasePlanningProjectionBlocked(
                "authoritative lawyer decision signals are not canonical"
            )

    def _validate_reextraction_obligations(
        self, authorized_refs: frozenset[str]
    ) -> None:
        if len(self.reextraction_obligations) > 500:
            raise CasePlanningProjectionBlocked(
                "authoritative projection has too many re-extraction obligations"
            )
        identities: set[str] = set()
        source_sets: set[tuple[str, ...]] = set()
        for item in self.reextraction_obligations:
            if not isinstance(item, ReextractionPlanningObligation):
                raise CasePlanningProjectionBlocked(
                    "authoritative re-extraction obligation is invalid"
                )
            try:
                item.validate()
            except CasePlannerBlocked as error:
                raise CasePlanningProjectionBlocked(
                    "authoritative re-extraction obligation has invalid shape"
                ) from error
            if item.obligation_id in identities or item.source_ref_ids in source_sets:
                raise CasePlanningProjectionBlocked(
                    "authoritative re-extraction obligations are not exact cohorts"
                )
            identities.add(item.obligation_id)
            source_sets.add(item.source_ref_ids)
            if not set(item.source_ref_ids).issubset(authorized_refs):
                raise CasePlanningProjectionBlocked(
                    "re-extraction obligation cites an object outside this projection"
                )
        if (
            tuple(
                sorted(
                    self.reextraction_obligations,
                    key=lambda item: item.obligation_id,
                )
            )
            != self.reextraction_obligations
        ):
            raise CasePlanningProjectionBlocked(
                "authoritative re-extraction obligations are not canonical"
            )


class CasePlanningProjectionRepository(Protocol):
    """Read all governed inputs in one tenant-scoped repeatable-read snapshot.

    A PostgreSQL implementation must authorize ``actor`` as the dedicated
    same-firm SYSTEM_WORKER, bind the exact ``expected_case_snapshot``, and
    return both snapshot fences from the same transaction.  It must not call
    the existing per-ledger snapshot methods sequentially.
    """

    def read_atomic_projection(
        self,
        *,
        firm_id: str,
        matter_id: str,
        actor: Actor,
        expected_case_snapshot: CaseSnapshotRef,
    ) -> AuthoritativeCasePlanningProjection: ...


@dataclass(frozen=True)
class ExecutablePlanningSkill:
    """One semantic Skill proven by an actual registered adapter instance."""

    skill_id: str
    adapter: object = field(repr=False, compare=False)
    supported_object_types: frozenset[PlanningProjectionObjectType]
    neutral_material_inventory: bool = False
    supported_media_types: frozenset[str] = frozenset()

    @property
    def manifest(self) -> RuntimeAdapterManifest:
        value = getattr(self.adapter, "manifest", None)
        if not isinstance(value, RuntimeAdapterManifest):
            raise CasePlanningProjectionBlocked(
                "executable planning Skill has no registered adapter manifest"
            )
        return value

    def validate(self) -> None:
        _code(self.skill_id, "executable skill_id")
        manifest = self.manifest
        try:
            manifest.validate()
        except AgentSupervisorBlocked as error:
            raise CasePlanningProjectionBlocked(
                "executable planning Skill adapter manifest is invalid"
            ) from error
        if not callable(getattr(self.adapter, "execute", None)) or not callable(
            getattr(self.adapter, "reconcile", None)
        ):
            raise CasePlanningProjectionBlocked(
                "executable planning Skill adapter is incomplete"
            )
        if not self.supported_object_types:
            raise CasePlanningProjectionBlocked(
                "executable planning Skill has no declared input capability"
            )
        if any(
            not isinstance(value, PlanningProjectionObjectType)
            for value in self.supported_object_types
        ):
            raise CasePlanningProjectionBlocked(
                "executable planning Skill input capability is invalid"
            )
        if self.neutral_material_inventory and not self.supported_object_types.issubset(
            _NEUTRAL_OBJECT_TYPES
        ):
            raise CasePlanningProjectionBlocked(
                "neutral material Skill cannot authorize non-material case objects"
            )
        if self.supported_media_types:
            if self.supported_object_types != frozenset(
                {PlanningProjectionObjectType.EVIDENCE_PAGE}
            ):
                raise CasePlanningProjectionBlocked(
                    "media-scoped Skill must accept only evidence pages"
                )
            if not self.supported_media_types.issubset(
                {"application/pdf", "image/jpeg", "image/png"}
            ):
                raise CasePlanningProjectionBlocked(
                    "executable planning Skill media capability is invalid"
                )


class AuthoritativeCasePlanningSnapshotProvider:
    """Build exact, capability-bound planning snapshots for a durable run."""

    def __init__(
        self,
        *,
        repository: CasePlanningProjectionRepository,
        executable_skills: Iterable[ExecutablePlanningSkill],
        max_inputs: int = 500,
    ) -> None:
        if not callable(getattr(repository, "read_atomic_projection", None)):
            raise ValueError("authoritative planning projection repository is invalid")
        if not 1 <= max_inputs <= 500:
            raise ValueError("planning input limit must be between 1 and 500")
        skills = tuple(executable_skills)
        if not skills:
            raise ValueError("at least one executable planning Skill is required")
        skill_ids: set[str] = set()
        for skill in skills:
            if not isinstance(skill, ExecutablePlanningSkill):
                raise ValueError("executable planning Skill declaration is invalid")
            skill.validate()
            if skill.skill_id in skill_ids:
                raise ValueError("executable planning Skill ids must be unique")
            skill_ids.add(skill.skill_id)
        self._repository = repository
        self._skills = tuple(sorted(skills, key=lambda item: item.skill_id))
        self._max_inputs = max_inputs

    def build_for_run(
        self, *, state: AgentRunState, actor: Actor
    ) -> CasePlanningSnapshot:
        _require_worker_scope(state=state, actor=actor)
        try:
            state.snapshot.validate()
        except AgentSupervisorBlocked as error:
            raise CasePlanningProjectionBlocked("Agent run snapshot is invalid") from error
        projection = self._repository.read_atomic_projection(
            firm_id=state.firm_id,
            matter_id=state.matter_id,
            actor=actor,
            expected_case_snapshot=state.snapshot,
        )
        if not isinstance(projection, AuthoritativeCasePlanningProjection):
            raise CasePlanningProjectionBlocked(
                "authoritative repository returned an invalid projection"
            )
        if projection.firm_id != state.firm_id or projection.matter_id != state.matter_id:
            raise CasePlanningProjectionBlocked(
                "authoritative projection belongs to another firm or matter"
            )
        if (
            projection.opening_case_snapshot != state.snapshot
            or projection.closing_case_snapshot != state.snapshot
        ):
            raise CasePlanningProjectionBlocked(
                "matter version or hash changed while the planning projection was read"
            )
        projection.validate()
        if any(
            item.control_run_id != state.run_id
            for item in projection.reextraction_obligations
        ):
            raise CasePlanningProjectionBlocked(
                "active re-extraction obligation belongs to another control run"
            )
        if projection.posture_state is not ProjectionSectionState.AVAILABLE:
            return self._build_neutral_snapshot(state=state, projection=projection)
        return self._build_full_snapshot(state=state, projection=projection)

    def _build_neutral_snapshot(
        self,
        *,
        state: AgentRunState,
        projection: AuthoritativeCasePlanningProjection,
    ) -> CasePlanningSnapshot:
        material_objects = tuple(
            item for item in projection.objects if item.object_type in _NEUTRAL_OBJECT_TYPES
        )
        inputs = self._build_inputs(material_objects, neutral_only=True)
        if not inputs:
            raise CasePlanningProjectionBlocked(
                "posture is not confirmed and there is no executable neutral material input"
            )
        signals: list[CasePlanningSignal] = [CasePlanningSignal(
            signal_id=f"signal:posture-gap:{state.matter_id}",
            category=PlanningSignalCategory.PARTY_POSTURE,
            code=(
                "POSTURE_LEDGER_NOT_CONFIGURED"
                if projection.posture_state is ProjectionSectionState.NOT_CONFIGURED
                else "POSTURE_CONFIRMATION_REQUIRED"
            ),
            status=PlanningInputStatus.OPEN,
            summary=(
                "案件代理地位尚未形成当前有效的律师确认记录；当前规划仅允许中性材料盘点，"
                "不得推定原告、被告或固定交付清单。"
            ),
            source_ref_ids=(inputs[0].ref_id,),
        )]
        input_by_ref = {item.ref_id: item for item in inputs}
        withheld: list[GovernedLawyerPlanningSignal] = []
        for item in projection.lawyer_signals:
            if set(item.source_ref_ids).issubset(input_by_ref):
                signals.extend(_governed_lawyer_signals_for_items((item,), input_by_ref))
            else:
                withheld.append(item)
        if withheld:
            withheld_hash = _canonical_hash(
                [
                    {
                        "signal_id": item.signal_id,
                        "signal_version": item.signal_version,
                        "decision_hash": item.decision_hash,
                    }
                    for item in withheld
                ]
            )
            signals.append(
                CasePlanningSignal(
                    signal_id=f"signal:lawyer-withheld:{withheld_hash[:16]}",
                    category=PlanningSignalCategory.PARTY_POSTURE,
                    code="LAWYER_DECISIONS_PENDING_POSTURE",
                    status=PlanningInputStatus.BLOCKED,
                    summary=(
                        f"另有{len(withheld)}项律师纠正或否认绑定非材料对象；代理地位确认前"
                        "不向中性材料盘点开放其内容。"
                    ),
                    source_ref_ids=(inputs[0].ref_id,),
                )
            )
        return _build_snapshot(
            case_snapshot=state.snapshot,
            inputs=inputs,
            signals=tuple(signals),
            reextraction_obligations=projection.reextraction_obligations,
        )

    def _build_full_snapshot(
        self,
        *,
        state: AgentRunState,
        projection: AuthoritativeCasePlanningProjection,
    ) -> CasePlanningSnapshot:
        inputs = self._build_inputs(projection.objects, neutral_only=False)
        if not inputs:
            raise CasePlanningProjectionBlocked(
                "authoritative projection has no executable planning inputs"
            )
        input_by_ref = {item.ref_id: item for item in inputs}
        signals = _full_signals(projection=projection, input_by_ref=input_by_ref)
        return _build_snapshot(
            case_snapshot=state.snapshot,
            inputs=inputs,
            signals=signals,
            reextraction_obligations=projection.reextraction_obligations,
        )

    def _build_inputs(
        self,
        objects: tuple[AuthoritativePlanningObject, ...],
        *,
        neutral_only: bool,
    ) -> tuple[PlanningInputRef, ...]:
        if len(objects) > self._max_inputs:
            raise CasePlanningProjectionBlocked(
                "authorized planning input limit exceeded; no page or object was truncated"
            )
        inputs: list[PlanningInputRef] = []
        for item in objects:
            allowed = tuple(
                skill.skill_id
                for skill in self._skills
                if item.object_type in skill.supported_object_types
                and (
                    not skill.supported_media_types
                    or item.source_media_type in skill.supported_media_types
                )
                and (not neutral_only or skill.neutral_material_inventory)
            )
            if not allowed:
                raise CasePlanningProjectionBlocked(
                    f"no registered executable adapter supports {item.object_type.value}"
                )
            inputs.append(
                PlanningInputRef(
                    ref_id=item.ref_id,
                    kind=_INPUT_KIND[item.object_type],
                    object_version=item.object_version,
                    content_hash=item.content_hash,
                    status=item.status,
                    allowed_skill_ids=allowed,
                )
            )
        return tuple(inputs)


def planning_object_ref_id(
    object_type: PlanningProjectionObjectType, object_id: str
) -> str:
    """Return the stable opaque reference resolved by server task bindings."""

    if not isinstance(object_type, PlanningProjectionObjectType):
        raise CasePlanningProjectionBlocked("planning object type is invalid")
    _uuid(object_id, "planning object_id")
    return f"{_REF_PREFIX[object_type]}:{object_id}"


def object_version_code(value: int) -> str:
    """Encode a positive database version for PlanningInputRef validation."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CasePlanningProjectionBlocked("object version must be a positive integer")
    return f"v{value}"


def _full_signals(
    *,
    projection: AuthoritativeCasePlanningProjection,
    input_by_ref: dict[str, PlanningInputRef],
) -> tuple[CasePlanningSignal, ...]:
    posture = projection.posture
    if posture is None:
        raise CasePlanningProjectionBlocked("confirmed posture projection is missing")
    posture_ref = planning_object_ref_id(
        PlanningProjectionObjectType.POSTURE_PROFILE, posture.profile_id
    )
    if posture_ref not in input_by_ref:
        raise CasePlanningProjectionBlocked("confirmed posture input is not executable")
    signals: list[CasePlanningSignal] = [
        CasePlanningSignal(
            signal_id=f"signal:proceeding:{posture.profile_id}",
            category=PlanningSignalCategory.PROCEEDING,
            code="CONFIRMED_PROCEEDING_CONTEXT",
            status=PlanningInputStatus.CONFIRMED,
            summary=(
                f"律师已确认案件类型={posture.case_type_code}、程序阶段="
                f"{posture.procedure_stage}；该状态本身不生成固定文书清单。"
            ),
            source_ref_ids=(posture_ref,),
        ),
        CasePlanningSignal(
            signal_id=f"signal:party-posture:{posture.profile_id}",
            category=PlanningSignalCategory.PARTY_POSTURE,
            code="CONFIRMED_REPRESENTED_POSITION",
            status=PlanningInputStatus.CONFIRMED,
            summary=(
                f"律师已确认代理地位={posture.represented_position}、委托范围="
                f"{posture.authority_scope_code}、委托状态={posture.engagement_state}；"
                "交付物仍须依据本案事实、程序事件和法源动态判断。"
            ),
            source_ref_ids=(posture_ref,),
        ),
    ]
    signals.extend(_ledger_signals(projection, input_by_ref))
    signals.append(_work_plan_signal(projection, input_by_ref, posture_ref))
    signals.append(
        _optional_section_signal(
            projection=projection,
            input_by_ref=input_by_ref,
            posture_ref=posture_ref,
            object_type=PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
            state=projection.legal_state,
            category=PlanningSignalCategory.LEGAL_GAP,
            available_code="VERIFIED_LEGAL_SOURCES_AVAILABLE",
            empty_code="VERIFIED_LEGAL_SOURCES_REQUIRED",
            not_configured_code="LEGAL_SOURCE_LEDGER_NOT_CONFIGURED",
            available_summary=(
                "当前规划存在 {count} 项已核验法源元数据；具体规则、版本与适用结论仍须"
                "经受控研究和律师复核。"
            ),
            empty_summary="当前案件尚无已核验法源；系统不得据此编造法律依据。",
            not_configured_summary=(
                "法源投影尚未接入本次原子快照；系统不得把未核验网络结果当作法律依据。"
            ),
        )
    )
    signals.append(
        _optional_section_signal(
            projection=projection,
            input_by_ref=input_by_ref,
            posture_ref=posture_ref,
            object_type=PlanningProjectionObjectType.PROCEDURAL_EVENT,
            state=projection.procedure_state,
            category=PlanningSignalCategory.PROCEEDING,
            available_code="APPROVED_PROCEDURAL_EVENTS_AVAILABLE",
            empty_code="PROCEDURAL_EVENTS_REQUIRED",
            not_configured_code="PROCEDURAL_EVENT_LEDGER_NOT_CONFIGURED",
            available_summary=(
                "当前规划存在 {count} 项已批准程序事件元数据；系统未由此自行推定送达日、"
                "期限或诉讼策略。"
            ),
            empty_summary=(
                "当前案件尚无已批准程序事件；系统未推定送达日、期限或应提交文件。"
            ),
            not_configured_summary=(
                "程序事件投影尚未接入本次原子快照；系统不得自行推定法院期限。"
            ),
        )
    )
    signals.extend(_governed_lawyer_signals(projection, input_by_ref))
    return tuple(signals)


def _governed_lawyer_signals(
    projection: AuthoritativeCasePlanningProjection,
    input_by_ref: dict[str, PlanningInputRef],
) -> tuple[CasePlanningSignal, ...]:
    return _governed_lawyer_signals_for_items(projection.lawyer_signals, input_by_ref)


def _governed_lawyer_signals_for_items(
    items: tuple[GovernedLawyerPlanningSignal, ...],
    input_by_ref: dict[str, PlanningInputRef],
) -> tuple[CasePlanningSignal, ...]:
    result: list[CasePlanningSignal] = []
    for item in items:
        if not set(item.source_ref_ids).issubset(input_by_ref):
            raise CasePlanningProjectionBlocked(
                "lawyer decision signal source is not executable in this release"
            )
        result.append(
            CasePlanningSignal(
                signal_id=f"signal:lawyer:{item.signal_id}:{item.decision_hash[:16]}",
                category=item.category,
                code=item.code,
                status=item.status,
                summary=item.summary,
                source_ref_ids=item.source_ref_ids,
            )
        )
    return tuple(result)


def _ledger_signals(
    projection: AuthoritativeCasePlanningProjection,
    input_by_ref: dict[str, PlanningInputRef],
) -> tuple[CasePlanningSignal, ...]:
    ledger_types = (
        PlanningProjectionObjectType.CASE_FACT,
        PlanningProjectionObjectType.CASE_CLAIM,
        PlanningProjectionObjectType.DISPUTE_ISSUE,
        PlanningProjectionObjectType.CASE_TRANSACTION,
    )
    groups = {
        value: tuple(item for item in projection.objects if item.object_type is value)
        for value in ledger_types
    }
    all_items = tuple(item for value in ledger_types for item in groups[value])
    if not all_items:
        return ()
    sources: list[str] = []
    for value in ledger_types:
        if groups[value]:
            ref_id = groups[value][0].ref_id
            if ref_id not in input_by_ref:
                raise CasePlanningProjectionBlocked("case-ledger input is not executable")
            sources.append(ref_id)
    unresolved = sum(
        item.status
        in {
            PlanningInputStatus.OPEN,
            PlanningInputStatus.REVIEW_REQUIRED,
            PlanningInputStatus.DISPUTED,
            PlanningInputStatus.BLOCKED,
        }
        for item in all_items
    )
    return (
        CasePlanningSignal(
            signal_id=f"signal:ledger-counts:{projection.matter_id}",
            category=PlanningSignalCategory.CONFIRMED_FACT,
            code="CASE_LEDGER_SCOPE_COUNTS",
            status=(
                PlanningInputStatus.REVIEW_REQUIRED
                if unresolved
                else PlanningInputStatus.CONFIRMED
            ),
            summary=(
                f"当前同版台账包含事实{len(groups[PlanningProjectionObjectType.CASE_FACT])}项、"
                f"诉请{len(groups[PlanningProjectionObjectType.CASE_CLAIM])}项、争点"
                f"{len(groups[PlanningProjectionObjectType.DISPUTE_ISSUE])}项、交易"
                f"{len(groups[PlanningProjectionObjectType.CASE_TRANSACTION])}项；其中"
                f"{unresolved}项仍需复核或处理。"
            ),
            source_ref_ids=tuple(sources),
        ),
    )


def _work_plan_signal(
    projection: AuthoritativeCasePlanningProjection,
    input_by_ref: dict[str, PlanningInputRef],
    posture_ref: str,
) -> CasePlanningSignal:
    plan = projection.active_work_plan
    if projection.work_plan_state is ProjectionSectionState.AVAILABLE and plan is not None:
        first_ref = planning_object_ref_id(
            PlanningProjectionObjectType.WORK_PLAN_ITEM, plan.item_ids[0]
        )
        if first_ref not in input_by_ref:
            raise CasePlanningProjectionBlocked("active work-plan input is not executable")
        return CasePlanningSignal(
            signal_id=f"signal:work-plan:{plan.plan_id}",
            category=PlanningSignalCategory.WORK_PLAN,
            code="ACTIVE_DYNAMIC_WORK_PLAN",
            status=PlanningInputStatus.CONFIRMED,
            summary=(
                f"律师已激活动态工作计划：可执行{plan.actionable_count}项、待补信息"
                f"{plan.needs_information_count}项、待研究{plan.needs_research_count}项；"
                "该计划依据来源对象，而非代理地位固定映射。"
            ),
            source_ref_ids=(first_ref,),
        )
    return CasePlanningSignal(
        signal_id=f"signal:work-plan-gap:{projection.matter_id}",
        category=PlanningSignalCategory.WORK_PLAN,
        code=(
            "WORK_PLAN_LEDGER_NOT_CONFIGURED"
            if projection.work_plan_state is ProjectionSectionState.NOT_CONFIGURED
            else "ACTIVE_DYNAMIC_WORK_PLAN_REQUIRED"
        ),
        status=PlanningInputStatus.OPEN,
        summary=(
            "当前案件尚无同版、已激活的动态工作计划；系统不得按原告或被告套用固定流程。"
        ),
        source_ref_ids=(posture_ref,),
    )


def _optional_section_signal(
    *,
    projection: AuthoritativeCasePlanningProjection,
    input_by_ref: dict[str, PlanningInputRef],
    posture_ref: str,
    object_type: PlanningProjectionObjectType,
    state: ProjectionSectionState,
    category: PlanningSignalCategory,
    available_code: str,
    empty_code: str,
    not_configured_code: str,
    available_summary: str,
    empty_summary: str,
    not_configured_summary: str,
) -> CasePlanningSignal:
    items = tuple(item for item in projection.objects if item.object_type is object_type)
    if state is ProjectionSectionState.AVAILABLE:
        first_ref = items[0].ref_id
        if first_ref not in input_by_ref:
            raise CasePlanningProjectionBlocked("optional ledger input is not executable")
        return CasePlanningSignal(
            signal_id=f"signal:{_REF_PREFIX[object_type]}:{projection.matter_id}",
            category=category,
            code=available_code,
            status=PlanningInputStatus.CONFIRMED,
            summary=available_summary.format(count=len(items)),
            source_ref_ids=(first_ref,),
        )
    return CasePlanningSignal(
        signal_id=f"signal:{_REF_PREFIX[object_type]}-gap:{projection.matter_id}",
        category=category,
        code=(not_configured_code if state is ProjectionSectionState.NOT_CONFIGURED else empty_code),
        status=PlanningInputStatus.OPEN,
        summary=(not_configured_summary if state is ProjectionSectionState.NOT_CONFIGURED else empty_summary),
        source_ref_ids=(posture_ref,),
    )


def _build_snapshot(
    *,
    case_snapshot: CaseSnapshotRef,
    inputs: tuple[PlanningInputRef, ...],
    signals: tuple[CasePlanningSignal, ...],
    reextraction_obligations: tuple[ReextractionPlanningObligation, ...] = (),
) -> CasePlanningSnapshot:
    try:
        return CasePlanningSnapshot.build(
            case_snapshot=case_snapshot,
            authorized_inputs=inputs,
            signals=signals,
            reextraction_obligations=reextraction_obligations,
        )
    except (CasePlannerBlocked, AgentSupervisorBlocked) as error:
        raise CasePlanningProjectionBlocked(
            "authoritative projection cannot form a valid planning snapshot"
        ) from error


def _require_worker_scope(*, state: AgentRunState, actor: Actor) -> None:
    if not isinstance(state, AgentRunState) or not isinstance(actor, Actor):
        raise CasePlanningProjectionBlocked("Agent run or worker identity is invalid")
    if actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError(
            "planning snapshot requires a dedicated SYSTEM_WORKER identity"
        )
    if actor.firm_id != state.firm_id:
        raise PermissionError("planning worker belongs to another firm")
    _uuid(state.firm_id, "run firm_id")
    _uuid(state.matter_id, "run matter_id")
    if state.snapshot.matter_id != state.matter_id:
        raise CasePlanningProjectionBlocked("Agent run snapshot belongs to another matter")
    if state.cancelled:
        raise CasePlanningProjectionBlocked("cancelled Agent run cannot be planned")


def _validate_optional_section(
    *,
    name: str,
    state: ProjectionSectionState,
    items: tuple[AuthoritativePlanningObject, ...],
    allowed_statuses: frozenset[PlanningInputStatus],
) -> None:
    if state is ProjectionSectionState.AVAILABLE:
        if not items:
            raise CasePlanningProjectionBlocked(
                f"available {name} section has no authoritative objects"
            )
        if any(item.status not in allowed_statuses for item in items):
            raise CasePlanningProjectionBlocked(
                f"{name} section contains an unverified or unapproved object"
            )
    elif items:
        raise CasePlanningProjectionBlocked(
            f"empty or unconfigured {name} section contains authoritative objects"
        )


def _projection_payload(
    *,
    firm_id: str,
    matter_id: str,
    opening_case_snapshot: CaseSnapshotRef,
    closing_case_snapshot: CaseSnapshotRef,
    objects: tuple[AuthoritativePlanningObject, ...],
    posture_state: ProjectionSectionState,
    posture: ConfirmedPostureProjection | None,
    work_plan_state: ProjectionSectionState,
    active_work_plan: ActiveDynamicWorkPlanProjection | None,
    legal_state: ProjectionSectionState,
    procedure_state: ProjectionSectionState,
    lawyer_signals: tuple[GovernedLawyerPlanningSignal, ...],
    reextraction_obligations: tuple[ReextractionPlanningObligation, ...],
) -> dict[str, object]:
    return {
        "schema_version": "authoritative-case-planning-projection-v3",
        "firm_id": firm_id,
        "matter_id": matter_id,
        "opening_case_snapshot": _snapshot_payload(opening_case_snapshot),
        "closing_case_snapshot": _snapshot_payload(closing_case_snapshot),
        "objects": [
            {
                "object_type": item.object_type.value,
                "object_id": item.object_id,
                "object_version": item.object_version,
                "content_hash": item.content_hash,
                "status": item.status.value,
                "source_media_type": item.source_media_type,
                **({"extraction_complete": item.extraction_complete}
                   if item.extraction_complete is not None else {}),
            }
            for item in objects
        ],
        "posture_state": posture_state.value,
        "posture": (
            None
            if posture is None
            else {
                "profile_id": posture.profile_id,
                "profile_version": posture.profile_version,
                "profile_hash": posture.profile_hash,
                "effective_status": posture.effective_status,
                "case_type_code": posture.case_type_code,
                "procedure_stage": posture.procedure_stage,
                "represented_position": posture.represented_position,
                "authority_scope_code": posture.authority_scope_code,
                "engagement_state": posture.engagement_state,
            }
        ),
        "work_plan_state": work_plan_state.value,
        "active_work_plan": (
            None
            if active_work_plan is None
            else {
                "plan_id": active_work_plan.plan_id,
                "plan_version": active_work_plan.plan_version,
                "plan_hash": active_work_plan.plan_hash,
                "bound_matter_version": active_work_plan.bound_matter_version,
                "item_ids": active_work_plan.item_ids,
                "actionable_count": active_work_plan.actionable_count,
                "needs_information_count": active_work_plan.needs_information_count,
                "needs_research_count": active_work_plan.needs_research_count,
            }
        ),
        "legal_state": legal_state.value,
        "procedure_state": procedure_state.value,
        "lawyer_signals": [
            {
                "signal_id": item.signal_id,
                "signal_version": item.signal_version,
                "decision_hash": item.decision_hash,
                "category": item.category.value,
                "code": item.code,
                "status": item.status.value,
                "summary": item.summary,
                "source_ref_ids": item.source_ref_ids,
            }
            for item in lawyer_signals
        ],
        "reextraction_obligations": [
            {
                "obligation_id": item.obligation_id,
                "control_run_id": item.control_run_id,
                "followup_ids": item.followup_ids,
                "source_ref_ids": item.source_ref_ids,
                "lifecycle_hash": item.lifecycle_hash,
            }
            for item in reextraction_obligations
        ],
    }


def _snapshot_payload(value: CaseSnapshotRef) -> dict[str, object]:
    return {
        "matter_id": value.matter_id,
        "matter_version": value.matter_version,
        "snapshot_hash": value.snapshot_hash,
        "schema_version": value.schema_version,
    }


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _uuid(value: object, label: str) -> None:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CasePlanningProjectionBlocked(f"{label} must be a UUID") from error
    if str(parsed) != value:
        raise CasePlanningProjectionBlocked(f"{label} must be a canonical UUID")


def _code(value: object, label: str) -> None:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise CasePlanningProjectionBlocked(f"{label} must be a stable code")


def _hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CasePlanningProjectionBlocked(f"{label} must be a lowercase SHA-256")


def _text(value: object, label: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise CasePlanningProjectionBlocked(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(value) > maximum:
        raise CasePlanningProjectionBlocked(f"{label} is missing or too long")
    if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise CasePlanningProjectionBlocked(f"{label} contains control characters")


def _ref(value: object, label: str) -> None:
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise CasePlanningProjectionBlocked(f"{label} is invalid")


_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


__all__ = [
    "ActiveDynamicWorkPlanProjection",
    "AuthoritativeCasePlanningProjection",
    "AuthoritativeCasePlanningSnapshotProvider",
    "AuthoritativePlanningObject",
    "CasePlanningProjectionBlocked",
    "CasePlanningProjectionRepository",
    "ConfirmedPostureProjection",
    "ExecutablePlanningSkill",
    "GovernedLawyerPlanningSignal",
    "PlanningProjectionObjectType",
    "ProjectionSectionState",
    "ReextractionPlanningObligation",
    "object_version_code",
    "planning_object_ref_id",
]
