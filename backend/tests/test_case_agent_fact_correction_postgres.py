import unittest
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

from case_kernel.case_agent_fact_correction_postgres import (
    FactCorrectionBlocked, PostgresFactCorrectionProposalStore,
)
from case_kernel.case_agent_ledger_extraction import build_case_ledger_extraction_candidate
from case_kernel.models import Actor, Role
from backend.tests import test_case_agent_ledger_extraction as extraction_fixture


class _Reader:
    def __init__(self, raw):
        self.raw, self.calls, self.after_read = raw, 0, lambda: None

    def read_verified_original(self, **kwargs):
        self.calls += 1
        self.after_read()
        return self.raw


class _Rows:
    def __init__(self, row=None): self.row = row
    def fetchone(self): return self.row


class _Store(PostgresFactCorrectionProposalStore):
    def __init__(self, reader, binding):
        super().__init__("unused", original_reader=reader)
        self.binding, self.version, self.prior, self.head = binding, 11, None, None
        self.inserts, self.allowed = 0, True
        self.saved = None

    @contextmanager
    def _transaction(self, actor, *, read_only=False): yield self

    def _authorize(self, *args, **kwargs):
        if not self.allowed: raise FactCorrectionBlocked("permission revoked")
        return self.version

    def _binding(self, *args): return dict(self.binding)
    def _prior(self, *args): return self.prior

    def execute(self, sql, args):
        if "pg_advisory" in sql: return _Rows()
        if "SELECT to_jsonb(case_facts)" in sql: return _Rows(getattr(self,"fact",None))
        if "SELECT proposal_id,revision_number,proposal_content" in sql: return _Rows(self.saved)
        if "proposal_content, requested_by, created_at" in sql:
            return _Rows(self.saved)
        if "SELECT proposal_id,revision_number" in sql: return _Rows(self.head)
        if "INSERT INTO" in sql:
            self.inserts += 1
            self.prior = dict(proposal_id=args[0], matter_id=args[2],
                expected_matter_version=args[4], revision_number=args[5], request_hash=args[9])
            self.head = dict(proposal_id=args[0], revision_number=args[5])
            self.saved = dict(self.prior, proposal_content=memoryview(args[10]),
                requested_by=args[7], created_at=datetime.now(timezone.utc))
            return _Rows()
        raise AssertionError(sql)


