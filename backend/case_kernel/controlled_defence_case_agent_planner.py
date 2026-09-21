"""Server-governed planning for the first-release defendant-response route.

This route deliberately does *not* ask a model how to plan a response case.
The controlled source set and task order are already product policy.  The one
permitted model call is reserved for the lawyer decision package, while all
original evidence remains inside the private evidence chain until a later,
separately governed task needs it.
"""

from __future__ import annotations

from typing import Iterable, Protocol
from dataclasses import replace

from .case_agent_lawyer_analysis_adapters import LAWYER_ANALYSIS_SKILL_ID
from .case_agent_planner import (
    CasePlanProposal,
    CasePlannerBlocked,
    CasePlanningSnapshot,
    PlannerRiskHint,
    PlannerSemanticSkill,
    ProposedPlannerTask,
)
from .case_agent_planning_snapshot import (
    AuthoritativeCasePlanningProjection,
    AuthoritativeCasePlanningSnapshotProvider,
    CasePlanningProjectionBlocked,
    ExecutablePlanningSkill,
    PlanningProjectionObjectType,
    ProjectionSectionState,
)
from .case_agent_supervisor import AgentDeliverableKind, AgentGoal, AgentRunState
from .case_agent_worker import DurablePlanningClaim, SemanticPlanner
from .models import Actor


CONTROLLED_DEFENCE_PLANNER_ID = "controlled-first-release-defence-planner-v1"
CONTROLLED_DEFENCE_ROUTER_ID = "case-agent-planning-router-v1"
_CASE_CONTEXT_SKILL_ID = "case_context_review"
_LEDGER_EXTRACTION_SKILL_ID = "case_ledger_extraction"
_ANALYSIS_REQUIRED_REF_PREFIXES = ("posture-profile:", "fact:")

_REQUIRED_REF_PREFIXES = (
    "posture-profile:",
    "fact:",
    "claim:",
    "legal-source:",
    "legal-rule:",
)
_OPTIONAL_ANALYSIS_REF_PREFIXES = ("issue:",)
_CONTROLLED_REF_PREFIXES = _REQUIRED_REF_PREFIXES + _OPTIONAL_ANALYSIS_REF_PREFIXES

# ADR-0056 deliberately keeps raw PDF/image evidence inside the private
# evidence chain during the first defendant-response analysis. Unconfirmed
# extraction candidates stay in the material-review queue as well: sending a
# large, unreviewed ledger packet is both an unsafe factual shortcut and an
# unreliable way to make one model request do the work of an evidence review.
# The one model call receives governed posture, facts, claims, legal inputs and
# typed review obligations. This is a product boundary, not a convenience
# filter for an OCR outage: originals and every candidate remain available for
# lawyer review and later, separately governed analysis.
_CONTROLLED_DEFENCE_OBJECT_TYPES = frozenset(
    {
        PlanningProjectionObjectType.POSTURE_PROFILE,
        PlanningProjectionObjectType.CASE_FACT,
        PlanningProjectionObjectType.CASE_CLAIM,
        PlanningProjectionObjectType.DISPUTE_ISSUE,
        PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
        PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
        PlanningProjectionObjectType.REVIEW_OBLIGATION,
    }
)


