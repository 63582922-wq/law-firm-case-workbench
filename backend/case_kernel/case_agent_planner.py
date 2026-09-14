"""Bounded planning kernel for the lawyer case Agent.

The model is an untrusted proposer.  It receives a server-built projection of
the current matter and may return only semantic Skill requests.  This module
then resolves every executable property (Tool, adapter, versions, scopes,
approval, sandbox, network, retry and budgets) from server policy before the
existing supervisor accepts the graph.

Nothing in this module accepts a browser path, URL, shell command or Tool
payload.  Case material text and summaries remain untrusted data even after
they have entered a planning snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping
from uuid import UUID, uuid5

from .case_agent_supervisor import (
    ActivePlanDeliverableRef,
    AgentAutonomyLevel,
    AgentDeliverableFormat,
    AgentGoal,
    AgentRiskLevel,
    AgentSupervisorBlocked,
    AgentTaskGraph,
    AgentTaskSpec,
    CaseSnapshotRef,
    NetworkPolicy,
    RetryMode,
    RunResourceBudget,
    RuntimeAdapterManifest,
    SkillBinding,
    TaskCapabilityContract,
    TaskResourceBudget,
    compile_task_graph,
)
from .skill_registry import (
    ApprovalGate,
    CapabilityScope,
    CaseSkillRegistry,
    SkillMaturity,
    SkillRegistryBlocked,
)
from .case_agent_legal_research_contract import LEGAL_RESEARCH_PLANNING_SKILL_ID
from .public_legal_vocabulary import derive_public_legal_terms


PLANNER_PROPOSAL_SCHEMA_VERSION = "lawyer-agent-plan-proposal-v1"
PLANNING_SNAPSHOT_SCHEMA_VERSION = "lawyer-agent-planning-snapshot-v2"
REEXTRACTION_SKILL_ID = "case_ledger_extraction"
ACTIVE_PLAN_DOCUMENT_SKILL_ID = "dynamic_document_delivery"
ACTIVE_PLAN_SPREADSHEET_SKILL_ID = "dynamic_spreadsheet_delivery"
SERVER_ONLY_EXECUTION_SKILL_IDS = frozenset(
    {
        ACTIVE_PLAN_DOCUMENT_SKILL_ID,
        ACTIVE_PLAN_SPREADSHEET_SKILL_ID,
        LEGAL_RESEARCH_PLANNING_SKILL_ID,
    }
)
_ACTIVE_PLAN_EXECUTION_SCHEMA_VERSION = "active-work-plan-execution-task-v1"
_REEXTRACTION_OBLIGATION_SCHEMA_VERSION = (
    "case-ledger-reextraction-planning-obligation-v1"
)
_LEGAL_RESEARCH_OBLIGATION_SCHEMA_VERSION = (
    "case-agent-legal-research-planning-obligation-v1"
)


class CasePlannerBlocked(ValueError):
    """The snapshot, model proposal or server policy cannot be compiled safely."""


class CasePlannerBudgetExceeded(CasePlannerBlocked):
    """Server-computed capacity diagnostic; never an authorization to expand it."""

    def __init__(self, *, dimension: str, required: int, available: int, task_count: int) -> None:
        super().__init__(f"compiled task graph exceeds the run {dimension} budget")
        self.dimension = dimension
        self.required = required
        self.available = available
        self.task_count = task_count


class CasePlannerAdmissionBlocked(CasePlannerBlocked):
    """A deterministic server obligation cannot enter this task graph."""

    def __init__(self, message: str, *, error_code: str) -> None:
        _bounded_code(error_code, "planning admission error_code", 80)
        if not error_code.startswith(("REEXTRACTION_", "LEGAL_RESEARCH_")):
            raise ValueError("planning admission error code is outside policy")
        super().__init__(message)
        self.error_code = error_code


class PlanningInputKind(StrEnum):
    MATERIAL = "MATERIAL"
    EVIDENCE_PAGE = "EVIDENCE_PAGE"
    CONFIRMED_FACT = "CONFIRMED_FACT"
    CONFIRMED_TRANSACTION = "CONFIRMED_TRANSACTION"
    LEGAL_GAP = "LEGAL_GAP"
    WORK_PLAN_ITEM = "WORK_PLAN_ITEM"
    PROCEDURAL_EVENT = "PROCEDURAL_EVENT"
    VERIFIED_SOURCE = "VERIFIED_SOURCE"
    REVIEWABLE_ARTIFACT = "REVIEWABLE_ARTIFACT"
    AUTHORIZED_MEMORY = "AUTHORIZED_MEMORY"


class PlanningInputStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    CONFIRMED = "CONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DISPUTED = "DISPUTED"
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    LOCKED = "LOCKED"


class PlanningSignalCategory(StrEnum):
    PROCEEDING = "PROCEEDING"
    PARTY_POSTURE = "PARTY_POSTURE"
    WORK_PLAN = "WORK_PLAN"
    CONFIRMED_FACT = "CONFIRMED_FACT"
    LEGAL_GAP = "LEGAL_GAP"


class PlannerRiskHint(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class PlanningInputRef:
    """An already-authorized, versioned object reference; never a file path."""

    ref_id: str
    kind: PlanningInputKind
    object_version: str
    content_hash: str
    status: PlanningInputStatus
    allowed_skill_ids: tuple[str, ...]
    planner_visible: bool = True

    def validate(self) -> None:
        _identifier(self.ref_id, "planning input ref_id")
        _bounded_code(self.object_version, "planning input object_version", 200)
        _sha256(self.content_hash, "planning input content_hash")
        if not self.allowed_skill_ids or len(self.allowed_skill_ids) > 100:
            raise CasePlannerBlocked("planning input requires a bounded Skill allowlist")
        if tuple(sorted(set(self.allowed_skill_ids))) != self.allowed_skill_ids:
            raise CasePlannerBlocked("planning input Skill allowlist must be sorted and unique")
        for skill_id in self.allowed_skill_ids:
            _code(skill_id, "planning input allowed skill_id")
        if not isinstance(self.planner_visible, bool):
            raise CasePlannerBlocked("planning input visibility flag is invalid")


@dataclass(frozen=True)
class CasePlanningSignal:
    """A status signal derived by the server from governed case ledgers.

    ``summary`` is deliberately treated as untrusted case data.  It helps a
    planner distinguish posture, proceeding stage and gaps, but can never
    authorize an operation.
    """

    signal_id: str
    category: PlanningSignalCategory
    code: str
    status: PlanningInputStatus
    summary: str
    source_ref_ids: tuple[str, ...]

    def validate(self) -> None:
        _identifier(self.signal_id, "planning signal_id")
        _bounded_code(self.code, "planning signal code", 200)
        _business_text(self.summary, "planning signal summary", 1_000)
        if not self.source_ref_ids or len(self.source_ref_ids) > 100:
            raise CasePlannerBlocked("planning signal requires bounded source references")
        if len(self.source_ref_ids) != len(set(self.source_ref_ids)):
            raise CasePlannerBlocked("planning signal source references must be unique")
        for ref_id in self.source_ref_ids:
            _identifier(ref_id, "planning signal source ref_id")


@dataclass(frozen=True)
class ReextractionPlanningObligation:
    """Server-only exact-source work that a model is not allowed to omit.

    The lifecycle repository derives these obligations from the current
    ``ACTIVE`` 0049 heads.  They are included in the private planning hash but
    deliberately excluded from the external planner payload.  ``obligation_id``
    is a deterministic identity for one control run and one exact page set;
    multiple follow-ups over that same page set therefore compile to one task.
    """

    obligation_id: str
    control_run_id: str
    followup_ids: tuple[str, ...]
    source_ref_ids: tuple[str, ...]
    lifecycle_hash: str

    @classmethod
    def build(
        cls,
        *,
        control_run_id: str,
        followup_ids: Iterable[str],
        source_ref_ids: Iterable[str],
        lifecycle_hash: str,
    ) -> "ReextractionPlanningObligation":
        _canonical_uuid(control_run_id, "re-extraction control_run_id")
        followups = tuple(sorted(tuple(followup_ids)))
        sources = tuple(sorted(tuple(source_ref_ids)))
        obligation_id = str(
            uuid5(
                UUID(control_run_id),
                _canonical_hash(
                    {
                        "schema_version": _REEXTRACTION_OBLIGATION_SCHEMA_VERSION,
                        "source_ref_ids": sources,
                    }
                ),
            )
        )
        result = cls(
            obligation_id=obligation_id,
            control_run_id=control_run_id,
            followup_ids=followups,
            source_ref_ids=sources,
            lifecycle_hash=lifecycle_hash,
        )
        result.validate()
        return result

    def validate(self) -> None:
        _canonical_uuid(self.obligation_id, "re-extraction obligation_id")
        _canonical_uuid(self.control_run_id, "re-extraction control_run_id")
        _sha256(self.lifecycle_hash, "re-extraction lifecycle_hash")
        if (
            not self.followup_ids
            or len(self.followup_ids) > 500
            or tuple(sorted(set(self.followup_ids))) != self.followup_ids
        ):
            raise CasePlannerBlocked(
                "re-extraction obligation follow-ups must be canonical and bounded"
            )
        for followup_id in self.followup_ids:
            _canonical_uuid(followup_id, "re-extraction followup_id")
        if (
            not self.source_ref_ids
            or len(self.source_ref_ids) > 500
            or tuple(sorted(set(self.source_ref_ids))) != self.source_ref_ids
        ):
            raise CasePlannerBlocked(
                "re-extraction obligation sources must be canonical and bounded"
            )
        for ref_id in self.source_ref_ids:
            _identifier(ref_id, "re-extraction source ref_id")
            if not ref_id.startswith("evidence-page:"):
                raise CasePlannerBlocked(
                    "re-extraction obligation may cite only evidence pages"
                )
            _canonical_uuid(
                ref_id.removeprefix("evidence-page:"),
                "re-extraction evidence_page_id",
            )
        expected_id = str(
            uuid5(
                UUID(self.control_run_id),
                _canonical_hash(
                    {
                        "schema_version": _REEXTRACTION_OBLIGATION_SCHEMA_VERSION,
                        "source_ref_ids": self.source_ref_ids,
                    }
                ),
            )
        )
        if self.obligation_id != expected_id:
            raise CasePlannerBlocked(
                "re-extraction obligation identity differs from its exact sources"
            )


@dataclass(frozen=True)
class CasePlanningSnapshot:
    """Server-built planning projection bound to an immutable case snapshot."""

    case_snapshot: CaseSnapshotRef
    authorized_inputs: tuple[PlanningInputRef, ...]
    signals: tuple[CasePlanningSignal, ...]
    planning_hash: str
    reextraction_obligations: tuple[ReextractionPlanningObligation, ...] = ()
    schema_version: str = PLANNING_SNAPSHOT_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        case_snapshot: CaseSnapshotRef,
        authorized_inputs: Iterable[PlanningInputRef],
        signals: Iterable[CasePlanningSignal],
        reextraction_obligations: Iterable[ReextractionPlanningObligation] = (),
    ) -> "CasePlanningSnapshot":
        case_snapshot.validate()
        inputs = tuple(sorted(tuple(authorized_inputs), key=lambda item: item.ref_id))
        signal_items = tuple(sorted(tuple(signals), key=lambda item: item.signal_id))
        obligations = tuple(
            sorted(
                tuple(reextraction_obligations),
                key=lambda item: item.obligation_id,
            )
        )
        if not inputs or len(inputs) > 500:
            raise CasePlannerBlocked("planning snapshot requires 1 to 500 authorized inputs")
        if len(signal_items) > 200:
            raise CasePlannerBlocked("planning snapshot has too many status signals")
        input_ids: set[str] = set()
        for item in inputs:
            item.validate()
            if item.ref_id in input_ids:
                raise CasePlannerBlocked("planning input references must be unique")
            input_ids.add(item.ref_id)
        signal_ids: set[str] = set()
        for item in signal_items:
            item.validate()
            if item.signal_id in signal_ids:
                raise CasePlannerBlocked("planning signal identifiers must be unique")
            signal_ids.add(item.signal_id)
            if not set(item.source_ref_ids).issubset(input_ids):
                raise CasePlannerBlocked("planning signal cites an unauthorized input reference")
        if len(obligations) > 500:
            raise CasePlannerBlocked("planning snapshot has too many re-extraction obligations")
        input_index = {item.ref_id: item for item in inputs}
        obligation_ids: set[str] = set()
        obligation_sources: set[tuple[str, ...]] = set()
        control_run_ids: set[str] = set()
        for obligation in obligations:
            if not isinstance(obligation, ReextractionPlanningObligation):
                raise CasePlannerBlocked("re-extraction planning obligation is invalid")
            obligation.validate()
            if obligation.obligation_id in obligation_ids:
                raise CasePlannerBlocked("re-extraction obligation identifiers must be unique")
            if obligation.source_ref_ids in obligation_sources:
                raise CasePlannerBlocked(
                    "one control run may have only one re-extraction obligation per source set"
                )
            obligation_ids.add(obligation.obligation_id)
            obligation_sources.add(obligation.source_ref_ids)
            control_run_ids.add(obligation.control_run_id)
            if not set(obligation.source_ref_ids).issubset(input_ids):
                raise CasePlannerBlocked(
                    "re-extraction obligation cites an unauthorized evidence page"
                )
            for ref_id in obligation.source_ref_ids:
                source = input_index[ref_id]
                if (
                    source.kind is not PlanningInputKind.EVIDENCE_PAGE
                    or REEXTRACTION_SKILL_ID not in source.allowed_skill_ids
                ):
                    raise CasePlannerBlocked(
                        "re-extraction obligation source has no executable extraction Skill"
                    )
        if len(control_run_ids) > 1:
            raise CasePlannerBlocked(
                "active re-extraction obligations span multiple control runs"
            )
        payload = _planning_snapshot_payload(
            case_snapshot=case_snapshot,
            authorized_inputs=inputs,
            signals=signal_items,
            reextraction_obligations=obligations,
        )
        return cls(
            case_snapshot=case_snapshot,
            authorized_inputs=inputs,
            signals=signal_items,
            planning_hash=_canonical_hash(payload),
            reextraction_obligations=obligations,
        )

    def validate(self) -> None:
        expected = CasePlanningSnapshot.build(
            case_snapshot=self.case_snapshot,
            authorized_inputs=self.authorized_inputs,
            signals=self.signals,
            reextraction_obligations=self.reextraction_obligations,
        )
        if self.schema_version != PLANNING_SNAPSHOT_SCHEMA_VERSION or self != expected:
            raise CasePlannerBlocked("planning snapshot hash or schema differs from its contents")


@dataclass(frozen=True)
class ProposedPlannerTask:
    proposal_id: str
    skill_id: str
    purpose: str
    dependency_ids: tuple[str, ...]
    input_ref_ids: tuple[str, ...]
    risk_hint: PlannerRiskHint


@dataclass(frozen=True)
class CasePlanProposal:
    goal_hash: str
    planning_snapshot_hash: str
    tasks: tuple[ProposedPlannerTask, ...]
    schema_version: str = PLANNER_PROPOSAL_SCHEMA_VERSION


@dataclass(frozen=True)
class PlannerSemanticSkill:
    """The only Skill information exposed to a model."""

    skill_id: str
    title: str
    output_kind: str
    max_risk_hint: PlannerRiskHint = PlannerRiskHint.LOW
    max_input_refs: int = 500

    def validate(self) -> None:
        _code(self.skill_id, "semantic skill_id")
        _business_text(self.title, "semantic skill title", 300)
        _code(self.output_kind, "semantic skill output_kind")
        if not isinstance(self.max_risk_hint, PlannerRiskHint):
            raise CasePlannerBlocked("semantic skill risk ceiling is invalid")
        if (
            isinstance(self.max_input_refs, bool)
            or not isinstance(self.max_input_refs, int)
            or not 1 <= self.max_input_refs <= 500
        ):
            raise CasePlannerBlocked("semantic skill input-reference limit is invalid")


@dataclass(frozen=True)
class ServerSkillExecutionPolicy:
    """Server-owned executable policy for one semantic Skill.

    The planner never sees this structure.  Consequently it cannot select a
    Tool or adapter, alter approval, request a domain, relax a sandbox or
    assign its own retry/resource budget.
    """

    skill_id: str
    tool_id: str
    sandbox_profile: str
    allowed_domains: tuple[str, ...]
    risk_level: AgentRiskLevel
    autonomy_level: AgentAutonomyLevel
    approval_gate: ApprovalGate
    retry_mode: RetryMode
    task_budget: TaskResourceBudget
    max_input_refs: int = 500

    def validate_shape(self) -> None:
        _code(self.skill_id, "policy skill_id")
        _code(self.tool_id, "policy tool_id")
        _code(self.sandbox_profile, "policy sandbox_profile")
        self.task_budget.validate()
        if (
            isinstance(self.max_input_refs, bool)
            or not isinstance(self.max_input_refs, int)
            or not 1 <= self.max_input_refs <= 500
        ):
            raise CasePlannerBlocked("policy input-reference limit is invalid")
        if tuple(sorted(set(self.allowed_domains))) != self.allowed_domains:
            raise CasePlannerBlocked("policy network domains must be sorted and unique")
        for domain in self.allowed_domains:
            if _DOMAIN_RE.fullmatch(domain) is None or "*" in domain:
                raise CasePlannerBlocked("policy network domains must be exact DNS names")


class CaseAgentPlannerCompiler:
    """Compile strict semantic proposals into supervisor-owned task graphs."""

    def __init__(
        self,
        *,
        registry: CaseSkillRegistry,
        adapters: Mapping[str, RuntimeAdapterManifest],
        skill_policies: Iterable[ServerSkillExecutionPolicy],
    ) -> None:
        policies = tuple(skill_policies)
        self._registry = registry
        self._adapters = dict(adapters)
        self._policies = {item.skill_id: item for item in policies}
        if not policies or len(self._policies) != len(policies):
            raise CasePlannerBlocked("planner Skill policies must be non-empty and unique")
        for policy in policies:
            policy.validate_shape()
            self._validate_server_policy(policy)

    def semantic_skill_catalog(self) -> tuple[PlannerSemanticSkill, ...]:
        catalog: list[PlannerSemanticSkill] = []
        for skill_id in sorted(self._policies):
            if skill_id in SERVER_ONLY_EXECUTION_SKILL_IDS:
                # These capabilities are selected only from an ACTIVE plan by
                # ``_compile_active_plan_execution``.  Advertising them to an
                # external semantic planner would let model output bypass the
                # exact one-plan-item/one-document binding.
                continue
            policy = self._policies[skill_id]
            skill = self._registry.get_skill(skill_id)
            if skill.maturity is not SkillMaturity.IMPLEMENTED:
                # A disabled capability is never advertised to a model.
                continue
            item = PlannerSemanticSkill(
                skill.skill_id,
                skill.title,
                skill.output_kind,
                _risk_hint(policy.risk_level),
                policy.max_input_refs,
            )
            item.validate()
            catalog.append(item)
        if not catalog:
            raise CasePlannerBlocked("no executable semantic Skills are available")
        return tuple(catalog)

    def compile(
        self,
        *,
        graph_id: str,
        graph_version: int,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
        proposal: CasePlanProposal,
        run_budget: RunResourceBudget,
        allow_server_only_inputs: bool = False,
    ) -> AgentTaskGraph:
        snapshot.validate()
        run_budget.validate()
        if (
            snapshot.reextraction_obligations
            and len(snapshot.reextraction_obligations) >= run_budget.max_tasks
        ):
            raise CasePlannerAdmissionBlocked(
                "active re-extraction obligations leave no guaranteed planning capacity",
                error_code="REEXTRACTION_GRAPH_CAPACITY_EXCEEDED",
            )
        if not isinstance(allow_server_only_inputs, bool):
            raise CasePlannerBlocked("server-only input policy is invalid")
        _validate_proposal(
            proposal,
            expected_goal_hash=goal.goal_hash,
            expected_snapshot_hash=snapshot.planning_hash,
        )
        try:
            graph_namespace = UUID(graph_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise CasePlannerBlocked("graph_id must be a UUID") from error

        input_index = {item.ref_id: item for item in snapshot.authorized_inputs}
        if goal.material_read_refs:
            # Scope is server-bound goal data, never inferred from model prose.
            if snapshot.reextraction_obligations or goal.active_plan_execution is not None or goal.requested_deliverables:
                raise CasePlannerBlocked("material reading cannot absorb whole-case workflows")
            allowed_refs = set(goal.material_read_refs)
            used_refs = set()
            for task in proposal.tasks:
                if task.skill_id not in {"pdf_reading", "office_reading", "image_visual_ocr"}:
                    raise CasePlannerBlocked("material reading scope rejects non-reading Skill")
                if not set(task.input_ref_ids) <= allowed_refs:
                    raise CasePlannerBlocked("material reading task exceeds exact source scope")
                used_refs.update(task.input_ref_ids)
            if used_refs != allowed_refs:
                raise CasePlannerBlocked("material reading proposal omits requested sources")
            if any(ref not in input_index or input_index[ref].kind not in {
                PlanningInputKind.MATERIAL, PlanningInputKind.EVIDENCE_PAGE,
            } for ref in allowed_refs):
                raise CasePlannerBlocked("material reading scope is not available original material")
        if goal.active_plan_execution is not None:
            return self._compile_active_plan_execution(
                graph_id=graph_id,
                graph_version=graph_version,
                graph_namespace=graph_namespace,
                goal=goal,
                snapshot=snapshot,
                input_index=input_index,
                run_budget=run_budget,
            )
        legal_research_inputs = self._legal_research_obligation_inputs(
            goal=goal,
            snapshot=snapshot,
        )
        legal_research_task_id = (
            str(
                uuid5(
                    graph_namespace,
                    f"{_LEGAL_RESEARCH_OBLIGATION_SCHEMA_VERSION}:"
                    f"{snapshot.planning_hash}:{goal.goal_hash}",
                )
            )
            if legal_research_inputs
            else None
        )
        server_obligation_count = len(snapshot.reextraction_obligations) + (
            1 if legal_research_inputs else 0
        )
        if server_obligation_count >= run_budget.max_tasks:
            if snapshot.reextraction_obligations:
                raise CasePlannerAdmissionBlocked(
                    "server obligations leave no guaranteed planning capacity",
                    error_code="REEXTRACTION_GRAPH_CAPACITY_EXCEEDED",
                )
            raise CasePlannerAdmissionBlocked(
                "legal-research planning leaves no guaranteed planning capacity",
                error_code="LEGAL_RESEARCH_GRAPH_CAPACITY_EXCEEDED",
            )
        obligation_by_sources = {
            item.source_ref_ids: item for item in snapshot.reextraction_obligations
        }
        obligation_task_ids = {
            item.obligation_id: str(
                uuid5(
                    graph_namespace,
                    f"{_REEXTRACTION_OBLIGATION_SCHEMA_VERSION}:{item.obligation_id}",
                )
            )
            for item in snapshot.reextraction_obligations
        }
        replacement_by_proposal: dict[str, ReextractionPlanningObligation] = {}
        task_id_by_proposal: dict[str, str] = {}
        for task in proposal.tasks:
            replacement = (
                obligation_by_sources.get(tuple(sorted(task.input_ref_ids)))
                if task.skill_id == REEXTRACTION_SKILL_ID
                else None
            )
            if replacement is not None:
                replacement_by_proposal[task.proposal_id] = replacement
                task_id_by_proposal[task.proposal_id] = obligation_task_ids[
                    replacement.obligation_id
                ]
                continue
            task_id_by_proposal[task.proposal_id] = str(
                uuid5(
                    graph_namespace,
                    _canonical_hash(
                        {
                            "schema_version": PLANNER_PROPOSAL_SCHEMA_VERSION,
                            "goal_hash": proposal.goal_hash,
                            "planning_snapshot_hash": proposal.planning_snapshot_hash,
                            "task": _proposal_task_payload(task),
                        }
                    ),
                )
            )

        # Re-extraction obligations are intentionally hidden from the model,
        # but they still consume real graph slots.  Reject the combined graph
        # with a durable, policy-specific admission code before compiling any
        # task instead of letting the model's otherwise-valid proposal fail as
        # a generic graph-size error later.  Exact model duplicates of a
        # server obligation have already been identified above and collapse to
        # the authoritative server task, so they do not consume another slot.
        effective_task_count = (
            len(snapshot.reextraction_obligations)
            + (1 if legal_research_inputs else 0)
            + len(proposal.tasks)
            - len(replacement_by_proposal)
        )
        if (
            snapshot.reextraction_obligations
            and effective_task_count > run_budget.max_tasks
        ):
            raise CasePlannerAdmissionBlocked(
                "active re-extraction obligations exceed the remaining task budget",
                error_code="REEXTRACTION_GRAPH_CAPACITY_EXCEEDED",
            )
        if legal_research_inputs and effective_task_count > run_budget.max_tasks:
            raise CasePlannerAdmissionBlocked(
                "legal-research planning exceeds the remaining task budget",
                error_code="LEGAL_RESEARCH_GRAPH_CAPACITY_EXCEEDED",
            )

        compiled: list[AgentTaskSpec] = []
        for obligation in snapshot.reextraction_obligations:
            compiled.append(
                self._compile_reextraction_obligation_task(
                    task_id=obligation_task_ids[obligation.obligation_id],
                    sequence=len(compiled) + 1,
                    goal=goal,
                    snapshot=snapshot,
                    obligation=obligation,
                    input_index=input_index,
                )
            )
        if legal_research_inputs:
            assert legal_research_task_id is not None
            compiled.append(
                self._compile_legal_research_obligation_task(
                    task_id=legal_research_task_id,
                    sequence=len(compiled) + 1,
                    goal=goal,
                    snapshot=snapshot,
                    input_refs=legal_research_inputs,
                    input_index=input_index,
                )
            )
        for proposed in proposal.tasks:
            if proposed.proposal_id in replacement_by_proposal:
                # The server task above is authoritative.  Model duplicates
                # are collapsed, and downstream dependencies are redirected
                # through ``task_id_by_proposal`` to that exact task.
                continue
            if proposed.skill_id in SERVER_ONLY_EXECUTION_SKILL_IDS:
                raise CasePlannerBlocked(
                    "proposal requested a server-owned execution Skill"
                )
            policy = self._policies.get(proposed.skill_id)
            if policy is None:
                raise CasePlannerBlocked("proposal requested an unavailable semantic Skill")
            selected_inputs: list[PlanningInputRef] = []
            for ref_id in proposed.input_ref_ids:
                input_ref = input_index.get(ref_id)
                if input_ref is None:
                    raise CasePlannerBlocked("proposal cited an unauthorized input reference")
                if proposed.skill_id not in input_ref.allowed_skill_ids:
                    raise CasePlannerBlocked("proposal Skill is not authorized for an input reference")
                selected_inputs.append(input_ref)
            if not selected_inputs:
                raise CasePlannerBlocked("every proposed task requires server-owned input references")
            if len(selected_inputs) > policy.max_input_refs:
                raise CasePlannerBlocked(
                    "proposal exceeds the server policy input-reference limit"
                )
            if (
                not allow_server_only_inputs
                and any(not item.planner_visible for item in selected_inputs)
            ):
                raise CasePlannerBlocked(
                    "external planner cited a server-only authorized input reference"
                )
            unknown_dependencies = set(proposed.dependency_ids) - set(task_id_by_proposal)
            if unknown_dependencies:
                raise CasePlannerBlocked("proposal cites an unknown dependency")
            if _RISK_ORDER[proposed.risk_hint] > _RISK_ORDER[_risk_hint(policy.risk_level)]:
                raise CasePlannerBlocked("proposal risk hint exceeds the registered server policy")

            dependency_ids = tuple(
                dict.fromkeys(
                    task_id_by_proposal[item] for item in proposed.dependency_ids
                )
            )

            skill = self._registry.get_skill(policy.skill_id)
            tool = self._registry.authorize_tool(
                skill_id=policy.skill_id,
                tool_id=policy.tool_id,
                granted_scopes=skill.required_scopes,
                lawyer_approved=True,
                release_locked=True,
            )
            adapter = self._adapters[policy.tool_id]
            network_policy = (
                NetworkPolicy.EXACT_ALLOWLIST
                if tool.allows_external_network
                else NetworkPolicy.DENY
            )
            input_hash = _canonical_hash(
                {
                    "schema_version": "lawyer-agent-compiled-task-input-v1",
                    "goal_hash": goal.goal_hash,
                    "case_snapshot_hash": snapshot.case_snapshot.snapshot_hash,
                    "planning_snapshot_hash": snapshot.planning_hash,
                    "proposal": _proposal_task_payload(proposed),
                    "authorized_inputs": [
                        {
                            "ref_id": item.ref_id,
                            "kind": item.kind.value,
                            "object_version": item.object_version,
                            "content_hash": item.content_hash,
                            "status": item.status.value,
                        }
                        for item in selected_inputs
                    ],
                    "server_policy": _server_policy_payload(policy, adapter),
                }
            )
            compiled.append(
                AgentTaskSpec(
                    task_id=task_id_by_proposal[proposed.proposal_id],
                    sequence=len(compiled) + 1,
                    title=skill.title,
                    purpose=proposed.purpose,
                    rationale=(
                        "服务器依据当前案件快照、已登记语义 Skill 与固定执行策略编译；"
                        "模型建议不授予任何额外权限。"
                    ),
                    dependency_ids=dependency_ids,
                    input_refs=proposed.input_ref_ids,
                    input_hash=input_hash,
                    skill=SkillBinding(
                        skill_id=skill.skill_id,
                        skill_version=skill.version,
                        tool_id=tool.tool_id,
                        tool_version=tool.version,
                        adapter_id=adapter.adapter_id,
                        adapter_version=adapter.adapter_version,
                    ),
                    granted_scopes=skill.required_scopes,
                    capability=TaskCapabilityContract(
                        execution_mode=adapter.execution_mode,
                        network_policy=network_policy,
                        allowed_domains=policy.allowed_domains,
                        sandbox_profile=policy.sandbox_profile,
                        sandbox_policy_version=adapter.sandbox_policy_version,
                        sandbox_policy_hash=adapter.sandbox_policy_hash,
                        reads_case_objects=proposed.input_ref_ids,
                        writes_managed_derivatives=tool.writes_only_managed_derivatives,
                        external_request_approval_required=tool.allows_external_network,
                    ),
                    risk_level=policy.risk_level,
                    autonomy_level=policy.autonomy_level,
                    approval_gate=policy.approval_gate,
                    retry_mode=policy.retry_mode,
                    budget=policy.task_budget,
                )
            )
        compiled = self._allocate_optional_retries(compiled, run_budget)
        self._validate_aggregate_budget(compiled, run_budget)
        try:
            return compile_task_graph(
                graph_id=graph_id,
                graph_version=graph_version,
                goal=goal,
                snapshot=snapshot.case_snapshot,
                tasks=compiled,
                registry=self._registry,
                adapters=self._adapters,
                run_budget=run_budget,
            )
        except (AgentSupervisorBlocked, SkillRegistryBlocked, KeyError) as error:
            raise CasePlannerBlocked("semantic proposal failed server compilation") from error

    def _legal_research_obligation_inputs(
        self,
        *,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
    ) -> tuple[str, ...]:
        """Return exact current refs for the non-network legal-gap obligation.

        The model never sees or selects this task.  A production composition
        that installs the deterministic planning Skill therefore cannot finish
        a general whole-case review while silently omitting an open legal-source
        gap.  Recovery and exact ACTIVE-plan delivery runs remain separate hard
        workflows and do not absorb this obligation.
        """

        if (
            LEGAL_RESEARCH_PLANNING_SKILL_ID not in self._policies
            or bool(goal.material_read_refs)
            or goal.active_plan_execution is not None
            or snapshot.reextraction_obligations
        ):
            return ()
        open_gaps = tuple(
            signal
            for signal in snapshot.signals
            if signal.category is PlanningSignalCategory.LEGAL_GAP
            and signal.status
            in {
                PlanningInputStatus.OPEN,
                PlanningInputStatus.BLOCKED,
                PlanningInputStatus.REVIEW_REQUIRED,
                PlanningInputStatus.DISPUTED,
            }
        )
        if not open_gaps:
            return ()
        supported_kinds = {
            PlanningInputKind.CONFIRMED_FACT,
            PlanningInputKind.CONFIRMED_TRANSACTION,
            PlanningInputKind.LEGAL_GAP,
            PlanningInputKind.WORK_PLAN_ITEM,
            PlanningInputKind.PROCEDURAL_EVENT,
            PlanningInputKind.VERIFIED_SOURCE,
        }
        selected = tuple(
            item.ref_id
            for item in snapshot.authorized_inputs
            if item.kind in supported_kinds
            and LEGAL_RESEARCH_PLANNING_SKILL_ID in item.allowed_skill_ids
        )
        if not selected:
            raise CasePlannerAdmissionBlocked(
                "open legal gap has no executable governed research inputs",
                error_code="LEGAL_RESEARCH_INPUTS_UNAVAILABLE",
            )
        if not set(ref for signal in open_gaps for ref in signal.source_ref_ids).intersection(
            selected
        ):
            raise CasePlannerAdmissionBlocked(
                "open legal gap is not bound to the legal-research task inputs",
                error_code="LEGAL_RESEARCH_INPUTS_UNAVAILABLE",
            )
        return selected

    def _compile_legal_research_obligation_task(
        self,
        *,
        task_id: str,
        sequence: int,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
        input_refs: tuple[str, ...],
        input_index: Mapping[str, PlanningInputRef],
    ) -> AgentTaskSpec:
        policy = self._policies.get(LEGAL_RESEARCH_PLANNING_SKILL_ID)
        if policy is None:  # pragma: no cover - selected only when installed
            raise CasePlannerBlocked("legal-research planning policy is unavailable")
        selected_inputs = tuple(input_index[ref] for ref in input_refs)
        if len(selected_inputs) > policy.max_input_refs:
            raise CasePlannerAdmissionBlocked(
                "legal-research planning exceeds its exact input limit",
                error_code="LEGAL_RESEARCH_INPUTS_UNAVAILABLE",
            )
        skill = self._registry.get_skill(policy.skill_id)
        tool = self._registry.authorize_tool(
            skill_id=policy.skill_id,
            tool_id=policy.tool_id,
            granted_scopes=skill.required_scopes,
            lawyer_approved=True,
            release_locked=True,
        )
        adapter = self._adapters[policy.tool_id]
        if tool.allows_external_network or adapter.network_capable:
            raise CasePlannerBlocked(
                "legal-research planning obligation must remain network denied"
            )
        gap_signals = tuple(
            signal
            for signal in snapshot.signals
            if signal.category is PlanningSignalCategory.LEGAL_GAP
            and signal.status is not PlanningInputStatus.CONFIRMED
        )
        public_terms = derive_public_legal_terms(
            (
                goal.objective,
                *goal.success_criteria,
                *(signal.summary for signal in snapshot.signals),
            )
        )
        input_hash = _canonical_hash(
            {
                "schema_version": _LEGAL_RESEARCH_OBLIGATION_SCHEMA_VERSION,
                "goal_hash": goal.goal_hash,
                "case_snapshot_hash": snapshot.case_snapshot.snapshot_hash,
                "planning_snapshot_hash": snapshot.planning_hash,
                "legal_gap_signals": [
                    {
                        "signal_id": signal.signal_id,
                        "code": signal.code,
                        "status": signal.status.value,
                        "source_ref_ids": list(signal.source_ref_ids),
                    }
                    for signal in gap_signals
                ],
                "public_legal_terms": list(public_terms),
                "authorized_inputs": [
                    {
                        "ref_id": item.ref_id,
                        "kind": item.kind.value,
                        "object_version": item.object_version,
                        "content_hash": item.content_hash,
                        "status": item.status.value,
                    }
                    for item in selected_inputs
                ],
                "server_policy": _server_policy_payload(policy, adapter),
            }
        )
        return AgentTaskSpec(
            task_id=task_id,
            sequence=sequence,
            title=skill.title,
            purpose=(
                "围绕服务器识别的法律缺口和安全公共词“"
                + "、".join(public_terms)
                + "”形成官方法源研究候选；本步骤不联网、不确认法源效力，"
                "并为后续律师批准的脱敏检索保留明确边界。"
            ),
            rationale=(
                "该任务由服务器根据当前案件快照中的未解决法律缺口强制编译；"
                "模型不能删除、改成普通网页结论或把研究计划冒充正式法律依据。"
            ),
            dependency_ids=(),
            input_refs=input_refs,
            input_hash=input_hash,
            skill=SkillBinding(
                skill_id=skill.skill_id,
                skill_version=skill.version,
                tool_id=tool.tool_id,
                tool_version=tool.version,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            ),
            granted_scopes=skill.required_scopes,
            capability=TaskCapabilityContract(
                execution_mode=adapter.execution_mode,
                network_policy=NetworkPolicy.DENY,
                allowed_domains=(),
                sandbox_profile=policy.sandbox_profile,
                sandbox_policy_version=adapter.sandbox_policy_version,
                sandbox_policy_hash=adapter.sandbox_policy_hash,
                reads_case_objects=input_refs,
                writes_managed_derivatives=tool.writes_only_managed_derivatives,
                external_request_approval_required=False,
            ),
            risk_level=policy.risk_level,
            autonomy_level=policy.autonomy_level,
            approval_gate=policy.approval_gate,
            retry_mode=policy.retry_mode,
            budget=policy.task_budget,
        )

    def _compile_active_plan_execution(
        self,
        *,
        graph_id: str,
        graph_version: int,
        graph_namespace: UUID,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
        input_index: Mapping[str, PlanningInputRef],
        run_budget: RunResourceBudget,
    ) -> AgentTaskGraph:
        """Compile only the exact ACTIVE-plan deliverables owned by the server.

        The external planner response is deliberately not used to select,
        remove, rename or widen these tasks.  It remains a recorded planning
        exchange, but execution authority comes solely from the goal's
        server-derived active-plan binding and the current snapshot hashes.
        """

        execution = goal.active_plan_execution
        if execution is None:  # pragma: no cover - caller invariant
            raise CasePlannerBlocked("active-plan execution authority is missing")
        execution.validate()
        if snapshot.reextraction_obligations:
            raise CasePlannerBlocked(
                "active-plan document execution cannot absorb ledger recovery obligations"
            )
        if len(execution.items) > run_budget.max_tasks:
            raise CasePlannerBlocked(
                "active-plan deliverables exceed the run task budget"
            )
        active_signal = next(
            (
                item
                for item in snapshot.signals
                if item.code == "ACTIVE_DYNAMIC_WORK_PLAN"
                and item.signal_id == f"signal:work-plan:{execution.plan_id}"
                and item.status is PlanningInputStatus.CONFIRMED
            ),
            None,
        )
        if active_signal is None:
            raise CasePlannerBlocked(
                "server-bound execution plan is not the current ACTIVE work plan"
            )
        compiled = tuple(
            self._compile_active_plan_deliverable_task(
                task_id=str(
                    uuid5(
                        graph_namespace,
                        f"{_ACTIVE_PLAN_EXECUTION_SCHEMA_VERSION}:{item.item_id}:"
                        f"{item.item_hash}:{item.deliverable_kind.value}",
                    )
                ),
                sequence=sequence,
                goal=goal,
                snapshot=snapshot,
                item=item,
                input_index=input_index,
            )
            for sequence, item in enumerate(execution.items, start=1)
        )
        compiled = self._allocate_optional_retries(compiled, run_budget)
        self._validate_aggregate_budget(compiled, run_budget)
        try:
            return compile_task_graph(
                graph_id=graph_id,
                graph_version=graph_version,
                goal=goal,
                snapshot=snapshot.case_snapshot,
                tasks=compiled,
                registry=self._registry,
                adapters=self._adapters,
                run_budget=run_budget,
            )
        except (AgentSupervisorBlocked, SkillRegistryBlocked, KeyError) as error:
            raise CasePlannerBlocked(
                "active-plan deliverables failed server compilation"
            ) from error

    def _compile_active_plan_deliverable_task(
        self,
        *,
        task_id: str,
        sequence: int,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
        item: ActivePlanDeliverableRef,
        input_index: Mapping[str, PlanningInputRef],
    ) -> AgentTaskSpec:
        item.validate()
        skill_id = (
            ACTIVE_PLAN_DOCUMENT_SKILL_ID
            if item.output_format is AgentDeliverableFormat.DOCX
            else ACTIVE_PLAN_SPREADSHEET_SKILL_ID
        )
        policy = self._policies.get(skill_id)
        if policy is None:
            raise CasePlannerBlocked(
                "active-plan deliverable has no executable server policy"
            )
        ref_id = f"work-plan-item:{item.item_id}"
        input_ref = input_index.get(ref_id)
        if (
            input_ref is None
            or input_ref.kind is not PlanningInputKind.WORK_PLAN_ITEM
            or input_ref.status is not PlanningInputStatus.CONFIRMED
            or input_ref.content_hash != item.item_hash
            or skill_id not in input_ref.allowed_skill_ids
        ):
            raise CasePlannerBlocked(
                "active-plan deliverable item is stale or not executable in this release"
            )
        skill = self._registry.get_skill(policy.skill_id)
        tool = self._registry.authorize_tool(
            skill_id=policy.skill_id,
            tool_id=policy.tool_id,
            granted_scopes=skill.required_scopes,
            lawyer_approved=True,
            release_locked=True,
        )
        adapter = self._adapters[policy.tool_id]
        input_hash = _canonical_hash(
            {
                "schema_version": _ACTIVE_PLAN_EXECUTION_SCHEMA_VERSION,
                "goal_hash": goal.goal_hash,
                "case_snapshot_hash": snapshot.case_snapshot.snapshot_hash,
                "planning_snapshot_hash": snapshot.planning_hash,
                "active_plan_execution": {
                    "plan_id": goal.active_plan_execution.plan_id,
                    "plan_hash": goal.active_plan_execution.plan_hash,
                    "source_run_id": goal.active_plan_execution.source_run_id,
                },
                "deliverable": {
                    "item_id": item.item_id,
                    "item_hash": item.item_hash,
                    "deliverable_kind": item.deliverable_kind.value,
                    "output_format": item.output_format.value,
                },
                "authorized_input": {
                    "ref_id": input_ref.ref_id,
                    "kind": input_ref.kind.value,
                    "object_version": input_ref.object_version,
                    "content_hash": input_ref.content_hash,
                    "status": input_ref.status.value,
                },
                "server_policy": _server_policy_payload(policy, adapter),
            }
        )
        return AgentTaskSpec(
            task_id=task_id,
            sequence=sequence,
            title=skill.title,
            purpose=(
                "执行主办律师已激活计划中的"
                f"{item.deliverable_kind.value}，生成可复核源文件及PDF预览"
            ),
            rationale=(
                "任务由服务器依据ACTIVE计划项、精确模板类型和当前来源哈希编译；"
                "模型不能删改交付类型、格式或来源范围。"
            ),
            dependency_ids=(),
            input_refs=(ref_id,),
            input_hash=input_hash,
            skill=SkillBinding(
                skill_id=skill.skill_id,
                skill_version=skill.version,
                tool_id=tool.tool_id,
                tool_version=tool.version,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            ),
            granted_scopes=skill.required_scopes,
            capability=TaskCapabilityContract(
                execution_mode=adapter.execution_mode,
                network_policy=(
                    NetworkPolicy.EXACT_ALLOWLIST
                    if tool.allows_external_network
                    else NetworkPolicy.DENY
                ),
                allowed_domains=policy.allowed_domains,
                sandbox_profile=policy.sandbox_profile,
                sandbox_policy_version=adapter.sandbox_policy_version,
                sandbox_policy_hash=adapter.sandbox_policy_hash,
                reads_case_objects=(ref_id,),
                writes_managed_derivatives=tool.writes_only_managed_derivatives,
                external_request_approval_required=tool.allows_external_network,
            ),
            risk_level=policy.risk_level,
            autonomy_level=policy.autonomy_level,
            approval_gate=policy.approval_gate,
            retry_mode=policy.retry_mode,
            budget=policy.task_budget,
        )

    def _compile_reextraction_obligation_task(
        self,
        *,
        task_id: str,
        sequence: int,
        goal: AgentGoal,
        snapshot: CasePlanningSnapshot,
        obligation: ReextractionPlanningObligation,
        input_index: Mapping[str, PlanningInputRef],
    ) -> AgentTaskSpec:
        """Compile the exact task independently of an untrusted proposal."""

        policy = self._policies.get(REEXTRACTION_SKILL_ID)
        if policy is None:
            raise CasePlannerBlocked(
                "active re-extraction obligation has no executable server policy"
            )
        if len(obligation.source_ref_ids) > policy.max_input_refs:
            raise CasePlannerAdmissionBlocked(
                "active re-extraction obligation exceeds the executable page limit",
                error_code="REEXTRACTION_PAGE_LIMIT_EXCEEDED",
            )
        selected_inputs: list[PlanningInputRef] = []
        for ref_id in obligation.source_ref_ids:
            input_ref = input_index.get(ref_id)
            if input_ref is None:
                raise CasePlannerBlocked(
                    "re-extraction obligation cites an unauthorized evidence page"
                )
            if REEXTRACTION_SKILL_ID not in input_ref.allowed_skill_ids:
                raise CasePlannerBlocked(
                    "re-extraction obligation source is not authorized for extraction"
                )
            selected_inputs.append(input_ref)

        skill = self._registry.get_skill(policy.skill_id)
        tool = self._registry.authorize_tool(
            skill_id=policy.skill_id,
            tool_id=policy.tool_id,
            granted_scopes=skill.required_scopes,
            lawyer_approved=True,
            release_locked=True,
        )
        adapter = self._adapters[policy.tool_id]
        network_policy = (
            NetworkPolicy.EXACT_ALLOWLIST
            if tool.allows_external_network
            else NetworkPolicy.DENY
        )
        input_hash = _canonical_hash(
            {
                "schema_version": "lawyer-agent-compiled-reextraction-obligation-v1",
                "goal_hash": goal.goal_hash,
                "case_snapshot_hash": snapshot.case_snapshot.snapshot_hash,
                "planning_snapshot_hash": snapshot.planning_hash,
                "obligation": _reextraction_obligation_payload(obligation),
                "authorized_inputs": [
                    {
                        "ref_id": item.ref_id,
                        "kind": item.kind.value,
                        "object_version": item.object_version,
                        "content_hash": item.content_hash,
                        "status": item.status.value,
                    }
                    for item in selected_inputs
                ],
                "server_policy": _server_policy_payload(policy, adapter),
            }
        )
        return AgentTaskSpec(
            task_id=task_id,
            sequence=sequence,
            title=skill.title,
            purpose="依律师当前重提取要求，对所引证据页执行完整、精确且可恢复的重新提取",
            rationale=(
                "服务器从当前ACTIVE重提取事项编译该任务；模型不能删除、拆分或扩大"
                "其证据页范围，同源多个事项只执行一次。"
            ),
            dependency_ids=(),
            input_refs=obligation.source_ref_ids,
            input_hash=input_hash,
            skill=SkillBinding(
                skill_id=skill.skill_id,
                skill_version=skill.version,
                tool_id=tool.tool_id,
                tool_version=tool.version,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            ),
            granted_scopes=skill.required_scopes,
            capability=TaskCapabilityContract(
                execution_mode=adapter.execution_mode,
                network_policy=network_policy,
                allowed_domains=policy.allowed_domains,
                sandbox_profile=policy.sandbox_profile,
                sandbox_policy_version=adapter.sandbox_policy_version,
                sandbox_policy_hash=adapter.sandbox_policy_hash,
                reads_case_objects=obligation.source_ref_ids,
                writes_managed_derivatives=tool.writes_only_managed_derivatives,
                external_request_approval_required=tool.allows_external_network,
            ),
            risk_level=policy.risk_level,
            autonomy_level=policy.autonomy_level,
            approval_gate=policy.approval_gate,
            retry_mode=policy.retry_mode,
            budget=policy.task_budget,
        )

    def _validate_server_policy(self, policy: ServerSkillExecutionPolicy) -> None:
        try:
            skill = self._registry.get_skill(policy.skill_id)
        except SkillRegistryBlocked as error:
            raise CasePlannerBlocked("server policy references an unknown Skill") from error
        adapter = self._adapters.get(policy.tool_id)
        if adapter is None:
            raise CasePlannerBlocked("server policy Tool has no runtime adapter")
        adapter.validate()
        if adapter.tool_id != policy.tool_id:
            raise CasePlannerBlocked("runtime adapter is bound to another Tool")
        try:
            tool = self._registry.authorize_tool(
                skill_id=policy.skill_id,
                tool_id=policy.tool_id,
                granted_scopes=skill.required_scopes,
                lawyer_approved=True,
                release_locked=True,
            )
        except SkillRegistryBlocked as error:
            # A GATED/PLANNED Skill cannot be smuggled in through planner policy.
            raise CasePlannerBlocked("server policy Skill/Tool is not executable") from error
        if skill.maturity is not SkillMaturity.IMPLEMENTED:
            raise CasePlannerBlocked("server policy Skill is not implemented")
        if tool.allows_external_network:
            if (
                not policy.allowed_domains
                or not adapter.network_capable
                or not adapter.supports_reconciliation
                or policy.approval_gate
                not in {ApprovalGate.LAWYER_REVIEW, ApprovalGate.RELEASE_LOCK}
                or policy.autonomy_level is not AgentAutonomyLevel.A3_LAWYER_APPROVAL
                or policy.retry_mode
                not in {RetryMode.BEFORE_EXTERNAL_SUBMISSION_ONLY, RetryMode.NEVER_AUTOMATIC}
            ):
                raise CasePlannerBlocked(
                    "external planner policy requires exact domains, reconciliation and lawyer approval"
                )
        elif policy.allowed_domains:
            raise CasePlannerBlocked("local planner policy cannot contain network domains")

    @staticmethod
    def _allocate_optional_retries(
        tasks: Iterable[AgentTaskSpec], run_budget: RunResourceBudget
    ) -> tuple[AgentTaskSpec, ...]:
        """Reserve the complete plan before optional idempotent retries (ADR-0090)."""
        original = tuple(tasks)
        try:
            CaseAgentPlannerCompiler._validate_aggregate_budget(original, run_budget)
            return original
        except CasePlannerBlocked:
            pass
        allocated = [
            replace(item, budget=replace(item.budget, max_attempts=1))
            if item.retry_mode is RetryMode.IDEMPOTENT else item
            for item in original
        ]
        # No task removal, timeout reduction or new provider authority can make
        # an infeasible first execution fit. Non-time caps remain unchanged.
        CaseAgentPlannerCompiler._validate_aggregate_budget(allocated, run_budget)
        attempts = sum(item.budget.max_attempts for item in allocated)
        runtime = sum(item.budget.max_attempts * item.budget.timeout_seconds for item in allocated)
        order = sorted(range(len(allocated)), key=lambda index: allocated[index].sequence)
        while True:
            changed = False
            for index in order:
                item = allocated[index]
                if (item.retry_mode is not RetryMode.IDEMPOTENT
                        or item.budget.max_attempts >= original[index].budget.max_attempts
                        or attempts + 1 > run_budget.max_total_attempts
                        or runtime + item.budget.timeout_seconds > run_budget.max_runtime_seconds):
                    continue
                allocated[index] = replace(item, budget=replace(item.budget, max_attempts=item.budget.max_attempts + 1))
                attempts += 1
                runtime += item.budget.timeout_seconds
                changed = True
            if not changed:
                return tuple(allocated)

    @staticmethod
    def _validate_aggregate_budget(
        tasks: Iterable[AgentTaskSpec], run_budget: RunResourceBudget
    ) -> None:
        items = tuple(tasks)
        totals = {
            "attempt": sum(item.budget.max_attempts for item in items),
            "external-call": sum(item.budget.max_external_calls for item in items),
            "runtime": sum(item.budget.timeout_seconds * item.budget.max_attempts for item in items),
            "cost": sum(item.budget.max_cost_minor_units for item in items),
            "output": sum(item.budget.max_output_bytes for item in items),
        }
        maxima = {
            "attempt": run_budget.max_total_attempts,
            "external-call": run_budget.max_external_calls,
            "runtime": run_budget.max_runtime_seconds,
            "cost": run_budget.max_cost_minor_units,
            "output": run_budget.max_output_bytes,
        }
        for label, total in totals.items():
            if total > maxima[label]:
                raise CasePlannerBudgetExceeded(
                    dimension=label, required=total, available=maxima[label], task_count=len(items)
                )


@dataclass(frozen=True)
class RetainedMaterialReviewCandidate:
    """Local review evidence only; never authority to accept or dispatch a graph."""

    original_goal_hash: str
    original_proposal_hash: str
    planning_hash: str
    effective_goal: AgentGoal
    derived_proposal: CasePlanProposal
    derived_proposal_hash: str
    effective_budget: RunResourceBudget
    graph: AgentTaskGraph


def prepare_retained_material_review(
    *, compiler: CaseAgentPlannerCompiler, original_goal: AgentGoal,
    snapshot: CasePlanningSnapshot, retained_proposal: CasePlanProposal,
    expected_proposal_hash: str, material_read_refs: tuple[str, ...],
    run_budget: RunResourceBudget, graph_id: str,
    output_ceiling_bytes: int,
) -> RetainedMaterialReviewCandidate:
    """Rebind a retained proposal as an explicitly DIFFERENT review candidate.

    The caller must separately prove successful exchange, current permissions,
    original run state and durable review approval. This pure function neither
    records that approval nor replaces the original response or goal.
    """
    from .case_agent_supervisor import _validate_goal

    _validate_goal(original_goal)
    snapshot.validate()
    run_budget.validate()
    _sha256(expected_proposal_hash, "retained proposal hash")
    _validate_proposal(retained_proposal, expected_goal_hash=original_goal.goal_hash,
        expected_snapshot_hash=snapshot.planning_hash)
    if _canonical_hash(case_plan_proposal_payload(retained_proposal)) != expected_proposal_hash:
        raise CasePlannerBlocked("retained material review proposal fingerprint differs")
    if original_goal.material_read_refs or original_goal.requested_deliverables or original_goal.active_plan_execution:
        raise CasePlannerBlocked("retained material review requires an unscoped initial goal")
    if not material_read_refs:
        raise CasePlannerBlocked("retained material review requires an exact source scope")
    if type(output_ceiling_bytes) is not int or not 0 < output_ceiling_bytes <= 64 * 1024 * 1024:
        raise CasePlannerBlocked("retained material review output ceiling is invalid")
    goal = AgentGoal.build(goal_id=original_goal.goal_id, objective=original_goal.objective,
        success_criteria=original_goal.success_criteria, constraints=original_goal.constraints,
        requested_by=original_goal.requested_by, material_read_refs=material_read_refs)
    # Task content and snapshot hash remain exact; only a labelled derived
    # candidate can refer to the newly scoped goal hash.
    derived = replace(retained_proposal, goal_hash=goal.goal_hash)
    budget = run_budget
    kwargs = dict(graph_id=graph_id, graph_version=1, goal=goal, snapshot=snapshot, proposal=derived)
    try:
        graph = compiler.compile(**kwargs, run_budget=budget)
    except CasePlannerBudgetExceeded as error:
        if error.dimension != "output" or error.required > output_ceiling_bytes:
            raise
        budget = replace(budget, max_output_bytes=error.required)
        graph = compiler.compile(**kwargs, run_budget=budget)
    if sum(task.budget.max_output_bytes for task in graph.tasks) > output_ceiling_bytes:
        raise CasePlannerBlocked("retained material graph exceeds review output ceiling")
    return RetainedMaterialReviewCandidate(original_goal.goal_hash, expected_proposal_hash,
        snapshot.planning_hash, goal, derived,
        _canonical_hash(case_plan_proposal_payload(derived)), budget, graph)


def parse_case_plan_proposal(
    value: str | bytes,
    *,
    expected_goal_hash: str,
    expected_snapshot_hash: str,
) -> CasePlanProposal:
    """Parse exact model JSON while rejecting duplicates and unknown fields."""

    _sha256(expected_goal_hash, "expected goal_hash")
    _sha256(expected_snapshot_hash, "expected planning_snapshot_hash")
    if isinstance(value, bytes):
        if len(value) > 512 * 1024:
            raise CasePlannerBlocked("planner proposal exceeds the response limit")
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CasePlannerBlocked("planner proposal is not UTF-8") from error
    elif isinstance(value, str):
        text = value
        if len(text.encode("utf-8")) > 512 * 1024:
            raise CasePlannerBlocked("planner proposal exceeds the response limit")
    else:
        raise CasePlannerBlocked("planner proposal must be JSON text")
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CasePlannerBlocked("planner proposal is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise CasePlannerBlocked("planner proposal must be a JSON object")
    _exact_keys(
        parsed,
        {"schema_version", "goal_hash", "planning_snapshot_hash", "tasks"},
        "planner proposal",
    )
    if parsed["schema_version"] != PLANNER_PROPOSAL_SCHEMA_VERSION:
        raise CasePlannerBlocked("planner proposal schema is unsupported")
    if parsed["goal_hash"] != expected_goal_hash or parsed["planning_snapshot_hash"] != expected_snapshot_hash:
        raise CasePlannerBlocked("planner proposal is bound to another goal or snapshot")
    raw_tasks = parsed["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 100:
        raise CasePlannerBlocked("planner proposal requires 1 to 100 tasks")
    tasks: list[ProposedPlannerTask] = []
    proposal_ids: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise CasePlannerBlocked("planner task must be an object")
        _exact_keys(
            raw,
            {
                "proposal_id",
                "skill_id",
                "purpose",
                "dependency_ids",
                "input_ref_ids",
                "risk_hint",
            },
            "planner task",
        )
        proposal_id = _bounded_code(raw["proposal_id"], "proposal_id", 80)
        if proposal_id in proposal_ids:
            raise CasePlannerBlocked("planner task proposal_id is duplicated")
        proposal_ids.add(proposal_id)
        skill_id = _bounded_code(raw["skill_id"], "planner task skill_id", 200)
        purpose = _business_text(raw["purpose"], "planner task purpose", 2_000)
        dependencies = _strict_code_list(
            raw["dependency_ids"], "planner dependency_ids", maximum_items=100
        )
        input_refs = _strict_code_list(
            raw["input_ref_ids"], "planner input_ref_ids", maximum_items=500, require_nonempty=True
        )
        try:
            risk_hint = PlannerRiskHint(raw["risk_hint"])
        except (TypeError, ValueError) as error:
            raise CasePlannerBlocked("planner task risk_hint is invalid") from error
        tasks.append(
            ProposedPlannerTask(
                proposal_id=proposal_id,
                skill_id=skill_id,
                purpose=purpose,
                dependency_ids=dependencies,
                input_ref_ids=input_refs,
                risk_hint=risk_hint,
            )
        )
    proposal = CasePlanProposal(
        goal_hash=parsed["goal_hash"],
        planning_snapshot_hash=parsed["planning_snapshot_hash"],
        tasks=tuple(tasks),
    )
    _validate_proposal(
        proposal,
        expected_goal_hash=expected_goal_hash,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    return proposal


def _validate_proposal(
    proposal: CasePlanProposal,
    *,
    expected_goal_hash: str,
    expected_snapshot_hash: str,
) -> None:
    if proposal.schema_version != PLANNER_PROPOSAL_SCHEMA_VERSION:
        raise CasePlannerBlocked("planner proposal schema is unsupported")
    if proposal.goal_hash != expected_goal_hash or proposal.planning_snapshot_hash != expected_snapshot_hash:
        raise CasePlannerBlocked("planner proposal is bound to another goal or snapshot")
    if not 1 <= len(proposal.tasks) <= 100:
        raise CasePlannerBlocked("planner proposal task count is invalid")
    ids = tuple(item.proposal_id for item in proposal.tasks)
    if len(ids) != len(set(ids)):
        raise CasePlannerBlocked("planner task identifiers must be unique")
    known = set(ids)
    for item in proposal.tasks:
        _bounded_code(item.proposal_id, "proposal_id", 80)
        _bounded_code(item.skill_id, "planner task skill_id", 200)
        _business_text(item.purpose, "planner task purpose", 2_000)
        if not item.input_ref_ids or len(item.input_ref_ids) > 500:
            raise CasePlannerBlocked("planner task input references are invalid")
        if len(item.input_ref_ids) != len(set(item.input_ref_ids)):
            raise CasePlannerBlocked("planner task input references must be unique")
        if len(item.dependency_ids) != len(set(item.dependency_ids)):
            raise CasePlannerBlocked("planner task dependencies must be unique")
        if not set(item.dependency_ids).issubset(known):
            raise CasePlannerBlocked("planner task dependency is unknown")
        if item.proposal_id in item.dependency_ids:
            raise CasePlannerBlocked("planner task cannot depend on itself")
        if not isinstance(item.risk_hint, PlannerRiskHint):
            raise CasePlannerBlocked("planner task risk hint is invalid")


def _planning_snapshot_payload(
    *,
    case_snapshot: CaseSnapshotRef,
    authorized_inputs: tuple[PlanningInputRef, ...],
    signals: tuple[CasePlanningSignal, ...],
    reextraction_obligations: tuple[ReextractionPlanningObligation, ...],
) -> dict[str, object]:
    return {
        "schema_version": PLANNING_SNAPSHOT_SCHEMA_VERSION,
        "case_snapshot": {
            "matter_id": case_snapshot.matter_id,
            "matter_version": case_snapshot.matter_version,
            "snapshot_hash": case_snapshot.snapshot_hash,
            "schema_version": case_snapshot.schema_version,
        },
        "authorized_inputs": [
            {
                "ref_id": item.ref_id,
                "kind": item.kind.value,
                "object_version": item.object_version,
                "content_hash": item.content_hash,
                "status": item.status.value,
                "allowed_skill_ids": item.allowed_skill_ids,
                "planner_visible": item.planner_visible,
            }
            for item in authorized_inputs
        ],
        "signals": [
            {
                "signal_id": item.signal_id,
                "category": item.category.value,
                "code": item.code,
                "status": item.status.value,
                "summary": item.summary,
                "source_ref_ids": item.source_ref_ids,
            }
            for item in signals
        ],
        "reextraction_obligations": [
            _reextraction_obligation_payload(item)
            for item in reextraction_obligations
        ],
    }


def planning_snapshot_public_payload(snapshot: CasePlanningSnapshot) -> dict[str, object]:
    """Return the bounded external-planner projection.

    Server-only authorized inputs may participate in deterministic planning
    and compilation without being disclosed to an external model.  Signals
    bound to any such input are removed with them; the full planning hash is
    still supplied so a provider proposal cannot be replayed against another
    server-side authorization set.
    """

    snapshot.validate()
    visible_inputs = tuple(
        item for item in snapshot.authorized_inputs if item.planner_visible
    )
    visible_refs = frozenset(item.ref_id for item in visible_inputs)
    visible_signals = tuple(
        item
        for item in snapshot.signals
        if set(item.source_ref_ids).issubset(visible_refs)
    )
    payload = _planning_snapshot_payload(
        case_snapshot=snapshot.case_snapshot,
        authorized_inputs=visible_inputs,
        signals=visible_signals,
        reextraction_obligations=(),
    )
    # The existence and source shape of a server lifecycle obligation is not
    # a model capability.  Keep even the empty private field out of the public
    # contract so no provider can start depending on it.
    payload.pop("reextraction_obligations")
    return {**payload, "planning_hash": snapshot.planning_hash}


def case_plan_proposal_payload(proposal: CasePlanProposal) -> dict[str, object]:
    """Return the canonical private payload stored for crash-safe recovery."""

    _validate_proposal(
        proposal,
        expected_goal_hash=proposal.goal_hash,
        expected_snapshot_hash=proposal.planning_snapshot_hash,
    )
    return {
        "schema_version": proposal.schema_version,
        "goal_hash": proposal.goal_hash,
        "planning_snapshot_hash": proposal.planning_snapshot_hash,
        "tasks": [_proposal_task_payload(item) for item in proposal.tasks],
    }


def _proposal_task_payload(task: ProposedPlannerTask) -> dict[str, object]:
    return {
        "proposal_id": task.proposal_id,
        "skill_id": task.skill_id,
        "purpose": task.purpose,
        "dependency_ids": task.dependency_ids,
        "input_ref_ids": task.input_ref_ids,
        "risk_hint": task.risk_hint.value,
    }


def _reextraction_obligation_payload(
    obligation: ReextractionPlanningObligation,
) -> dict[str, object]:
    return {
        "schema_version": _REEXTRACTION_OBLIGATION_SCHEMA_VERSION,
        "obligation_id": obligation.obligation_id,
        "control_run_id": obligation.control_run_id,
        "followup_ids": obligation.followup_ids,
        "source_ref_ids": obligation.source_ref_ids,
        "lifecycle_hash": obligation.lifecycle_hash,
    }


def _server_policy_payload(
    policy: ServerSkillExecutionPolicy, adapter: RuntimeAdapterManifest
) -> dict[str, object]:
    return {
        "skill_id": policy.skill_id,
        "tool_id": policy.tool_id,
        "adapter_id": adapter.adapter_id,
        "adapter_version": adapter.adapter_version,
        "sandbox_profile": policy.sandbox_profile,
        "sandbox_policy_version": adapter.sandbox_policy_version,
        "sandbox_policy_hash": adapter.sandbox_policy_hash,
        "allowed_domains": policy.allowed_domains,
        "risk_level": policy.risk_level.value,
        "autonomy_level": policy.autonomy_level.value,
        "approval_gate": policy.approval_gate.value,
        "retry_mode": policy.retry_mode.value,
        "max_input_refs": policy.max_input_refs,
        "budget": {
            "max_attempts": policy.task_budget.max_attempts,
            "timeout_seconds": policy.task_budget.timeout_seconds,
            "max_external_calls": policy.task_budget.max_external_calls,
            "max_cost_minor_units": policy.task_budget.max_cost_minor_units,
            "max_output_bytes": policy.task_budget.max_output_bytes,
        },
    }


def _risk_hint(value: AgentRiskLevel) -> PlannerRiskHint:
    if value is AgentRiskLevel.LOW:
        return PlannerRiskHint.LOW
    if value is AgentRiskLevel.MEDIUM:
        return PlannerRiskHint.MEDIUM
    return PlannerRiskHint.HIGH


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CasePlannerBlocked("planner JSON contains duplicate fields")
        result[key] = value
    return result


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CasePlannerBlocked(f"{label} contains missing or unknown fields")


def _strict_code_list(
    value: object,
    label: str,
    *,
    maximum_items: int,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items or (require_nonempty and not value):
        raise CasePlannerBlocked(f"{label} is invalid")
    result = tuple(_bounded_code(item, label, 200) for item in value)
    if len(result) != len(set(result)):
        raise CasePlannerBlocked(f"{label} contains duplicates")
    return result


def _business_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CasePlannerBlocked(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise CasePlannerBlocked(f"{label} is missing or too long")
    if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise CasePlannerBlocked(f"{label} contains control characters")
    return normalized


def _bounded_code(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or _CODE_RE.fullmatch(value) is None:
        raise CasePlannerBlocked(f"{label} must be a stable code")
    return value


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CasePlannerBlocked(f"{label} is invalid")


def _code(value: object, label: str) -> None:
    _bounded_code(value, label, 200)


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CasePlannerBlocked(f"{label} must be a lowercase SHA-256")


def _canonical_uuid(value: object, label: str) -> None:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CasePlannerBlocked(f"{label} must be a UUID") from error
    if str(parsed) != value:
        raise CasePlannerBlocked(f"{label} must be a canonical UUID")


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


_RISK_ORDER = {
    PlannerRiskHint.LOW: 1,
    PlannerRiskHint.MEDIUM: 2,
    PlannerRiskHint.HIGH: 3,
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)


__all__ = (
    "CaseAgentPlannerCompiler",
    "CasePlanProposal",
    "CasePlannerAdmissionBlocked",
    "CasePlannerBlocked",
    "CasePlanningSignal",
    "CasePlanningSnapshot",
    "PLANNER_PROPOSAL_SCHEMA_VERSION",
    "PLANNING_SNAPSHOT_SCHEMA_VERSION",
    "PlannerRiskHint",
    "PlannerSemanticSkill",
    "PlanningInputKind",
    "PlanningInputRef",
    "PlanningInputStatus",
    "PlanningSignalCategory",
    "ProposedPlannerTask",
    "REEXTRACTION_SKILL_ID",
    "ReextractionPlanningObligation",
    "ServerSkillExecutionPolicy",
    "case_plan_proposal_payload",
    "parse_case_plan_proposal",
    "planning_snapshot_public_payload",
)
