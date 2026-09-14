"""Fixed synthetic run continuation. Preflight compiles; apply preserves one reviewed event."""
from dataclasses import replace
import json
import os
import sys
from uuid import UUID, uuid5

import run_managed_defence_acceptance as harness
from case_kernel.case_agent_analysis_stage import (prepare_case_analysis_revision,
    current_analysis_candidate_bindings, validate_case_analysis_stage_graph)
from case_kernel.case_agent_supervisor import (AgentEventType, AgentSupervisorEvent,
    CaseAnalysisStageReviewPayload, replay_agent_events)

MATTER = "767fda38-e3de-5a15-816f-510a686c7600"
RUN = "4af42bf5-11eb-55dd-9779-5ec650ffabf0"
REVIEWER = "22222222-2222-4222-8222-222222222222"
KEY = "unassisted-discovered-analysis-revision-20260910-v1"


ARTIFACT = "0185291a-4292-5465-96b6-b99d23f416a5"
REASON = "旧分析未覆盖重复记录、退款、币种和手续费，且攻防立场倒置。已将强制逐事实风险合同替换为来源绑定的自主争点分析；保留旧结果，执行一次修订。"


def prepare_revision(*, state, candidate_bindings, approved_by):
    artifact = next(item for item in state.artifacts if item.artifact_id == ARTIFACT)
    return prepare_case_analysis_revision(state=state, candidate_bindings=candidate_bindings,
        approved_by=approved_by, revised_artifact_id=ARTIFACT,
        revised_artifact_hash=artifact.content_hash, revision_reason=REASON)