class _ControlledDefenceProjectionRepository:
    """Expose only ADR-0056 sources to the one-call defendant route."""

    def __init__(self, repository: object) -> None:
        if not callable(getattr(repository, "read_atomic_projection", None)):
            raise ValueError("authoritative planning projection repository is invalid")
        self._repository = repository

    def read_atomic_projection(self, **kwargs: object) -> object:
        projection = self._repository.read_atomic_projection(**kwargs)
        if not isinstance(projection, AuthoritativeCasePlanningProjection):
            # Preserve the base provider's ordinary invalid-repository error
            # rather than masking it with a route-specific conversion error.
            return projection
        if projection.reextraction_obligations:
            # An unresolved evidence obligation must never disappear merely
            # because raw evidence is intentionally outside this first call.
            raise CasePlanningProjectionBlocked(
                "defence route cannot exclude active evidence re-extraction"
            )
        # A requested document is an end goal, not proof that intake is done.
        # Preserve raw input for local reading when governed inputs are absent.
        # The ordinary provider still validates adapters, scope and versions.
        present_types = {item.object_type for item in projection.objects}
        if not {PlanningProjectionObjectType.POSTURE_PROFILE,
                PlanningProjectionObjectType.CASE_FACT}.issubset(present_types):
            return projection
        objects = tuple(
            item
            for item in projection.objects
            if item.object_type in _CONTROLLED_DEFENCE_OBJECT_TYPES
            or (item.object_type is PlanningProjectionObjectType.EVIDENCE_PAGE
                and item.extraction_complete is not True)
        )
        source_refs = frozenset(item.ref_id for item in objects)
        # Rebind only verified review metadata to its own typed source. The
        # source content hash still binds the note and exact original pages.
        from .case_agent_review_obligations import REVIEW_OBLIGATION_CODES
        signals = tuple(replace(item, source_ref_ids=(f"review-obligation:{item.signal_id}",))
            if item.code in REVIEW_OBLIGATION_CODES
                and f"review-obligation:{item.signal_id}" in source_refs else item
            for item in projection.lawyer_signals)
        if any(
            not set(item.source_ref_ids).issubset(source_refs)
            for item in signals
        ):
            # A governed lawyer correction/rejection is more important than
            # keeping this special route narrow. Stop rather than silently
            # omit it from the model packet.
            raise CasePlanningProjectionBlocked(
                "defence route has a lawyer signal outside its governed source set"
            )
        return AuthoritativeCasePlanningProjection.build(
            firm_id=projection.firm_id,
            matter_id=projection.matter_id,
            opening_case_snapshot=projection.opening_case_snapshot,
            closing_case_snapshot=projection.closing_case_snapshot,
            objects=objects,
            posture_state=projection.posture_state,
            posture=projection.posture,
            # An ACTIVE work plan is separately bound into the second run's
            # server-owned goal. It cannot broaden the first model packet.
            work_plan_state=ProjectionSectionState.EMPTY,
            active_work_plan=None,
            legal_state=projection.legal_state,
            # The first route must not treat an unselected procedural event as
            # a source or fabricate a deadline/conclusion from its absence.
            procedure_state=ProjectionSectionState.EMPTY,
            lawyer_signals=signals,
            reextraction_obligations=(),
        )


class ControlledDefencePlanningSnapshotProvider:
    """Use the narrow planning projection only for governed defence goals."""

    def __init__(
        self,
        *,
        repository: object,
        executable_skills: Iterable[ExecutablePlanningSkill],
    ) -> None:
        skills = tuple(executable_skills)
        self._standard_provider = AuthoritativeCasePlanningSnapshotProvider(
            repository=repository,
            executable_skills=skills,
        )
        self._defence_provider = AuthoritativeCasePlanningSnapshotProvider(
            repository=_ControlledDefenceProjectionRepository(repository),
            executable_skills=skills,
        )

    def build_for_run(
        self, *, state: AgentRunState, actor: Actor
    ) -> CasePlanningSnapshot:
        provider = (
            self._defence_provider
            if _is_defence_goal(state.goal) and state.goal.active_plan_execution is None
            else self._standard_provider
        )
        return provider.build_for_run(state=state, actor=actor)


class ControlledDefencePlanningOutcomeRecorder(Protocol):
    """Persist a server-created plan without creating a fake provider event."""

    def record_local_planning_outcome(
        self,
        *,
        matter_id: str,
        claim: DurablePlanningClaim,
        planner_id: str,
        proposal: CasePlanProposal,
    ) -> None: ...


