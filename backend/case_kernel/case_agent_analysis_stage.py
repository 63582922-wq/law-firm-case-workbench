"""One source-bound analysis continuation; no retry or legal approval is implied."""
from dataclasses import asdict, dataclass, replace
from uuid import UUID

from .case_agent_material_stage import _digest
from .case_agent_supervisor import (AgentRunStatus, AgentSupervisorBlocked,
    RunResourceBudget, NetworkPolicy)


@dataclass(frozen=True)
class CaseAnalysisStage:
    run_id: str
    expected_event_version: int
    snapshot_hash: str
    previous_graph_hash: str
    candidate_bindings: tuple[tuple[str, str], ...]
    previous_budget: RunResourceBudget
    proposed_budget: RunResourceBudget
    approved_by: str
    stage_hash: str


@dataclass(frozen=True)
class CaseAnalysisRevisionStage(CaseAnalysisStage):
    revised_artifact_id: str
    revised_artifact_hash: str
    revision_reason: str


@dataclass(frozen=True)
class CaseAnalysisRequestRepairStage(CaseAnalysisStage):
    failed_attempt_id: str
    failed_request_hash: str
    repaired_request_hash: str
    repair_reason: str


def current_analysis_candidate_bindings(connection, *, firm_id, matter_id):
    from .case_agent_transaction_candidates import read_transaction_candidates, read_fact_candidates
    return tuple(sorted((prefix + ':' + candidate.candidate_id, candidate.content_hash)
        for prefix, reader in (("transaction-candidate", read_transaction_candidates),
                               ("fact-candidate", read_fact_candidates))
        for candidate in reader(connection, firm_id=firm_id, matter_id=matter_id)))


def prepare_case_analysis_stage(*, state, candidate_bindings, approved_by,
                                external_call_cap=1, cost_cap_minor_units=120):
    return _prepare_analysis_stage(state=state, candidate_bindings=candidate_bindings, approved_by=approved_by,
        external_call_cap=external_call_cap, cost_cap_minor_units=cost_cap_minor_units, revision=None)


def prepare_case_analysis_revision(*, state, candidate_bindings, approved_by,
                                  revised_artifact_id, revised_artifact_hash, revision_reason):
    return _prepare_analysis_stage(state=state, candidate_bindings=candidate_bindings, approved_by=approved_by,
        external_call_cap=1, cost_cap_minor_units=120, revision=dict(revised_artifact_id=revised_artifact_id,
            revised_artifact_hash=revised_artifact_hash, revision_reason=revision_reason))


def _prepare_analysis_stage(*, state, candidate_bindings, approved_by,
                            external_call_cap, cost_cap_minor_units, revision, repair=None):
    UUID(approved_by)
    expected_status = AgentRunStatus.WAITING_INPUT if repair is not None else AgentRunStatus.READY_FOR_REVIEW
    if (state.status is not expected_status or state.cancelled or state.stale
            or state.graph is None or (repair is None and not state.verification_hash)
            or state.goal.active_plan_execution is not None):
        raise AgentSupervisorBlocked("analysis continuation requires a retained verified stage")
    # This transition consumes a completed extraction, not another analysis or a failed attempt.
    if revision is None and repair is None and not any(task.skill.tool_id == "extract_case_ledger" for task in state.graph.tasks):
        raise AgentSupervisorBlocked("analysis continuation requires completed extraction")
    if repair is not None:
        UUID(repair["failed_attempt_id"])
        receipts = [receipt for task in state.tasks if task.spec.skill.tool_id == "analyze_lawyer_decision_package"
                    for receipt in task.receipts if receipt.attempt_id == repair["failed_attempt_id"]]
        if (isinstance(state.analysis_stage, CaseAnalysisRequestRepairStage) or len(receipts) != 1
                or receipts[0].error_code != "LAWYER_ANALYSIS_HTTP_400" or receipts[0].status.value != "FAILED"
                or receipts[0].artifacts or receipts[0].cost_minor_units != 0
                or not isinstance(repair["repair_reason"], str) or not 10 <= len(repair["repair_reason"].strip()) <= 1000
                or repair["failed_request_hash"] == repair["repaired_request_hash"]):
            raise AgentSupervisorBlocked("request repair must bind a known rejection and changed request once")
        for digest in (repair["failed_request_hash"], repair["repaired_request_hash"]):
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise AgentSupervisorBlocked("request repair digest differs")
    if revision is not None:
        UUID(revision["revised_artifact_id"])
        if (isinstance(state.analysis_stage, CaseAnalysisRevisionStage)
                or not isinstance(revision["revision_reason"], str)
                or not 10 <= len(revision["revision_reason"].strip()) <= 1000):
            raise AgentSupervisorBlocked("analysis revision requires a reason and cannot renew itself")
        analysis_tasks = [task for task in state.graph.tasks if task.skill.tool_id == "analyze_lawyer_decision_package"]
        if len(analysis_tasks) != 1 or not any(artifact.artifact_id == revision["revised_artifact_id"]
            and artifact.content_hash == revision["revised_artifact_hash"]
            and artifact.artifact_kind == "LAWYER_DECISION_PACKAGE_CANDIDATE"
            and artifact.source_input_hash == analysis_tasks[0].input_hash for artifact in state.artifacts):
            raise AgentSupervisorBlocked("analysis revision must bind the retained current output")
    if (type(external_call_cap) is not int or external_call_cap != 1
            or type(cost_cap_minor_units) is not int or cost_cap_minor_units != 120):
        raise AgentSupervisorBlocked("analysis continuation requires one call capped at 120 minor units")
    if (not isinstance(candidate_bindings, tuple) or not candidate_bindings
            or candidate_bindings != tuple(sorted(set(candidate_bindings)))
            or len({ref for ref, _ in candidate_bindings}) != len(candidate_bindings)):
        raise AgentSupervisorBlocked("analysis candidate bindings are invalid")
    for ref, digest in candidate_bindings:
        prefix, object_id = ref.split(":", 1)
        if prefix not in {"fact-candidate", "transaction-candidate"}:
            raise AgentSupervisorBlocked("analysis candidate type is invalid")
        UUID(object_id)
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AgentSupervisorBlocked("analysis candidate digest is invalid")
    old, used = state.budget, state.budget_usage
    proposed = replace(old,
        max_total_attempts=max(old.max_total_attempts, used.attempts + 3),
        max_external_calls=max(old.max_external_calls, used.external_calls + 1),
        max_cost_minor_units=max(old.max_cost_minor_units, used.cost_minor_units + 120),
        max_runtime_seconds=max(old.max_runtime_seconds, used.runtime_seconds + 1200),
        max_output_bytes=max(old.max_output_bytes, used.output_bytes + 4 * 1024 * 1024))
    proposed.validate()
    values = dict(run_id=state.run_id, expected_event_version=state.event_version,
        snapshot_hash=state.snapshot.snapshot_hash, previous_graph_hash=state.graph.graph_hash,
        candidate_bindings=candidate_bindings, previous_budget=old, proposed_budget=proposed,
        approved_by=approved_by)
    if revision is not None:
        values.update(revision)
    if repair is not None:
        values.update(repair)
    stage_type = CaseAnalysisRequestRepairStage if repair is not None else (CaseAnalysisRevisionStage if revision is not None else CaseAnalysisStage)
    return stage_type(**values, stage_hash=_digest({**values,
        "previous_budget": asdict(old), "proposed_budget": asdict(proposed)}))


