"""Persistent encrypted Agent draft candidates and lawyer approval to Agent plan.

This store has no model, filesystem or HTTP input.  A desktop SYSTEM_WORKER
first places canonical candidate JSON in encrypted storage, then registers its
content-addressed reference here.  Lawyer approval atomically creates the one
matching Agent proposal.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .agent_draft_review import prepare_docx_review_candidate, prepare_xlsx_review_candidate
from .agent_structured_draft import parse_docx_candidate, parse_xlsx_candidate
from .case_ledger_postgres import CaseLedgerCommandReceipt, CaseLedgerPersistenceBlocked, _advisory_lock, _authorize_and_lock_matter, _finish_command, _payload_hash, _prior_receipt, _require_positive_version, _require_roles, _validate_command_identity, _validate_sha256
from .models import Actor, Role
from .skill_registry import CaseSkillRegistry, SkillMaturity, default_case_skill_registry


@dataclass(frozen=True)
class AgentDraftCandidateSpec:
    document_kind: str
    agent_id: str
    agent_version: str
    content_object_key: str
    content_sha256: str
    content_bytes: int
    rationale_hash: str


class PostgresAgentDraftCandidateStore:
    _REGISTER = frozenset({Role.SYSTEM_WORKER})
    _APPROVE = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})

    def __init__(self, dsn: str, *, artifact_reader: Callable[[str, str], bytes] | None, registry: CaseSkillRegistry | None = None) -> None:
        self._dsn = dsn
        self._reader = artifact_reader
        self._registry = registry or default_case_skill_registry()

    def stage(self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, spec: AgentDraftCandidateSpec) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._REGISTER); _require_positive_version(expected_version)
        candidate = self._validated_candidate(spec)
        payload = {"matter_id": matter_id, "expected_version": expected_version, "spec": spec.__dict__, "input_hash": candidate.input_hash, "review_hash": candidate.review_hash, "skill_id": candidate.skill_id, "tool_id": candidate.tool_id}
        return self._command(matter_id, actor, expected_version, idempotency_key, "STAGE_AGENT_DRAFT_CANDIDATE", payload, self._REGISTER, lambda c: self._insert_candidate(c, matter_id, actor, spec, candidate))

    def approve(self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, candidate_id: str, approval_hash: str) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._APPROVE); _require_positive_version(expected_version); _validate_sha256("approval_hash", approval_hash)
        payload = {"matter_id": matter_id, "expected_version": expected_version, "candidate_id": candidate_id, "approval_hash": approval_hash}
        def apply(connection):
            row = connection.execute("SELECT * FROM agent_draft_candidates WHERE candidate_id = %s AND matter_id = %s AND firm_id = %s FOR UPDATE", (candidate_id, matter_id, actor.firm_id)).fetchone()
            if row is None: raise KeyError(candidate_id)
            if row["status"] != "CANDIDATE" or row["review_hash"] != approval_hash: raise CaseLedgerPersistenceBlocked("Agent draft candidate approval must bind to its current exact review hash")
            run_id, proposal_id = str(uuid4()), str(uuid4())
            skill = self._registry.get_skill(row["skill_id"])
            if skill.maturity is not SkillMaturity.IMPLEMENTED or row["tool_id"] not in skill.allowed_tools:
                raise CaseLedgerPersistenceBlocked("reviewable Office drafting is not enabled for this desktop runtime")
            connection.execute("INSERT INTO agent_runs (run_id, firm_id, matter_id, requested_by, agent_id, agent_version, policy_manifest_hash, input_hash, input_matter_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (run_id, actor.firm_id, matter_id, actor.actor_id, row["agent_id"], row["agent_version"], _policy_hash(self._registry), row["input_hash"], expected_version))
            connection.execute("INSERT INTO agent_action_proposals (proposal_id, firm_id, matter_id, run_id, sequence, skill_id, skill_version, tool_id, approval_gate, required_scopes, input_hash, rationale_hash) VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,%s::jsonb,%s,%s)", (proposal_id, actor.firm_id, matter_id, run_id, row["skill_id"], skill.version, row["tool_id"], skill.approval_gate.value, "[\"MANAGED_DERIVATIVE_WRITE\"]", row["input_hash"], row["rationale_hash"]))
            connection.execute("UPDATE agent_draft_candidates SET status = 'APPROVED', approved_by = %s, approval_hash = %s, approved_at = now(), run_id = %s, proposal_id = %s WHERE candidate_id = %s", (actor.actor_id, approval_hash, run_id, proposal_id, candidate_id))
            return run_id, proposal_id
        return self._command(matter_id, actor, expected_version, idempotency_key, "APPROVE_AGENT_DRAFT_CANDIDATE", payload, self._APPROVE, apply)

    def _validated_candidate(self, spec: AgentDraftCandidateSpec):
        if spec.document_kind not in {"DOCX", "XLSX"} or not spec.agent_id.strip() or not spec.agent_version.strip() or not 0 < spec.content_bytes <= 2 * 1024 * 1024: raise CaseLedgerPersistenceBlocked("Agent draft candidate metadata is invalid")
        _validate_sha256("content_sha256", spec.content_sha256); _validate_sha256("rationale_hash", spec.rationale_hash)
        if self._reader is None:
            raise CaseLedgerPersistenceBlocked("Agent draft candidate storage requires encrypted artifact authentication")
        raw = self._reader(spec.content_object_key, spec.content_sha256)
        if len(raw) != spec.content_bytes or sha256(raw).hexdigest() != spec.content_sha256: raise CaseLedgerPersistenceBlocked("encrypted Agent draft candidate object failed authentication")
        return prepare_docx_review_candidate(parse_docx_candidate(raw)) if spec.document_kind == "DOCX" else prepare_xlsx_review_candidate(parse_xlsx_candidate(raw))

    def _insert_candidate(self, c, matter_id, actor, spec, candidate):
        c.execute("INSERT INTO agent_draft_candidates (candidate_id, firm_id, matter_id, document_kind, agent_id, agent_version, skill_id, tool_id, content_object_key, content_sha256, content_bytes, input_hash, review_hash, rationale_hash, status, registered_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CANDIDATE',%s)", (str(uuid4()), actor.firm_id, matter_id, spec.document_kind, spec.agent_id.strip(), spec.agent_version.strip(), candidate.skill_id, candidate.tool_id, spec.content_object_key, spec.content_sha256, spec.content_bytes, candidate.input_hash, candidate.review_hash, spec.rationale_hash, actor.actor_id))
        return None

    def _command(self, matter_id, actor, version, key, name, payload, roles, apply):
        digest = _payload_hash(payload)
        with self._transaction(actor.firm_id) as c:
            _advisory_lock(c, actor=actor, matter_id=matter_id, command_name=name, idempotency_key=key)
            prior = _prior_receipt(c, actor=actor, matter_id=matter_id, command_name=name, idempotency_key=key, payload_hash=digest)
            if prior is not None: return prior
            _authorize_and_lock_matter(c, actor=actor, matter_id=matter_id, expected_version=version, allowed_roles=roles)
            result = apply(c)
            object_id = result[0] if result else str(uuid4())
            return _finish_command(c, actor=actor, matter_id=matter_id, expected_version=version, command_name=name, idempotency_key=key, payload_hash=digest, event_type=name, object_type="AGENT_DRAFT_CANDIDATE", object_id=object_id, audit_payload={"candidate_hash": payload.get("review_hash", payload.get("approval_hash")), "result": result}, stale_submission=False, stale_calculations=False)

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,)); yield connection


def _policy_hash(registry: CaseSkillRegistry) -> str:
    return _payload_hash({"registry": [(s.skill_id, s.version, s.maturity.value) for s in registry.list_skills()]})