class FactCorrectionStoreTests(unittest.TestCase):
    def setUp(self):
        fixture = extraction_fixture.CaseLedgerExtractionTests(); fixture.setUp()
        candidate = fixture._fact()
        raw, source = build_case_ledger_extraction_candidate(
            task_input_hash=extraction_fixture.digest("task"), source_pages=(fixture.page,), candidates=(candidate,))
        self.reader = _Reader(raw)
        self.store = _Store(self.reader, dict(candidate_hash=candidate.candidate_hash,
            artifact_id=str(uuid4()),artifact_content_sha256=sha256(raw).hexdigest(),source_hash=source))
        self.args = dict(actor=Actor(str(uuid4()),str(uuid4()),frozenset({Role.LEAD_LAWYER})),
            matter_id=str(uuid4()),candidate_id=str(uuid4()),expected_matter_version=11,
            expected_revision=0,idempotency_key="correction-test-v1",
            revised_text="合成材料陈述，尚待核验。",reason="补充材料归属说明。")

    def test_exact_replay_and_readback_after_version_change(self):
        first = self.store.save(**self.args)
        self.store.version = 12
        self.assertEqual(self.store.save(**self.args), first)
        self.assertEqual(self.store.find_by_key(actor=self.args["actor"],matter_id=self.args["matter_id"],
            idempotency_key=self.args["idempotency_key"]), first)
        self.assertEqual(self.store.inserts, 1)
        self.assertEqual(self.reader.calls, 1)
        with self.assertRaisesRegex(FactCorrectionBlocked, "different correction"):
            self.store.save(**{**self.args,"reason":"不同理由"})
        self.store.allowed = False
        with self.assertRaises(FactCorrectionBlocked): self.store.save(**self.args)

    def test_version_change_during_source_read_refuses_insert(self):
        self.reader.after_read = lambda: setattr(self.store,"version",12)
        with self.assertRaisesRegex(FactCorrectionBlocked,"changed during review"):
            self.store.save(**self.args)
        self.assertEqual(self.store.inserts,0)

    def test_recover_draft_preserves_original_and_marks_stale_without_approval(self):
        query = dict(actor=self.args["actor"], matter_id=self.args["matter_id"],
                     candidate_id=self.args["candidate_id"])
        self.assertIsNone(self.store.read_current(**query))
        self.assertEqual(self.store.read_context(**query),dict(current_matter_version=11,draft=None))
        receipt = self.store.save(**self.args)
        draft = self.store.read_current(**query)
        self.assertEqual(draft["proposal_id"], receipt.proposal_id)
        self.assertEqual(draft["proposal"]["revised_text"], self.args["revised_text"])
        self.assertIn("original_candidate", draft["proposal"])
        self.assertFalse(draft["stale"])
        self.store.version = 12
        draft = self.store.read_current(**query)
        self.assertEqual(self.store.read_context(**query)["current_matter_version"],12)
        self.assertTrue(draft["stale"])
        self.assertFalse(draft["court_ready"])
        self.assertEqual(draft["review_status"], "NEEDS_LAWYER_REVIEW")
        self.assertEqual(self.reader.calls, 1)
        self.store.allowed = False
        with self.assertRaises(FactCorrectionBlocked): self.store.read_current(**query)

    def test_predecessor_conflict_and_tampered_original_refuse_insert(self):
        self.store.head = dict(proposal_id=str(uuid4()),revision_number=1)
        with self.assertRaisesRegex(FactCorrectionBlocked,"another correction"):
            self.store.save(**self.args)
        self.store.head = None
        self.reader.raw += b" "
        with self.assertRaises(ValueError): self.store.save(**self.args)
        self.assertEqual(self.store.inserts,0)

    def test_missing_reader_and_worker_identity_rejected(self):
        with self.assertRaises(ValueError): PostgresFactCorrectionProposalStore("unused", original_reader=None)
        store = PostgresFactCorrectionProposalStore("unused", original_reader=self.reader)
        actor = Actor(str(uuid4()),str(uuid4()),frozenset({Role.SYSTEM_WORKER}))
        with self.assertRaisesRegex(FactCorrectionBlocked,"lawyer role"):
            store.save(**{**self.args,"actor":actor})
        mixed = Actor(actor.actor_id,actor.firm_id,frozenset({Role.SYSTEM_WORKER,Role.LEAD_LAWYER}))
        with self.assertRaisesRegex(FactCorrectionBlocked,"lawyer role"):
            store.find_by_key(actor=mixed,matter_id=self.args["matter_id"],idempotency_key="mixed-role-test")

    def _saved_review(self):
        receipt = self.store.save(**self.args)
        return dict(actor=self.args["actor"], matter_id=self.args["matter_id"],
            candidate_id=self.args["candidate_id"], proposal_id=receipt.proposal_id,
            expected_matter_version=11)

    def test_review_rechecks_sources_without_writing_or_approving(self):
        args = self._saved_review()
        result = self.store.verify_for_fact_review(**args)
        self.assertEqual(result.proposal_id, args["proposal_id"])
        self.assertEqual(result.matter_version, 11)
        self.assertEqual(self.reader.calls, 2)
        self.assertEqual(self.store.inserts, 1)
        content = json.loads(result.proposal_content)
        self.assertFalse(content["court_ready"])
        self.assertEqual(content["review_status"], "NEEDS_LAWYER_REVIEW")

    def test_review_rejects_missing_replaced_and_stale_proposal(self):
        args = self._saved_review()
        with self.assertRaisesRegex(FactCorrectionBlocked, "latest saved"):
            self.store.verify_for_fact_review(**{**args, "proposal_id": str(uuid4())})
        self.store.version = 12
        with self.assertRaisesRegex(FactCorrectionBlocked, "stale"):
            self.store.verify_for_fact_review(**{**args, "expected_matter_version": 12})
        self.store.version = 11
        self.store.saved = None
        with self.assertRaisesRegex(FactCorrectionBlocked, "latest saved"):
            self.store.verify_for_fact_review(**args)
        self.assertEqual(self.reader.calls, 1)

    def test_review_rejects_mutated_original_or_saved_source_link(self):
        args = self._saved_review()
        original = self.reader.raw
        self.reader.raw += b" "
        with self.assertRaises(ValueError):
            self.store.verify_for_fact_review(**args)
        self.reader.raw = original
        content = json.loads(bytes(self.store.saved["proposal_content"]))
        content["source_hash"] = "0" * 64
        self.store.saved["proposal_content"] = json.dumps(content).encode()
        with self.assertRaisesRegex(FactCorrectionBlocked, "does not match"):
            self.store.verify_for_fact_review(**args)

    def test_review_rejects_changes_during_original_read(self):
        for change in ("version", "permission", "head", "binding"):
            with self.subTest(change=change):
                self.setUp()
                args = self._saved_review()
                def mutate():
                    if change == "version": self.store.version += 1
                    elif change == "permission": self.store.allowed = False
                    elif change == "head": self.store.saved["proposal_id"] = str(uuid4())
                    else: self.store.binding["source_hash"] = "0" * 64
                self.reader.after_read = mutate
                with self.assertRaises(FactCorrectionBlocked):
                    self.store.verify_for_fact_review(**args)
                self.assertEqual(self.store.inserts, 1)


    def _decision_args(self):
        receipt=self.store.save(**self.args)
        self.store.version=12
        self.store.fact=dict(candidate_id=self.args['candidate_id'],proposal_id=receipt.proposal_id)
        return dict(actor=self.args['actor'],matter_id=self.args['matter_id'],fact_id=str(uuid4()),
                    expected_matter_version=12)

    def test_decision_verifies_original_after_submission_version_advance(self):
        args=self._decision_args()
        checked=self.store.verify_for_decision(**args)
        self.assertEqual(checked.fact_id,args['fact_id'])
        self.assertEqual(checked.matter_version,12)
        self.assertEqual(self.reader.calls,2)
        self.store.assert_decision_binding(self.store,verified=checked,**args)
        self.assertEqual(self.store.inserts,1)
        self.store.saved['proposal_id']=str(uuid4())
        with self.assertRaises(FactCorrectionBlocked):
            self.store.assert_decision_binding(self.store,verified=checked,**args)

    def test_decision_refuses_revocation_or_source_change_before_write(self):
        args=self._decision_args()
        self.reader.after_read=lambda:setattr(self.store,'allowed',False)
        with self.assertRaises(FactCorrectionBlocked):self.store.verify_for_decision(**args)
        self.store.allowed=True
        self.reader.after_read=lambda:None
        self.reader.raw+=b' '
        with self.assertRaises(ValueError):self.store.verify_for_decision(**args)
        self.assertEqual(self.store.inserts,1)

    def test_ordinary_fact_needs_no_correction_reader(self):
        args=self._decision_args()
        self.store.fact=dict(candidate_id=None,proposal_id=None)
        self.assertIsNone(self.store.verify_for_decision(**args))
        self.assertEqual(self.reader.calls,1)


if __name__ == "__main__": unittest.main()