def main():
    if sys.argv[1:] == ["--check-plan"]:
        from case_api.case_agent_worker_entrypoint import CaseAgentWorkerProcessSettings, compose_production_case_agent_worker
        from case_api.case_agent_worker_deployment import compose_official_source_s3_adapters
        from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository
        from case_kernel.case_agent_postgres import PostgresCaseAgentStore
        from case_kernel.controlled_defence_case_agent_planner import _controlled_proposal
        from case_kernel.case_agent_worker import remaining_planning_budget
        settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
        actor = settings.runtime.actor
        store = PostgresCaseAgentStore(settings.runtime.postgres_dsn)
        state = store.replay_run(matter_id=MATTER, actor=actor, run_id=RUN)
        worker = compose_production_case_agent_worker(settings=settings,
            planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn),
            document_official_source_text=compose_official_source_s3_adapters(settings.object_store).verified_text).worker
        snapshot = worker._snapshot_provider.build_for_run(state=state, actor=actor)
        bindings = tuple(sorted((item.ref_id, item.content_hash) for item in snapshot.authorized_inputs
            if item.ref_id.startswith(("fact-candidate:", "transaction-candidate:"))))
        stage = prepare_revision(state=state, candidate_bindings=bindings, approved_by=REVIEWER)
        proposal = _controlled_proposal(goal=state.goal, snapshot=snapshot, skills=worker._compiler.semantic_skill_catalog())
        graph = worker._compiler.compile(graph_id=str(uuid5(UUID(RUN), KEY + "-preflight")),
            graph_version=state.graph.graph_version + 1, goal=state.goal, snapshot=snapshot,
            proposal=proposal, run_budget=remaining_planning_budget(replace(state, budget=stage.proposed_budget)))
        print(json.dumps({"compiled_tasks": [{"skill": task.skill.skill_id,
            "attempt_cap": task.budget.max_attempts, "calls": task.budget.max_external_calls,
            "network": task.capability.network_policy.value} for task in graph.tasks]}))
        validate_case_analysis_stage_graph(stage=stage, graph=graph)
        print(json.dumps({"mode": "COMPILED_PREFLIGHT_ONLY", "stage_hash": stage.stage_hash,
            "candidates": len(bindings), "tasks": [task.skill.skill_id for task in graph.tasks],
            "calls_cap": stage.proposed_budget.max_external_calls,
            "cost_cap_minor_units": stage.proposed_budget.max_cost_minor_units}))
        return
    if sys.argv[1:] not in ([], ["--apply"], ["--approve-analysis"]):
        raise RuntimeError("supported modes: --check-plan or --apply")
    harness._select_acceptance_scenario(harness._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = harness._build_composition(None)
    identity, session = harness._issue_fixture_identity(composition)
    try:
        if identity.actor.actor_id != REVIEWER:
            raise RuntimeError("synthetic reviewer differs")
        store = composition.api_dependencies.case_agent_control_service._store
        store.read_projection(matter_id=MATTER, actor=identity.actor, run_id=RUN)
        if sys.argv[1:] == ["--approve-analysis"]:
            control = composition.api_dependencies.case_agent_control_service
            state = store.replay_run(matter_id=MATTER, actor=identity.actor, run_id=RUN)
            approvals = control.list_approvals(identity=identity, matter_id=MATTER, run_id=RUN)
            if (state.event_version != 52 or state.analysis_stage is None
                    or state.budget_usage.external_calls != 3 or len(approvals) != 1):
                raise RuntimeError("analysis approval state differs; do not submit again")
            task = next(item.spec for item in state.tasks if item.spec.task_id == approvals[0].approval_id)
            if (task.skill.skill_id != "lawyer_decision_package" or task.budget.max_external_calls != 1
                    or task.budget.max_attempts != 1 or task.budget.max_cost_minor_units > 120):
                raise RuntimeError("analysis capability or cap differs")
            result = control.submit_approval(identity=identity, matter_id=MATTER, run_id=RUN,
                approval_id=task.task_id, approved=True,
                note="按既有用户授权执行合成案件一次分析；不是事实、法律立场或文书审批。",
                expected_run_version=state.event_version, idempotency_key=KEY + "-model",
                now=harness._safe_now())
            print(json.dumps({"mode": "ANALYSIS_EXECUTION_APPROVED", "task_id": task.task_id,
                "run_id": RUN, "version": result.version}))
            return
        event_id = str(uuid5(UUID(RUN), KEY))
        with store._read_transaction(identity.actor.firm_id) as connection:
            events = store._load_events(connection, actor=identity.actor, matter_id=MATTER, run_id=RUN)
            existing = next((event for event in events if event.event_id == event_id), None)
            if existing is not None:
                if existing.event_type is not AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED:
                    raise RuntimeError("retained event differs")
                print(json.dumps({"mode": "ALREADY_RECORDED", "event_version": existing.sequence}))
                return
            state = replay_agent_events(events)
            bindings = current_analysis_candidate_bindings(connection, firm_id=identity.actor.firm_id, matter_id=MATTER)
        if state.event_version != 45 or state.snapshot.matter_version != 14 or len(bindings) != 56:
            raise RuntimeError("fixed synthetic state changed; inspect, do not retry")
        stage = prepare_revision(state=state, candidate_bindings=bindings, approved_by=REVIEWER)
        if sys.argv[1:] == ["--apply"]:
            receipt = store.review_case_analysis_stage(matter_id=MATTER, actor=identity.actor,
                expected_event_version=state.event_version, idempotency_key=KEY,
                event=AgentSupervisorEvent(event_id=event_id, run_id=RUN, firm_id=identity.actor.firm_id,
                    matter_id=MATTER, sequence=state.event_version + 1,
                    event_type=AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED,
                    occurred_at=harness._safe_now(), actor_id=REVIEWER, payload=CaseAnalysisStageReviewPayload(stage)))
            print(json.dumps({"mode": "STAGE_RECORDED", "event_version": receipt.event_version,
                "stage_hash": stage.stage_hash}))
        else:
            print(json.dumps({"mode": "PREFLIGHT_ONLY", "stage_hash": stage.stage_hash}))
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session)


if __name__ == "__main__":
    main()
