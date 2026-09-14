"""One fixed synthetic run: bounded planning only, never task dispatch."""
from dataclasses import asdict, replace
from decimal import Decimal
import json
import os
from pathlib import Path

from case_api.case_agent_worker_entrypoint import CaseAgentWorkerProcessSettings, compose_production_case_agent_worker
from case_kernel.case_agent_planning_snapshot_postgres import PostgresCasePlanningProjectionRepository
from case_kernel.deepseek_case_agent_planner import prepare_deepseek_planner_request

MATTER = "ad70242f-0a41-540b-a125-d4f809f0309e"
RUN = "781a290d-7588-5e12-93a0-6e7545cd5ea3"
CHECKPOINT = Path("/var/lib/lawcase/planning-checkpoint")


def record(name, value):
    with (CHECKPOINT / name).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, default=str, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
    settings = replace(settings, document_delivery_enabled=False, document_renderer_settings=None,
        deepseek_config=replace(settings.deepseek_config, max_output_tokens=4096, timeout_seconds=60))
    if settings.runtime.actor.firm_id != "11111111-1111-4111-8111-111111111111":
        raise RuntimeError("synthetic firm only")
    runtime = compose_production_case_agent_worker(settings=settings,
        planning_repository=PostgresCasePlanningProjectionRepository(settings.runtime.postgres_dsn))
    worker = runtime.worker
    state = worker._projection(matter_id=MATTER, run_id=RUN).state
    if state.event_version != 1 or state.status.value != "CREATED" or state.snapshot.matter_version != 20:
        raise RuntimeError("run already advanced; inspect persistent result, never replay")
    snapshot = worker._snapshot_provider.build_for_run(state=state, actor=settings.runtime.actor)
    request = prepare_deepseek_planner_request(goal=state.goal, snapshot=snapshot,
        skills=worker._compiler.semantic_skill_catalog(), config=settings.deepseek_config)
    # Deliberately overestimate text tokens plus serialization overhead; peak
    # public list rates checked 2026-09-07. Not an invoice or a tokenizer count.
    input_allowance = 2 * len(request.body) + 4096
    conservative_cny = (Decimal(input_allowance)*9 + Decimal(4096)*27)/1000000 + Decimal("0.06")
    if request.model != "deepseek-v4-pro" or conservative_cny > Decimal("1.00"):
        raise RuntimeError("fixed planning plus OCR budget exceeded before submission")
    record("planning-preflight.json", dict(run_id=RUN, request_hash=request.request_hash,
        planning_hash=snapshot.planning_hash, request_bytes=len(request.body),
        max_output_tokens=4096, conservative_total_cny=conservative_cny,
        planning_calls_allowed=1, task_dispatch_allowed=False))
    real_planner = worker._planner

    class OncePlanner:
        planner_id = real_planner.planner_id
        used = False

        def plan(self, **kwargs):
            candidate = prepare_deepseek_planner_request(goal=kwargs["goal"], snapshot=kwargs["snapshot"],
                skills=kwargs["skills"], config=settings.deepseek_config)
            if self.used or candidate.request_hash != request.request_hash:
                raise RuntimeError("planning request changed or duplicate send attempted")
            self.used = True
            return real_planner.plan(**kwargs)

    worker._planner = OncePlanner()
    result = worker.process_run_once(matter_id=MATTER, run_id=RUN)
    record("planning-step.json", asdict(result))
    print(json.dumps(asdict(result), default=str), flush=True)


if __name__ == "__main__":
    main()
