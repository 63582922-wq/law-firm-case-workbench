"""Concrete PostgreSQL bridge for the recoverable case-Agent worker.

The event store deliberately exposes storage-oriented records.  This adapter
maps them to the narrower worker/DeepSeek guard contracts without introducing
workflow state of its own.  Every write still passes through
``PostgresCaseAgentStore`` and therefore through its RLS, optimistic versions,
leases and supervisor replay.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Mapping, Protocol
from uuid import UUID, uuid5

from .case_agent_planner import (
    CasePlanProposal,
    parse_case_plan_proposal,
    case_plan_proposal_payload,
)
from .case_agent_postgres import (
    ClaimedAgentPlanningAttempt,
    ClaimedAgentTask,
    PostgresCaseAgentStore,
)
from .case_agent_supervisor import (
    AgentEventType,
    AgentRunState,
    AgentSupervisorEvent,
    AgentTaskGraph,
    PlanningFailurePayload,
    SupervisorCommandKind,
    TaskGraphPayload,
)
from .case_agent_worker import (
    AgentWorkerHeartbeat,
    CaseAgentWorkerBlocked,
    DurablePlanningClaim,
    DurableTaskClaim,
    ReapedAttemptResult,
    ReapedPlanningAttemptResult,
    DurableVerificationClaim,
)
from .case_agent_verifier import RunVerificationReceipt
from .models import Actor


class PlanningLookupStatus(StrEnum):
    """Provider states returned by a lookup-only reconciliation connector."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PlanningLookupOutcome:
    """One observation of the *existing* planner request.

    The connector cannot use this object to request another generation.  A
    non-terminal observation is deliberately not written as a terminal ledger
    event and leaves the supervisor in reconciliation-required state.
    """

    status: PlanningLookupStatus
    output_hash: str | None = None
    error_code: str | None = None
    structured_proposal: Mapping[str, object] | None = None

    def validate(self) -> None:
        if not isinstance(self.status, PlanningLookupStatus):
            raise CaseAgentWorkerBlocked("planning lookup status is invalid")
        if self.status is PlanningLookupStatus.SUCCEEDED:
            _sha256(self.output_hash, "planning lookup output_hash")
            if self.error_code is not None or not isinstance(
                self.structured_proposal, Mapping
            ):
                raise CaseAgentWorkerBlocked(
                    "successful planning lookup is missing its structured proposal"
                )
            _canonical_mapping(self.structured_proposal)
            return
        if self.status is PlanningLookupStatus.FAILED:
            if (
                not _safe_error_code(self.error_code)
                or self.structured_proposal is not None
            ):
                raise CaseAgentWorkerBlocked("failed planning lookup is malformed")
            if self.output_hash is not None:
                _sha256(self.output_hash, "planning lookup failure output_hash")
            return
        if any(
            value is not None
            for value in (self.output_hash, self.error_code, self.structured_proposal)
        ):
            raise CaseAgentWorkerBlocked(
                "non-terminal planning lookup cannot carry terminal result data"
            )


class PlanningResultReconciler(Protocol):
    """Lookup an existing provider request; implementations cannot submit."""

    def lookup(
        self, *, external_request_id: str, request_hash: str
    ) -> PlanningLookupOutcome: ...