def validate_case_analysis_stage_review(*, state, stage, actor_id):
    if not isinstance(stage, CaseAnalysisStage):
        raise AgentSupervisorBlocked("analysis stage type differs")
    if isinstance(stage, CaseAnalysisRequestRepairStage):
        expected = prepare_case_analysis_request_repair(state=state, candidate_bindings=stage.candidate_bindings,
            approved_by=actor_id, failed_attempt_id=stage.failed_attempt_id,
            failed_request_hash=stage.failed_request_hash, repaired_request_hash=stage.repaired_request_hash,
            repair_reason=stage.repair_reason)
    elif isinstance(stage, CaseAnalysisRevisionStage):
        expected = prepare_case_analysis_revision(state=state, candidate_bindings=stage.candidate_bindings,
            approved_by=actor_id, revised_artifact_id=stage.revised_artifact_id,
            revised_artifact_hash=stage.revised_artifact_hash, revision_reason=stage.revision_reason)
    else:
        expected = prepare_case_analysis_stage(state=state,
            candidate_bindings=stage.candidate_bindings, approved_by=actor_id)
    if stage != expected:
        raise AgentSupervisorBlocked("analysis stage bindings or budget differ")


def prepare_case_analysis_request_repair(*, state, candidate_bindings, approved_by,
                                       failed_attempt_id, failed_request_hash, repaired_request_hash, repair_reason):
    return _prepare_analysis_stage(state=state, candidate_bindings=candidate_bindings, approved_by=approved_by,
        external_call_cap=1, cost_cap_minor_units=120, revision=None,
        repair=dict(failed_attempt_id=failed_attempt_id, failed_request_hash=failed_request_hash,
                    repaired_request_hash=repaired_request_hash, repair_reason=repair_reason))


def validate_case_analysis_stage_graph(*, stage, graph):
    if not isinstance(stage, CaseAnalysisStage) or not 2 <= len(graph.tasks) <= 3:
        raise AgentSupervisorBlocked("analysis stage requires context and analysis with optional local research planning")
    research = tuple(task for task in graph.tasks if (task.skill.tool_id, task.skill.skill_id) ==
        ("plan_authoritative_rule_research", "legal_rule_research_planning"))
    if len(research) > 1 or any(task.capability.network_policy is not NetworkPolicy.DENY
        or task.budget.max_external_calls or not 1 <= task.budget.max_attempts <= 3 for task in research):
        raise AgentSupervisorBlocked("analysis research planning must remain local")
    core = tuple(task for task in graph.tasks if task not in research)
    if len(core) != 2:
        raise AgentSupervisorBlocked("analysis core task count differs")
    context, analysis = core
    if ((context.skill.tool_id, context.skill.skill_id) != ("review_case_context", "case_context_review")
            or (analysis.skill.tool_id, analysis.skill.skill_id) !=
            ("analyze_lawyer_decision_package", "lawyer_decision_package")):
        raise AgentSupervisorBlocked("analysis stage cannot dispatch another capability")
    refs = {ref for ref, _ in stage.candidate_bindings}
    for task in core:
        if ({ref for ref in task.input_refs if ref.startswith(("fact-candidate:", "transaction-candidate:"))} != refs
                or not 1 <= task.budget.max_attempts <= 3):
            raise AgentSupervisorBlocked("analysis graph source scope or attempts differ")
    if (set(context.dependency_ids) - {task.task_id for task in research}
            or context.task_id not in analysis.dependency_ids
            or set(analysis.dependency_ids) - {context.task_id, *(task.task_id for task in research)}
            or set(context.input_refs) != set(analysis.input_refs)
            or context.capability.network_policy is not NetworkPolicy.DENY
            or context.budget.max_external_calls != 0
            or analysis.budget.max_external_calls != 1
            or analysis.budget.max_attempts != 1
            or not 1 <= analysis.budget.max_cost_minor_units <= 120):
        raise AgentSupervisorBlocked("analysis stage must assemble locally then analyze once")
