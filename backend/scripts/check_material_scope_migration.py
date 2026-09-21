"""Fixed synthetic SQL trigger probe, always rolled back; no runtime dispatch."""
import hashlib
import json
from pathlib import Path
import subprocess

RUN = "781a290d-7588-5e12-93a0-6e7545cd5ea3"
EVENT = "781a290d-7588-5e12-93a0-6e7545cd5ea4"
CMD = ["docker", "exec", "-i", "--user", "postgres", "lawcase-managed-alpha-postgres-1",
       "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "lawcase"]


def sql(query):
    result = subprocess.run(CMD, input=query, text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main():
    if sql("SELECT to_regprocedure('public.guard_case_agent_material_scope_review()') IS NULL;").stdout.strip() != "t":
        raise RuntimeError("migration already present; do not replay predeployment probe")
    base = json.loads(sql(f"SELECT projection FROM case_agent_checkpoints WHERE run_id='{RUN}' AND event_version=3;").stdout)
    outcome = json.loads(sql(f"SELECT jsonb_build_object('request_hash',request_hash,'input_hash',input_hash,'proposal',structured_proposal) FROM case_agent_planning_external_events WHERE run_id='{RUN}' AND status='SUCCEEDED';").stdout)
    refs = ["evidence-page:0ed71e7c-8f9e-46e2-b3e9-ca382d9f0fc7"]
    goal = {key: value for key, value in base["goal"].items() if key != "goal_hash"}
    goal.update(schema_version="lawyer-agent-goal-v3", material_read_refs=refs)
    payload = dict(snapshot=base["snapshot"], original_goal_hash=base["goal"]["goal_hash"],
        original_proposal_hash=digest(outcome["proposal"]), request_hash=outcome["request_hash"],
        planning_hash=outcome["input_hash"], material_read_refs=refs, previous_output_bytes=8388608,
        approved_output_bytes=33554432, effective_goal_hash=digest(goal),
        derived_proposal_hash=digest({**outcome["proposal"], "goal_hash": digest(goal)}),
        compiled_graph_hash="aec1c00289fedf1b5e91833be44f9c2bc0a48aadafcb4b746e85f673d8f33736",
        approved_by="22222222-2222-4222-8222-222222222222")
    encoded = json.dumps(payload, ensure_ascii=False).replace("'", "''")
    # Placeholder event hash is intentional: this tests SQL triggers only,
    # never the application event-hash/replay contract or an approved event.
    insert = f"""INSERT INTO case_agent_events(event_id,run_id,firm_id,matter_id,event_sequence,event_type,actor_id,payload,event_hash,occurred_at)
      VALUES('{EVENT}','{RUN}','11111111-1111-4111-8111-111111111111','ad70242f-0a41-540b-a125-d4f809f0309e',4,
      'PLANNING_MATERIAL_SCOPE_REVIEWED','22222222-2222-4222-8222-222222222222',PAYLOAD,repeat('a',64),now());"""
    probe = "SET LOCAL ROLE lawcase_web_application; SELECT set_config('app.firm_id','11111111-1111-4111-8111-111111111111',true);\n"
    probe += f"DO $$ DECLARE p jsonb := '{encoded}'::jsonb; rejected boolean := false; BEGIN BEGIN "
    probe += insert.replace("PAYLOAD", "p || jsonb_build_object('derived_proposal_hash',repeat('0',64))")
    probe += " EXCEPTION WHEN raise_exception THEN IF SQLERRM <> 'derived material proposal differs' THEN RAISE; END IF; rejected := true; END; IF NOT rejected THEN RAISE EXCEPTION 'tampered review accepted'; END IF; "
    probe += insert.replace("PAYLOAD", "p")
    probe += " RAISE NOTICE 'WEB_ROLE_REVIEW_INSERT_AND_TAMPER_REJECTION_PASS_ROLLBACK_ONLY'; END $$; ROLLBACK;"
    migration = (Path(__file__).resolve().parents[1] / "migrations/0084_retained_material_scope_reviews.sql").read_text()
    assert migration.count("COMMIT;") == 1
    result = sql(migration.replace("COMMIT;", "") + probe)
    print(result.stderr.strip())
    assert sql(f"SELECT count(*) FROM case_agent_events WHERE event_id='{EVENT}';").stdout.strip() == "0"
    assert sql("SELECT to_regprocedure('public.guard_case_agent_material_scope_review()') IS NULL;").stdout.strip() == "t"
    print("ROLLBACK_INDEPENDENTLY_CONFIRMED_NO_EVENT_NO_MIGRATION")


if __name__ == "__main__":
    main()
