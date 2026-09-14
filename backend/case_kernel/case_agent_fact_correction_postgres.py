"""Private fact-correction proposals; no fact approval or Agent dispatch.

Not wired until a production source-reverification reader is supplied. The
reader must re-read the private artifact and its current original-page links;
returning browser-supplied bytes or a cached model response is not sufficient.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .case_agent_ledger_extraction import build_fact_correction_proposal
from .case_agent_ledger_extraction_postgres import PostgresCaseLedgerExtractionPromotionStore
from .case_ledger_postgres import (
    CaseLedgerCommandReceipt, _advisory_lock, _prior_receipt, _finish_command, _evidence_payload,
)
from .models import Actor, Role


class FactCorrectionBlocked(ValueError):
    pass


class FactCorrectionOriginalReader(Protocol):
    def read_verified_original(self, *, actor: Actor, matter_id: str,
                              artifact_id: str, expected_matter_version: int) -> bytes: ...


@dataclass(frozen=True)
class FactCorrectionReceipt:
    proposal_id: str
    revision_number: int
    source_matter_version: int
    review_status: str = "NEEDS_LAWYER_REVIEW"


@dataclass(frozen=True)
class VerifiedFactCorrection:
    """Short-lived source check, not an approval or a reusable write capability.

    A consuming command must lock the matter and recheck role, version and
    proposal head in its own write transaction before creating a linked fact.
    """
    proposal_id: str
    candidate_id: str
    matter_version: int
    revision_number: int
    proposal_content: bytes
    fact_id: str | None = None


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise FactCorrectionBlocked("invalid correction identity")
    return value


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _receipt(row):
    return FactCorrectionReceipt(str(row["proposal_id"]), row["revision_number"],
                                 row["expected_matter_version"])


class PostgresFactCorrectionProposalStore:
    def __init__(self, dsn: str, *, original_reader: FactCorrectionOriginalReader):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("correction PostgreSQL DSN required")
        if not callable(getattr(original_reader, "read_verified_original", None)):
            raise ValueError("correction requires independent original/source re-reading")
        self._dsn, self._reader = dsn, original_reader

    def is_available(self) -> bool:
        """Do not advertise saving before the append-only deployment is ready."""
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=3) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout='3s'")
                row = connection.execute("""
                    SELECT c.relrowsecurity AND c.relforcerowsecurity
                        AND has_table_privilege(current_user,c.oid,'SELECT')
                        AND has_table_privilege(current_user,c.oid,'INSERT')
                        AND NOT has_table_privilege(current_user,c.oid,'UPDATE')
                        AND NOT has_table_privilege(current_user,c.oid,'DELETE')
                        AND EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid
                            AND t.tgname='fact_correction_proposal_guard' AND t.tgenabled='O') AS ready
                    FROM pg_class c WHERE c.oid=to_regclass('public.case_agent_fact_correction_proposals')
                    """).fetchone()
                return bool(row and row["ready"])
        except psycopg.Error:
            return False

    def is_submission_available(self) -> bool:
        """Keep the HTTP write closed until the lineage migration is installed."""
        if not self.is_available():
            return False
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=3) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL statement_timeout='3s'")
                row = connection.execute("""
                    SELECT
                      (SELECT count(*) FROM pg_constraint WHERE conrelid='case_facts'::regclass
                        AND convalidated AND conname IN ('fact_correction_pair','fact_correction_source',
                          'fact_correction_unique_proposal','fact_correction_unique_candidate')) = 4
                      AND EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='case_facts'::regclass
                        AND tgname='fact_correction_candidate_lineage_guard' AND tgenabled='O')
                      AND EXISTS (SELECT 1 FROM pg_trigger
                        WHERE tgrelid='case_agent_ledger_extraction_promotions'::regclass
                          AND tgname='extraction_promotion_correction_guard' AND tgenabled='O') AS ready
                    """).fetchone()
                return bool(row and row["ready"])
        except psycopg.Error:
            return False

    @contextmanager
    def _transaction(self, actor, *, read_only=False):
        if Role.SYSTEM_WORKER in actor.roles or not actor.roles.intersection({Role.LEAD_LAWYER, Role.REVIEWER}):
            raise FactCorrectionBlocked("lawyer role required")
        _uuid(actor.actor_id); _uuid(actor.firm_id)
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            if read_only:
                connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL statement_timeout = '10s'")
            connection.execute("SELECT set_config('app.firm_id',%s,true)", (actor.firm_id,))
            connection.execute("SELECT set_config('app.actor_id',%s,true)", (actor.actor_id,))
            yield connection

    def _authorize(self, connection, actor, matter_id, *, lock=False):
        row = connection.execute("""
            SELECT m.version FROM matters m WHERE m.matter_id=%s AND m.firm_id=%s
            AND EXISTS (SELECT 1 FROM users u JOIN matter_actor_roles a
              ON a.user_id=u.user_id AND a.firm_id=u.firm_id
              WHERE u.user_id=%s AND u.firm_id=m.firm_id AND u.status='ACTIVE'
                AND a.matter_id=m.matter_id AND a.revoked_at IS NULL
                AND a.role IN ('LEAD_LAWYER','REVIEWER'))
            """ + (" FOR UPDATE OF m" if lock else ""),
            (matter_id, actor.firm_id, actor.actor_id)).fetchone()
        if row is None:
            raise FactCorrectionBlocked("current matter lawyer access required")
        return row["version"]

    def _prior(self, connection, actor, key_hash):
        return connection.execute("""
            SELECT proposal_id, matter_id, request_hash, revision_number, expected_matter_version
            FROM case_agent_fact_correction_proposals
            WHERE firm_id=%s AND requested_by=%s AND idempotency_key_hash=%s
            """, (actor.firm_id, actor.actor_id, key_hash)).fetchone()

    def _binding(self, connection, actor, matter_id, candidate_id):
        row = connection.execute("""
            SELECT c.candidate_hash,b.artifact_id,b.artifact_content_sha256,b.source_hash
            FROM case_agent_ledger_extraction_candidates c
            JOIN case_agent_ledger_extraction_batches b
              ON b.extraction_batch_id=c.extraction_batch_id AND b.firm_id=c.firm_id AND b.matter_id=c.matter_id
            WHERE c.extraction_candidate_id=%s AND c.firm_id=%s AND c.matter_id=%s AND c.candidate_kind='FACT'
            """, (candidate_id, actor.firm_id, matter_id)).fetchone()
        if row is None:
            raise FactCorrectionBlocked("exact original fact candidate not visible")
        return row

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 200 or value != value.strip():
            raise FactCorrectionBlocked("stable request key required")
        return sha256(value.encode()).hexdigest()

    def find_by_key(self, *, actor: Actor, matter_id: str, idempotency_key: str):
        _uuid(matter_id)
        key_hash = self._key(idempotency_key)
        with self._transaction(actor, read_only=True) as connection:
            self._authorize(connection, actor, matter_id)
            prior = self._prior(connection, actor, key_hash)
            return _receipt(prior) if prior and str(prior["matter_id"]) == matter_id else None

    def find_submission_by_key(self, *, actor: Actor, matter_id: str,
                               idempotency_key: str) -> CaseLedgerCommandReceipt | None:
        """A missing receipt is unknown, not permission to send a new command."""
        _uuid(matter_id)
        self._key(idempotency_key)
        with self._transaction(actor, read_only=True) as connection:
            self._authorize(connection, actor, matter_id)
            row = connection.execute("""
                SELECT response_json FROM command_idempotency
                WHERE firm_id=%s AND matter_id=%s AND actor_id=%s
                  AND command_name='CREATE_FACT_CANDIDATE_FROM_CORRECTION' AND idempotency_key=%s
                """, (actor.firm_id,matter_id,actor.actor_id,idempotency_key)).fetchone()
            return CaseLedgerCommandReceipt(**row["response_json"]) if row else None

    def read_submission(self, *, actor: Actor, matter_id: str, candidate_id: str):
        """Read the actual linked fact, independent of this browser's request key."""
        _uuid(matter_id); _uuid(candidate_id)
        with self._transaction(actor, read_only=True) as connection:
            self._authorize(connection, actor, matter_id)
            self._binding(connection, actor, matter_id, candidate_id)
            row = connection.execute("""
                SELECT m.version,f.fact_id,f.status,f.correction_proposal_id
                FROM matters m LEFT JOIN case_facts f
                  ON f.matter_id=m.matter_id AND f.firm_id=m.firm_id
                  AND f.correction_candidate_id=%s
                WHERE m.matter_id=%s AND m.firm_id=%s
                """, (candidate_id,matter_id,actor.firm_id)).fetchone()
            if row is None:
                raise FactCorrectionBlocked("current matter not visible")
            submission = None if row["fact_id"] is None else dict(
                fact_id=str(row["fact_id"]),status=row["status"],
                proposal_id=str(row["correction_proposal_id"]))
            return dict(current_matter_version=row["version"],submission=submission)

    def read_current(self, *, actor: Actor, matter_id: str, candidate_id: str):
        return self.read_context(actor=actor,matter_id=matter_id,candidate_id=candidate_id)["draft"]

    def _decision_snapshot(self, connection, *, actor, matter_id, fact_id, expected_matter_version):
        version = self._authorize(connection, actor, matter_id)
        if version != expected_matter_version:
            raise FactCorrectionBlocked("matter version changed before decision")
        fact = connection.execute("""
            SELECT to_jsonb(case_facts)->>'correction_candidate_id' AS candidate_id,
                   to_jsonb(case_facts)->>'correction_proposal_id' AS proposal_id
            FROM case_facts WHERE fact_id=%s AND matter_id=%s AND firm_id=%s
            """, (fact_id,matter_id,actor.firm_id)).fetchone()
        if fact is None:
            raise FactCorrectionBlocked("fact not visible")
        if fact["proposal_id"] is None:
            return None
        head = connection.execute("""
            SELECT proposal_id,revision_number,proposal_content FROM case_agent_fact_correction_proposals
            WHERE extraction_candidate_id=%s AND matter_id=%s AND firm_id=%s
            ORDER BY revision_number DESC LIMIT 1
            """, (fact["candidate_id"],matter_id,actor.firm_id)).fetchone()
        if head is None or str(head["proposal_id"]) != fact["proposal_id"]:
            raise FactCorrectionBlocked("submitted correction is no longer the latest proposal")
        return VerifiedFactCorrection(str(head["proposal_id"]),fact["candidate_id"],version,
            head["revision_number"],bytes(head["proposal_content"]),fact_id)

    def verify_for_decision(self, *, actor: Actor, matter_id: str, fact_id: str,
                            expected_matter_version: int) -> VerifiedFactCorrection | None:
        """Recheck submitted sources against today's case, not the save version.

        Submission itself advances the version, so reusing the pre-submission
        verifier would permanently block the first real lawyer decision.
        """
        _uuid(matter_id); _uuid(fact_id)
        if type(expected_matter_version) is not int or expected_matter_version < 1:
            raise FactCorrectionBlocked("positive matter version required")
        args=dict(actor=actor,matter_id=matter_id,fact_id=fact_id,
                  expected_matter_version=expected_matter_version)
        with self._transaction(actor,read_only=True) as connection:
            verified=self._decision_snapshot(connection,**args)
            if verified is None:
                return None
            binding=self._binding(connection,actor,matter_id,verified.candidate_id)
        content=json.loads(verified.proposal_content)
        raw=self._reader.read_verified_original(actor=actor,matter_id=matter_id,
            artifact_id=str(binding['artifact_id']),expected_matter_version=expected_matter_version)
        rebuilt=build_fact_correction_proposal(raw,expected_artifact_hash=binding['artifact_content_sha256'],
            candidate_hash=binding['candidate_hash'],revised_text=content['revised_text'],reason=content['reason'])
        if rebuilt != verified.proposal_content or content['source_hash'] != binding['source_hash']:
            raise FactCorrectionBlocked("decision original source binding differs")
        with self._transaction(actor,read_only=True) as connection:
            self.assert_decision_binding(connection,verified=verified,**args)
            if self._binding(connection,actor,matter_id,verified.candidate_id) != binding:
                raise FactCorrectionBlocked("decision source changed during verification")
        return verified

    def assert_decision_binding(self, connection, *, actor: Actor, matter_id: str, fact_id: str,
                                expected_matter_version: int, verified: object) -> None:
        # Called again while the ledger holds the matter lock. New proposals
        # also acquire that lock, so the proposal head cannot race this write.
        if (not isinstance(verified,VerifiedFactCorrection) or verified.fact_id != fact_id
                or self._decision_snapshot(connection,actor=actor,matter_id=matter_id,fact_id=fact_id,
                    expected_matter_version=expected_matter_version) != verified):
            raise FactCorrectionBlocked("verified correction changed before decision commit")

    def submit_for_fact_review(self, *, actor: Actor, matter_id: str,
                               candidate_id: str, proposal_id: str,
                               expected_matter_version: int, idempotency_key: str):
        """Atomically create a linked CANDIDATE and the existing ledger receipt.

        Requires migration 0082. This is not an approval and does not close an
        extraction follow-up. No browser-supplied source or text is accepted.
        """
        _uuid(matter_id); _uuid(candidate_id); _uuid(proposal_id)
        self._key(idempotency_key)
        if type(expected_matter_version) is not int or expected_matter_version < 1:
            raise FactCorrectionBlocked("positive matter version required")
        command_name = "CREATE_FACT_CANDIDATE_FROM_CORRECTION"
        payload_hash = _hash(dict(command=command_name, matter_id=matter_id,
            candidate_id=candidate_id, proposal_id=proposal_id,
            expected_matter_version=expected_matter_version))
        receipt_args = dict(actor=actor, matter_id=matter_id, command_name=command_name,
            idempotency_key=idempotency_key, payload_hash=payload_hash)
        with self._transaction(actor, read_only=True) as connection:
            self._authorize(connection, actor, matter_id)
            prior = _prior_receipt(connection, **receipt_args)
            if prior is not None:
                return prior
        verified = self.verify_for_fact_review(actor=actor, matter_id=matter_id,
            candidate_id=candidate_id, proposal_id=proposal_id,
            expected_matter_version=expected_matter_version)
        content = json.loads(verified.proposal_content)
        with self._transaction(actor) as connection:
            _advisory_lock(connection, **{k:v for k,v in receipt_args.items() if k != "payload_hash"})
            version = self._authorize(connection, actor, matter_id, lock=True)
            prior = _prior_receipt(connection, **receipt_args)
            if prior is not None:
                return prior
            if version != verified.matter_version:
                raise FactCorrectionBlocked("matter version changed before fact submission")
            head = connection.execute("""
                SELECT proposal_id, proposal_content FROM case_agent_fact_correction_proposals
                WHERE extraction_candidate_id=%s AND firm_id=%s AND matter_id=%s
                ORDER BY revision_number DESC LIMIT 1
                """, (candidate_id, actor.firm_id, matter_id)).fetchone()
            if (head is None or str(head["proposal_id"]) != proposal_id
                    or bytes(head["proposal_content"]) != verified.proposal_content):
                raise FactCorrectionBlocked("latest saved correction changed before submission")
            binding = self._binding(connection, actor, matter_id, candidate_id)
            if (binding["artifact_content_sha256"] != content["original_artifact_hash"]
                    or binding["source_hash"] != content["source_hash"]
                    or binding["candidate_hash"] != content["original_candidate"]["candidate_hash"]):
                raise FactCorrectionBlocked("original correction binding changed")
            links, _ = PostgresCaseLedgerExtractionPromotionStore._read_source_links(
                connection, actor=actor, matter_id=matter_id, lock_rows=False,
                candidates=[dict(extraction_candidate_id=candidate_id,
                    candidate_payload=content["original_candidate"])])
            fact_id = str(uuid4())
            connection.execute("""
                INSERT INTO case_facts (fact_id,firm_id,matter_id,original_text,origin,status,
                    evidence_links,correction_proposal_id,correction_candidate_id)
                VALUES (%s,%s,%s,%s,'ASSISTANT_ENTRY','CANDIDATE',%s,%s,%s)
                """, (fact_id,actor.firm_id,matter_id,content["revised_text"],
                    Jsonb(_evidence_payload(links[candidate_id])),proposal_id,candidate_id))
            return _finish_command(connection, **receipt_args, expected_version=version,
                event_type="FACT_CANDIDATE_CREATED", object_type="FACT", object_id=fact_id,
                audit_payload=dict(fact_id=fact_id,status="CANDIDATE",correction_proposal_id=proposal_id,
                    extraction_candidate_id=candidate_id,
                    correction_content_sha256=sha256(verified.proposal_content).hexdigest()),
                stale_submission=False)

    def verify_for_fact_review(self, *, actor: Actor, matter_id: str,
                               candidate_id: str, proposal_id: str,
                               expected_matter_version: int) -> VerifiedFactCorrection:
        """Re-read sources for the exact current saved proposal without writing.

        Draft recovery intentionally does not attest source integrity. This
        boundary does, and rejects replacement/revocation during slow object
        reads. It must not be exposed as an approval endpoint.
        """
        _uuid(matter_id); _uuid(candidate_id); _uuid(proposal_id)
        if type(expected_matter_version) is not int or expected_matter_version < 1:
            raise FactCorrectionBlocked("positive matter version required")

        def snapshot():
            with self._transaction(actor, read_only=True) as connection:
                version = self._authorize(connection, actor, matter_id)
                if version != expected_matter_version:
                    raise FactCorrectionBlocked("matter version changed")
                binding = self._binding(connection, actor, matter_id, candidate_id)
                row = connection.execute("""
                    SELECT proposal_id, revision_number, expected_matter_version,
                           proposal_content, requested_by, created_at
                    FROM case_agent_fact_correction_proposals
                    WHERE extraction_candidate_id=%s AND firm_id=%s AND matter_id=%s
                    ORDER BY revision_number DESC LIMIT 1
                    """, (candidate_id, actor.firm_id, matter_id)).fetchone()
                if row is None or str(row["proposal_id"]) != proposal_id:
                    raise FactCorrectionBlocked("latest saved correction required")
                if row["expected_matter_version"] != version:
                    raise FactCorrectionBlocked("saved correction is stale")
                return binding, VerifiedFactCorrection(proposal_id, candidate_id,
                    version, row["revision_number"], bytes(row["proposal_content"]))

        binding, verified = snapshot()
        try:
            content = json.loads(verified.proposal_content)
            revised_text, reason = content["revised_text"], content["reason"]
        except (ValueError, TypeError, KeyError) as error:
            raise FactCorrectionBlocked("saved correction content invalid") from error
        raw = self._reader.read_verified_original(actor=actor, matter_id=matter_id,
            artifact_id=str(binding["artifact_id"]),
            expected_matter_version=expected_matter_version)
        rebuilt = build_fact_correction_proposal(raw,
            expected_artifact_hash=binding["artifact_content_sha256"],
            candidate_hash=binding["candidate_hash"], revised_text=revised_text, reason=reason)
        if (rebuilt != verified.proposal_content
                or content.get("source_hash") != binding["source_hash"]):
            raise FactCorrectionBlocked("saved correction does not match verified original")
        if snapshot() != (binding, verified):
            raise FactCorrectionBlocked("correction sources changed during review")
        return verified

    def read_context(self, *, actor: Actor, matter_id: str, candidate_id: str):
        """Recover the latest saved draft, never an approval or source attestation.

        Source bytes need not be downloaded to recover a draft. Approval must
        independently revalidate them; even a same-version draft remains pending.
        """
        _uuid(matter_id); _uuid(candidate_id)
        with self._transaction(actor, read_only=True) as connection:
            version = self._authorize(connection, actor, matter_id)
            self._binding(connection, actor, matter_id, candidate_id)
            row = connection.execute("""
                SELECT proposal_id, revision_number, expected_matter_version,
                       proposal_content, requested_by, created_at
                FROM case_agent_fact_correction_proposals
                WHERE extraction_candidate_id=%s AND firm_id=%s AND matter_id=%s
                ORDER BY revision_number DESC LIMIT 1
                """, (candidate_id, actor.firm_id, matter_id)).fetchone()
            if row is None:
                return dict(current_matter_version=version,draft=None)
            draft = dict(schema_version="fact-correction-draft-review-v1",
                proposal_id=str(row["proposal_id"]), candidate_id=candidate_id,
                revision_number=row["revision_number"],
                source_matter_version=row["expected_matter_version"],
                current_matter_version=version,
                stale=row["expected_matter_version"] != version,
                review_status="NEEDS_LAWYER_REVIEW", court_ready=False,
                requested_by=str(row["requested_by"]), created_at=row["created_at"].isoformat(),
                proposal=json.loads(bytes(row["proposal_content"])))
            return dict(current_matter_version=version,draft=draft)

    def save(self, *, actor: Actor, matter_id: str, candidate_id: str,
             expected_matter_version: int, expected_revision: int,
             idempotency_key: str, revised_text: str, reason: str) -> FactCorrectionReceipt:
        _uuid(matter_id); _uuid(candidate_id)
        if type(expected_matter_version) is not int or expected_matter_version < 1:
            raise FactCorrectionBlocked("positive matter version required")
        if type(expected_revision) is not int or not 0 <= expected_revision < 999:
            raise FactCorrectionBlocked("bounded predecessor revision required")
        if not isinstance(revised_text, str) or not 1 <= len(revised_text.strip()) <= 4000:
            raise FactCorrectionBlocked("bounded revised text required")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise FactCorrectionBlocked("bounded correction reason required")
        key_hash = self._key(idempotency_key)
        request_hash = _hash(dict(schema="save-fact-correction-v1", matter_id=matter_id,
            candidate_id=candidate_id, expected_matter_version=expected_matter_version,
            expected_revision=expected_revision, revised_text=revised_text.strip(), reason=reason.strip()))

        def replay(row):
            if str(row["matter_id"]) != matter_id or row["request_hash"] != request_hash:
                raise FactCorrectionBlocked("request key was used for a different correction")
            return _receipt(row)

        with self._transaction(actor, read_only=True) as connection:
            version = self._authorize(connection, actor, matter_id)
            prior = self._prior(connection, actor, key_hash)
            if prior:
                return replay(prior)
            if version != expected_matter_version:
                raise FactCorrectionBlocked("matter version changed")
            binding = self._binding(connection, actor, matter_id, candidate_id)
        # Never hold a database lock while re-reading private source objects.
        raw = self._reader.read_verified_original(actor=actor, matter_id=matter_id,
            artifact_id=str(binding["artifact_id"]), expected_matter_version=expected_matter_version)
        proposal = build_fact_correction_proposal(raw,
            expected_artifact_hash=binding["artifact_content_sha256"],
            candidate_hash=binding["candidate_hash"], revised_text=revised_text, reason=reason)
        with self._transaction(actor) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"fact-correction:{actor.firm_id}:{actor.actor_id}:{key_hash}",))
            version = self._authorize(connection, actor, matter_id, lock=True)
            prior = self._prior(connection, actor, key_hash)
            if prior:
                return replay(prior)
            if version != expected_matter_version or self._binding(connection, actor, matter_id, candidate_id) != binding:
                raise FactCorrectionBlocked("correction sources or matter changed during review")
            head = connection.execute("""
                SELECT proposal_id,revision_number FROM case_agent_fact_correction_proposals
                WHERE extraction_candidate_id=%s AND firm_id=%s AND matter_id=%s
                ORDER BY revision_number DESC LIMIT 1
                """, (candidate_id, actor.firm_id, matter_id)).fetchone()
            if (head["revision_number"] if head else 0) != expected_revision:
                raise FactCorrectionBlocked("another correction revision was saved")
            identifier = str(uuid4())
            connection.execute("""
                INSERT INTO case_agent_fact_correction_proposals
                (proposal_id,firm_id,matter_id,extraction_candidate_id,expected_matter_version,
                 revision_number,predecessor_proposal_id,requested_by,idempotency_key_hash,request_hash,proposal_content)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (identifier,actor.firm_id,matter_id,candidate_id,expected_matter_version,
                    expected_revision+1,head["proposal_id"] if head else None,actor.actor_id,key_hash,request_hash,proposal))
            return FactCorrectionReceipt(identifier, expected_revision+1, expected_matter_version)