class ControlledDefencePlanningRouter:
    """Choose a local, source-complete plan only for governed defence goals."""

    planner_id = CONTROLLED_DEFENCE_ROUTER_ID

    def __init__(
        self,
        *,
        fallback: SemanticPlanner,
        outcome_recorder: ControlledDefencePlanningOutcomeRecorder,
    ) -> None:
        if not callable(getattr(fallback, "plan", None)) or not getattr(
            fallback, "planner_id", ""
        ):
            raise ValueError("fallback semantic planner is invalid")
        if not callable(
            getattr(outcome_recorder, "record_local_planning_outcome", None)
        ):
            raise ValueError("local planning outcome recorder is invalid")
        self._fallback = fallback
        self._outcome_recorder = outcome_recorder

    def plan(
        self,
        *,
        goal: object,
        snapshot: CasePlanningSnapshot,
        skills: tuple[PlannerSemanticSkill, ...],
        execution: DurablePlanningClaim,
    ) -> CasePlanProposal:
        if not _is_defence_goal(goal):
            return self._fallback.plan(
                goal=goal,
                snapshot=snapshot,
                skills=skills,
                execution=execution,
            )
        if not isinstance(goal, AgentGoal):  # pragma: no cover - narrowed above
            raise CasePlannerBlocked("defence planning goal is invalid")
        snapshot.validate()
        execution.validate()
        if execution.state.goal != goal:
            raise CasePlannerBlocked("defence planning claim differs from its goal")
        if execution.planning_hash != snapshot.planning_hash:
            raise CasePlannerBlocked("defence planning claim differs from its snapshot")
        if snapshot.reextraction_obligations:
            # Re-extraction can route through a separately governed external
            # Skill.  It belongs in its own resumed run, never inside the
            # one-call defendant-response acceptance route.
            raise CasePlannerBlocked(
                "defence route cannot absorb active evidence re-extraction"
            )
        proposal = _controlled_proposal(goal=goal, snapshot=snapshot, skills=skills)
        self._outcome_recorder.record_local_planning_outcome(
            matter_id=execution.state.matter_id,
            claim=execution,
            planner_id=CONTROLLED_DEFENCE_PLANNER_ID,
            proposal=proposal,
        )
        return proposal


def _is_defence_goal(goal: object) -> bool:
    return isinstance(goal, AgentGoal) and (
        AgentDeliverableKind.DEFENCE_STATEMENT in goal.requested_deliverables
    )


def _controlled_proposal(
    *,
    goal: AgentGoal,
    snapshot: CasePlanningSnapshot,
    skills: tuple[PlannerSemanticSkill, ...],
) -> CasePlanProposal:
    available_skills = {item.skill_id for item in skills}
    if goal.active_plan_execution is not None:
        # The second run is not another merits analysis.  Its authority is the
        # exact set of server-selected ACTIVE-plan item hashes; document
        # binding rechecks their source lineage before any renderer runs.  Do
        # not require a new visible facts/claims/legal-source packet here:
        # the standard projection deliberately exposes active-plan inputs as
        # non-planner-visible so no planner can repurpose them for analysis.
        active_item_refs = tuple(
            item.ref_id
            for item in snapshot.authorized_inputs
            if item.ref_id.startswith("work-plan-item:")
        )
        if not active_item_refs:
            raise CasePlannerBlocked(
                "active plan execution lacks its server-bound work-plan inputs"
            )
        # The graph compiler, not this durable proposal record, verifies the
        # ACTIVE_DYNAMIC_WORK_PLAN signal.  Keeping that check at compilation
        # avoids a second planner-only representation of the same authority.
        if _CASE_CONTEXT_SKILL_ID not in available_skills:
            raise CasePlannerBlocked("defence route has no case-context Skill")
        return CasePlanProposal(
            goal_hash=goal.goal_hash,
            planning_snapshot_hash=snapshot.planning_hash,
            tasks=(
                ProposedPlannerTask(
                    proposal_id="active-plan-authority",
                    skill_id=_CASE_CONTEXT_SKILL_ID,
                    purpose="核对当前动态计划所依赖的已确认被告应诉来源边界。",
                    dependency_ids=(),
                    input_ref_ids=active_item_refs,
                    risk_hint=PlannerRiskHint.LOW,
                ),
            ),
        )
    required_prefixes = _ANALYSIS_REQUIRED_REF_PREFIXES
    missing = any(
        not any(item.planner_visible and item.ref_id.startswith(prefix)
                for item in snapshot.authorized_inputs)
        for prefix in required_prefixes
    )
    # Original pages are durable source records, not an instruction to re-read
    # them on every later Agent run.  Once the governed posture and confirmed
    # facts exist, analysis must use those source-bound records; otherwise a
    # preserved PDF page would repeatedly route the matter through extraction.
    if missing and goal.active_plan_execution is None:
        return _material_preparation_proposal(goal=goal, snapshot=snapshot, skills=skills)
    if _CASE_CONTEXT_SKILL_ID not in available_skills:
        raise CasePlannerBlocked("defence route has no case-context Skill")
    selected = _controlled_sources(snapshot, required_prefixes=required_prefixes)
    if LAWYER_ANALYSIS_SKILL_ID not in available_skills:
        raise CasePlannerBlocked("defence route has no lawyer-analysis Skill")
    if any(
        LAWYER_ANALYSIS_SKILL_ID not in item.allowed_skill_ids
        for item in snapshot.authorized_inputs
        if item.ref_id in selected
    ):
        raise CasePlannerBlocked(
            "defence sources are not authorized for lawyer analysis"
        )
    tasks = (
        ProposedPlannerTask(
            proposal_id="defence-case-context",
            skill_id=_CASE_CONTEXT_SKILL_ID,
            purpose="依据已确认被告应诉来源形成案件情境与缺口候选。",
            dependency_ids=(),
            input_ref_ids=selected,
            risk_hint=PlannerRiskHint.LOW,
        ),
        ProposedPlannerTask(
            proposal_id="defence-lawyer-analysis",
            skill_id=LAWYER_ANALYSIS_SKILL_ID,
            purpose="在不创设事实、金额、期限或法律结论的前提下形成律师决策包候选。",
            dependency_ids=("defence-case-context",),
            input_ref_ids=selected,
            risk_hint=PlannerRiskHint.HIGH,
        ),
    )
    return CasePlanProposal(
        goal_hash=goal.goal_hash,
        planning_snapshot_hash=snapshot.planning_hash,
        tasks=tasks,
    )


