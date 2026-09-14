"""Opt-in rollback probe against the isolated managed synthetic case.

Run from the repository root with LAWCASE_CORRECTION_RUNTIME_PROBE=1.
The psql bridge keeps uncommitted migration DDL and Web-role SQL in one
transaction. This is NOT a test of independent commits or concurrent clients.
No credentials, source documents, or proposal content are printed.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import unittest
from uuid import uuid4

from psycopg import sql

from case_kernel.case_agent_fact_correction_postgres import (
    FactCorrectionBlocked, PostgresFactCorrectionProposalStore,
)
from case_kernel.models import Actor, Role

ROOT = Path(__file__).resolve().parents[2]
MATTER = "767fda38-e3de-5a15-816f-510a686c7600"
FIRM = "11111111-1111-4111-8111-111111111111"
ACTOR = "22222222-2222-4222-8222-222222222222"


class _Psql:
    def __init__(self):
        self.process = subprocess.Popen([
            "docker", "exec", "-i", "--user", "postgres",
            "lawcase-managed-alpha-postgres-1", "psql", "-X", "-qAt",
            "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "lawcase",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def execute(self, query, args=()):
        parts = query.split("%s")
        if len(parts) != len(args) + 1:
            raise AssertionError("parameter count")
        query = parts[0] + "".join(sql.Literal(value).as_string() + suffix
                                   for value, suffix in zip(args, parts[1:]))
        select = query.lstrip().upper().startswith("SELECT")
        returning = "RETURNING " in query.upper()
        if query.lstrip().upper().startswith(("UPDATE ", "DELETE ")) and not returning:
            query = query.rstrip("; \n") + " RETURNING 1 AS affected"
            returning = True
        if select:
            query = "SELECT row_to_json(probe_row) FROM (" + query + ") probe_row"
        elif returning:
            query = "WITH probe_row AS (" + query + ") SELECT row_to_json(probe_row) FROM probe_row"
        marker = "probe_" + uuid4().hex
        self.process.stdin.write(query.rstrip("; \n") + ";\n\\echo " + marker + "\n")
        self.process.stdin.flush()
        lines = []
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError("rollback SQL probe stopped; inspect PostgreSQL error")
            if line.strip() == marker:
                break
            if line.strip(): lines.append(line.strip())
        self.rows = [json.loads(line) for line in lines] if select or returning else []
        for row in self.rows:
            if "proposal_content" in row:
                row["proposal_content"] = bytes.fromhex(row["proposal_content"][2:])
            if "created_at" in row:
                row["created_at"] = datetime.fromisoformat(row["created_at"])
        self.row = self.rows[0] if self.rows else None
        self.rowcount = len(self.rows)
        return self

    def fetchone(self): return self.row
    def fetchall(self): return self.rows

    def close(self):
        # EOF also rolls back if an assertion or SQL error terminated the probe.
        if self.process.poll() is None:
            try: self.execute("ROLLBACK")
            finally: self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close()


class _ActualSourceReader:
    def read_verified_original(self, **kwargs):
        modules = [
            ("case_kernel.case_agent_ledger_extraction", "backend/case_kernel/case_agent_ledger_extraction.py"),
            ("case_kernel.case_agent_fact_correction_postgres", "backend/case_kernel/case_agent_fact_correction_postgres.py"),
            ("case_api.web_fact_correction", "backend/case_api/web_fact_correction.py"),
        ]
        program = "import sys, types\n"
        for name, path in modules:
            program += (f"m=types.ModuleType({name!r});m.__package__={name.rsplit('.',1)[0]!r};"
                        f"sys.modules[{name!r}]=m;exec(compile({(ROOT/path).read_text()!r},"
                        f"{path!r},'exec'),m.__dict__)\n")
        program += f'''
sys.path.insert(0,'/app/backend/scripts')
from run_managed_defence_acceptance import _build_composition, _issue_fixture_identity
from case_api.web_fact_correction import WebFactCorrectionOriginalReader
from case_kernel.web_agent_evidence_projection import WebAgentEvidenceProjectionSource, WebAgentEvidenceProjectionPolicy
import base64
c=_build_composition(None)
i,sid=_issue_fixture_identity(c)
try:
    assert i.actor.actor_id == {ACTOR!r} and i.actor.firm_id == {FIRM!r}
    projection=WebAgentEvidenceProjectionSource(evidence_store=c.evidence_manifest_store,object_store=c.object_store,system_worker_for_firm=c.system_worker_for_firm,policy=WebAgentEvidenceProjectionPolicy(worker_root=c.private_roots.worker_materialization_root/'ledger-confirmation'))
    reader=WebFactCorrectionOriginalReader(artifact_review_service=c.api_dependencies.case_agent_artifact_review_service,evidence_projection=projection,matter_store=c.api_dependencies.matter_store)
    raw=reader.read_verified_original(actor=i.actor,matter_id={kwargs['matter_id']!r},artifact_id={kwargs['artifact_id']!r},expected_matter_version={kwargs['expected_matter_version']!r})
    print(base64.b64encode(raw).decode())
finally:
    c.api_dependencies.session_authority.revoke(session_id=sid)
'''
        result = subprocess.run(["docker", "exec", "-i", "lawcase-managed-alpha-api-1", "python", "-"],
                                input=program, text=True, capture_output=True, timeout=50)
        if result.returncode:
            raise AssertionError("actual source-reader process failed: " + result.stderr[-1500:])
        import base64
        return base64.b64decode(result.stdout.strip(), validate=True)


@unittest.skipUnless(os.environ.get("LAWCASE_CORRECTION_RUNTIME_PROBE") == "1", "explicit managed fixture probe only")
class FactCorrectionRuntimeTests(unittest.TestCase):
    def test_actual_source_save_replay_and_draft_recovery_rollback(self):
        connection = _Psql()
        try:
            connection.execute("BEGIN")
            if connection.execute("SELECT to_regclass('public.case_agent_fact_correction_proposals') IS NOT NULL AS deployed").fetchone()["deployed"]:
                self.skipTest("predeployment rollback probe only; preserve deployed proposals")
            connection.execute("SET LOCAL statement_timeout='15s'")
            connection.execute("SET LOCAL lock_timeout='5s'")
            migration = (ROOT / "backend/migrations/0081_lawyer_fact_correction_proposals.sql").read_text()
            migration = "\n".join(line for line in migration.splitlines() if line not in {"BEGIN;", "COMMIT;"})
            connection.execute(migration)
            connection.execute("GRANT INSERT ON case_agent_fact_correction_proposals TO lawcase_web_application")
            connection.execute("SET LOCAL ROLE lawcase_web_application")
            connection.execute("SELECT set_config('app.firm_id',%s,true)", (FIRM,))
            connection.execute("SELECT set_config('app.actor_id',%s,true)", (ACTOR,))
            candidate = connection.execute("""SELECT extraction_candidate_id
                FROM case_agent_ledger_extraction_candidates WHERE matter_id=%s AND firm_id=%s
                AND candidate_kind='FACT' AND candidate_payload->>'fact_text' LIKE %s
                ORDER BY extraction_candidate_id LIMIT 1""", (MATTER,FIRM,"%自认已支付利息147,000元%")).fetchone()
            self.assertIsNotNone(candidate, "fixed synthetic correction target missing")

            class Store(PostgresFactCorrectionProposalStore):
                @contextmanager
                def _transaction(self, actor, *, read_only=False):
                    yield connection

            store = Store("probe bridge", original_reader=_ActualSourceReader())
            actor = Actor(ACTOR,FIRM,frozenset({Role.LEAD_LAWYER}))
            args = dict(actor=actor,matter_id=MATTER,candidate_id=candidate["extraction_candidate_id"],
                expected_matter_version=11,expected_revision=0,idempotency_key="rollback-correction-probe",
                revised_text="原告在诉讼请求中主张扣除已付利息147,000元；该表述不单独确定付款主体。",
                reason="纠正原候选错误推定付款主体的措辞，保留原告诉请属性，待律师核验。")
            receipt = store.save(**args)
            self.assertEqual(store.save(**args),receipt)
            self.assertEqual(store.find_by_key(actor=actor,matter_id=MATTER,idempotency_key=args["idempotency_key"]),receipt)
            draft = store.read_current(actor=actor,matter_id=MATTER,candidate_id=args["candidate_id"])
            self.assertEqual(draft["proposal"]["revised_text"],args["revised_text"])
            self.assertFalse(draft["court_ready"])
            self.assertEqual(draft["revision_number"],1)
            with self.assertRaises(FactCorrectionBlocked):
                store.save(**{**args,"reason":"同键不同内容应被拒绝"})
            self.assertEqual(connection.execute("SELECT count(*) AS n FROM case_agent_fact_correction_proposals").fetchone()["n"],1)
        finally:
            connection.close()


if __name__ == "__main__": unittest.main()
