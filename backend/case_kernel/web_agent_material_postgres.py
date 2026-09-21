"""PostgreSQL persistence for the recoverable Web material-review Agent.

This store deliberately has its own run-version and idempotency ledger.  Agent
progress is operational state: it does not silently advance the formal matter
version or turn a model candidate into an approved case fact.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from typing import Any, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Actor, Role
from .web_agent_material_review import (
    AgentCandidateStatus,
    AgentEvidencePageBinding,
    AgentFailureCode,
    AgentIntent,
    AgentMaterialReviewBlocked,
    AgentMaterialRunSnapshot,
    AgentMaterialTask,
    AgentPageCandidate,
    AgentPlanContext,
    AgentRunStatus,
    AgentTaskKind,
    AgentTaskStatus,
    CandidateReasonCode,
    PageCandidateKind,
    ReviewPriority,
    _binding_payload,
    _canonical_hash,
    _fixed_task_plan,
    _plan_context_payload,
    _require_idempotency_key,
    _require_positive_int,
    _require_run_version,
    _require_sha256,
    _validate_page_bindings,
    _validate_persisted_candidates,
    _validate_plan_context,
    agent_candidate_output_hash,
)


class PostgresAgentMaterialRunStore:
    """Tenant-scoped Agent run state with exact authorization and leases."""

    _QUEUE_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )
    _BIND_ROLES = frozenset({Role.LEAD_LAWYER, Role.REVIEWER})
    _WORKER_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = _QUEUE_ROLES | _WORKER_ROLES

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def create_run(
        self,
        *,
        actor: Actor,
        matter_id: str,
        matter_version: int,
        input_hash: str,
        request_hash: str,
        page_bindings: tuple[AgentEvidencePageBinding, ...],
        plan_context: AgentPlanContext,
        idempotency_key: str,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._QUEUE_ROLES)
        self._uuid("matter_id", matter_id)
        _require_positive_int(matter_version, "matter_version")
        _require_sha256(input_hash, "input_hash")
        _require_sha256(request_hash, "request_hash")
        _require_idempotency_key(idempotency_key)
        _validate_plan_context(plan_context, require_profile=False)
        _validate_page_bindings(matter_id, matter_version, input_hash, page_bindings)
        command_name = "CREATE_AGENT_MATERIAL_RUN"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "firm_id": actor.firm_id,
                "matter_id": matter_id,
                "matter_version": matter_version,
                "input_hash": input_hash,
                "request_hash": request_hash,
                "page_bindings": [_binding_payload(value) for value in page_bindings],
                "plan_context": _plan_context_payload(plan_context),
            }
        )
        with self._transaction(actor.firm_id) as connection:
            self._advisory_lock(
                connection, actor=actor, matter_id=matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=matter_id, command_name=command_name,
                idempotency_key=idempotency_key, request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=matter_id,
                allowed_roles=self._QUEUE_ROLES, expected_matter_version=matter_version,
            )
            self._verify_page_bindings(
                connection, actor=actor, matter_id=matter_id, bindings=page_bindings,
            )
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO web_agent_material_runs (
                    run_id, firm_id, matter_id, requested_by, matter_version,
                    input_hash, plan_request_hash, agent_intent,
                    representation_profile_version, representation_profile_hash,
                    status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED')
                """,
                (
                    run_id, actor.firm_id, matter_id, actor.actor_id, matter_version,
                    input_hash, request_hash, plan_context.intent.value,
                    plan_context.representation_profile_version,
                    plan_context.representation_profile_hash,
                ),
            )
            for sequence, kind in enumerate(_fixed_task_plan(), start=1):
                connection.execute(
                    """
                    INSERT INTO web_agent_material_tasks (
                        task_id, run_id, firm_id, matter_id, sequence, task_kind, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,'QUEUED')
                    """,
                    (str(uuid4()), run_id, actor.firm_id, matter_id, sequence, kind.value),
                )
            for sequence, binding in enumerate(page_bindings, start=1):
                connection.execute(
                    """
                    INSERT INTO web_agent_material_page_bindings (
                        run_id, firm_id, matter_id, sequence, evidence_page_id,
                        source_file_sha256, page_number, extracted_text_sha256
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        run_id, actor.firm_id, matter_id, sequence,
                        binding.evidence_page_id, binding.source_file_sha256,
                        binding.page_number, binding.extracted_text_sha256,
                    ),
                )
            self._finish_command(
                connection, actor=actor, matter_id=matter_id, run_id=run_id,
                from_version=0, to_version=1, command_name=command_name,
                idempotency_key=idempotency_key, request_hash=command_hash,
                event_type="WEB_AGENT_MATERIAL_RUN_QUEUED",
                payload={
                    "input_hash": input_hash,
                    "plan_request_hash": request_hash,
                    "page_count": len(page_bindings),
                    "agent_intent": plan_context.intent.value,
                    "representation_profile_hash": plan_context.representation_profile_hash,
                },
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def bind_external_request(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        authorized_matter_version: int,
        external_request_id: str,
        idempotency_key: str,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._BIND_ROLES)
        self._uuid("run_id", run_id)
        self._uuid("external_request_id", external_request_id)
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_positive_int(authorized_matter_version, "authorized_matter_version")
        _require_idempotency_key(idempotency_key)
        command_name = "BIND_AGENT_MATERIAL_EXTERNAL_REQUEST"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "authorized_matter_version": authorized_matter_version,
                "external_request_id": external_request_id,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            run = self._lock_run(connection, actor=actor, run_id=run_id)
            self._advisory_lock(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._BIND_ROLES,
                expected_matter_version=authorized_matter_version,
            )
            _require_run_version(run, expected_run_version)
            if run.status is not AgentRunStatus.QUEUED or run.external_request_id is not None:
                raise AgentMaterialReviewBlocked(
                    "only an unbound queued Agent run can bind an external request"
                )
            _validate_plan_context(run.plan_context, require_profile=True)
            if authorized_matter_version != run.matter_version + 1:
                raise AgentMaterialReviewBlocked(
                    "external authorization must be the next exact matter version after Agent queueing"
                )
            authorization = self._load_external_authorization(
                connection, actor=actor, matter_id=run.matter_id,
                external_request_id=external_request_id,
            )
            self._validate_external_authorization(authorization, run=run)
            next_version = run.run_version + 1
            updated = connection.execute(
                """
                UPDATE web_agent_material_runs
                SET matter_version = %s, external_request_id = %s,
                    run_version = %s, updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND run_version = %s AND status = 'QUEUED'
                  AND external_request_id IS NULL
                """,
                (
                    authorized_matter_version, external_request_id, next_version,
                    run_id, actor.firm_id, run.matter_id, expected_run_version,
                ),
            )
            if updated.rowcount != 1:
                raise AgentMaterialReviewBlocked("Agent run version conflict")
            self._finish_command(
                connection, actor=actor, matter_id=run.matter_id, run_id=run_id,
                from_version=run.run_version, to_version=next_version,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
                event_type="WEB_AGENT_EXTERNAL_REQUEST_BOUND",
                payload={
                    "external_request_id": external_request_id,
                    "input_hash": run.input_hash,
                    "authorized_matter_version": authorized_matter_version,
                },
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def claim_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._WORKER_ROLES)
        self._uuid("run_id", run_id)
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        if not 30 <= lease_seconds <= 300:
            raise AgentMaterialReviewBlocked("Agent claim lease must be between 30 and 300 seconds")
        command_name = "CLAIM_AGENT_MATERIAL_RUN"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "lease_seconds": lease_seconds,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            run = self._lock_run(connection, actor=actor, run_id=run_id)
            self._advisory_lock(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._WORKER_ROLES,
                expected_matter_version=run.matter_version,
            )
            _require_run_version(run, expected_run_version)
            _validate_plan_context(run.plan_context, require_profile=True)
            if run.external_request_id is None:
                raise AgentMaterialReviewBlocked("Agent run has no exact external authorization")
            reclaimable = (
                run.status is AgentRunStatus.CLAIMED
                and run.lease_expires_at is not None
                and run.lease_expires_at <= datetime.now(timezone.utc)
            )
            if run.status is not AgentRunStatus.QUEUED and not reclaimable:
                raise AgentMaterialReviewBlocked(
                    "only a queued or expired claimed Agent run can be claimed"
                )
            if run.attempt_count >= 3:
                raise AgentMaterialReviewBlocked("Agent claim attempt limit is exhausted")
            authorization = self._load_external_authorization(
                connection, actor=actor, matter_id=run.matter_id,
                external_request_id=run.external_request_id,
            )
            self._validate_external_authorization(authorization, run=run)
            previous_attempt = connection.execute(
                """
                SELECT status FROM external_request_attempts
                WHERE request_id = %s AND firm_id = %s AND matter_id = %s
                ORDER BY sequence DESC LIMIT 1
                """,
                (run.external_request_id, actor.firm_id, run.matter_id),
            ).fetchone()
            if previous_attempt is not None:
                raise AgentMaterialReviewBlocked(
                    "external submission already has a ledger attempt; reconcile instead of reclaiming"
                )
            lease_id = str(uuid4())
            next_version = run.run_version + 1
            updated = connection.execute(
                """
                UPDATE web_agent_material_runs
                SET status = 'CLAIMED', run_version = %s,
                    attempt_count = attempt_count + 1, lease_id = %s,
                    lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND run_version = %s
                  AND (status = 'QUEUED' OR (status = 'CLAIMED' AND lease_expires_at <= now()))
                """,
                (
                    next_version, lease_id, lease_seconds, run_id, actor.firm_id,
                    run.matter_id, expected_run_version,
                ),
            )
            if updated.rowcount != 1:
                raise AgentMaterialReviewBlocked("Agent run claim conflict")
            self._finish_command(
                connection, actor=actor, matter_id=run.matter_id, run_id=run_id,
                from_version=run.run_version, to_version=next_version,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash, event_type="WEB_AGENT_MATERIAL_RUN_CLAIMED",
                payload={"lease_id": lease_id, "lease_seconds": lease_seconds},
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def mark_submission_started(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        lease_id: str,
        external_request_id: str,
        request_hash: str,
        idempotency_key: str,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._WORKER_ROLES)
        for label, value in (
            ("run_id", run_id), ("lease_id", lease_id),
            ("external_request_id", external_request_id),
        ):
            self._uuid(label, value)
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_sha256(request_hash, "request_hash")
        _require_idempotency_key(idempotency_key)
        command_name = "MARK_AGENT_MATERIAL_SUBMISSION_STARTED"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "lease_id": lease_id,
                "external_request_id": external_request_id,
                "request_hash": request_hash,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            run = self._lock_run(connection, actor=actor, run_id=run_id)
            self._advisory_lock(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._WORKER_ROLES, expected_matter_version=None,
            )
            _require_run_version(run, expected_run_version)
            if (
                run.status is not AgentRunStatus.CLAIMED
                or run.lease_id != lease_id
                or run.external_request_id != external_request_id
            ):
                raise AgentMaterialReviewBlocked(
                    "Agent submission start does not match the active authorized claim"
                )
            attempt = connection.execute(
                """
                SELECT status, provider_request_ref_hash
                FROM external_request_attempts
                WHERE request_id = %s AND firm_id = %s AND matter_id = %s
                ORDER BY sequence DESC LIMIT 1
                """,
                (external_request_id, actor.firm_id, run.matter_id),
            ).fetchone()
            if (
                attempt is None or attempt["status"] != "SUBMISSION_STARTED"
                or attempt["provider_request_ref_hash"] != request_hash
            ):
                raise AgentMaterialReviewBlocked(
                    "Agent run cannot enter RUNNING before the exact external submission ledger receipt"
                )
            next_version = run.run_version + 1
            updated = connection.execute(
                """
                UPDATE web_agent_material_runs
                SET status = 'RUNNING', run_version = %s, provider_request_hash = %s,
                    lease_id = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND run_version = %s AND status = 'CLAIMED' AND lease_id = %s
                  AND external_request_id = %s
                """,
                (
                    next_version, request_hash, run_id, actor.firm_id, run.matter_id,
                    expected_run_version, lease_id, external_request_id,
                ),
            )
            if updated.rowcount != 1:
                raise AgentMaterialReviewBlocked("Agent submission-start transition conflict")
            connection.execute(
                "UPDATE web_agent_material_tasks SET status = 'RUNNING' WHERE run_id = %s AND firm_id = %s AND status = 'QUEUED'",
                (run_id, actor.firm_id),
            )
            self._finish_command(
                connection, actor=actor, matter_id=run.matter_id, run_id=run_id,
                from_version=run.run_version, to_version=next_version,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
                event_type="WEB_AGENT_EXTERNAL_SUBMISSION_STARTED",
                payload={
                    "external_request_id": external_request_id,
                    "provider_request_hash": request_hash,
                },
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def save_candidates(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        candidates: tuple[AgentPageCandidate, ...],
        output_hash: str,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._WORKER_ROLES)
        self._uuid("run_id", run_id)
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        _require_sha256(output_hash, "output_hash")
        if agent_candidate_output_hash(candidates) != output_hash:
            raise AgentMaterialReviewBlocked("candidate output hash does not match candidate content")
        command_name = "SAVE_AGENT_MATERIAL_CANDIDATES"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "output_hash": output_hash,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            run = self._lock_run(connection, actor=actor, run_id=run_id)
            self._advisory_lock(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._WORKER_ROLES, expected_matter_version=None,
            )
            _require_run_version(run, expected_run_version)
            if run.status is not AgentRunStatus.RUNNING:
                raise AgentMaterialReviewBlocked("only a running Agent run can publish candidates")
            _validate_persisted_candidates(run, candidates)
            expected_pages = {value.evidence_page_id for value in run.page_bindings}
            if {value.evidence_page_id for value in candidates} != expected_pages:
                raise AgentMaterialReviewBlocked("Agent candidates must cover every exact run page")
            for candidate in candidates:
                self._uuid("candidate_id", candidate.candidate_id)
                connection.execute(
                    """
                    INSERT INTO web_agent_material_candidates (
                        candidate_id, run_id, firm_id, matter_id, evidence_page_id,
                        source_file_sha256, page_number, candidate_kind, confidence,
                        review_priority, reason_codes, supporting_excerpt,
                        duplicate_of_page_id, input_hash, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'NEEDS_REVIEW')
                    """,
                    (
                        candidate.candidate_id, run_id, actor.firm_id, run.matter_id,
                        candidate.evidence_page_id, candidate.source_file_sha256,
                        candidate.page_number, candidate.kind.value, candidate.confidence,
                        candidate.review_priority.value,
                        Jsonb([value.value for value in candidate.reason_codes]),
                        candidate.supporting_excerpt, candidate.duplicate_of_page_id,
                        candidate.input_hash,
                    ),
                )
            connection.execute(
                """
                UPDATE web_agent_material_tasks
                SET status = CASE WHEN task_kind = 'EXCEPTION_ROUTING'
                    THEN 'NEEDS_REVIEW' ELSE 'COMPLETED' END
                WHERE run_id = %s AND firm_id = %s AND status = 'RUNNING'
                """,
                (run_id, actor.firm_id),
            )
            next_version = run.run_version + 1
            updated = connection.execute(
                """
                UPDATE web_agent_material_runs
                SET status = 'NEEDS_REVIEW', run_version = %s, output_hash = %s,
                    completed_at = now(), updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND run_version = %s AND status = 'RUNNING'
                """,
                (
                    next_version, output_hash, run_id, actor.firm_id,
                    run.matter_id, expected_run_version,
                ),
            )
            if updated.rowcount != 1:
                raise AgentMaterialReviewBlocked("Agent candidate publication conflict")
            self._finish_command(
                connection, actor=actor, matter_id=run.matter_id, run_id=run_id,
                from_version=run.run_version, to_version=next_version,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
                event_type="WEB_AGENT_MATERIAL_CANDIDATES_STAGED",
                payload={"output_hash": output_hash, "candidate_count": len(candidates)},
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def fail_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        failure_code: AgentFailureCode,
    ) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._WORKER_ROLES)
        self._uuid("run_id", run_id)
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        if not isinstance(failure_code, AgentFailureCode):
            raise AgentMaterialReviewBlocked("Agent failure code is invalid")
        command_name = "FAIL_AGENT_MATERIAL_RUN"
        command_hash = _canonical_hash(
            {
                "command": command_name,
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "failure_code": failure_code.value,
            }
        )
        with self._transaction(actor.firm_id) as connection:
            run = self._lock_run(connection, actor=actor, run_id=run_id)
            self._advisory_lock(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
            )
            replay = self._prior_run(
                connection, actor=actor, matter_id=run.matter_id,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash,
            )
            if replay is not None:
                return replay
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._WORKER_ROLES, expected_matter_version=None,
            )
            _require_run_version(run, expected_run_version)
            if run.status not in {AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING}:
                raise AgentMaterialReviewBlocked("only a claimed or running Agent run can fail")
            connection.execute(
                "UPDATE web_agent_material_tasks SET status = 'FAILED' WHERE run_id = %s AND firm_id = %s AND status IN ('QUEUED','RUNNING')",
                (run_id, actor.firm_id),
            )
            next_version = run.run_version + 1
            updated = connection.execute(
                """
                UPDATE web_agent_material_runs
                SET status = 'FAILED', run_version = %s, failure_code = %s,
                    lease_id = NULL, lease_expires_at = NULL,
                    completed_at = now(), updated_at = now()
                WHERE run_id = %s AND firm_id = %s AND matter_id = %s
                  AND run_version = %s AND status IN ('CLAIMED','RUNNING')
                """,
                (
                    next_version, failure_code.value, run_id, actor.firm_id,
                    run.matter_id, expected_run_version,
                ),
            )
            if updated.rowcount != 1:
                raise AgentMaterialReviewBlocked("Agent failure transition conflict")
            self._finish_command(
                connection, actor=actor, matter_id=run.matter_id, run_id=run_id,
                from_version=run.run_version, to_version=next_version,
                command_name=command_name, idempotency_key=idempotency_key,
                request_hash=command_hash, event_type="WEB_AGENT_MATERIAL_RUN_FAILED",
                payload={"failure_code": failure_code.value},
            )
            return self._load_run(connection, actor=actor, run_id=run_id)

    def get_run(self, *, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot:
        self._validate_actor(actor, self._READ_ROLES)
        self._uuid("run_id", run_id)
        with self._transaction(actor.firm_id) as connection:
            run = self._load_run(connection, actor=actor, run_id=run_id)
            self._authorize_matter(
                connection, actor=actor, matter_id=run.matter_id,
                allowed_roles=self._READ_ROLES, expected_matter_version=None,
            )
            return run

    def _lock_run(self, connection, *, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot:
        row = connection.execute(
            "SELECT run_id FROM web_agent_material_runs WHERE run_id = %s AND firm_id = %s FOR UPDATE",
            (run_id, actor.firm_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._load_run(connection, actor=actor, run_id=run_id)

    def _load_run(self, connection, *, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot:
        row = connection.execute(
            """
            SELECT run_id, firm_id, matter_id, requested_by, matter_version,
                   input_hash, plan_request_hash, agent_intent,
                   representation_profile_version, representation_profile_hash,
                   external_request_id, run_version, status, attempt_count,
                   lease_id, lease_expires_at, output_hash, failure_code,
                   created_at, updated_at
            FROM web_agent_material_runs
            WHERE run_id = %s AND firm_id = %s
            """,
            (run_id, actor.firm_id),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        tasks = connection.execute(
            """
            SELECT task_id, sequence, task_kind, status
            FROM web_agent_material_tasks
            WHERE run_id = %s AND firm_id = %s ORDER BY sequence
            """,
            (run_id, actor.firm_id),
        ).fetchall()
        bindings = connection.execute(
            """
            SELECT evidence_page_id, source_file_sha256, page_number, extracted_text_sha256
            FROM web_agent_material_page_bindings
            WHERE run_id = %s AND firm_id = %s ORDER BY sequence
            """,
            (run_id, actor.firm_id),
        ).fetchall()
        candidates = connection.execute(
            """
            SELECT candidate_id, matter_id, evidence_page_id, source_file_sha256,
                   page_number, candidate_kind, confidence, review_priority,
                   reason_codes, supporting_excerpt, duplicate_of_page_id,
                   input_hash, status
            FROM web_agent_material_candidates
            WHERE run_id = %s AND firm_id = %s ORDER BY page_number, candidate_id
            """,
            (run_id, actor.firm_id),
        ).fetchall()
        return AgentMaterialRunSnapshot(
            run_id=str(row["run_id"]),
            firm_id=str(row["firm_id"]),
            matter_id=str(row["matter_id"]),
            requested_by=str(row["requested_by"]),
            matter_version=int(row["matter_version"]),
            input_hash=str(row["input_hash"]),
            request_hash=str(row["plan_request_hash"]),
            page_bindings=tuple(
                AgentEvidencePageBinding(
                    evidence_page_id=str(value["evidence_page_id"]),
                    source_file_sha256=str(value["source_file_sha256"]),
                    page_number=int(value["page_number"]),
                    extracted_text_sha256=str(value["extracted_text_sha256"]),
                )
                for value in bindings
            ),
            run_version=int(row["run_version"]),
            status=AgentRunStatus(row["status"]),
            tasks=tuple(
                AgentMaterialTask(
                    task_id=str(value["task_id"]), sequence=int(value["sequence"]),
                    kind=AgentTaskKind(value["task_kind"]),
                    status=AgentTaskStatus(value["status"]),
                )
                for value in tasks
            ),
            candidates=tuple(self._candidate_from_row(value) for value in candidates),
            output_hash=str(row["output_hash"]) if row["output_hash"] is not None else None,
            failure_code=(
                AgentFailureCode(row["failure_code"])
                if row["failure_code"] is not None else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            external_request_id=(
                str(row["external_request_id"])
                if row["external_request_id"] is not None else None
            ),
            lease_id=str(row["lease_id"]) if row["lease_id"] is not None else None,
            lease_expires_at=row["lease_expires_at"],
            attempt_count=int(row["attempt_count"]),
            plan_context=AgentPlanContext(
                intent=AgentIntent(row["agent_intent"]),
                representation_profile_version=(
                    int(row["representation_profile_version"])
                    if row["representation_profile_version"] is not None else None
                ),
                representation_profile_hash=(
                    str(row["representation_profile_hash"])
                    if row["representation_profile_hash"] is not None else None
                ),
            ),
        )

    @staticmethod
    def _candidate_from_row(row: dict[str, Any]) -> AgentPageCandidate:
        reasons = row["reason_codes"]
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        return AgentPageCandidate(
            candidate_id=str(row["candidate_id"]),
            matter_id=str(row["matter_id"]),
            evidence_page_id=str(row["evidence_page_id"]),
            source_file_sha256=str(row["source_file_sha256"]),
            page_number=int(row["page_number"]),
            kind=PageCandidateKind(row["candidate_kind"]),
            confidence=float(row["confidence"]),
            review_priority=ReviewPriority(row["review_priority"]),
            reason_codes=tuple(CandidateReasonCode(value) for value in reasons),
            supporting_excerpt=str(row["supporting_excerpt"]),
            duplicate_of_page_id=(
                str(row["duplicate_of_page_id"])
                if row["duplicate_of_page_id"] is not None else None
            ),
            input_hash=str(row["input_hash"]),
            status=AgentCandidateStatus(row["status"]),
        )

    def _prior_run(
        self, connection, *, actor: Actor, matter_id: str, command_name: str,
        idempotency_key: str, request_hash: str,
    ) -> AgentMaterialRunSnapshot | None:
        row = connection.execute(
            """
            SELECT request_hash, run_id FROM web_agent_material_commands
            WHERE firm_id = %s AND matter_id = %s AND actor_id = %s
              AND command_name = %s AND idempotency_key = %s
            """,
            (
                actor.firm_id, matter_id, actor.actor_id,
                command_name, idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise AgentMaterialReviewBlocked(
                "Agent idempotency key was already used with different input"
            )
        return self._load_run(connection, actor=actor, run_id=str(row["run_id"]))

    def _finish_command(
        self, connection, *, actor: Actor, matter_id: str, run_id: str,
        from_version: int, to_version: int, command_name: str,
        idempotency_key: str, request_hash: str, event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO web_agent_material_events (
                firm_id, matter_id, run_id, actor_id, event_type,
                from_run_version, to_run_version, payload
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                actor.firm_id, matter_id, run_id, actor.actor_id, event_type,
                from_version, to_version, Jsonb(payload),
            ),
        )
        connection.execute(
            """
            INSERT INTO web_agent_material_commands (
                firm_id, matter_id, actor_id, command_name, idempotency_key,
                request_hash, run_id, response_run_version
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                actor.firm_id, matter_id, actor.actor_id, command_name,
                idempotency_key, request_hash, run_id, to_version,
            ),
        )

    @staticmethod
    def _verify_page_bindings(
        connection, *, actor: Actor, matter_id: str,
        bindings: tuple[AgentEvidencePageBinding, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT ep.evidence_page_id, ep.page_number, eof.original_file_sha256
            FROM evidence_pages ep
            JOIN evidence_original_files eof
              ON eof.evidence_file_id = ep.evidence_file_id
             AND eof.firm_id = ep.firm_id AND eof.matter_id = ep.matter_id
            WHERE ep.matter_id = %s AND ep.firm_id = %s
              AND ep.evidence_page_id = ANY(%s)
            """,
            (matter_id, actor.firm_id, [value.evidence_page_id for value in bindings]),
        ).fetchall()
        expected = {
            str(value["evidence_page_id"]): (
                str(value["original_file_sha256"]), int(value["page_number"])
            )
            for value in rows
        }
        supplied = {
            value.evidence_page_id: (value.source_file_sha256, value.page_number)
            for value in bindings
        }
        if expected != supplied:
            raise AgentMaterialReviewBlocked(
                "Agent page bindings do not match registered server evidence"
            )

    @staticmethod
    def _load_external_authorization(
        connection, *, actor: Actor, matter_id: str, external_request_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT request_kind, provider_id, processor_region, selected_field_ids,
                   service_id, call_cap, input_hash, expires_at
            FROM external_request_authorizations
            WHERE request_id = %s AND firm_id = %s AND matter_id = %s
            FOR KEY SHARE
            """,
            (external_request_id, actor.firm_id, matter_id),
        ).fetchone()
        if row is None:
            raise KeyError(external_request_id)
        return row

    @staticmethod
    def _validate_external_authorization(
        authorization: dict[str, Any], *, run: AgentMaterialRunSnapshot,
    ) -> None:
        selected = authorization["selected_field_ids"]
        if isinstance(selected, str):
            selected = json.loads(selected)
        if (
            authorization["request_kind"] != "MODEL"
            or authorization["provider_id"] != "deepseek"
            or authorization["service_id"] != "deepseek-v4-pro"
            or authorization["call_cap"] != 1
            or selected != [f"agent-material-input:{run.input_hash}"]
            or authorization["input_hash"] != run.input_hash
        ):
            raise AgentMaterialReviewBlocked(
                "external request does not authorize this exact Agent material input"
            )
        expires_at = authorization["expires_at"]
        if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
            raise AgentMaterialReviewBlocked("Agent external request authorization is expired")

    @staticmethod
    def _authorize_matter(
        connection, *, actor: Actor, matter_id: str,
        allowed_roles: frozenset[Role], expected_matter_version: int | None,
    ) -> None:
        row = connection.execute(
            """
            SELECT m.version,
                   EXISTS (
                       SELECT 1 FROM matter_actor_roles mar
                       JOIN users u ON u.user_id = mar.user_id AND u.firm_id = mar.firm_id
                       WHERE mar.matter_id = m.matter_id AND mar.firm_id = m.firm_id
                         AND mar.user_id = %s AND mar.revoked_at IS NULL
                         AND mar.role = ANY(%s) AND u.status = 'ACTIVE'
                   ) AS permitted
            FROM matters m WHERE m.matter_id = %s AND m.firm_id = %s
            """,
            (
                actor.actor_id, [role.value for role in allowed_roles],
                matter_id, actor.firm_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        if not row["permitted"]:
            raise PermissionError("actor lacks an active database role for this matter")
        if expected_matter_version is not None and row["version"] != expected_matter_version:
            raise AgentMaterialReviewBlocked("Agent matter version conflict")

    @staticmethod
    def _advisory_lock(
        connection, *, actor: Actor, matter_id: str,
        command_name: str, idempotency_key: str,
    ) -> None:
        scope = "|".join((actor.actor_id, matter_id, command_name, idempotency_key))
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope,))

    @staticmethod
    def _validate_actor(actor: Actor, allowed_roles: frozenset[Role]) -> None:
        if not isinstance(actor, Actor) or not actor.roles.intersection(allowed_roles):
            raise AgentMaterialReviewBlocked("actor is not allowed to perform this Agent command")
        PostgresAgentMaterialRunStore._uuid("actor_id", actor.actor_id)
        PostgresAgentMaterialRunStore._uuid("firm_id", actor.firm_id)

    @staticmethod
    def _uuid(label: str, value: str) -> None:
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise AgentMaterialReviewBlocked(f"{label} must be UUID") from error

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


__all__ = ("PostgresAgentMaterialRunStore",)
