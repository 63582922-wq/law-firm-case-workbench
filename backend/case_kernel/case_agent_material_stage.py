"""Bound a reviewed supplementary-reading stage without renewing a run.

This is a server-side proposal contract, not permission to dispatch. The
transactional command must re-read coverage, authorize the reviewer and append
the reviewed event before the worker may use the proposed cumulative budget.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from uuid import UUID

from .case_agent_material_coverage import MaterialExtractionCoverage
from .case_agent_supervisor import (
    AgentRunState, AgentRunStatus, AgentSupervisorBlocked, RunResourceBudget,
    AgentTaskGraph, NetworkPolicy,
)


@dataclass(frozen=True)
class SupplementaryMaterialStage:
    run_id: str
    expected_event_version: int
    snapshot_hash: str
    previous_graph_hash: str
    coverage_hash: str
    page_refs: tuple[str, ...]
    previous_budget: RunResourceBudget
    proposed_budget: RunResourceBudget
    approved_by: str
    stage_hash: str


def prepare_supplementary_material_stage(
    *, state: AgentRunState, coverage: tuple[MaterialExtractionCoverage, ...],
    approved_by: str, external_call_cap: int, cost_cap_minor_units: int,
) -> SupplementaryMaterialStage:
    """An exact one-call stage; preserve accumulated usage and previous output.

    Successful graph verification is necessary, not lawyer acceptance. This
    function neither approves the previous analysis nor changes case facts.
    Coverage must come from the same authorized database snapshot as state.
    """
    UUID(approved_by)
    if (state.status is not AgentRunStatus.READY_FOR_REVIEW or state.cancelled
            or state.stale or state.graph is None or not state.verification_hash
            or state.goal.active_plan_execution is not None):
        raise AgentSupervisorBlocked("supplementary reading needs a retained verified stage")
    if (type(external_call_cap) is not int or external_call_cap != 1
            or type(cost_cap_minor_units) is not int
            or not 1 <= cost_cap_minor_units <= 120):
        raise AgentSupervisorBlocked("supplementary reading requires one bounded call")
    files, pages, pending = set(), set(), []
    normalized = []
    for item in sorted(coverage, key=lambda value: value.evidence_file_id):
        UUID(item.evidence_file_id)
        if (item.evidence_file_id in files or len(item.original_sha256) != 64
                or any(ch not in "0123456789abcdef" for ch in item.original_sha256)
                or not item.page_ids or len(set(item.page_ids)) != len(item.page_ids)
                or len(set(item.extracted_page_ids)) != len(item.extracted_page_ids)
                or not set(item.extracted_page_ids).issubset(item.page_ids)):
            raise AgentSupervisorBlocked("supplementary coverage is inconsistent")
        files.add(item.evidence_file_id)
        for page in item.page_ids:
            UUID(page)
            if page in pages:
                raise AgentSupervisorBlocked("supplementary page identity is duplicated")
            pages.add(page)
        pending.extend(f"evidence-page:{page}" for page in item.pending_page_ids)
        normalized.append({"file_id": item.evidence_file_id,
            "original_sha256": item.original_sha256,
            "page_ids": sorted(item.page_ids),
            "extracted_page_ids": sorted(item.extracted_page_ids)})
    if not pending:
        raise AgentSupervisorBlocked("no unprocessed original pages remain")
    used, old = state.budget_usage, state.budget
    # Keep unused allowance, but do not add it to the new stage's allowance.
    # Most importantly, never zero the immutable accumulated usage.
    proposed = replace(old,
        max_total_attempts=max(old.max_total_attempts, used.attempts + 3),
        max_external_calls=max(old.max_external_calls, used.external_calls + 1),
        max_runtime_seconds=max(old.max_runtime_seconds, used.runtime_seconds + 1200),
        max_cost_minor_units=max(old.max_cost_minor_units, used.cost_minor_units + cost_cap_minor_units),
        max_output_bytes=max(old.max_output_bytes, used.output_bytes + 64 * 1024 * 1024))
    proposed.validate()
    values = dict(run_id=state.run_id, expected_event_version=state.event_version,
        snapshot_hash=state.snapshot.snapshot_hash, previous_graph_hash=state.graph.graph_hash,
        coverage_hash=_digest(normalized), page_refs=tuple(sorted(pending)),
        previous_budget=old, proposed_budget=proposed, approved_by=approved_by)
    return SupplementaryMaterialStage(**values, stage_hash=_digest({**values,
        "previous_budget": asdict(old), "proposed_budget": asdict(proposed)}))


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def validate_supplementary_stage_review(*, state: AgentRunState,
                                       stage: object, actor_id: str) -> None:
    if not isinstance(stage, SupplementaryMaterialStage):
        raise AgentSupervisorBlocked("supplementary stage type differs")
    if (state.status is not AgentRunStatus.READY_FOR_REVIEW or state.stale or state.cancelled
            or not state.verification_hash or state.graph is None
            or state.goal.active_plan_execution is not None
            or stage.run_id != state.run_id or stage.expected_event_version != state.event_version
            or stage.snapshot_hash != state.snapshot.snapshot_hash
            or stage.previous_graph_hash != state.graph.graph_hash
            or stage.previous_budget != state.budget or stage.approved_by != actor_id):
        raise AgentSupervisorBlocked("supplementary stage binding differs")
    UUID(actor_id)
    if not stage.page_refs or tuple(sorted(set(stage.page_refs))) != stage.page_refs:
        raise AgentSupervisorBlocked("supplementary source scope is invalid")
    for ref in stage.page_refs:
        if not ref.startswith("evidence-page:"):
            raise AgentSupervisorBlocked("supplementary scope must bind original pages")
        UUID(ref.removeprefix("evidence-page:"))
    for digest in (stage.coverage_hash, stage.stage_hash):
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AgentSupervisorBlocked("supplementary digest is invalid")
    values = asdict(stage)
    values.pop("stage_hash")
    if _digest(values) != stage.stage_hash:
        raise AgentSupervisorBlocked("supplementary stage fingerprint differs")
    old, new, used = state.budget, stage.proposed_budget, state.budget_usage
    new.validate()
    if (new.max_tasks != old.max_tasks
            or new.max_total_attempts != max(old.max_total_attempts, used.attempts + 3)
            or new.max_external_calls != max(old.max_external_calls, used.external_calls + 1)
            or new.max_runtime_seconds != max(old.max_runtime_seconds, used.runtime_seconds + 1200)
            or not old.max_cost_minor_units <= new.max_cost_minor_units <= max(old.max_cost_minor_units, used.cost_minor_units + 120)
            or new.max_output_bytes != max(old.max_output_bytes, used.output_bytes + 64 * 1024 * 1024)):
        raise AgentSupervisorBlocked("supplementary stage exceeds bounded allowance")


def validate_supplementary_stage_graph(*, stage: object, graph: AgentTaskGraph) -> None:
    if not isinstance(stage, SupplementaryMaterialStage):
        raise AgentSupervisorBlocked("supplementary graph has no reviewed scope")
    allowed_pairs = {("extract_pdf_text", "pdf_reading"), ("extract_case_ledger", "case_ledger_extraction")}
    if not 2 <= len(graph.tasks) <= 3:
        raise AgentSupervisorBlocked("supplementary graph has unexpected tasks")
    readers, extractors, research = [], [], []
    for task in graph.tasks:
        if (task.skill.tool_id, task.skill.skill_id) == ("plan_authoritative_rule_research", "legal_rule_research_planning"):
            research.append(task)
            if task.capability.network_policy is not NetworkPolicy.DENY or task.budget.max_external_calls:
                raise AgentSupervisorBlocked("supplementary legal research planning must remain local")
            continue
        if (task.skill.tool_id, task.skill.skill_id) not in allowed_pairs:
            raise AgentSupervisorBlocked("supplementary stage cannot dispatch another capability")
        if set(task.input_refs) != set(stage.page_refs):
            raise AgentSupervisorBlocked("supplementary graph must cover exactly the reviewed pages")
        if task.skill.tool_id == "extract_pdf_text":
            readers.append(task)
            if task.capability.network_policy is not NetworkPolicy.DENY or task.budget.max_external_calls:
                raise AgentSupervisorBlocked("supplementary PDF reading must remain local")
        else:
            extractors.append(task)
            if task.budget.max_external_calls != 1 or not 1 <= task.budget.max_cost_minor_units <= 120 or task.budget.max_attempts != 1:
                raise AgentSupervisorBlocked("supplementary extraction is not one bounded call")
    if len(readers) != 1 or len(extractors) != 1 or len(research) > 1 or extractors[0].dependency_ids != (readers[0].task_id,):
        raise AgentSupervisorBlocked("supplementary stage must read then extract once")