def _material_preparation_proposal(
    *, goal: AgentGoal, snapshot: CasePlanningSnapshot,
    skills: tuple[PlannerSemanticSkill, ...],
) -> CasePlanProposal:
    """Read original inputs without inventing facts or calling a planner.

    The compiler retains all per-tool permissions, approvals and run budgets.
    Reading output is only a candidate; it is not a defence statement.
    """
    available = {item.skill_id for item in skills}
    readers = ("pdf_reading", "office_reading", "image_visual_ocr")
    tasks = []
    grouped: dict[str, list[str]] = {reader: [] for reader in readers}
    extractable: list[str] = []
    for item in sorted(snapshot.authorized_inputs, key=lambda value: value.ref_id):
        if not item.planner_visible or not item.ref_id.startswith(("evidence-page:", "material-object:")):
            continue
        eligible = [reader for reader in readers
                    if reader in available and reader in item.allowed_skill_ids]
        # Native PDF text is the deterministic, local first pass. OCR being
        # available is a fallback capability, not an ambiguous source type.
        # Empty/unreadable text still follows the reader's review contract;
        # this preference must never pretend a scanned page was extracted.
        if (item.ref_id.startswith("evidence-page:") and "pdf_reading" in eligible
                and set(eligible).issubset({"pdf_reading", "image_visual_ocr"})):
            eligible = ["pdf_reading"]
        if len(eligible) != 1:
            raise CasePlannerBlocked("material preparation requires one authorized reading Skill per source")
        reader = eligible[0]
        grouped[reader].append(item.ref_id)
        if (reader == "pdf_reading" and item.ref_id.startswith("evidence-page:")
                and _LEDGER_EXTRACTION_SKILL_ID in available
                and _LEDGER_EXTRACTION_SKILL_ID in item.allowed_skill_ids):
            extractable.append(item.ref_id)
    for reader, refs in grouped.items():
        if not refs:
            continue
        tasks.append(ProposedPlannerTask(
            proposal_id=f"defence-read-{len(tasks) + 1}",
            skill_id=reader,
            purpose="读取本案原始材料并形成可回到来源核对的候选；不确认事实或生成法律立场。",
            dependency_ids=(), input_ref_ids=tuple(refs),
            risk_hint=PlannerRiskHint.HIGH if reader == "image_visual_ocr" else PlannerRiskHint.LOW,
        ))
    if not tasks:
        raise CasePlannerBlocked("defence route lacks required governed sources and readable original materials")
    if extractable:
        read_ids = tuple(task.proposal_id for task in tasks
                         if task.skill_id in {"pdf_reading", "image_visual_ocr"})
        tasks.append(ProposedPlannerTask(
            proposal_id="defence-extract-candidates", skill_id=_LEDGER_EXTRACTION_SKILL_ID,
            purpose="从已读取的授权原生PDF页提取保留陈述主体、逐字摘录和来源页的事实及交易候选，交律师复核；不自动确认或形成法律立场。",
            dependency_ids=read_ids, input_ref_ids=tuple(extractable),
            risk_hint=PlannerRiskHint.HIGH,
        ))
    return CasePlanProposal(goal_hash=goal.goal_hash,
                            planning_snapshot_hash=snapshot.planning_hash,
                            tasks=tuple(tasks))