class PostgresCaseAgentWorkerAdapter:
    """One shared adapter for worker store, DeepSeek guard and health store."""

    def __init__(
        self,
        *,
        store: PostgresCaseAgentStore,
        actor: Actor,
        planning_reconciler: "PlanningResultReconciler | None" = None,
    ) -> None:
        if not isinstance(store, PostgresCaseAgentStore):
            raise TypeError("Postgres case-Agent store is required")
        self._store = store
        self._actor = actor
        self._planning_reconciler = planning_reconciler

    def read_projection(self, *, matter_id: str, actor: Actor, run_id: str):
        self._same_actor(actor)
        return self._store.read_projection(
            matter_id=matter_id, actor=actor, run_id=run_id
        )

    def apply_pending_snapshot_refresh(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> int | None:
        self._same_actor(actor)
        applied = self._store.apply_pending_snapshot_refresh(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
        )
        if applied is None:
            return None
        if (
            applied.run_id != run_id
            or applied.matter_id != matter_id
            or applied.event_version < 1
        ):
            raise CaseAgentWorkerBlocked(
                "snapshot refresh store returned an invalid transition"
            )
        return applied.event_version

    def start_verification(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        execution_actor_id: str,
        verifier_id: str,
        verifier_version: str,
        policy_hash: str,
    ) -> DurableVerificationClaim:
        self._same_actor(actor)
        claim = self._store.start_verification(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
            expected_event_version=expected_event_version,
            idempotency_key=idempotency_key,
            execution_actor_id=execution_actor_id,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            policy_hash=policy_hash,
        )
        if not isinstance(claim, DurableVerificationClaim):
            raise CaseAgentWorkerBlocked(
                "verification store returned an invalid durable claim"
            )
        claim.validate()
        return claim

    def record_verification_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurableVerificationClaim,
        idempotency_key: str,
        receipt: RunVerificationReceipt,
    ) -> int:
        self._same_actor(actor)
        return self._store.record_verification_outcome(
            matter_id=matter_id,
            actor=actor,
            claim=claim,
            idempotency_key=idempotency_key,
            receipt=receipt,
        )

    def claim_next_planning_command(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        planning_hash: str,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> DurablePlanningClaim:
        self._same_actor(actor)
        projection = self._store.read_projection(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if projection.state.event_version != expected_event_version:
            raise CaseAgentWorkerBlocked("Agent run changed before planning claim")
        command = next(
            (
                item
                for item in projection.next_commands
                if item.kind
                in {
                    SupervisorCommandKind.REQUEST_PLAN,
                    SupervisorCommandKind.REQUEST_REPLAN,
                }
            ),
            None,
        )
        if command is None:
            raise CaseAgentWorkerBlocked("supervisor did not request a plan")
        planning_kind = (
            "REPLAN"
            if command.kind is SupervisorCommandKind.REQUEST_REPLAN
            else "PLAN"
        )
        record = self._store.claim_planning_attempt(
            matter_id=matter_id,
            actor=actor,
            run_id=run_id,
            expected_event_version=expected_event_version,
            planning_hash=planning_hash,
            planning_kind=planning_kind,
            idempotency_key=idempotency_key,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        # Claiming PLANNING_STARTED increments the event stream, so replay the
        # exact post-claim state rather than manufacturing it in the adapter.
        claimed_state = self._store.read_projection(
            matter_id=matter_id, actor=actor, run_id=run_id
        ).state
        return self._planning_claim(
            record=record,
            state=claimed_state,
            command_kind=command.kind,
            command_id=command.command_id,
        )

    def heartbeat_planning_attempt(self, **kwargs):
        version, expires = self._store.heartbeat_planning_attempt(**kwargs)
        return version, expires

    def record_planning_failure(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurablePlanningClaim,
        expected_attempt_version: int,
        status: str,
        error_code: str,
    ) -> None:
        self._same_actor(actor)
        # DeepSeek's request guard already records FAILED/UNKNOWN outcomes and
        # corresponding supervisor events.  Re-read first; never append a
        # duplicate state transition or reinterpret UNKNOWN as a local fail.
        current = self._store.current_planning_attempt(
            matter_id=matter_id, actor=actor, run_id=claim.run_id
        )
        if current is None or current.planning_attempt_id != claim.planning_attempt_id:
            raise CaseAgentWorkerBlocked("current planning attempt differs from failure")
        if current.status in {"FAILED", "UNKNOWN_SUBMISSION"}:
            return
        if current.lease_token != claim.lease_token:
            raise CaseAgentWorkerBlocked(
                "stale planning lease cannot classify the current planner outcome"
            )
        if status == "FAILED" and current.status == "SUCCEEDED":
            # The provider response is durably preserved, but the independent
            # compiler rejected it.  Record that known local outcome in the
            # supervisor; never rewrite the provider ledger as if it failed.
            projection = self._store.read_projection(
                matter_id=matter_id, actor=actor, run_id=claim.run_id
            )
            event = AgentSupervisorEvent(
                event_id=str(
                    uuid5(
                        UUID(claim.planning_attempt_id),
                        f"compile-failed:{error_code}:{projection.state.event_version}",
                    )
                ),
                run_id=claim.run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                sequence=projection.state.event_version + 1,
                event_type=AgentEventType.PLANNING_FAILED,
                occurred_at=datetime.now(timezone.utc),
                actor_id=actor.actor_id,
                payload=PlanningFailurePayload(error_code=error_code),
            )
            self._store.append_event(
                matter_id=matter_id,
                actor=actor,
                expected_event_version=projection.state.event_version,
                idempotency_key=(
                    f"agent-plan-compile-failed:{claim.planning_attempt_id}:"
                    f"{projection.state.event_version}"
                ),
                event=event,
            )
            return
        if status == "FAILED" and current.status == "CLAIMED":
            self._store.fail_planning_before_submission(
                matter_id=matter_id,
                actor=actor,
                run_id=claim.run_id,
                planning_attempt_id=claim.planning_attempt_id,
                error_code=error_code,
            )
            return
        # Anything else may have crossed the provider boundary.  Guessing a
        # terminal result would make a second call possible after restart.
        _ = expected_attempt_version
        raise CaseAgentWorkerBlocked(
            "planning failure cannot be classified safely from its durable boundary"
        )

    def record_local_planning_outcome(
        self,
        *,
        matter_id: str,
        claim: DurablePlanningClaim,
        planner_id: str,
        proposal: CasePlanProposal,
    ) -> None:
        """Store a server-governed plan without entering a provider ledger."""

        claim.validate()
        if (
            claim.state.matter_id != matter_id
            or claim.state.firm_id != self._actor.firm_id
            or claim.run_id != claim.state.run_id
        ):
            raise CaseAgentWorkerBlocked(
                "local planning outcome is outside the current worker claim"
            )
        self._store.record_local_planning_outcome(
            matter_id=matter_id,
            actor=self._actor,
            run_id=claim.run_id,
            planning_attempt_id=claim.planning_attempt_id,
            lease_token=claim.lease_token,
            matter_version=claim.matter_version,
            planner_id=planner_id,
            proposal=proposal,
        )

    def accept_planning_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurablePlanningClaim,
        expected_attempt_version: int,
        idempotency_key: str,
        graph: object,
    ) -> int:
        self._same_actor(actor)
        if not isinstance(graph, AgentTaskGraph):
            raise CaseAgentWorkerBlocked("planner compiler did not return a task graph")
        projection = self._store.read_projection(
            matter_id=matter_id, actor=actor, run_id=claim.run_id
        )
        current = self._store.current_planning_attempt(
            matter_id=matter_id, actor=actor, run_id=claim.run_id
        )
        if (
            current is None
            or current.planning_attempt_id != claim.planning_attempt_id
            or current.status != "SUCCEEDED"
            or current.lease_token != claim.lease_token
            or current.recovered_proposal is None
        ):
            raise CaseAgentWorkerBlocked(
                "compiled graph has no successful durable planner proposal"
            )
        # The planning attempt version can advance independently from the
        # event stream, but the worker must never accept an older claim after a
        # different attempt became current.
        if current.attempt_version < expected_attempt_version:
            raise CaseAgentWorkerBlocked("planning attempt version moved backwards")
        if (
            graph.graph_id != claim.graph_id
            or graph.graph_version != claim.graph_version
            or graph.goal_hash != projection.state.goal.goal_hash
            or graph.snapshot != projection.state.snapshot
            or current.run_id != claim.run_id
            or current.planning_hash != claim.planning_hash
            or current.matter_version != graph.snapshot.matter_version
            or projection.state.run_id != claim.run_id
            or projection.state.matter_id != matter_id
            or projection.state.firm_id != actor.firm_id
        ):
            raise CaseAgentWorkerBlocked("compiled graph differs from the durable planning claim")
        self._reviewed_or_original_proposal(record=current, state=projection.state, actor=actor, graph=graph)
        event = AgentSupervisorEvent(
            event_id=str(uuid5(UUID(claim.planning_attempt_id), graph.graph_hash)),
            run_id=claim.run_id,
            firm_id=actor.firm_id,
            matter_id=matter_id,
            sequence=projection.state.event_version + 1,
            event_type=AgentEventType.TASK_GRAPH_ACCEPTED,
            occurred_at=datetime.now(timezone.utc),
            actor_id=actor.actor_id,
            payload=TaskGraphPayload(graph=graph),
        )
        receipt = self._store.append_event(
            matter_id=matter_id,
            actor=actor,
            expected_event_version=projection.state.event_version,
            idempotency_key=idempotency_key,
            event=event,
        )
        return receipt.event_version

    def recover_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> DurablePlanningClaim:
        self._same_actor(actor)
        projection = self._store.read_projection(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if projection.state.event_version != expected_event_version:
            raise CaseAgentWorkerBlocked("Agent run changed before planning recovery")
        record = self._store.current_planning_attempt(
            matter_id=matter_id, actor=actor, run_id=run_id
        )
        if record is None:
            raise CaseAgentWorkerBlocked("planning run has no durable attempt")
        if projection.state.goal.material_read_refs:
            reviewed_scope = self._store.material_scope_review(matter_id=matter_id, actor=actor,
                run_id=run_id, expected_event_version=expected_event_version)
            if reviewed_scope is not None and (record.status != "SUCCEEDED" or record.recovered_proposal is None):
                raise CaseAgentWorkerBlocked("reviewed material recovery requires retained success; no lookup or submission")
        command = next(
            (
                item
                for item in projection.next_commands
                if item.kind is SupervisorCommandKind.RECONCILE_PLAN_RESULT
            ),
            None,
        )
        # A terminal v2/v3 proposal may have committed immediately before the
        # worker crashed.  The reducer cannot see that private ledger entry, so
        # it will still emit RECONCILE_PLAN_RESULT.  Compile the durable result
        # first; never acquire another reconciliation lease or call lookup.
        if record.status == "SUCCEEDED" and record.recovered_proposal is not None:
            pass
        elif command is not None:
            if self._planning_reconciler is None:
                # Do not acquire a lease that this process cannot use.  More
                # importantly, never reinterpret a missing lookup API as
                # permission to submit a second planner request.
                raise CaseAgentWorkerBlocked(
                    "planning provider has no lookup-only reconciliation connector"
                )
            record = self._store.claim_planning_reconciliation(
                matter_id=matter_id,
                actor=actor,
                run_id=run_id,
                planning_attempt_id=record.planning_attempt_id,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
            )
            if record.request_hash is None:
                raise CaseAgentWorkerBlocked(
                    "planning reconciliation has no durable original request hash"
                )
            outcome = self._planning_reconciler.lookup(
                external_request_id=record.external_request_id,
                request_hash=record.request_hash,
            )
            if not isinstance(outcome, PlanningLookupOutcome):
                raise CaseAgentWorkerBlocked(
                    "planning reconciliation connector returned an unsupported result"
                )
            outcome.validate()
            if outcome.status in {
                PlanningLookupStatus.PENDING,
                PlanningLookupStatus.UNKNOWN,
            }:
                raise CaseAgentWorkerBlocked(
                    "planning provider result remains non-terminal; no retry was sent"
                )
            self._store.record_planning_reconciliation_outcome(
                matter_id=matter_id,
                actor=actor,
                run_id=run_id,
                planning_attempt_id=record.planning_attempt_id,
                external_request_id=record.external_request_id,
                request_hash=record.request_hash,
                lease_owner=record.lease_owner,
                lease_token=record.lease_token,
                expected_attempt_version=record.attempt_version,
                status=outcome.status.value,
                output_hash=outcome.output_hash,
                error_code=outcome.error_code,
                structured_proposal=(
                    dict(outcome.structured_proposal)
                    if outcome.structured_proposal is not None
                    else None
                ),
            )
            projection = self._store.read_projection(
                matter_id=matter_id, actor=actor, run_id=run_id
            )
            record = self._store.current_planning_attempt(
                matter_id=matter_id, actor=actor, run_id=run_id
            )
            if record is None or record.status != "SUCCEEDED":
                raise CaseAgentWorkerBlocked(
                    "planning reconciliation reached a known failed outcome"
                )
        elif record.status != "SUCCEEDED" or record.recovered_proposal is None:
            raise CaseAgentWorkerBlocked(
                "in-flight planning has no durable successful proposal to resume"
            )
        proposal = self._reviewed_or_original_proposal(record=record, state=projection.state, actor=actor)
        _ = idempotency_key  # all actual writes remain idempotent in the store
        command_kind = (
            SupervisorCommandKind.REQUEST_REPLAN
            if record.planning_kind == "REPLAN"
            else SupervisorCommandKind.REQUEST_PLAN
        )
        command_id = (
            command.command_id
            if command is not None
            else _digest(
                {
                    "run_id": run_id,
                    "planning_attempt_id": record.planning_attempt_id,
                    "planning_hash": record.planning_hash,
                }
            )
        )
        return self._planning_claim(
            record=record,
            state=projection.state,
            command_kind=command_kind,
            command_id=command_id,
            proposal=proposal,
        )

    def claim_next_dispatchable_task(self, **kwargs) -> DurableTaskClaim:
        record = self._store.claim_next_dispatchable_task(**kwargs)
        return self._task_claim(record)

    def claim_next_reconciliation(self, **kwargs) -> DurableTaskClaim:
        record = self._store.claim_next_reconciliation(**kwargs)
        return self._task_claim(record)

    def heartbeat_attempt(self, **kwargs):
        return self._store.heartbeat_attempt(**kwargs)

    def record_external_submission_started(self, **kwargs):
        return self._store.record_external_submission_started(**kwargs)

    def record_receipt(self, **kwargs):
        return self._store.record_receipt(**kwargs)

    def reap_expired_attempt(self, **kwargs) -> ReapedAttemptResult | None:
        record = self._store.reap_expired_attempt(**kwargs)
        if record is None:
            return None
        return ReapedAttemptResult(
            run_id=record.run_id,
            task_id=record.task_id,
            attempt_id=record.attempt_id,
            event_version=record.event_version,
            requires_reconciliation=record.requires_reconciliation,
        )

    def reap_expired_planning_attempt(
        self, **kwargs
    ) -> ReapedPlanningAttemptResult | None:
        record = self._store.reap_expired_planning_attempt(**kwargs)
        if record is None:
            return None
        return ReapedPlanningAttemptResult(
            run_id=record.run_id,
            planning_attempt_id=record.planning_attempt_id,
            event_version=record.event_version,
            requires_reconciliation=record.requires_reconciliation,
        )

    # DeepSeekPlannerRequestGuard -------------------------------------------------

    def begin_submission(
        self,
        *,
        external_request_id: str,
        run_id: str,
        claim_lease_id: str,
        lease_token: str,
        matter_id: str,
        matter_version: int,
        provider_id: str,
        service_id: str,
        input_hash: str,
        request_hash: str,
    ) -> int:
        return self._store.begin_planning_submission(
            matter_id=matter_id,
            actor=self._actor,
            run_id=run_id,
            planning_attempt_id=claim_lease_id,
            lease_token=lease_token,
            external_request_id=external_request_id,
            matter_version=matter_version,
            provider_id=provider_id,
            service_id=service_id,
            input_hash=input_hash,
            request_hash=request_hash,
        )

    def record_outcome(
        self,
        *,
        external_request_id: str,
        run_id: str,
        matter_id: str,
        matter_version: int,
        lease_token: str,
        expected_external_ledger_version: int,
        request_hash: str,
        status: str,
        output_hash: str | None,
        error_code: str | None,
        structured_proposal: Mapping[str, object] | None,
    ) -> None:
        current = self._store.current_planning_attempt(
            matter_id=matter_id, actor=self._actor, run_id=run_id
        )
        if current is None or current.external_request_id != external_request_id:
            raise CaseAgentWorkerBlocked("planner outcome differs from the current claim")
        self._store.record_planning_outcome(
            matter_id=matter_id,
            actor=self._actor,
            run_id=run_id,
            planning_attempt_id=current.planning_attempt_id,
            external_request_id=external_request_id,
            lease_token=lease_token,
            matter_version=matter_version,
            expected_external_ledger_version=expected_external_ledger_version,
            request_hash=request_hash,
            status=status,
            output_hash=output_hash,
            error_code=error_code,
            structured_proposal=(
                dict(structured_proposal)
                if structured_proposal is not None
                else None
            ),
        )

    # WorkerHealthStore ----------------------------------------------------------

    def probe_case_agent_store(self, *, firm_id: str) -> bool:
        if firm_id != self._actor.firm_id:
            return False
        return self._store.probe_case_agent_store(firm_id=firm_id)

    def record_worker_heartbeat(self, heartbeat: AgentWorkerHeartbeat) -> None:
        if heartbeat.actor_id != self._actor.actor_id or heartbeat.firm_id != self._actor.firm_id:
            raise CaseAgentWorkerBlocked("worker heartbeat is outside this service principal")
        self._store.record_worker_heartbeat(heartbeat)

    def latest_worker_heartbeat(
        self, *, firm_id: str, worker_id: str
    ) -> AgentWorkerHeartbeat | None:
        if firm_id != self._actor.firm_id:
            return None
        record = self._store.latest_worker_heartbeat(
            firm_id=firm_id, worker_id=worker_id
        )
        if record is None:
            return None
        return AgentWorkerHeartbeat(
            firm_id=record.firm_id,
            worker_id=record.worker_id,
            actor_id=record.actor_id,
            planner_id=record.planner_id,
            adapter_catalog_hash=record.adapter_catalog_hash,
            verifier_actor_id=record.verifier_actor_id,
            verifier_id=record.verifier_id,
            verifier_version=record.verifier_version,
            verifier_policy_hash=record.verifier_policy_hash,
            observed_at=record.observed_at,
            expires_at=record.expires_at,
        )

    def _planning_claim(
        self,
        *,
        record: ClaimedAgentPlanningAttempt,
        state: AgentRunState,
        command_kind: SupervisorCommandKind,
        command_id: str,
        proposal: CasePlanProposal | None = None,
    ) -> DurablePlanningClaim:
        graph_version = 1 if state.graph is None else state.graph.graph_version + 1
        graph_id = str(uuid5(UUID(record.planning_attempt_id), f"graph:{graph_version}"))
        if (state.goal.material_read_refs and record.recovered_proposal is not None
                and record.recovered_proposal.get("goal_hash") != state.goal.goal_hash):
            # Reviewed first graph has a stable run-derived identity recorded
            # by the review compiler, not the legacy attempt-derived identity.
            graph_id = state.run_id
        return DurablePlanningClaim(
            planning_attempt_id=record.planning_attempt_id,
            run_id=record.run_id,
            command_kind=command_kind,
            command_id=command_id,
            planning_hash=record.planning_hash,
            claim_lease_id=record.planning_attempt_id,
            lease_token=record.lease_token,
            external_request_id=record.external_request_id,
            event_version=record.event_version,
            attempt_version=record.attempt_version,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            graph_id=graph_id,
            graph_version=graph_version,
            state=state,
            recovered_proposal=proposal,
        )

    @staticmethod
    def _task_claim(record: ClaimedAgentTask) -> DurableTaskClaim:
        return DurableTaskClaim(
            run_id=record.run_id,
            task_id=record.task_id,
            attempt_id=record.attempt_id,
            event_version=record.event_version,
            attempt_version=record.attempt_version,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            task=record.task,
            reconciliation=record.reconciliation,
            external_request_id=record.external_request_id,
        )

    def _reviewed_or_original_proposal(self, *, record: ClaimedAgentPlanningAttempt,
        state: AgentRunState, actor: Actor, graph: AgentTaskGraph | None = None) -> CasePlanProposal:
        raw = record.recovered_proposal
        if raw is None:
            raise CaseAgentWorkerBlocked("recovery has no retained proposal")
        if raw.get("goal_hash") == state.goal.goal_hash:
            return self._parse_recovered_proposal(raw, goal_hash=state.goal.goal_hash, planning_hash=record.planning_hash)
        # No planner or lookup fallback exists on this branch.
        if not state.goal.material_read_refs:
            raise CaseAgentWorkerBlocked("retained proposal goal differs without scope review")
        review = self._store.material_scope_review(matter_id=state.matter_id, actor=actor,
            run_id=state.run_id, expected_event_version=state.event_version)
        if (review is None or review.planning_hash != record.planning_hash
                or review.request_hash != record.request_hash
                or review.snapshot != state.snapshot
                or review.effective_goal_hash != state.goal.goal_hash
                or review.material_read_refs != state.goal.material_read_refs):
            raise CaseAgentWorkerBlocked("retained proposal lacks exact material scope review")
        original = self._parse_recovered_proposal(raw, goal_hash=review.original_goal_hash,
            planning_hash=review.planning_hash)
        if _digest(case_plan_proposal_payload(original)) != review.original_proposal_hash:
            raise CaseAgentWorkerBlocked("reviewed original proposal fingerprint differs")
        derived = replace(original, goal_hash=state.goal.goal_hash)
        if _digest(case_plan_proposal_payload(derived)) != review.derived_proposal_hash:
            raise CaseAgentWorkerBlocked("reviewed derived proposal fingerprint differs")
        if graph is not None and graph.graph_hash != review.compiled_graph_hash:
            raise CaseAgentWorkerBlocked("recovered graph differs from reviewed graph")
        return derived

    @staticmethod
    def _parse_recovered_proposal(
        value: Mapping[str, object], *, goal_hash: str, planning_hash: str
    ) -> CasePlanProposal:
        try:
            encoded = json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise CaseAgentWorkerBlocked(
                "durable planner proposal is not canonical JSON"
            ) from error
        return parse_case_plan_proposal(
            encoded,
            expected_goal_hash=goal_hash,
            expected_snapshot_hash=planning_hash,
        )

    def _same_actor(self, actor: Actor) -> None:
        if actor != self._actor:
            raise PermissionError("worker adapter actor differs from its service principal")


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CaseAgentWorkerBlocked(f"{label} must be a SHA-256 digest")


def _canonical_mapping(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise CaseAgentWorkerBlocked(
            "planning lookup proposal is not canonical JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise CaseAgentWorkerBlocked("planning lookup proposal must be an object")
    return decoded


def _safe_error_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 180
        and all(character.isalnum() or character in "._:-" for character in value)
        and value[0].isalpha()
    )


__all__ = [
    "PlanningLookupOutcome",
    "PlanningLookupStatus",
    "PlanningResultReconciler",
    "PostgresCaseAgentWorkerAdapter",
]
