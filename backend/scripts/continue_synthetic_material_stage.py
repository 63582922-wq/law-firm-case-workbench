"""Continue the fixed synthetic run; no new case, upload, legal approval or model call.

Default is read-only preflight. --apply records the one reviewed supplementary
stage. A repeated invocation recognizes its exact retained event, not a retry.
"""
import json
import sys
import os
from uuid import UUID, uuid5

import run_managed_defence_acceptance as harness
from case_kernel.case_agent_material_coverage import read_material_extraction_coverage
from case_kernel.case_agent_material_stage import prepare_supplementary_material_stage
from case_kernel.case_agent_supervisor import (AgentEventType, AgentSupervisorEvent,
    SupplementaryMaterialStageReviewPayload, replay_agent_events)

MATTER = "767fda38-e3de-5a15-816f-510a686c7600"
RUN = "4af42bf5-11eb-55dd-9779-5ec650ffabf0"
KEY = "unassisted-supplementary-material-stage-20260910-v1"


def main():
    if sys.argv[1:] in (["--check-plan"], ["--check-analysis-inputs"]):
        from case_api.case_agent_worker_entrypoint import CaseAgentWorkerProcessSettings, compose_production_case_agent_worker
        from case_api.case_agent_worker_deployment import compose_official_source_s3_adapters
        from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository
        from case_kernel.case_agent_postgres import PostgresCaseAgentStore
        from case_kernel.controlled_defence_case_agent_planner import _controlled_proposal
        from case_kernel.case_agent_worker import remaining_planning_budget
        from case_kernel.case_agent_material_stage import validate_supplementary_stage_graph
        settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
        actor = settings.runtime.actor
        store = PostgresCaseAgentStore(settings.runtime.postgres_dsn)
        state = store.replay_run(matter_id=MATTER, actor=actor, run_id=RUN)
        worker = compose_production_case_agent_worker(settings=settings,
            planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn),
            document_official_source_text=compose_official_source_s3_adapters(settings.object_store).verified_text).worker
        snapshot = worker._snapshot_provider.build_for_run(state=state, actor=actor)
        proposal = _controlled_proposal(goal=state.goal, snapshot=snapshot, skills=worker._compiler.semantic_skill_catalog())
        if sys.argv[1:] == ["--check-analysis-inputs"]:
            candidates = {item.ref_id for item in snapshot.authorized_inputs
                          if item.ref_id.startswith("transaction-candidate:")}
            facts = {item.ref_id for item in snapshot.authorized_inputs
                     if item.ref_id.startswith("fact-candidate:")}
            if (not candidates or not facts or tuple(task.skill_id for task in proposal.tasks) !=
                    ("case_context_review", "lawyer_decision_package")
                    or any(not (candidates | facts).issubset(task.input_ref_ids) for task in proposal.tasks)):
                raise RuntimeError("analysis does not include all candidate sources")
            print(json.dumps({"mode": "ANALYSIS_INPUTS_ONLY_NOT_AUTHORIZATION",
                "sources": len(snapshot.authorized_inputs), "candidate_transactions": len(candidates),
                "candidate_facts": len(facts),
                "tasks": [task.skill_id for task in proposal.tasks]}))
            return
        graph = worker._compiler.compile(graph_id=str(uuid5(UUID(RUN), "stage-read-only-preflight")),
            graph_version=state.graph.graph_version + 1, goal=state.goal, snapshot=snapshot,
            proposal=proposal, run_budget=remaining_planning_budget(state))
        validate_supplementary_stage_graph(stage=state.material_stage, graph=graph)
        print(json.dumps({"mode": "COMPILED_PREFLIGHT_ONLY", "tasks": [
            {"tool": task.skill.tool_id, "pages": len(task.input_refs),
             "calls": task.budget.max_external_calls, "cost_cap": task.budget.max_cost_minor_units}
            for task in graph.tasks]}))
        return
    if sys.argv[1:] not in ([], ["--apply"]):
        raise RuntimeError("only --apply is supported")
    harness._select_acceptance_scenario(harness._M3_CURRENT_RUNTIME_FULL_FLOW_ACCEPTANCE_NAME)
    composition = harness._build_composition(None)
    identity, session = harness._issue_fixture_identity(composition)
    try:
        store = composition.api_dependencies.case_agent_control_service._store
        store.read_projection(matter_id=MATTER, actor=identity.actor, run_id=RUN)
        event_id = str(uuid5(UUID(RUN), KEY))
        with store._read_transaction(identity.actor.firm_id) as connection:
            events = store._load_events(connection, actor=identity.actor, matter_id=MATTER, run_id=RUN)
            existing = next((event for event in events if event.event_id == event_id), None)
            if existing is not None:
                if existing.event_type is not AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED:
                    raise RuntimeError("retained event identity differs")
                print(json.dumps({"mode": "ALREADY_RECORDED", "run_id": RUN,
                    "event_version": existing.sequence}))
                return
            state = replay_agent_events(events)
            coverage = read_material_extraction_coverage(connection,
                firm_id=identity.actor.firm_id, matter_id=MATTER)
        stage = prepare_supplementary_material_stage(state=state, coverage=coverage,
            approved_by=identity.actor.actor_id, external_call_cap=1, cost_cap_minor_units=120)
        if state.snapshot.matter_version != 14 or len(stage.page_refs) != 22 or state.event_version != 16:
            raise RuntimeError("fixed synthetic source state changed; inspect instead of retrying")
        if sys.argv[1:] == ["--apply"]:
            receipt = store.review_supplementary_material_stage(matter_id=MATTER, actor=identity.actor,
                expected_event_version=state.event_version, idempotency_key=KEY,
                event=AgentSupervisorEvent(event_id=event_id, run_id=RUN,
                    firm_id=identity.actor.firm_id, matter_id=MATTER, sequence=state.event_version + 1,
                    event_type=AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED,
                    occurred_at=harness._safe_now(), actor_id=identity.actor.actor_id,
                    payload=SupplementaryMaterialStageReviewPayload(stage)))
            print(json.dumps({"mode": "STAGE_RECORDED", "run_id": RUN,
                "event_version": receipt.event_version, "pending_pages": len(stage.page_refs),
                "cumulative_calls_cap": stage.proposed_budget.max_external_calls,
                "cumulative_cost_minor_units_cap": stage.proposed_budget.max_cost_minor_units}))
        else:
            print(json.dumps({"mode": "PREFLIGHT_ONLY", "run_id": RUN,
                "pending_pages": len(stage.page_refs), "stage_hash": stage.stage_hash}))
    finally:
        composition.api_dependencies.session_authority.revoke(session_id=session)


if __name__ == "__main__":
    main()
