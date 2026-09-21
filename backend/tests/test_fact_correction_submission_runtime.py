"""Rollback-only real PostgreSQL bridge for migration 0082 and fact submission.

Private bytes are read before DDL locks; the command uses that fixed test reader.
This proves SQL/ledger composition, not live-source concurrency or HTTP approval.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
import unittest

from backend.tests.test_fact_correction_runtime import (
    _Psql, _ActualSourceReader, ROOT, MATTER, FIRM, ACTOR,
)
from case_kernel.case_agent_fact_correction_postgres import PostgresFactCorrectionProposalStore
from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.fact_claim_ledger import FactStatus
from case_kernel.models import Actor, Role


@unittest.skipUnless(os.environ.get("LAWCASE_CORRECTION_SUBMISSION_PROBE") == "1",
                     "explicit managed synthetic rollback probe only")
class FactCorrectionSubmissionRuntimeTests(unittest.TestCase):
    def test_submit_replay_dispute_decision_and_lineage_guards_rollback(self):
        connection = _Psql()
        try:
            connection.execute("BEGIN")
            connection.execute("SET LOCAL statement_timeout='15s'")
            connection.execute("SET LOCAL lock_timeout='5s'")
            row = connection.execute("""SELECT p.proposal_id,p.extraction_candidate_id,
                p.expected_matter_version,b.artifact_id,m.version AS current_version
                FROM case_agent_fact_correction_proposals p
                JOIN case_agent_ledger_extraction_candidates c USING (extraction_candidate_id,firm_id,matter_id)
                JOIN case_agent_ledger_extraction_batches b USING (extraction_batch_id,firm_id,matter_id)
                JOIN matters m USING (matter_id,firm_id)
                WHERE p.proposal_id=%s AND p.matter_id=%s AND p.firm_id=%s""",
                ('49d495f1-d866-4d3a-b6b6-b8139ea6251d',MATTER,FIRM)).fetchone()
            self.assertIsNotNone(row)
            if row['current_version'] != row['expected_matter_version']:
                self.skipTest('fixed proposal already consumed or stale; do not rewind the real case')
            raw = _ActualSourceReader().read_verified_original(matter_id=MATTER,
                artifact_id=row['artifact_id'],expected_matter_version=row['expected_matter_version'])
            deployed = connection.execute("""SELECT EXISTS(SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='case_facts'
                AND column_name='correction_proposal_id') AS deployed""").fetchone()['deployed']
            if not deployed:
                migration = (ROOT/'backend/migrations/0082_fact_correction_candidate_lineage.sql').read_text()
                connection.execute('\n'.join(line for line in migration.splitlines()
                    if line not in {'BEGIN;','COMMIT;'}))
            connection.execute("SET LOCAL ROLE lawcase_web_application")
            connection.execute("SELECT set_config('app.firm_id',%s,true)",(FIRM,))
            connection.execute("SELECT set_config('app.actor_id',%s,true)",(ACTOR,))

            class Reader:
                def read_verified_original(self, **kwargs): return raw

            class Store(PostgresFactCorrectionProposalStore):
                @contextmanager
                def _transaction(self, actor, *, read_only=False): yield connection

            class Ledger(PostgresCaseLedgerStore):
                @contextmanager
                def _transaction(self, firm_id): yield connection

            store = Store('rollback probe',original_reader=Reader())
            actor = Actor(ACTOR,FIRM,frozenset({Role.LEAD_LAWYER}))
            args = dict(actor=actor,matter_id=MATTER,candidate_id=row['extraction_candidate_id'],
                proposal_id=row['proposal_id'],expected_matter_version=row['expected_matter_version'],
                idempotency_key='rollback-submit-correction-v1')
            receipt = store.submit_for_fact_review(**args)
            self.assertEqual(store.submit_for_fact_review(**args),receipt)
            recovery = dict(actor=actor,matter_id=MATTER,idempotency_key=args['idempotency_key'])
            self.assertEqual(store.find_submission_by_key(**recovery),receipt)
            self.assertIsNone(store.find_submission_by_key(**{**recovery,'idempotency_key':'unknown-probe-key'}))
            fact = connection.execute("SELECT status,correction_proposal_id FROM case_facts WHERE fact_id=%s",
                (receipt.object_id,)).fetchone()
            self.assertEqual(fact['status'],'CANDIDATE')
            self.assertEqual(fact['correction_proposal_id'],row['proposal_id'])
            self.assertEqual(receipt.matter_version,row['expected_matter_version']+1)
            decided = Ledger('rollback probe').decide_fact(actor=actor,matter_id=MATTER,
                fact_id=receipt.object_id,expected_version=receipt.matter_version,
                idempotency_key='rollback-decide-correction-v1',status=FactStatus.DISPUTED,
                decision_hash=sha256(b'synthetic lawyer decision').hexdigest())
            self.assertEqual(decided.matter_version,receipt.matter_version+1)
            # Trigger rejection caught inside a subtransaction, not a swallowed
            # Python error after PostgreSQL has aborted the whole transaction.
            connection.execute("""DO $guard$ BEGIN
                BEGIN
                    UPDATE case_facts SET original_text='tampered' WHERE fact_id=%s;
                    RAISE EXCEPTION 'guard did not reject' USING ERRCODE='ZX001';
                EXCEPTION WHEN raise_exception THEN NULL;
                END;
                BEGIN
                    DELETE FROM case_facts WHERE fact_id=%s;
                    RAISE EXCEPTION 'guard did not reject' USING ERRCODE='ZX001';
                EXCEPTION WHEN raise_exception THEN NULL;
                END;
            END $guard$""",(receipt.object_id,receipt.object_id))
            self.assertEqual(store.submit_for_fact_review(**args),receipt)
        finally:
            connection.close()


if __name__ == '__main__': unittest.main()
