"""Production deployment entrypoint for one firm-scoped Agent Worker."""

import os

from case_kernel.case_agent_planning_snapshot_postgres import (
    PostgresCasePlanningProjectionRepository,
)
from case_kernel.case_agent_runtime_postgres import (
    PostgresCaseAgentRunnerIncidentSink,
    preflight_case_agent_runtime_contract,
)
from case_kernel.case_agent_research_postgres import (
    preflight_case_agent_research_runtime_contract,
)
from case_kernel.qwen_visual_ocr_postgres import (
    preflight_case_agent_visual_ocr_runtime_contract,
)
from case_kernel.case_agent_memory_postgres import PostgresCaseAgentMemoryStore
from case_kernel.official_source_private_store import (
    compose_official_source_s3_adapters,
)

from .case_agent_worker_entrypoint import (
    CaseAgentWorkerProcessSettings,
    compose_production_case_agent_worker,
)
from .case_agent_worker_runtime import run_composed_worker


def main() -> None:
    # Parse all server-only settings before opening provider clients.  The
    # repository shares the execution Worker's RLS-bound DSN and performs one
    # REPEATABLE READ planning projection; the verifier uses its separately
    # configured principal inside the composed runtime.
    settings = CaseAgentWorkerProcessSettings.from_environment(os.environ)
    # Do not construct providers or enter the consumer loop until both
    # principals can see the exact 0033/0034/0035 contract.  Since readiness
    # heartbeats are emitted only by ``serve_forever``, a failed preflight can
    # never make the Web API advertise a paper capability.
    preflight_case_agent_runtime_contract(
        execution_dsn=settings.runtime.postgres_dsn,
        verifier_dsn=settings.runtime.verifier_postgres_dsn,
        firm_id=settings.runtime.actor.firm_id,
        execution_actor_id=settings.runtime.actor.actor_id,
        verifier_actor_id=settings.runtime.verifier_actor.actor_id,
    )
    # Public search is optional.  If the administrator supplies its key, the
    # exact 0037 contract becomes a hard startup dependency for that adapter;
    # without the key, PDF/Office and planning memory stay available.
    if getattr(settings, "brave_credentials", None) is not None:
        preflight_case_agent_research_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
        )
    # Visual OCR is independently optional.  Supplying its server-only
    # configuration makes the immutable 0038 source/egress ledger a hard
    # dependency before any provider or repository object is constructed.
    if getattr(settings, "qwen_credentials", None) is not None:
        preflight_case_agent_visual_ocr_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
        )
    repository = PostgresCasePlanningProjectionRepository(
        settings.runtime.postgres_dsn
    )
    memory = PostgresCaseAgentMemoryStore(settings.runtime.postgres_dsn)
    incidents = PostgresCaseAgentRunnerIncidentSink(
        dsn=settings.runtime.postgres_dsn,
        actor=settings.runtime.actor,
    )
    official_source_text = None
    if getattr(settings, "document_delivery_enabled", False):
        official_source_text = compose_official_source_s3_adapters(
            settings.object_store
        ).verified_text
    runtime = compose_production_case_agent_worker(
        settings=settings,
        planning_repository=repository,
        memory_checkpoint=memory,
        incident_sink=incidents,
        document_official_source_text=official_source_text,
    )
    run_composed_worker(runtime)


if __name__ == "__main__":
    main()
