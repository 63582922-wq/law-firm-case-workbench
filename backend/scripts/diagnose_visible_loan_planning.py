"""Read-only compilation of the fixed retained synthetic proposal; no dispatch."""
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from uuid import UUID, uuid5
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from case_api.case_agent_worker_entrypoint import CaseAgentWorkerProcessSettings, compose_production_case_agent_worker
from case_kernel.case_agent_planner import (CasePlannerBudgetExceeded, parse_case_plan_proposal,
    prepare_retained_material_review, case_plan_proposal_payload, _canonical_hash)
from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository

MATTER = "ad70242f-0a41-540b-a125-d4f809f0309e"
RUN = "781a290d-7588-5e12-93a0-6e7545cd5ea3"


def main():
    if sys.argv[1:] == ["--inspect-visual-recovery"]:
        inspect_visual_recovery()
        return
    clone_resume = sys.argv[1:] == ["--clone-resume"]
    clone_mode = clone_resume or sys.argv[1:] == ["--clone-recovery"]
    managed_resume = sys.argv[1:] == ["--material-resume"]
    managed_recovery = sys.argv[1:] == ["--material-recovery"]
    settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
    settings = replace(settings, document_delivery_enabled=False, document_renderer_settings=None,
        deepseek_config=replace(settings.deepseek_config, max_output_tokens=4096, timeout_seconds=60))
    if clone_mode:
        def clone_dsn(value):
            details = conninfo_to_dict(value)
            if details.get("dbname") != "lawcase":
                raise RuntimeError("unexpected original database")
            details["dbname"] = "lawcase_scope_review_probe_20260907"
            return make_conninfo(**details)
        settings = replace(settings, runtime=replace(settings.runtime,
            postgres_dsn=clone_dsn(settings.runtime.postgres_dsn),
            verifier_postgres_dsn=clone_dsn(settings.runtime.verifier_postgres_dsn)))
    if settings.runtime.actor.firm_id != "11111111-1111-4111-8111-111111111111":
        raise RuntimeError("synthetic firm only")
    runtime = compose_production_case_agent_worker(settings=settings,
        planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn))
    worker = runtime.worker
    if managed_resume or managed_recovery:
        if conninfo_to_dict(settings.runtime.postgres_dsn).get("dbname") != "lawcase":
            raise RuntimeError("managed recovery database differs")
        with psycopg.connect(settings.runtime.postgres_dsn) as check:
            if check.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='public' AND p.proname IN ('case_agent_ledger_exception_source_policy',
                'case_agent_ledger_exception_risk_policy','case_agent_document_source_refs_hash')
                AND p.proconfig @> ARRAY['search_path=pg_catalog, public']
                AND to_regprocedure('public.guard_case_agent_material_scope_review()') IS NOT NULL""").fetchone()[0] != 3:
                raise RuntimeError("managed recovery migrations are not deployed")
    state = worker._projection(matter_id=MATTER, run_id=RUN).state
    if sys.argv[1:] == ["--stage-visual-recovery"]:
        from case_kernel.visual_candidate_recovery_postgres import PostgresVisualRecoveryStore
        from case_kernel.web_object_store import S3CompatiblePrivateObjectStore
        if (state.status.value, state.event_version, state.snapshot.matter_version) != ("FAILED", 10, 20):
            raise RuntimeError("fixed original state changed")
        if len(state.artifacts) != 1 or state.artifacts[0].content_hash != "71ef29c0e3021c148359d17c5a8fc27bc8ac3f2401587f27df6dcbe894516f33":
            raise RuntimeError("fixed original artifact differs")
        store = PostgresVisualRecoveryStore(dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor, object_store=S3CompatiblePrivateObjectStore(settings.object_store))
        draft = store.stage(matter_id=MATTER, run_id=RUN,
            source_artifact_id="2ffeeb3b-f456-5a76-93af-f6324a3d3122", expected_event_version=10)
        after = worker._projection(matter_id=MATTER, run_id=RUN).state
        if after != state:
            raise RuntimeError("Agent state changed during independent recovery")
        print(json.dumps(dict(recovery_id=draft.recovery_id, content_sha256=draft.content_sha256,
            byte_size=len(draft.payload), persisted=True, original_status=after.status.value,
            original_event_version=after.event_version, model_calls=0, task_dispatch_calls=0)), flush=True)
        return
    if sys.argv[1:] == ["--verify-visual"]:
        if (state.status.value, state.event_version, state.snapshot.matter_version) != ("VERIFYING", 8, 20):
            raise RuntimeError("visual verification state differs; no replay")
        if len(state.tasks) != 1 or state.tasks[0].status.value != "SUCCEEDED" or len(state.artifacts) != 1:
            raise RuntimeError("visual verification inputs differ")
        result = worker.process_run_once(matter_id=MATTER, run_id=RUN)
        with Path("/var/lib/lawcase/ocr-checkpoint/verification.json").open("x", encoding="utf-8") as stream:
            json.dump(asdict(result), stream, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(asdict(result), default=str), flush=True)
        return
    if sys.argv[1:] == ["--dispatch-visual"]:
        if state.event_version != 6 or state.snapshot.matter_version != 20 or len(state.tasks) != 1:
            raise RuntimeError("visual dispatch state changed; inspect without retry")
        task = state.tasks[0]
        if (task.attempt_count != 0 or task.receipts or task.spec.skill.skill_id != "image_visual_ocr"
                or task.spec.input_refs != ("evidence-page:0ed71e7c-8f9e-46e2-b3e9-ca382d9f0fc7",)
                or task.spec.budget.max_attempts != 1 or task.spec.budget.max_external_calls != 1
                or task.spec.budget.max_cost_minor_units != 6 or task.spec.retry_mode.value != "NEVER_AUTOMATIC"
                or state.graph.graph_hash != "aec1c00289fedf1b5e91833be44f9c2bc0a48aadafcb4b746e85f673d8f33736"):
            raise RuntimeError("visual dispatch exceeds fixed source or budget")
        checkpoint = Path("/var/lib/lawcase/ocr-checkpoint")
        def record(name, value):
            with (checkpoint / name).open("x", encoding="utf-8") as stream:
                json.dump(value, stream, default=str, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        record("intent.json", dict(run_id=RUN, event_version=6, task_id=task.spec.task_id,
            graph_hash=state.graph.graph_hash, inputs=task.spec.input_refs,
            budget=asdict(task.spec.budget), price_rechecked_on="2026-09-07",
            price_source="https://help.aliyun.com/zh/model-studio/qwen3-5-ocr",
            reserved_cny="0.06", maximum_dispatch_steps=1, automatic_retry=False))
        class NoDispatchPlanner:
            planner_id = "forbidden-planning-during-ocr"
            def plan(self, **kwargs):
                raise RuntimeError("new planning forbidden")
        worker._planner = NoDispatchPlanner()
        result = worker.process_run_once(matter_id=MATTER, run_id=RUN)
        record("result.json", asdict(result))
        print(json.dumps(asdict(result), default=str), flush=True)
        return
    if clone_resume or managed_resume:
        if (state.status.value, state.event_version, state.snapshot.matter_version) != ("PLANNING", 4, 20):
            raise RuntimeError("clone resume state differs; no replay")
        class NoResumePlanner:
            planner_id = "forbidden-clone-planning"
            def plan(self, **kwargs):
                raise RuntimeError("new model planning is forbidden")
        worker._planner = NoResumePlanner()
        result = worker.process_run_once(matter_id=MATTER, run_id=RUN)
        print(json.dumps(dict(recovery_result=asdict(result), isolated_clone=clone_mode, task_dispatch_calls=0), default=str), flush=True)
        return
    if (state.status.value, state.event_version, state.snapshot.matter_version) != ("WAITING_INPUT", 3, 20):
        raise RuntimeError("fixed run has changed; inspect without replay")
    snapshot = worker._snapshot_provider.build_for_run(state=state, actor=settings.runtime.actor)
    with psycopg.connect(settings.runtime.postgres_dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SELECT set_config('app.firm_id',%s,true)", (state.firm_id,))
        rows = connection.execute("SELECT request_hash,input_hash,structured_proposal FROM case_agent_planning_external_events WHERE run_id=%s AND status='SUCCEEDED'", (RUN,)).fetchall()
    if len(rows) != 1 or rows[0]["input_hash"] != snapshot.planning_hash:
        raise RuntimeError("retained proposal or current sources differ")
    proposal = parse_case_plan_proposal(json.dumps(rows[0]["structured_proposal"], ensure_ascii=False),
        expected_goal_hash=state.goal.goal_hash, expected_snapshot_hash=snapshot.planning_hash)
    if clone_mode or managed_recovery or sys.argv[1:] == ["--review-candidate"]:
        candidate = prepare_retained_material_review(compiler=worker._compiler,
            original_goal=state.goal, snapshot=snapshot, retained_proposal=proposal,
            expected_proposal_hash=_canonical_hash(case_plan_proposal_payload(proposal)),
            material_read_refs=("evidence-page:0ed71e7c-8f9e-46e2-b3e9-ca382d9f0fc7",),
            run_budget=state.budget, graph_id=RUN, output_ceiling_bytes=32 * 1024 * 1024)
        if clone_mode or managed_recovery:
            from case_kernel.case_agent_postgres import PostgresCaseAgentStore
            from case_kernel.case_agent_supervisor import AgentEventType, AgentSupervisorEvent, PlanningMaterialScopeReviewPayload
            from case_kernel.models import Actor, Role
            app_dsn = os.environ["LAWCASE_RECOVERY_APP_DSN"]
            if clone_mode:
                app_dsn = clone_dsn(app_dsn)
            elif conninfo_to_dict(app_dsn).get("dbname") != "lawcase":
                raise RuntimeError("managed review database differs")
            app = PostgresCaseAgentStore(app_dsn)
            reviewer = Actor("22222222-2222-4222-8222-222222222222", state.firm_id, frozenset({Role.LEAD_LAWYER}))
            payload = PlanningMaterialScopeReviewPayload(state.snapshot, candidate.original_goal_hash,
                candidate.original_proposal_hash, rows[0]["request_hash"], snapshot.planning_hash,
                candidate.effective_goal.material_read_refs, state.budget.max_output_bytes,
                candidate.effective_budget.max_output_bytes, candidate.effective_goal.goal_hash,
                candidate.derived_proposal_hash, candidate.graph.graph_hash, reviewer.actor_id)
            review_key = "clone-material-review-v1" if clone_mode else "material-scope-review-v1"
            event = AgentSupervisorEvent(event_id=str(uuid5(UUID(RUN), review_key)),
                run_id=RUN, firm_id=state.firm_id, matter_id=MATTER, sequence=4,
                event_type=AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED,
                occurred_at=datetime.now(timezone.utc), actor_id=reviewer.actor_id, payload=payload)
            args = dict(matter_id=MATTER, actor=reviewer, expected_event_version=3,
                idempotency_key=review_key, event=event,
                compiler=worker._compiler, planning_snapshot=snapshot)
            receipt = app.review_retained_material_scope(**args)
            assert app.review_retained_material_scope(**args) == receipt
            print("MATERIAL_REVIEW_COMMITTED_AND_IDEMPOTENT", flush=True)
            # A fresh composition proves recovery does not need the in-memory
            # candidate. The original planner is never an available fallback.
            runtime = compose_production_case_agent_worker(settings=settings,
                planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn))
            class NoPlanner:
                planner_id = "forbidden-clone-planning"
                def plan(self, **kwargs):
                    raise RuntimeError("new model planning is forbidden")
            runtime.worker._planner = NoPlanner()
            result = runtime.worker.process_run_once(matter_id=MATTER, run_id=RUN)
            print(json.dumps(dict(recovery_result=asdict(result), isolated_clone=clone_mode, task_dispatch_calls=0), default=str), flush=True)
            return
        print(json.dumps(dict(run_id=RUN, status=state.status.value, event_version=state.event_version,
            original_goal_hash=candidate.original_goal_hash,
            original_proposal_hash=candidate.original_proposal_hash,
            effective_goal_hash=candidate.effective_goal.goal_hash,
            derived_proposal_hash=candidate.derived_proposal_hash,
            graph_hash=candidate.graph.graph_hash,
            tasks=[dict(skill=t.skill.skill_id, inputs=t.input_refs, budget=asdict(t.budget))
                for t in candidate.graph.tasks],
            effective_budget=asdict(candidate.effective_budget),
            model_calls=0, writes=0, review_approved=False), ensure_ascii=False))
        return
    if sys.argv[1:]:
        raise RuntimeError("unknown diagnostic mode")
    kwargs = dict(graph_id=RUN, graph_version=1, goal=state.goal, snapshot=snapshot, proposal=proposal)
    try:
        worker._compiler.compile(**kwargs, run_budget=state.budget)
    except CasePlannerBudgetExceeded as error:
        diagnostic = dict(dimension=error.dimension, required=error.required,
            available=error.available, task_count=error.task_count)
        print(json.dumps({"actual_compiler_diagnostic": diagnostic}), flush=True)
        if error.dimension != "output" or not 0 < error.required <= 64 * 1024 * 1024:
            raise
        graph = worker._compiler.compile(**kwargs,
            run_budget=replace(state.budget, max_output_bytes=error.required))
    else:
        raise RuntimeError("expected budget failure no longer exists")
    print(json.dumps(dict(run_id=RUN, status=state.status.value, event_version=state.event_version,
        request_hash=rows[0]["request_hash"], diagnostic=diagnostic,
        hypothetical_graph=asdict(graph), model_calls=0, writes=0), default=str, ensure_ascii=False))


def inspect_visual_recovery():
    """Fixed synthetic, read-only interpretation proof. Not a lawyer Web route."""
    sys.path.insert(0, "/app/backend/scripts")
    from run_managed_defence_acceptance import _build_composition
    from case_kernel.visual_candidate_recovery import VisualRecoverySource, prepare_visual_candidate_recovery
    from case_kernel.web_object_store import StoredCaseAgentReviewCandidate
    from case_api.web_case_agent_artifacts import _visual_sections
    c = _build_composition(None)
    artifact = "2ffeeb3b-f456-5a76-93af-f6324a3d3122"
    expected_hash = "71ef29c0e3021c148359d17c5a8fc27bc8ac3f2401587f27df6dcbe894516f33"
    with psycopg.connect(c.settings.app_postgres_dsn, row_factory=dict_row) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        db.execute("SELECT set_config('app.firm_id', %s, true)",
                   ("11111111-1111-4111-8111-111111111111",))
        rows = db.execute("""
            SELECT candidate.*, run.status AS run_status, run.current_event_version,
                   run.failure_code, run.snapshot_matter_version, receipt.attempt_id,
                   receipt.result_status, receipt.external_request_id, receipt.cost_minor_units,
                   task.input_refs
              FROM case_agent_review_candidates candidate
              JOIN case_agent_runs run USING (run_id, firm_id, matter_id)
              JOIN matters matter USING (matter_id, firm_id)
              JOIN case_agent_artifacts artifact USING (artifact_id, run_id, firm_id, matter_id)
              JOIN case_agent_task_receipts receipt
                ON receipt.receipt_id=artifact.receipt_id AND receipt.run_id=run.run_id
               AND receipt.task_id=candidate.task_id
              JOIN case_agent_tasks task ON task.task_id=candidate.task_id
               AND task.graph_id=candidate.graph_id AND task.run_id=run.run_id
             WHERE candidate.artifact_id=%s AND run.run_id=%s AND run.matter_id=%s
               AND NOT run.is_stale AND NOT run.is_cancelled
               AND run.snapshot_matter_version=matter.version
               AND candidate.graph_id=run.current_graph_id
               AND artifact.content_hash=candidate.content_sha256
               AND artifact.byte_size=candidate.byte_size
               AND artifact.source_input_hash=candidate.task_input_hash
               AND receipt.input_hash=candidate.task_input_hash
               AND receipt.external_calls=1 AND receipt.external_submission_state='SUBMITTED'
               AND task.skill_id='image_visual_ocr'
        """, (artifact, RUN, MATTER)).fetchall()
        if len(rows) != 1:
            raise RuntimeError("exact current original candidate binding unavailable")
        row = rows[0]
        if (row["content_sha256"] != expected_hash or row["byte_size"] != 3020
                or row["current_event_version"] != 10 or row["snapshot_matter_version"] != 20):
            raise RuntimeError("fixed synthetic source changed")
        stored = StoredCaseAgentReviewCandidate(
            object_key=row["source_object_key"], content_sha256=row["content_sha256"],
            byte_size=row["byte_size"], object_version_id=row["source_object_version_id"])
        raw = c.api_dependencies.case_agent_artifact_review_service._object_store.read_case_agent_review_candidate(
            stored, artifact_id=artifact)
        source = VisualRecoverySource(
            firm_id=str(row["firm_id"]), matter_id=str(row["matter_id"]),
            matter_version=row["snapshot_matter_version"], run_id=RUN,
            run_event_version=row["current_event_version"], run_status=row["run_status"],
            failure_code=row["failure_code"], task_id=str(row["task_id"]),
            task_status=row["result_status"], attempt_id=str(row["attempt_id"]),
            artifact_id=artifact, content_sha256=expected_hash, byte_size=len(raw),
            task_input_hash=row["task_input_hash"], external_request_id=row["external_request_id"],
            cost_minor_units=row["cost_minor_units"], input_refs=tuple(row["input_refs"]))
        draft = prepare_visual_candidate_recovery(source=source, original=raw)
        envelope = json.loads(draft.payload)
        sections = _visual_sections(envelope["interpreted_candidate"])
        print(json.dumps(dict(
            diagnostic_only=True, persisted=False, original_sha256=expected_hash,
            original_status=row["run_status"], original_event_version=row["current_event_version"],
            recovery_id=draft.recovery_id, recovery_sha256=draft.content_sha256,
            recovery_bytes=len(draft.payload), section_count=len(sections),
            section_titles=[item.title for section in sections for item in section.items],
            completeness=envelope["completeness_status"], model_calls=0, database_writes=0,
            object_writes=0, browser_accepted=False), ensure_ascii=False))


if __name__ == "__main__":
    main()