def _controlled_sources(snapshot: CasePlanningSnapshot, *,
                        required_prefixes: tuple[str, ...] = _REQUIRED_REF_PREFIXES) -> tuple[str, ...]:
    by_prefix: dict[str, list[object]] = {prefix: [] for prefix in _CONTROLLED_REF_PREFIXES}
    for item in snapshot.authorized_inputs:
        if not item.planner_visible:
            continue
        for prefix in _CONTROLLED_REF_PREFIXES:
            if item.ref_id.startswith(prefix):
                by_prefix[prefix].append(item)
                break
    missing = tuple(prefix for prefix in required_prefixes if not by_prefix[prefix])
    if missing:
        raise CasePlannerBlocked(
            "defence route lacks required governed sources: " + ", ".join(missing)
        )
    selected = [
        item
        for prefix in _REQUIRED_REF_PREFIXES
        for item in by_prefix[prefix]
    ]
    # A confirmed dispute issue is not required for a first read: the Agent
    # may need to propose one.  When it exists, however, it must enter the
    # controlled analysis so the model reviews the lawyer-confirmed question
    # instead of rediscovering a parallel issue from the same facts.
    preliminary = required_prefixes == _ANALYSIS_REQUIRED_REF_PREFIXES
    if preliminary:
        selected.extend(by_prefix["issue:"])
    selected = tuple(selected)
    expected_status = {
        "posture-profile:": "CONFIRMED",
        "fact:": "CONFIRMED",
        "claim:": "CONFIRMED",
        "legal-source:": "LOCKED",
        "legal-rule:": "LOCKED",
        "issue:": "CONFIRMED",
    }
    if preliminary and not any(item.status.value == "CONFIRMED" for item in by_prefix["fact:"]):
        raise CasePlannerBlocked("initial analysis requires a confirmed source fact")
    for item in selected:
        prefix = next(
            candidate
            for candidate in _CONTROLLED_REF_PREFIXES
            if item.ref_id.startswith(candidate)
        )
        # A source-bound disputed/candidate fact is an analysis input, not an
        # approved premise. Keep its status/hash unchanged for the context and
        # decision contracts, which explicitly distinguish uncertain anchors.
        # Document execution retains the original confirmed-only gate.
        allowed_statuses = {expected_status[prefix]}
        if preliminary and prefix in {"fact:", "claim:"}:
            allowed_statuses.update({"REVIEW_REQUIRED", "DISPUTED", "OPEN", "BLOCKED"})
        if item.status.value not in allowed_statuses:
            raise CasePlannerBlocked("defence source status is not governed")
        if _CASE_CONTEXT_SKILL_ID not in item.allowed_skill_ids:
            raise CasePlannerBlocked("defence source is not authorized for case context")
    review_inputs = tuple(item for item in snapshot.authorized_inputs
        if item.planner_visible and item.ref_id.startswith("review-obligation:"))
    transaction_candidates = tuple(item for item in snapshot.authorized_inputs
        if item.planner_visible and item.ref_id.startswith(("transaction-candidate:", "fact-candidate:")))
    if transaction_candidates and not preliminary:
        raise CasePlannerBlocked("unconfirmed transactions cannot become document sources")
    for item in transaction_candidates:
        if item.status.value != "REVIEW_REQUIRED" or _CASE_CONTEXT_SKILL_ID not in item.allowed_skill_ids:
            raise CasePlannerBlocked("transaction candidate is not a governed review source")
    if review_inputs and not preliminary:
        raise CasePlannerBlocked("pending review obligations cannot become document sources")
    for item in review_inputs:
        if item.status.value not in {"OPEN", "BLOCKED", "DISPUTED"} or _CASE_CONTEXT_SKILL_ID not in item.allowed_skill_ids:
            raise CasePlannerBlocked("review obligation is not governed")
    # Candidate rows are deliberately not a model premise in the initial
    # decision package. They remain immutable material-review work, while the
    # review obligations above tell the lawyer and Agent what still needs
    # resolving before a candidate can become a confirmed fact.
    return tuple(sorted(item.ref_id for item in (*selected, *review_inputs)))


__all__ = [
    "CONTROLLED_DEFENCE_PLANNER_ID",
    "CONTROLLED_DEFENCE_ROUTER_ID",
    "ControlledDefencePlanningSnapshotProvider",
    "ControlledDefencePlanningOutcomeRecorder",
    "ControlledDefencePlanningRouter",
]
