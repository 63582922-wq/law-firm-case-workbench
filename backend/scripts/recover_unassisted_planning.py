"""One synthetic retained plan recovery; no provider retries or task dispatch."""
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from uuid import UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from case_api.case_agent_worker_entrypoint import CaseAgentWorkerProcessSettings, compose_production_case_agent_worker
from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository
from case_kernel.case_agent_postgres import PostgresCaseAgentStore, _payload_hash
from case_kernel.case_agent_supervisor import AgentEventType, AgentSupervisorEvent, PlanningBudgetReviewPayload
from case_kernel.models import Actor, Role

MATTER = "767fda38-e3de-5a15-816f-510a686c7600"
RUN = "bd10a172-5a80-5ebb-bda3-a6c3cb05ab70"
PROPOSAL = "c64c790767eb1a10c3cb9f01d765e057d8c534039ccde39e4a2510c08cfe4689"


class NoPlanningSubmission:
    planner_id = "retained-proposal-only"

    def plan(self, **kwargs):
        raise RuntimeError("recovery must never request a new model proposal")


def main():
    settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
    settings = replace(settings, document_delivery_enabled=False, document_renderer_settings=None,
        deepseek_config=replace(settings.deepseek_config, timeout_seconds=60, max_output_tokens=4096))
    if settings.runtime.actor.firm_id != "11111111-1111-4111-8111-111111111111":
        raise RuntimeError("synthetic firm only")
    runtime = compose_production_case_agent_worker(settings=settings,
        planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn))
    runtime.worker._planner = NoPlanningSubmission()
    state = runtime.worker._projection(matter_id=MATTER, run_id=RUN).state
    if (state.event_version, state.status.value) not in {(3, "WAITING_INPUT"), (4, "PLANNING")}:
        raise RuntimeError("unexpected recovery state; inspect without replay")
    snapshot = runtime.worker._snapshot_provider.build_for_run(state=state, actor=settings.runtime.actor)
    app_dsn = os.environ["LAWCASE_RECOVERY_APP_DSN"]
    with psycopg.connect(app_dsn, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SELECT set_config('app.firm_id',%s,true)", (state.firm_id,))
        assert connection.execute("SELECT current_user AS role").fetchone()["role"] == "lawcase_web_application"
        rows = connection.execute("SELECT request_hash,input_hash,structured_proposal FROM case_agent_planning_external_events WHERE run_id=%s AND status='SUCCEEDED'", (RUN,)).fetchall()
    if len(rows) != 1 or _payload_hash(rows[0]["structured_proposal"]) != PROPOSAL:
        raise RuntimeError("retained proposal differs")
    row = rows[0]
    if snapshot.planning_hash != row["input_hash"]:
        raise RuntimeError("current planning inputs differ")
    if state.event_version == 3:
        actor = Actor("22222222-2222-4222-8222-222222222222", state.firm_id, frozenset({Role.LEAD_LAWYER}))
        payload = PlanningBudgetReviewPayload(state.snapshot, 600, 660, row["request_hash"],
            row["input_hash"], PROPOSAL, actor.actor_id)
        event = AgentSupervisorEvent(event_id=str(uuid5(UUID(RUN), "runtime-review-660-v1")),
            run_id=RUN, firm_id=state.firm_id, matter_id=MATTER, sequence=4,
            event_type=AgentEventType.PLANNING_BUDGET_REVIEWED, actor_id=actor.actor_id,
            occurred_at=datetime.now(timezone.utc), payload=payload)
        receipt = PostgresCaseAgentStore(app_dsn).review_retained_planning_budget(matter_id=MATTER,
            actor=actor, expected_event_version=3, idempotency_key="unassisted-budget-review-660-v1",
            event=event, compiler=runtime.worker._compiler, planning_snapshot=snapshot)
        print(json.dumps({"budget_review_receipt": asdict(receipt)}, default=str), flush=True)
    current = runtime.worker._projection(matter_id=MATTER, run_id=RUN).state
    if current.status.value != "PLANNING" or current.event_version != 4 or current.budget.max_runtime_seconds != 660:
        raise RuntimeError("budget review was not replayed")
    result = runtime.worker.process_run_once(matter_id=MATTER, run_id=RUN)
    print(json.dumps({"recovery_result": asdict(result), "model_calls_allowed": 0}, default=str), flush=True)


if __name__ == "__main__":
    main()
