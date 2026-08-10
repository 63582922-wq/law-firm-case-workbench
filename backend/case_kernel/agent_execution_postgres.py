"""Append-only persistence for Agent plans and bounded Tool execution receipts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _require_text,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
)
from .models import Actor, Role
from .skill_registry import CaseSkillRegistry, SkillMaturity, default_case_skill_registry


@dataclass(frozen=True)
class AgentToolProposal:
    sequence: int
    skill_id: str
    tool_id: str
    input_hash: str
    rationale_hash: str


@dataclass(frozen=True)
class PersistentAgentExecutionSnapshot:
    matter_id: str
    matter_version: int
    runs: tuple[dict[str, Any], ...]
    proposals: tuple[dict[str, Any], ...]
    receipts: tuple[dict[str, Any], ...]
    snapshot_hash: str


class PostgresAgentExecutionStore:
    """Records a model's bounded plan without granting it execution authority."""

    _PLAN_ROLES = frozenset({Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER})
    _EXECUTOR_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = _PLAN_ROLES

    def __init__(self, dsn: str, *, registry: CaseSkillRegistry | None = None) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn
        self._registry = registry or default_case_skill_registry()

    def plan_agent_run(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        agent_id: str,
        agent_version: str,
        policy_manifest_hash: str,
        input_hash: str,
        proposals: tuple[AgentToolProposal, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._PLAN_ROLES)
        _require_positive_version(expected_version)
        _require_text(agent_id, "agent_id")
        _require_text(agent_version, "agent_version")
        _validate_sha256("policy_manifest_hash", policy_manifest_hash)
        _validate_sha256("input_hash", input_hash)
        validated = self._validate_proposals(proposals)
        command_name = "PLAN_AGENT_RUN"
        payload = {
            "matter_id": matter_id, "expected_version": expected_version,
            "agent_id": agent_id.strip(), "agent_version": agent_version.strip(),
            "policy_manifest_hash": policy_manifest_hash, "input_hash": input_hash,
            "proposals": [item.__dict__ for item in validated],
        }
        request_hash = _payload_hash(payload)
        run_id = str(uuid4())
        proposal_ids = [str(uuid4()) for _ in validated]
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                idempotency_key=idempotency_key, command_name=command_name, payload_hash=request_hash,
                allowed_roles=self._PLAN_ROLES,
            )
            if prior is not None:
                return prior
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, firm_id, matter_id, requested_by, agent_id, agent_version,
                    policy_manifest_hash, input_hash, input_matter_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, actor.firm_id, matter_id, actor.actor_id, agent_id.strip(),
                 agent_version.strip(), policy_manifest_hash, input_hash, expected_version),
            )
            for proposal_id, proposal, skill in zip(proposal_ids, validated, self._proposal_skills(validated), strict=True):
                connection.execute(
                    """
                    INSERT INTO agent_action_proposals (
                        proposal_id, firm_id, matter_id, run_id, sequence, skill_id,
                        skill_version, tool_id, approval_gate, required_scopes, input_hash,
                        rationale_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (proposal_id, actor.firm_id, matter_id, run_id, proposal.sequence,
                     proposal.skill_id, skill.version, proposal.tool_id, skill.approval_gate.value,
                     _json_scopes(skill), proposal.input_hash, proposal.rationale_hash),
                )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="AGENT_RUN_PLANNED", object_type="AGENT_RUN", object_id=run_id,
                audit_payload={"run_id": run_id, "agent_id": agent_id.strip(),
                               "agent_version": agent_version.strip(), "input_hash": input_hash,
                               "policy_manifest_hash": policy_manifest_hash,
                               "proposal_count": len(validated)},
                stale_submission=False, stale_calculations=False,
            )

    def record_tool_execution_receipt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        proposal_id: str,
        status: str,
        output_hash: str | None = None,
        error_code: str | None = None,
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._EXECUTOR_ROLES)
        _require_positive_version(expected_version)
        _validate_uuid_text("proposal_id", proposal_id)
        if status not in {"SUCCEEDED", "BLOCKED", "FAILED", "STALE_RESULT"}:
            raise CaseLedgerPersistenceBlocked("Agent Tool receipt status is invalid")
        if status == "SUCCEEDED":
            if output_hash is None or error_code is not None:
                raise CaseLedgerPersistenceBlocked("successful Agent Tool receipt requires only output hash")
            _validate_sha256("output_hash", output_hash)
        elif output_hash is not None or not isinstance(error_code, str) or not error_code.strip():
            raise CaseLedgerPersistenceBlocked("unsuccessful Agent Tool receipt requires only an error code")
        command_name = "RECORD_AGENT_TOOL_EXECUTION"
        payload = {"matter_id": matter_id, "expected_version": expected_version,
                   "proposal_id": proposal_id, "status": status, "output_hash": output_hash,
                   "error_code": error_code.strip() if isinstance(error_code, str) else None}
        request_hash = _payload_hash(payload)
        receipt_id = str(uuid4())
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                idempotency_key=idempotency_key, command_name=command_name, payload_hash=request_hash,
                allowed_roles=self._EXECUTOR_ROLES,
            )
            if prior is not None:
                return prior
            proposal = connection.execute(
                """
                SELECT proposal_id, skill_id, tool_id, input_hash
                FROM agent_action_proposals
                WHERE proposal_id = %s AND matter_id = %s AND firm_id = %s
                FOR KEY SHARE
                """, (proposal_id, matter_id, actor.firm_id),
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            connection.execute(
                """
                INSERT INTO agent_tool_execution_receipts (
                    receipt_id, firm_id, matter_id, proposal_id, executor_id, status, output_hash, error_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (receipt_id, actor.firm_id, matter_id, proposal_id, actor.actor_id,
                 status, output_hash, error_code.strip() if isinstance(error_code, str) else None),
            )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=request_hash,
                event_type="AGENT_TOOL_EXECUTION_RECORDED", object_type="AGENT_TOOL_EXECUTION_RECEIPT",
                object_id=receipt_id,
                audit_payload={"receipt_id": receipt_id, "proposal_id": proposal_id,
                               "skill_id": proposal["skill_id"], "tool_id": proposal["tool_id"],
                               "status": status, "output_hash": output_hash,
                               "error_code": error_code.strip() if isinstance(error_code, str) else None},
                stale_submission=False, stale_calculations=False,
            )

    def get_snapshot(self, *, matter_id: str, actor: Actor) -> PersistentAgentExecutionSnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES)
            matter = connection.execute(
                "SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            runs = _rows(connection.execute(
                "SELECT run_id, agent_id, agent_version, policy_manifest_hash, input_hash, input_matter_version, created_at FROM agent_runs WHERE matter_id = %s AND firm_id = %s ORDER BY created_at DESC, run_id DESC",
                (matter_id, actor.firm_id),
            ).fetchall())
            proposals = _rows(connection.execute(
                "SELECT proposal_id, run_id, sequence, skill_id, skill_version, tool_id, approval_gate, required_scopes, input_hash, rationale_hash, created_at FROM agent_action_proposals WHERE matter_id = %s AND firm_id = %s ORDER BY run_id DESC, sequence ASC",
                (matter_id, actor.firm_id),
            ).fetchall())
            receipts = _rows(connection.execute(
                "SELECT receipt_id, proposal_id, status, output_hash, error_code, executed_at FROM agent_tool_execution_receipts WHERE matter_id = %s AND firm_id = %s ORDER BY executed_at DESC, receipt_id DESC",
                (matter_id, actor.firm_id),
            ).fetchall())
        payload = {"matter_id": matter_id, "matter_version": matter["version"], "runs": runs, "proposals": proposals, "receipts": receipts}
        return PersistentAgentExecutionSnapshot(matter_id, matter["version"], tuple(runs), tuple(proposals), tuple(receipts), _payload_hash(payload))

    def _validate_proposals(self, proposals: tuple[AgentToolProposal, ...]) -> tuple[AgentToolProposal, ...]:
        if not 1 <= len(proposals) <= 100:
            raise CaseLedgerPersistenceBlocked("Agent run must contain 1 to 100 Tool proposals")
        sequences: set[int] = set()
        for proposal in proposals:
            if not isinstance(proposal, AgentToolProposal) or not isinstance(proposal.sequence, int) or proposal.sequence < 1:
                raise CaseLedgerPersistenceBlocked("Agent Tool proposal sequence is invalid")
            if proposal.sequence in sequences:
                raise CaseLedgerPersistenceBlocked("Agent Tool proposal sequences must be unique")
            sequences.add(proposal.sequence)
            _validate_sha256("proposal input_hash", proposal.input_hash)
            _validate_sha256("proposal rationale_hash", proposal.rationale_hash)
            skill = self._registry.get_skill(proposal.skill_id)
            if skill.maturity is not SkillMaturity.IMPLEMENTED or proposal.tool_id not in skill.allowed_tools:
                raise CaseLedgerPersistenceBlocked("Agent Tool proposal is not an enabled registered Skill Tool")
        if sequences != set(range(1, len(proposals) + 1)):
            raise CaseLedgerPersistenceBlocked("Agent Tool proposal sequences must be contiguous")
        return proposals

    def _proposal_skills(self, proposals: tuple[AgentToolProposal, ...]):
        return tuple(self._registry.get_skill(item.skill_id) for item in proposals)

    def _begin(self, connection, *, actor: Actor, matter_id: str, expected_version: int, idempotency_key: str, command_name: str, payload_hash: str, allowed_roles: frozenset[Role]) -> CaseLedgerCommandReceipt | None:
        _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key)
        prior = _prior_receipt(connection, actor=actor, matter_id=matter_id, command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash)
        if prior is not None:
            return prior
        _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id, expected_version=expected_version, allowed_roles=allowed_roles)
        return None

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _json_scopes(skill) -> str:
    import json
    return json.dumps(sorted(scope.value for scope in skill.required_scopes), separators=(",", ":"))


def _validate_uuid_text(label: str, value: str) -> None:
    from uuid import UUID
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise CaseLedgerPersistenceBlocked(f"{label} must be a UUID") from error


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append({key: value.isoformat() if hasattr(value, "isoformat") else str(value) if key.endswith("_id") and value is not None else value for key, value in row.items()})
    return result
