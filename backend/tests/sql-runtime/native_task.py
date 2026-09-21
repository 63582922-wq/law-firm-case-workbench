"""Execute a retained synthetic native trial; no planner, provider or schema resets."""
import json
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


class NoNewPlanning:
    planner_id = "native-existing-graph-only"

    def plan(self, **kwargs):
        raise RuntimeError("native continuation cannot make a new planning request")

    def build_for_run(self, **kwargs):
        raise RuntimeError("native continuation cannot replan")


def run_retained_task(*, run_id, socket_path, data_path, client, credentials, inspect_only=False):
    from case_kernel.case_agent_postgres import PostgresCaseAgentStore
    from case_kernel.case_agent_worker_postgres import PostgresCaseAgentWorkerAdapter
    from case_kernel.case_agent_worker import CaseAgentWorker
    from case_kernel.case_agent_runtime_identity import case_agent_worker_id
    from case_kernel.case_agent_runtime_postgres import PostgresReviewCandidateStagingPort, PostgresManagedArtifactAccessPort
    from case_kernel.case_agent_case_context_postgres import PostgresCaseContextProjectionPort
    from case_kernel.case_agent_case_context_adapters import DeterministicCaseContextTaskAdapter, CASE_CONTEXT_REVIEW_MANIFEST
    from case_kernel.case_agent_verifier import CaseAgentRunVerifier, first_release_review_candidate_verifiers
    from case_kernel.case_agent_planner import CaseAgentPlannerCompiler
    from case_kernel.case_work_plan_postgres import PostgresCaseWorkPlanStore
    from case_kernel.skill_registry import default_case_skill_registry
    from case_kernel.web_object_store import S3CompatiblePrivateObjectStore, S3PrivateObjectStoreConfig
    from case_kernel.models import Actor, Role
    from case_api.case_agent_worker_runtime import _case_context_policy

    project = Path(__file__).resolve().parents[3]
    socket_path, data_path = socket_path.resolve(strict=True), data_path.resolve(strict=True)
    if (not socket_path.is_relative_to(project / "artifacts") or socket_path.stat().st_mode & 0o077
            or not data_path.is_relative_to(project / "artifacts/native-pg16")):
        raise RuntimeError("only retained private native test cluster is allowed")
    base = make_conninfo(host=str(socket_path), port=5432, dbname="postgres", user="postgres", connect_timeout=5,
        options="-c default_transaction_read_only=on" if inspect_only else "")
    with psycopg.connect(base, row_factory=dict_row) as connection:
        assert Path(connection.execute("SHOW data_directory").fetchone()["data_directory"]).resolve() == data_path
        assert connection.execute("SHOW listen_addresses").fetchone()["listen_addresses"] == ""
        rows = connection.execute("""SELECT run.firm_id, run.matter_id, principal.user_id, principal.external_subject
            FROM case_agent_runs run JOIN firms firm USING(firm_id)
            JOIN matter_actor_roles role USING(firm_id,matter_id) JOIN users principal USING(firm_id,user_id)
            WHERE run.run_id=%s AND firm.display_name='Synthetic Test Firm'
              AND role.role='SYSTEM_WORKER' AND role.revoked_at IS NULL AND principal.status='ACTIVE'""", (run_id,)).fetchall()
    assert len(rows) == 2
    worker_row = next(row for row in rows if row["external_subject"].startswith("system-worker-"))
    verifier_row = next(row for row in rows if row["external_subject"].startswith("system-verifier-"))
    firm_id, matter_id = str(worker_row["firm_id"]), str(worker_row["matter_id"])
    worker_actor = Actor(str(worker_row["user_id"]), firm_id, frozenset({Role.SYSTEM_WORKER}))
    verifier_actor = Actor(str(verifier_row["user_id"]), firm_id, frozenset({Role.SYSTEM_WORKER}))
    read_only = " -c default_transaction_read_only=on" if inspect_only else ""
    worker_dsn = make_conninfo(base, options="-c role=lawcase_agent_worker" + read_only)
    verifier_dsn = make_conninfo(base, options="-c role=lawcase_agent_verifier" + read_only)
    for dsn, expected in ((worker_dsn, "lawcase_agent_worker"), (verifier_dsn, "lawcase_agent_verifier")):
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            row = connection.execute("SELECT current_user AS role, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            assert row["role"] == expected and not row["rolsuper"] and not row["rolbypassrls"]
    objects = S3CompatiblePrivateObjectStore(S3PrivateObjectStoreConfig(endpoint_url="http://127.0.0.1:19090",
        region_name="us-east-1", bucket="lawcase-native-synthetic", access_key_id=credentials["access_key"],
        secret_access_key=credentials["secret_key"], allow_insecure_internal_endpoint=True), client=client)
    task_adapter = DeterministicCaseContextTaskAdapter(
        projection_port=PostgresCaseContextProjectionPort(dsn=worker_dsn, worker_actor=worker_actor),
        staging_port=PostgresReviewCandidateStagingPort(dsn=worker_dsn, worker_actor=worker_actor, object_store=objects))
    store = PostgresCaseAgentStore(worker_dsn)
    initial = store.read_projection(matter_id=matter_id, actor=worker_actor, run_id=run_id).state
    assert initial.graph is not None and len(initial.graph.tasks) == 1
    assert initial.graph.tasks[0].skill.tool_id == CASE_CONTEXT_REVIEW_MANIFEST.tool_id
    compiler = CaseAgentPlannerCompiler(registry=default_case_skill_registry(case_context_review_enabled=True),
        adapters={CASE_CONTEXT_REVIEW_MANIFEST.tool_id: CASE_CONTEXT_REVIEW_MANIFEST}, skill_policies=(_case_context_policy(),))
    access = PostgresManagedArtifactAccessPort(dsn=verifier_dsn, verifier_actor=verifier_actor,
        execution_actor_id=worker_actor.actor_id, object_store=objects)
    if inspect_only:
        from hashlib import sha256
        from case_api.web_case_agent_artifacts import _case_context_sections

        if initial.status.value != "READY_FOR_REVIEW" or not initial.verification_hash:
            raise RuntimeError("inspection requires a retained verified review candidate")
        for artifact in initial.artifacts:
            managed = access.read_managed_artifact(firm_id=firm_id, matter_id=matter_id,
                run_id=run_id, artifact=artifact)
            if len(managed.content) != artifact.byte_size or sha256(managed.content).hexdigest() != artifact.content_hash:
                raise RuntimeError("retained artifact bytes differ from the run receipt")
            payload = json.loads(managed.content)
            title, sections = _case_context_sections(payload)
            print(json.dumps(dict(artifact_id=artifact.artifact_id, title=title,
                section_count=len(sections), candidate=payload), ensure_ascii=False), flush=True)
        after = store.read_projection(matter_id=matter_id, actor=worker_actor, run_id=run_id).state
        if after != initial:
            raise RuntimeError("run changed during inspection")
        print(json.dumps(dict(status="READ_ONLY_INSPECTION_PASSED", event_version=after.event_version)), flush=True)
        return
    verifier = CaseAgentRunVerifier(verifier_id="native-independent-verifier", verifier_version="1.0.0",
        artifact_access=access, artifact_verifiers=first_release_review_candidate_verifiers())
    worker = CaseAgentWorker(worker_id=case_agent_worker_id(firm_id), actor=worker_actor,
        store=PostgresCaseAgentWorkerAdapter(store=store, actor=worker_actor), snapshot_provider=NoNewPlanning(),
        planner=NoNewPlanning(), planner_compiler=compiler, adapters={task_adapter.manifest.tool_id: task_adapter},
        verifier=verifier, verifier_actor=verifier_actor,
        verifier_store=PostgresCaseAgentWorkerAdapter(store=PostgresCaseAgentStore(verifier_dsn), actor=verifier_actor),
        work_plan_promotion=PostgresCaseWorkPlanStore(worker_dsn))
    for _ in range(3):
        result = worker.process_run_once(matter_id=matter_id, run_id=run_id)
        print(json.dumps(dict(step=result.step.value, event_version=result.event_version, reason=result.reason_code)), flush=True)
        if result.step.value in {"WAITING_HUMAN", "IDLE", "FAILED"}:
            break
    final = store.read_projection(matter_id=matter_id, actor=worker_actor, run_id=run_id).state
    print(json.dumps(dict(status=final.status.value, event_version=final.event_version,
        artifact_count=len(final.artifacts), independently_verified=bool(final.verification_hash))), flush=True)
