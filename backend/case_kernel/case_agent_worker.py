"""Recoverable worker orchestration for the unified lawyer case Agent.

This module is deliberately narrower than a generic job runner.  It consumes
only commands derived by :mod:`case_agent_supervisor`, leases work through a
durable store and dispatches an immutable task to the one exact adapter that
was bound by the server-side planner compiler.  It has no shell, path, URL or
free-form Tool execution surface.

The PostgreSQL adapter is expected to implement the protocols below.  Planning
and reconciliation have their own lease contracts because both may cross an
external boundary.  If those contracts are absent, the worker fails closed;
an in-memory lock is not a substitute for process-restart recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import json
import logging
import re
import traceback
from threading import Event, RLock, Thread
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid5

from .case_agent_external_failure import CaseAgentKnownExternalFailure
from .case_agent_planner import (
    CaseAgentPlannerCompiler,
    CasePlanProposal,
    CasePlannerAdmissionBlocked,
    CasePlannerBudgetExceeded,
    CasePlannerBlocked,
    CasePlanningSnapshot,
    PlannerSemanticSkill,
)
from .case_agent_supervisor import (
    AgentRunStatus,
    AgentRunState,
    AgentTaskSpec,
    AgentTaskStatus,
    ArtifactReceipt,
    ExternalSubmissionState,
    NetworkPolicy,
    ResultStatus,
    RunResourceBudget,
    RuntimeAdapterManifest,
    SupervisorCommandKind,
    TaskResultReceipt,
)
from .deepseek_case_agent_planner import (
    DeepSeekPlannerPreDispatchFailure,
    DeepSeekPlannerRejected,
    DeepSeekPlannerUnknownSubmission,
)
from .case_agent_verifier import (
    CaseAgentRunVerifier,
    CaseAgentVerificationIndeterminate,
    RunVerificationReceipt,
    VerificationOutcome,
)
from .models import Actor, Role


class CaseAgentWorkerBlocked(RuntimeError):
    """Execution cannot continue without violating a durable safety boundary."""


def remaining_planning_budget(state: AgentRunState) -> RunResourceBudget:
    """Replanning never renews already consumed run resources."""
    budget, used = state.budget, state.budget_usage
    attempts = budget.max_total_attempts - used.attempts
    if attempts < 1:
        raise CasePlannerBudgetExceeded(dimension="attempt", required=1,
                                       available=max(0, attempts), task_count=0)
    return replace(budget, max_tasks=min(budget.max_tasks, attempts), max_total_attempts=attempts,
        max_external_calls=max(0, budget.max_external_calls - used.external_calls),
        max_runtime_seconds=max(0, budget.max_runtime_seconds - used.runtime_seconds),
        max_cost_minor_units=max(0, budget.max_cost_minor_units - used.cost_minor_units),
        max_output_bytes=max(0, budget.max_output_bytes - used.output_bytes))


def _planning_budget_failure_code(state: AgentRunState) -> str:
    used = state.budget_usage
    return ("NEXT_STAGE_BUDGET_REVIEW_REQUIRED" if any((used.attempts, used.external_calls,
        used.runtime_seconds, used.cost_minor_units, used.output_bytes)) else "PLANNER_PROPOSAL_REJECTED")


class CaseAgentReconciliationUnavailable(CaseAgentWorkerBlocked):
    """A lookup-only recovery found no terminal result and must now wait."""

    def __init__(self, reason_code: str) -> None:
        if (
            not isinstance(reason_code, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,199}", reason_code)
        ):
            raise ValueError("reconciliation-unavailable reason code is invalid")
        self.reason_code = reason_code
        super().__init__("external result remains unavailable after lookup-only recovery")


class AgentWorkerStep(StrEnum):
    IDLE = "IDLE"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_LEASE = "WAITING_LEASE"
    PLANNED = "PLANNED"
    PLANNING_FAILED = "PLANNING_FAILED"
    PLANNING_RECONCILIATION_REQUIRED = "PLANNING_RECONCILIATION_REQUIRED"
    PLANNING_RECONCILED = "PLANNING_RECONCILED"
    SNAPSHOT_REFRESHED = "SNAPSHOT_REFRESHED"
    TASK_SUCCEEDED = "TASK_SUCCEEDED"
    TASK_FAILED = "TASK_FAILED"
    TASK_RECONCILIATION_REQUIRED = "TASK_RECONCILIATION_REQUIRED"
    RECONCILIATION_DEFERRED = "RECONCILIATION_DEFERRED"
    ATTEMPT_REAPED = "ATTEMPT_REAPED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


@dataclass(frozen=True)
class WorkerStepResult:
    run_id: str
    step: AgentWorkerStep
    event_version: int
    task_id: str | None = None
    attempt_id: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class DurablePlanningClaim:
    """One database-leased PLAN/REPLAN command.

    ``external_request_id`` is stable across reconciliation.  The opaque
    ``lease_token`` is rotated on every takeover and fences provider writes;
    ``claim_lease_id`` identifies the durable planning attempt.  The state is
    replayed by the store after it appends ``PLANNING_STARTED``.
    """

    planning_attempt_id: str
    run_id: str
    command_kind: SupervisorCommandKind
    command_id: str
    planning_hash: str
    claim_lease_id: str
    lease_token: str
    external_request_id: str
    event_version: int
    attempt_version: int
    lease_owner: str
    lease_expires_at: datetime
    graph_id: str
    graph_version: int
    state: AgentRunState
    recovered_proposal: CasePlanProposal | None = None

    def validate(self) -> None:
        for label, value in (
            ("planning_attempt_id", self.planning_attempt_id),
            ("run_id", self.run_id),
            ("claim_lease_id", self.claim_lease_id),
            ("lease_token", self.lease_token),
            ("external_request_id", self.external_request_id),
            ("graph_id", self.graph_id),
        ):
            _uuid(value, label)
        if self.command_kind not in {
            SupervisorCommandKind.REQUEST_PLAN,
            SupervisorCommandKind.REQUEST_REPLAN,
        }:
            raise CaseAgentWorkerBlocked("planning claim is not a PLAN/REPLAN command")
        _sha256(self.command_id, "planning command_id")
        _sha256(self.planning_hash, "planning_hash")
        _positive(self.event_version, "planning event_version")
        _positive(self.attempt_version, "planning attempt_version")
        _positive(self.graph_version, "planning graph_version")
        _aware(self.lease_expires_at, "planning lease_expires_at")
        if self.state.run_id != self.run_id or self.state.event_version != self.event_version:
            raise CaseAgentWorkerBlocked("planning claim state differs from its durable run")

    # DeepSeek currently consumes this structural execution claim.  Keeping
    # the validation method here avoids putting a provider type in the worker
    # control-plane contract.
    @property
    def matter_version(self) -> int:
        return self.state.snapshot.matter_version


@dataclass(frozen=True)
class DurableTaskClaim:
    run_id: str
    task_id: str
    attempt_id: str
    event_version: int
    attempt_version: int
    lease_owner: str
    lease_expires_at: datetime
    task: AgentTaskSpec
    reconciliation: bool = False
    external_request_id: str | None = None

    def validate(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("task_id", self.task_id),
            ("attempt_id", self.attempt_id),
        ):
            _uuid(value, label)
        _positive(self.event_version, "task claim event_version")
        _positive(self.attempt_version, "task claim attempt_version")
        _aware(self.lease_expires_at, "task claim lease_expires_at")
        if self.task.task_id != self.task_id:
            raise CaseAgentWorkerBlocked("task claim differs from its immutable task")
        if self.reconciliation and not self.external_request_id:
            raise CaseAgentWorkerBlocked("reconciliation claim requires an external request id")


@dataclass(frozen=True)
class ReapedAttemptResult:
    run_id: str
    task_id: str
    attempt_id: str
    event_version: int
    requires_reconciliation: bool


@dataclass(frozen=True)
class ReapedPlanningAttemptResult:
    run_id: str
    planning_attempt_id: str
    event_version: int
    requires_reconciliation: bool


@dataclass(frozen=True)
class DurableVerificationClaim:
    verification_attempt_id: str
    run_id: str
    event_version: int
    graph_hash: str
    snapshot_hash: str
    execution_actor_id: str
    verifier_actor_id: str
    state: AgentRunState

    def validate(self) -> None:
        _uuid(self.verification_attempt_id, "verification_attempt_id")
        _uuid(self.run_id, "verification run_id")
        _positive(self.event_version, "verification event_version")
        _sha256(self.graph_hash, "verification graph_hash")
        _sha256(self.snapshot_hash, "verification snapshot_hash")
        _uuid(self.execution_actor_id, "execution actor_id")
        _uuid(self.verifier_actor_id, "verifier actor_id")
        if self.execution_actor_id == self.verifier_actor_id:
            raise CaseAgentWorkerBlocked(
                "verification actor must differ from the execution actor"
            )
        if (
            self.state.run_id != self.run_id
            or self.state.event_version != self.event_version
            or self.state.graph is None
            or self.state.graph.graph_hash != self.graph_hash
            or self.state.snapshot.snapshot_hash != self.snapshot_hash
        ):
            raise CaseAgentWorkerBlocked(
                "verification claim differs from the durable Agent run"
            )


@dataclass(frozen=True)
class TaskAdapterOutcome:
    status: ResultStatus
    external_submission_state: ExternalSubmissionState
    output_hash: str | None
    error_code: str | None
    external_request_id: str | None
    runtime_seconds: int
    cost_minor_units: int
    external_calls: int
    artifacts: tuple[ArtifactReceipt, ...] = ()


class WorkerRunProjection(Protocol):
    state: AgentRunState
    next_commands: tuple[object, ...]
    checkpoint_verified: bool


class CaseAgentWorkerStore(Protocol):
    """Durable operations required by a worker process.

    Implementations must authorize the dedicated ``SYSTEM_WORKER`` principal,
    take a database lock and re-run the supervisor before every claim/write.
    """

    def read_projection(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> WorkerRunProjection: ...

    def apply_pending_snapshot_refresh(
        self, *, matter_id: str, actor: Actor, run_id: str
    ) -> int | None: ...

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
    ) -> DurablePlanningClaim: ...

    def heartbeat_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        planning_attempt_id: str,
        expected_attempt_version: int,
        lease_owner: str,
        extend_seconds: int,
    ) -> tuple[int, datetime]: ...

    def record_planning_failure(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurablePlanningClaim,
        expected_attempt_version: int,
        status: str,
        error_code: str,
    ) -> None: ...

    def accept_planning_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurablePlanningClaim,
        expected_attempt_version: int,
        idempotency_key: str,
        graph: object,
    ) -> int: ...

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
    ) -> DurablePlanningClaim: ...

    def claim_next_dispatchable_task(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> DurableTaskClaim: ...

    def claim_next_reconciliation(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> DurableTaskClaim: ...

    def heartbeat_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        attempt_id: str,
        expected_attempt_version: int,
        lease_owner: str,
        extend_seconds: int,
    ) -> tuple[int, datetime]: ...

    def record_external_submission_started(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        attempt_id: str,
        expected_attempt_version: int,
        external_request_id: str,
        destination: str,
        request_hash: str,
        approval_id: str,
    ) -> int: ...

    def record_receipt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
        receipt: TaskResultReceipt,
    ) -> object: ...

    def reap_expired_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        expected_event_version: int,
        idempotency_key: str,
    ) -> ReapedAttemptResult | None: ...

    def reap_expired_planning_attempt(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
    ) -> ReapedPlanningAttemptResult | None: ...


class CaseAgentVerificationStore(Protocol):
    """Dedicated verifier-principal persistence; never the execution writer."""

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
    ) -> DurableVerificationClaim: ...

    def record_verification_outcome(
        self,
        *,
        matter_id: str,
        actor: Actor,
        claim: DurableVerificationClaim,
        idempotency_key: str,
        receipt: RunVerificationReceipt,
    ) -> int: ...


class CaseWorkPlanPromotionPort(Protocol):
    """Server-only edge from a PASSED graph to a review-only work plan."""

    def promote_verified_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        run_id: str,
    ) -> object: ...


class CaseLedgerExtractionStagingPort(Protocol):
    """Server-only private landing path for PASSED extraction artifacts."""

    def discover_verified_artifact_ids(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        run_id: str,
    ) -> tuple[str, ...]: ...

    def stage_verified_artifact(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        run_id: str,
        artifact_id: str,
    ) -> object: ...


class CaseLedgerExceptionFollowupAutomationPort(Protocol):
    """Worker-only bridge into the exact 0049 lifecycle commands."""

    def bind_reextraction_task_for_claim(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        run_id: str,
        graph_id: str,
        task_id: str,
    ) -> object | None: ...

    def satisfy_reextraction_graph(
        self,
        *,
        matter_id: str,
        actor: Actor,
        run_id: str,
        graph_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> object | None: ...


class CasePlanningSnapshotProvider(Protocol):
    def build_for_run(
        self, *, state: AgentRunState, actor: Actor
    ) -> CasePlanningSnapshot: ...


class SemanticPlanner(Protocol):
    planner_id: str

    def plan(
        self,
        *,
        goal: object,
        snapshot: CasePlanningSnapshot,
        skills: tuple[PlannerSemanticSkill, ...],
        execution: DurablePlanningClaim,
    ) -> CasePlanProposal: ...


class CaseAgentTaskAdapter(Protocol):
    manifest: RuntimeAdapterManifest

    def execute(self, *, context: "TaskExecutionContext") -> TaskAdapterOutcome: ...

    def reconcile(self, *, context: "TaskExecutionContext") -> TaskAdapterOutcome: ...


class WorkerHealthStore(Protocol):
    def probe_case_agent_store(self, *, firm_id: str) -> bool: ...

    def record_worker_heartbeat(self, heartbeat: "AgentWorkerHeartbeat") -> None: ...

    def latest_worker_heartbeat(
        self, *, firm_id: str, worker_id: str
    ) -> "AgentWorkerHeartbeat | None": ...


@dataclass(frozen=True)
class AgentWorkerHeartbeat:
    worker_id: str
    actor_id: str
    firm_id: str
    planner_id: str
    adapter_catalog_hash: str
    verifier_actor_id: str
    verifier_id: str
    verifier_version: str
    verifier_policy_hash: str
    observed_at: datetime
    expires_at: datetime

    def validate(self) -> None:
        _code(self.worker_id, "worker_id")
        _uuid(self.actor_id, "worker actor_id")
        _uuid(self.firm_id, "worker firm_id")
        _code(self.planner_id, "planner_id")
        _sha256(self.adapter_catalog_hash, "adapter_catalog_hash")
        _uuid(self.verifier_actor_id, "verifier actor_id")
        _code(self.verifier_id, "verifier_id")
        if not isinstance(self.verifier_version, str) or re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?",
            self.verifier_version,
        ) is None:
            raise CaseAgentWorkerBlocked("verifier_version is invalid")
        _sha256(self.verifier_policy_hash, "verifier_policy_hash")
        if self.verifier_actor_id == self.actor_id:
            raise CaseAgentWorkerBlocked(
                "worker heartbeat cannot identify the execution actor as verifier"
            )
        _aware(self.observed_at, "worker observed_at")
        _aware(self.expires_at, "worker expires_at")
        if self.expires_at <= self.observed_at:
            raise CaseAgentWorkerBlocked("worker heartbeat expiry must follow observation")


@dataclass(frozen=True)
class AgentWorkerReadinessSnapshot:
    ready: bool
    planner_registered: bool
    adapter_registry_ready: bool
    independent_verifier_ready: bool
    store_reachable: bool
    heartbeat_fresh: bool
    reason_codes: tuple[str, ...]
    observed_at: datetime
    heartbeat_expires_at: datetime | None


class AgentWorkerReadinessProbe:
    """Prove runtime readiness; a control-plane table alone is not readiness."""

    def __init__(
        self,
        *,
        worker_id: str,
        actor: Actor,
        planner: SemanticPlanner | None,
        adapters: Mapping[str, CaseAgentTaskAdapter],
        health_store: WorkerHealthStore,
        verifier: CaseAgentRunVerifier | None = None,
        verifier_actor: Actor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _worker_actor(actor)
        _code(worker_id, "worker_id")
        self._worker_id = worker_id
        self._actor = actor
        self._planner = planner
        self._adapters = _validated_adapters(adapters, allow_empty=True)
        self._health_store = health_store
        if verifier_actor is not None:
            _worker_actor(verifier_actor)
            if verifier_actor.firm_id != actor.firm_id:
                raise ValueError("verifier actor must belong to the execution firm")
        self._verifier = verifier
        self._verifier_actor = verifier_actor
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def publish_heartbeat(self, *, ttl_seconds: int = 90) -> AgentWorkerHeartbeat:
        """Persist liveness only after all in-process runtime bindings validate."""

        if not 30 <= ttl_seconds <= 900:
            raise ValueError("worker heartbeat TTL must be between 30 and 900 seconds")
        if self._planner is None or not callable(getattr(self._planner, "plan", None)):
            raise CaseAgentWorkerBlocked("cannot publish readiness without a planner")
        planner_id = getattr(self._planner, "planner_id", "")
        _code(planner_id, "planner_id")
        if not self._adapters:
            raise CaseAgentWorkerBlocked("cannot publish readiness without task adapters")
        if not self._independent_verifier_ready():
            raise CaseAgentWorkerBlocked(
                "cannot publish readiness without an independent verifier"
            )
        if self._health_store.probe_case_agent_store(firm_id=self._actor.firm_id) is not True:
            raise CaseAgentWorkerBlocked("cannot publish readiness while the store is unavailable")
        observed_at = self._clock()
        _aware(observed_at, "worker heartbeat time")
        heartbeat = AgentWorkerHeartbeat(
            worker_id=self._worker_id,
            actor_id=self._actor.actor_id,
            firm_id=self._actor.firm_id,
            planner_id=planner_id,
            adapter_catalog_hash=_adapter_catalog_hash(self._adapters),
            verifier_actor_id=self._verifier_actor.actor_id,
            verifier_id=self._verifier.verifier_id,
            verifier_version=self._verifier.verifier_version,
            verifier_policy_hash=self._verifier.policy_hash,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl_seconds),
        )
        heartbeat.validate()
        self._health_store.record_worker_heartbeat(heartbeat)
        return heartbeat

    def snapshot(self) -> AgentWorkerReadinessSnapshot:
        observed_at = self._clock()
        _aware(observed_at, "readiness observation")
        planner_registered = bool(
            self._planner is not None
            and isinstance(getattr(self._planner, "planner_id", None), str)
            and getattr(self._planner, "planner_id").strip()
            and callable(getattr(self._planner, "plan", None))
        )
        adapter_registry_ready = bool(self._adapters)
        independent_verifier_ready = self._independent_verifier_ready()
        try:
            store_reachable = self._health_store.probe_case_agent_store(
                firm_id=self._actor.firm_id
            ) is True
        except Exception:
            store_reachable = False
        heartbeat: AgentWorkerHeartbeat | None = None
        if store_reachable:
            try:
                heartbeat = self._health_store.latest_worker_heartbeat(
                    firm_id=self._actor.firm_id,
                    worker_id=self._worker_id,
                )
            except Exception:
                store_reachable = False
        heartbeat_fresh = False
        expires_at: datetime | None = None
        if heartbeat is not None:
            try:
                heartbeat.validate()
                expires_at = heartbeat.expires_at
                heartbeat_fresh = (
                    heartbeat.worker_id == self._worker_id
                    and heartbeat.actor_id == self._actor.actor_id
                    and heartbeat.firm_id == self._actor.firm_id
                    and planner_registered
                    and heartbeat.planner_id == getattr(self._planner, "planner_id", "")
                    and heartbeat.adapter_catalog_hash == _adapter_catalog_hash(self._adapters)
                    and independent_verifier_ready
                    and heartbeat.verifier_actor_id
                    == self._verifier_actor.actor_id
                    and heartbeat.verifier_id == self._verifier.verifier_id
                    and heartbeat.verifier_version
                    == self._verifier.verifier_version
                    and heartbeat.verifier_policy_hash == self._verifier.policy_hash
                    and heartbeat.observed_at <= observed_at < heartbeat.expires_at
                )
            except CaseAgentWorkerBlocked:
                heartbeat_fresh = False
        reasons: list[str] = []
        if not planner_registered:
            reasons.append("PLANNER_NOT_REGISTERED")
        if not adapter_registry_ready:
            reasons.append("ADAPTER_REGISTRY_EMPTY")
        if not independent_verifier_ready:
            reasons.append("INDEPENDENT_VERIFIER_NOT_REGISTERED")
        if not store_reachable:
            reasons.append("STORE_UNREACHABLE")
        if not heartbeat_fresh:
            reasons.append("WORKER_HEARTBEAT_STALE")
        return AgentWorkerReadinessSnapshot(
            ready=not reasons,
            planner_registered=planner_registered,
            adapter_registry_ready=adapter_registry_ready,
            independent_verifier_ready=independent_verifier_ready,
            store_reachable=store_reachable,
            heartbeat_fresh=heartbeat_fresh,
            reason_codes=tuple(reasons),
            observed_at=observed_at,
            heartbeat_expires_at=expires_at,
        )

    def _independent_verifier_ready(self) -> bool:
        return bool(
            isinstance(self._verifier, CaseAgentRunVerifier)
            and self._verifier_actor is not None
            and self._verifier_actor.roles == frozenset({Role.SYSTEM_WORKER})
            and self._verifier_actor.firm_id == self._actor.firm_id
            and self._verifier_actor.actor_id != self._actor.actor_id
        )


class TaskExecutionContext:
    """Lease-bound adapter context with one exact external-boundary method."""

    def __init__(
        self,
        *,
        store: CaseAgentWorkerStore,
        matter_id: str,
        actor: Actor,
        claim: DurableTaskClaim,
        approval_id: str | None,
        lease_seconds: int,
    ) -> None:
        claim.validate()
        self._store = store
        self._matter_id = matter_id
        self._actor = actor
        self.claim = claim
        self.task = claim.task
        self.input_refs = claim.task.input_refs
        self._approval_id = approval_id
        self._lease_seconds = lease_seconds
        self._attempt_version = claim.attempt_version
        self._lock = RLock()
        self._lease_lost = False
        self._external_request_id = claim.external_request_id
        self._external_boundary_committed = bool(claim.reconciliation)

    @property
    def lease_lost(self) -> bool:
        with self._lock:
            return self._lease_lost

    @property
    def external_boundary_committed(self) -> bool:
        with self._lock:
            return self._external_boundary_committed

    @property
    def external_request_id(self) -> str | None:
        with self._lock:
            return self._external_request_id

    def heartbeat(self) -> None:
        with self._lock:
            if self._lease_lost:
                raise CaseAgentWorkerBlocked("task lease was lost")
            try:
                next_version, _ = self._store.heartbeat_attempt(
                    matter_id=self._matter_id,
                    actor=self._actor,
                    attempt_id=self.claim.attempt_id,
                    expected_attempt_version=self._attempt_version,
                    lease_owner=self.claim.lease_owner,
                    extend_seconds=self._lease_seconds,
                )
            except Exception as error:
                self._lease_lost = True
                raise CaseAgentWorkerBlocked("task lease heartbeat failed") from error
            if next_version != self._attempt_version + 1:
                self._lease_lost = True
                raise CaseAgentWorkerBlocked("task heartbeat returned a non-contiguous version")
            self._attempt_version = next_version

    def begin_external_submission(
        self,
        *,
        external_request_id: str,
        destination: str,
        request_hash: str,
    ) -> None:
        """Persist the boundary before the adapter sends any network byte."""

        with self._lock:
            if self.claim.reconciliation:
                raise CaseAgentWorkerBlocked("reconciliation cannot start a second submission")
            if self.task.capability.network_policy is not NetworkPolicy.EXACT_ALLOWLIST:
                raise CaseAgentWorkerBlocked("a local adapter cannot start an external submission")
            if self._approval_id is None:
                raise CaseAgentWorkerBlocked("external submission lacks exact lawyer approval")
            if self._external_boundary_committed:
                raise CaseAgentWorkerBlocked("external submission boundary was already committed")
            _uuid(external_request_id, "external_request_id")
            _sha256(request_hash, "external request_hash")
            normalized_destination = destination.strip().lower()
            if normalized_destination not in self.task.capability.allowed_domains:
                raise CaseAgentWorkerBlocked("external destination is outside the compiled allowlist")
            if self._lease_lost:
                raise CaseAgentWorkerBlocked("task lease was lost before external submission")
            self._attempt_version = self._store.record_external_submission_started(
                matter_id=self._matter_id,
                actor=self._actor,
                run_id=self.claim.run_id,
                attempt_id=self.claim.attempt_id,
                expected_attempt_version=self._attempt_version,
                external_request_id=external_request_id,
                destination=normalized_destination,
                request_hash=request_hash,
                approval_id=self._approval_id,
            )
            self._external_boundary_committed = True
            self._external_request_id = external_request_id


class CaseAgentWorker:
    """Run one recoverable supervisor command at a time."""

    def __init__(
        self,
        *,
        worker_id: str,
        actor: Actor,
        store: CaseAgentWorkerStore,
        snapshot_provider: CasePlanningSnapshotProvider,
        planner: SemanticPlanner,
        planner_compiler: CaseAgentPlannerCompiler,
        adapters: Mapping[str, CaseAgentTaskAdapter],
        verifier: CaseAgentRunVerifier | None = None,
        verifier_actor: Actor | None = None,
        verifier_store: CaseAgentVerificationStore | None = None,
        ledger_extraction_staging: CaseLedgerExtractionStagingPort | None = None,
        ledger_exception_followups: (
            CaseLedgerExceptionFollowupAutomationPort | None
        ) = None,
        work_plan_promotion: CaseWorkPlanPromotionPort | None = None,
        lease_seconds: int = 120,
        heartbeat_interval_seconds: int = 30,
    ) -> None:
        _code(worker_id, "worker_id")
        _worker_actor(actor)
        if not 30 <= lease_seconds <= 900:
            raise ValueError("worker lease must be between 30 and 900 seconds")
        if not 1 <= heartbeat_interval_seconds < lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        if not callable(getattr(planner, "plan", None)) or not getattr(
            planner, "planner_id", ""
        ):
            raise ValueError("a registered semantic planner is required")
        if not callable(getattr(store, "apply_pending_snapshot_refresh", None)):
            raise ValueError(
                "durable case-snapshot refresh store is required"
            )
        self._worker_id = worker_id
        self._actor = actor
        self._store = store
        self._snapshot_provider = snapshot_provider
        self._planner = planner
        self._compiler = planner_compiler
        self._adapters = _validated_adapters(adapters, allow_empty=False)
        configured_verification = (
            verifier is not None,
            verifier_actor is not None,
            verifier_store is not None,
        )
        if any(configured_verification) and not all(configured_verification):
            raise ValueError(
                "verifier, verifier actor and verifier store must be configured together"
            )
        if verifier_actor is not None:
            _worker_actor(verifier_actor)
            if verifier_actor.firm_id != actor.firm_id:
                raise ValueError("verifier actor must belong to the execution firm")
            if verifier_actor.actor_id == actor.actor_id:
                raise ValueError(
                    "the execution worker cannot serve as its own independent verifier"
                )
        self._verifier = verifier
        self._verifier_actor = verifier_actor
        self._verifier_store = verifier_store
        if ledger_extraction_staging is not None and not all(
            callable(getattr(ledger_extraction_staging, method, None))
            for method in (
                "discover_verified_artifact_ids",
                "stage_verified_artifact",
            )
        ):
            raise ValueError("verified ledger-extraction staging port is invalid")
        self._ledger_extraction_staging = ledger_extraction_staging
        if ledger_exception_followups is not None and not all(
            callable(getattr(ledger_exception_followups, method, None))
            for method in (
                "bind_reextraction_task_for_claim",
                "satisfy_reextraction_graph",
            )
        ):
            raise ValueError("ledger exception follow-up automation port is invalid")
        self._ledger_exception_followups = ledger_exception_followups
        if work_plan_promotion is not None and not callable(
            getattr(work_plan_promotion, "promote_verified_graph", None)
        ):
            raise ValueError("verified work-plan promotion port is invalid")
        self._work_plan_promotion = work_plan_promotion
        self._lease_seconds = lease_seconds
        self._heartbeat_interval = heartbeat_interval_seconds

    def heartbeat_record(
        self, *, observed_at: datetime | None = None, ttl_seconds: int = 90
    ) -> AgentWorkerHeartbeat:
        if not 30 <= ttl_seconds <= 900:
            raise ValueError("worker heartbeat TTL must be between 30 and 900 seconds")
        now = observed_at or datetime.now(timezone.utc)
        _aware(now, "worker heartbeat time")
        if self._verifier is None or self._verifier_actor is None:
            raise CaseAgentWorkerBlocked(
                "worker heartbeat requires an independent verifier binding"
            )
        return AgentWorkerHeartbeat(
            worker_id=self._worker_id,
            actor_id=self._actor.actor_id,
            firm_id=self._actor.firm_id,
            planner_id=self._planner.planner_id,
            adapter_catalog_hash=_adapter_catalog_hash(self._adapters),
            verifier_actor_id=self._verifier_actor.actor_id,
            verifier_id=self._verifier.verifier_id,
            verifier_version=self._verifier.verifier_version,
            verifier_policy_hash=self._verifier.policy_hash,
            observed_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    def process_run_once(self, *, matter_id: str, run_id: str) -> WorkerStepResult:
        """Recover expired work, then process at most one supervisor command."""

        projection = self._projection(matter_id=matter_id, run_id=run_id)
        state = projection.state
        if state.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.FAILED,
        }:
            return WorkerStepResult(
                run_id=run_id,
                step=AgentWorkerStep.IDLE,
                event_version=state.event_version,
            )

        refreshed_event_version = self._store.apply_pending_snapshot_refresh(
            matter_id=matter_id,
            actor=self._actor,
            run_id=run_id,
        )
        if refreshed_event_version is not None:
            if type(refreshed_event_version) is not int or refreshed_event_version < 1:
                raise CaseAgentWorkerBlocked(
                    "snapshot refresh returned an invalid Agent event version"
                )
            return WorkerStepResult(
                run_id=run_id,
                step=AgentWorkerStep.SNAPSHOT_REFRESHED,
                event_version=refreshed_event_version,
                reason_code="CASE_SNAPSHOT_CHANGED",
            )

        projection = self._projection(matter_id=matter_id, run_id=run_id)
        state = projection.state
        if state.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.FAILED,
        }:
            return WorkerStepResult(
                run_id=run_id,
                step=AgentWorkerStep.IDLE,
                event_version=state.event_version,
            )
        reaped = self._store.reap_expired_attempt(
            matter_id=matter_id,
            actor=self._actor,
            run_id=run_id,
            expected_event_version=state.event_version,
            idempotency_key=f"agent-reap:{run_id}:{state.event_version}",
        )
        if reaped is not None:
            if reaped.run_id != run_id:
                raise CaseAgentWorkerBlocked("reaper returned an attempt from another run")
            return WorkerStepResult(
                run_id=run_id,
                step=AgentWorkerStep.ATTEMPT_REAPED,
                event_version=reaped.event_version,
                task_id=reaped.task_id,
                attempt_id=reaped.attempt_id,
                reason_code=(
                    "EXTERNAL_RESULT_UNKNOWN"
                    if reaped.requires_reconciliation
                    else "LEASE_EXPIRED_BEFORE_EXTERNAL_SUBMISSION"
                ),
            )

        reaped_plan = self._store.reap_expired_planning_attempt(
            matter_id=matter_id,
            actor=self._actor,
            run_id=run_id,
        )
        if reaped_plan is not None:
            if reaped_plan.run_id != run_id:
                raise CaseAgentWorkerBlocked(
                    "planning reaper returned an attempt from another run"
                )
            return WorkerStepResult(
                run_id=run_id,
                step=(
                    AgentWorkerStep.PLANNING_RECONCILIATION_REQUIRED
                    if reaped_plan.requires_reconciliation
                    else AgentWorkerStep.PLANNING_FAILED
                ),
                event_version=reaped_plan.event_version,
                attempt_id=reaped_plan.planning_attempt_id,
                reason_code=(
                    "PLANNER_LEASE_EXPIRED_RESULT_UNKNOWN"
                    if reaped_plan.requires_reconciliation
                    else "PLANNER_LEASE_EXPIRED_BEFORE_SUBMISSION"
                ),
            )

        # Re-read after the reaper's database transaction, even when it found
        # nothing; another process may have advanced the event stream.
        projection = self._projection(matter_id=matter_id, run_id=run_id)
        state = projection.state
        commands = tuple(projection.next_commands)
        if not commands:
            if state.status is AgentRunStatus.PLANNING:
                return self._recover_inflight_plan(matter_id=matter_id, state=state)
            if any(task.status is AgentTaskStatus.RUNNING for task in state.tasks):
                step = AgentWorkerStep.WAITING_LEASE
            else:
                step = AgentWorkerStep.IDLE
            return WorkerStepResult(run_id, step, state.event_version)
        # Human approval must not unnecessarily block a different, already
        # authorized branch of the DAG.  Unknown external outcomes and stale
        # snapshots still have absolute priority over all new dispatches.
        priority = {
            SupervisorCommandKind.RECONCILE_EXTERNAL_RESULT: 0,
            SupervisorCommandKind.RECONCILE_PLAN_RESULT: 0,
            SupervisorCommandKind.REQUEST_REPLAN: 1,
            SupervisorCommandKind.REQUEST_PLAN: 1,
            SupervisorCommandKind.DISPATCH_TASK: 2,
            SupervisorCommandKind.START_VERIFICATION: 3,
            SupervisorCommandKind.REQUEST_APPROVAL: 4,
            SupervisorCommandKind.REQUEST_FINAL_REVIEW: 4,
            SupervisorCommandKind.COMPLETE_RUN: 5,
        }
        command = min(
            commands,
            key=lambda item: priority.get(getattr(item, "kind", None), 99),
        )
        kind = getattr(command, "kind", None)
        if kind in {
            SupervisorCommandKind.REQUEST_PLAN,
            SupervisorCommandKind.REQUEST_REPLAN,
        }:
            return self._plan(matter_id=matter_id, state=state)
        if kind is SupervisorCommandKind.DISPATCH_TASK:
            return self._dispatch(matter_id=matter_id, state=state)
        if kind is SupervisorCommandKind.RECONCILE_EXTERNAL_RESULT:
            return self._reconcile(matter_id=matter_id, state=state)
        if kind is SupervisorCommandKind.RECONCILE_PLAN_RESULT:
            return self._reconcile_plan(matter_id=matter_id, state=state)
        if kind is SupervisorCommandKind.REQUEST_FINAL_REVIEW:
            staged_ledger_candidate_count = 0
            if self._ledger_extraction_staging is not None:
                artifact_ids = self._ledger_extraction_staging.discover_verified_artifact_ids(
                    matter_id=matter_id,
                    actor=self._actor,
                    expected_version=state.snapshot.matter_version,
                    run_id=state.run_id,
                )
                if (
                    not isinstance(artifact_ids, tuple)
                    or any(not isinstance(value, str) for value in artifact_ids)
                    or tuple(sorted(artifact_ids)) != artifact_ids
                    or len(set(artifact_ids)) != len(artifact_ids)
                ):
                    raise CaseAgentWorkerBlocked(
                        "verified extraction discovery returned an invalid artifact set"
                    )
                for artifact_id in artifact_ids:
                    _uuid(artifact_id, "verified extraction artifact_id")
                    staged = self._ledger_extraction_staging.stage_verified_artifact(
                        matter_id=matter_id,
                        actor=self._actor,
                        expected_version=state.snapshot.matter_version,
                        idempotency_key=(
                            f"verified-ledger-extraction-stage:{state.run_id}:"
                            f"{artifact_id}"
                        ),
                        run_id=state.run_id,
                        artifact_id=artifact_id,
                    )
                    staged_count = getattr(
                        staged, "staged_candidate_count", None
                    )
                    if type(staged_count) is not int or not 0 <= staged_count <= 500:
                        raise CaseAgentWorkerBlocked(
                            "ledger extraction staging returned an invalid candidate count"
                        )
                    staged_ledger_candidate_count += staged_count
            if self._ledger_exception_followups is not None:
                if state.graph is None or state.verification_hash is None:
                    raise CaseAgentWorkerBlocked(
                        "re-extraction completion requires an exact verified graph"
                    )
                graph_id = state.graph.graph_id
                try:
                    reextraction_receipt = (
                        self._ledger_exception_followups.satisfy_reextraction_graph(
                            matter_id=matter_id,
                            actor=self._actor,
                            run_id=state.run_id,
                            graph_id=graph_id,
                            expected_version=state.snapshot.matter_version,
                            idempotency_key=(
                                "ledger-reextract-set."
                                f"{uuid5(UUID(graph_id), _reextraction_set_intent(state))}"
                            ),
                        )
                    )
                except CaseAgentWorkerBlocked:
                    raise
                except Exception as error:
                    raise CaseAgentWorkerBlocked(
                        "re-extraction graph completion failed closed"
                    ) from error
                if reextraction_receipt is not None:
                    followup_count = getattr(
                        reextraction_receipt, "followup_count", None
                    )
                    matter_version = getattr(
                        reextraction_receipt, "matter_version", None
                    )
                    if (
                        type(followup_count) is not int
                        or not 1 <= followup_count <= 500
                        or matter_version != state.snapshot.matter_version + 1
                    ):
                        raise CaseAgentWorkerBlocked(
                            "re-extraction graph completion receipt differs"
                        )
                    # The set command already advanced the matter exactly
                    # once and emitted the snapshot-refresh outbox event.
                    # Never promote the now-stale graph or close a second
                    # subset under the old version in this Worker step.
                    return WorkerStepResult(
                        run_id,
                        AgentWorkerStep.WAITING_HUMAN,
                        state.event_version,
                        reason_code=(
                            "LEDGER_EXCEPTION_REEXTRACTION_SET_SATISFIED"
                        ),
                    )
            # A work-plan candidate derived from the pre-confirmation ledger
            # would become stale as soon as the lawyer confirms an extracted
            # fact/transaction.  Stop here.  0046 wakes this same run only
            # after every low-risk/exception record has a terminal decision;
            # CASE_SNAPSHOT_CHANGED then drives a fresh REPLAN on the new
            # ledger before 0043 may run.
            if staged_ledger_candidate_count:
                return WorkerStepResult(
                    run_id,
                    AgentWorkerStep.WAITING_HUMAN,
                    state.event_version,
                    reason_code="LEDGER_EXTRACTION_REVIEW_REQUIRED",
                )
            if (
                self._work_plan_promotion is not None
                and state.goal.active_plan_execution is None
            ):
                if state.graph is None or state.verification_hash is None:
                    raise CaseAgentWorkerBlocked(
                        "final review cannot start before exact graph verification"
                    )
                self._work_plan_promotion.promote_verified_graph(
                    matter_id=matter_id,
                    actor=self._actor,
                    expected_version=state.snapshot.matter_version,
                    idempotency_key=(
                        f"verified-agent-graph-work-plan:{state.run_id}:"
                        f"{state.verification_hash}"
                    ),
                    run_id=state.run_id,
                )
            return WorkerStepResult(
                run_id,
                AgentWorkerStep.WAITING_HUMAN,
                state.event_version,
                reason_code=getattr(command, "reason_code", None),
            )
        if kind is SupervisorCommandKind.REQUEST_APPROVAL:
            return WorkerStepResult(
                run_id, AgentWorkerStep.WAITING_HUMAN, state.event_version,
                task_id=getattr(command, "task_id", None),
                reason_code=getattr(command, "reason_code", None),
            )
        if kind is SupervisorCommandKind.START_VERIFICATION:
            if (
                self._verifier is None
                or self._verifier_actor is None
                or self._verifier_store is None
            ):
                return WorkerStepResult(
                    run_id,
                    AgentWorkerStep.VERIFICATION_REQUIRED,
                    state.event_version,
                    reason_code="INDEPENDENT_VERIFIER_NOT_REGISTERED",
                )
            return self._verify(matter_id=matter_id, state=state)
        raise CaseAgentWorkerBlocked("supervisor produced an unsupported worker command")

    def _verify(self, *, matter_id: str, state: AgentRunState) -> WorkerStepResult:
        assert self._verifier is not None
        assert self._verifier_actor is not None
        assert self._verifier_store is not None
        claim = self._verifier_store.start_verification(
            matter_id=matter_id,
            actor=self._verifier_actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            idempotency_key=(
                f"agent-verification-start:{state.run_id}:"
                f"{state.graph.graph_hash if state.graph else 'no-graph'}"
            ),
            execution_actor_id=self._actor.actor_id,
            verifier_id=self._verifier.verifier_id,
            verifier_version=self._verifier.verifier_version,
            policy_hash=self._verifier.policy_hash,
        )
        claim.validate()
        if (
            claim.run_id != state.run_id
            or claim.state.firm_id != self._actor.firm_id
            or claim.state.matter_id != matter_id
            or claim.execution_actor_id != self._actor.actor_id
            or claim.verifier_actor_id != self._verifier_actor.actor_id
        ):
            raise CaseAgentWorkerBlocked(
                "verification claim is outside this firm, run or principal pair"
            )
        try:
            receipt = self._verifier.verify(
                verification_attempt_id=claim.verification_attempt_id,
                state=claim.state,
                verifier_actor_id=self._verifier_actor.actor_id,
                execution_actor_id=self._actor.actor_id,
            )
        except CaseAgentVerificationIndeterminate:
            # STARTED remains durable.  A restarted verifier may re-read the
            # same server-owned artifacts under the same attempt; no terminal
            # state is guessed from an infrastructure exception.
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.VERIFICATION_REQUIRED,
                claim.event_version,
                reason_code="VERIFICATION_RESULT_INDETERMINATE",
            )
        receipt.validate()
        event_version = self._verifier_store.record_verification_outcome(
            matter_id=matter_id,
            actor=self._verifier_actor,
            claim=claim,
            idempotency_key=(
                f"agent-verification-outcome:{claim.verification_attempt_id}:"
                f"{receipt.verification_hash}"
            ),
            receipt=receipt,
        )
        return WorkerStepResult(
            state.run_id,
            (
                AgentWorkerStep.VERIFICATION_PASSED
                if receipt.outcome is VerificationOutcome.PASSED
                else AgentWorkerStep.VERIFICATION_FAILED
            ),
            event_version,
            reason_code=receipt.error_code,
        )

    def _plan(self, *, matter_id: str, state: AgentRunState) -> WorkerStepResult:
        snapshot = self._snapshot_provider.build_for_run(
            state=state, actor=self._actor
        )
        snapshot.validate()
        if snapshot.case_snapshot != state.snapshot:
            raise CaseAgentWorkerBlocked(
                "planning projection is not bound to the current case snapshot"
            )
        claim = self._store.claim_next_planning_command(
            matter_id=matter_id,
            actor=self._actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            planning_hash=snapshot.planning_hash,
            idempotency_key=f"agent-plan-claim:{state.run_id}:{state.event_version}",
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        claim.validate()
        if (
            claim.run_id != state.run_id
            or claim.state.firm_id != self._actor.firm_id
            or claim.state.matter_id != matter_id
        ):
            raise CaseAgentWorkerBlocked("planning claim is outside this firm or run")
        planning_context = _PlanningLeaseContext(
            store=self._store,
            matter_id=matter_id,
            actor=self._actor,
            claim=claim,
            lease_seconds=self._lease_seconds,
        )
        try:
            if (
                snapshot.case_snapshot != claim.state.snapshot
                or snapshot.planning_hash != claim.planning_hash
            ):
                raise CaseAgentWorkerBlocked(
                    "planning projection is not bound to the claimed case snapshot"
                )
            # Keep the database lease live during model latency.  Heartbeats
            # advance the attempt version, while the immutable external ledger
            # is fenced by the separate lease token, so the two cursors cannot
            # race or authorize an old process.
            with _HeartbeatPump(
                planning_context.heartbeat, self._heartbeat_interval
            ) as planning_pump:
                proposal = self._planner.plan(
                    goal=claim.state.goal,
                    snapshot=snapshot,
                    skills=self._compiler.semantic_skill_catalog(),
                    execution=claim,
                )
            # A guarded planner returns only after its exact lease token and
            # result are durable.  The pump can observe the intentional
            # CLAIMED -> SUCCEEDED transition between that commit and planner
            # return, so its final heartbeat error is not independently fatal:
            # the concrete accept path re-reads the exact durable token and
            # proposal.  Do not heartbeat the now-terminal attempt; crash
            # recovery can compile it without a second provider request.
            graph = self._compiler.compile(
                graph_id=claim.graph_id,
                graph_version=claim.graph_version,
                goal=claim.state.goal,
                snapshot=snapshot,
                proposal=proposal,
                run_budget=remaining_planning_budget(claim.state),
            )
            event_version = self._store.accept_planning_graph(
                matter_id=matter_id,
                actor=self._actor,
                claim=claim,
                expected_attempt_version=planning_context.attempt_version,
                idempotency_key=f"agent-plan-accept:{claim.planning_attempt_id}",
                graph=graph,
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNED,
                event_version,
                reason_code=claim.command_kind.value,
            )
        except DeepSeekPlannerUnknownSubmission:
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="UNKNOWN",
                error_code="PLANNER_EXTERNAL_RESULT_UNKNOWN",
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNING_RECONCILIATION_REQUIRED,
                claim.event_version,
                reason_code="PLANNER_EXTERNAL_RESULT_UNKNOWN",
            )
        except DeepSeekPlannerPreDispatchFailure as error:
            # The transport proved that no HTTP request byte was sent. Keep
            # the durable reason precise so a later re-dispatch is a new,
            # auditable decision rather than a hidden retry.
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="FAILED",
                error_code=error.error_code,
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNING_FAILED,
                claim.event_version,
                reason_code=error.error_code,
            )
        except CasePlannerAdmissionBlocked as error:
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="FAILED",
                error_code=error.error_code,
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNING_FAILED,
                claim.event_version,
                reason_code=error.error_code,
            )
        except CasePlannerBudgetExceeded:
            code = _planning_budget_failure_code(claim.state)
            self._record_planning_failure(matter_id=matter_id, claim=claim,
                attempt_version=planning_context.attempt_version, status="FAILED", error_code=code)
            return WorkerStepResult(state.run_id, AgentWorkerStep.PLANNING_FAILED,
                                    claim.event_version, reason_code=code)
        except (DeepSeekPlannerRejected, CasePlannerBlocked):
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="FAILED",
                error_code="PLANNER_PROPOSAL_REJECTED",
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNING_FAILED,
                claim.event_version,
                reason_code="PLANNER_PROPOSAL_REJECTED",
            )
        except CaseAgentWorkerBlocked:
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="FAILED",
                error_code="PLANNING_CONTROL_BOUNDARY_BLOCKED",
            )
            raise
        except Exception as error:
            # An unexpected planner exception may occur after transport.  It is
            # therefore uncertain, never a retryable known failure.
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=planning_context.attempt_version,
                status="UNKNOWN",
                error_code="PLANNER_OUTCOME_UNCERTAIN",
            )
            raise CaseAgentWorkerBlocked(
                "planner outcome is uncertain and requires reconciliation"
            ) from error

    def _recover_inflight_plan(
        self, *, matter_id: str, state: AgentRunState
    ) -> WorkerStepResult:
        """Resume compilation after a provider result was stored before a crash."""

        claim = self._store.recover_planning_attempt(
            matter_id=matter_id,
            actor=self._actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            idempotency_key=f"agent-plan-recover:{state.run_id}:{state.event_version}",
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        return self._compile_recovered_plan(
            matter_id=matter_id,
            state=state,
            claim=claim,
            reconciled=False,
        )

    def _dispatch(self, *, matter_id: str, state: AgentRunState) -> WorkerStepResult:
        claim = self._store.claim_next_dispatchable_task(
            matter_id=matter_id,
            actor=self._actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            idempotency_key=f"agent-task-claim:{state.run_id}:{state.event_version}",
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        claim.validate()
        if claim.run_id != state.run_id:
            raise CaseAgentWorkerBlocked("dispatch claim is outside this run")
        if claim.reconciliation:
            raise CaseAgentWorkerBlocked("dispatch claim was marked as reconciliation")
        self._bind_reextraction_claim(
            matter_id=matter_id, state=state, claim=claim
        )
        return self._execute_claim(matter_id=matter_id, claim=claim, reconcile=False)

    def _bind_reextraction_claim(
        self,
        *,
        matter_id: str,
        state: AgentRunState,
        claim: DurableTaskClaim,
    ) -> None:
        if not _is_exact_ledger_extraction_task(claim.task):
            return
        # Do not consume an obligation binding for an adapter that cannot
        # execute the immutable compiled contract in this process.
        self._adapter_for(claim.task)
        if self._ledger_exception_followups is None:
            return
        if state.graph is None:
            raise CaseAgentWorkerBlocked(
                "re-extraction task has no current accepted graph"
            )
        matching_specs = tuple(
            task
            for task in state.graph.tasks
            if task.task_id == claim.task_id and task == claim.task
        )
        if len(matching_specs) != 1:
            raise CaseAgentWorkerBlocked(
                "re-extraction claim differs from the current accepted graph"
            )
        try:
            receipt = (
                self._ledger_exception_followups.bind_reextraction_task_for_claim(
                    matter_id=matter_id,
                    actor=self._actor,
                    expected_version=state.snapshot.matter_version,
                    run_id=state.run_id,
                    graph_id=state.graph.graph_id,
                    task_id=claim.task_id,
                )
            )
        except CaseAgentWorkerBlocked:
            raise
        except Exception as error:
            raise CaseAgentWorkerBlocked(
                "re-extraction task binding failed closed"
            ) from error
        if receipt is None:
            return
        task_binding_id = getattr(receipt, "task_binding_id", None)
        matter_version = getattr(receipt, "matter_version", None)
        _uuid(task_binding_id, "re-extraction task_binding_id")
        if matter_version != state.snapshot.matter_version:
            raise CaseAgentWorkerBlocked(
                "re-extraction task binding receipt differs"
            )

    def _reconcile(self, *, matter_id: str, state: AgentRunState) -> WorkerStepResult:
        claim = self._store.claim_next_reconciliation(
            matter_id=matter_id,
            actor=self._actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            idempotency_key=f"agent-reconcile-claim:{state.run_id}:{state.event_version}",
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        claim.validate()
        if claim.run_id != state.run_id:
            raise CaseAgentWorkerBlocked("reconciliation claim is outside this run")
        if not claim.reconciliation:
            raise CaseAgentWorkerBlocked("reconciliation store returned a dispatch claim")
        try:
            return self._execute_claim(
                matter_id=matter_id, claim=claim, reconcile=True
            )
        except CaseAgentReconciliationUnavailable as error:
            # Preserve the original UNKNOWN receipt and external boundary.
            # The runtime will audit and quiet this run; it must not poll or
            # submit another provider request.
            return WorkerStepResult(
                run_id=state.run_id,
                step=AgentWorkerStep.RECONCILIATION_DEFERRED,
                event_version=state.event_version,
                task_id=claim.task_id,
                attempt_id=claim.attempt_id,
                reason_code=error.reason_code,
            )

    def _reconcile_plan(
        self, *, matter_id: str, state: AgentRunState
    ) -> WorkerStepResult:
        """Recover a provider outcome without sending a second plan request.

        The store may return only a durably recovered structured proposal.  A
        provider-specific reconciliation connector must have populated it
        under the original external request id before this method runs.
        """

        claim = self._store.recover_planning_attempt(
            matter_id=matter_id,
            actor=self._actor,
            run_id=state.run_id,
            expected_event_version=state.event_version,
            idempotency_key=f"agent-plan-reconcile:{state.run_id}:{state.event_version}",
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        return self._compile_recovered_plan(
            matter_id=matter_id,
            state=state,
            claim=claim,
            reconciled=True,
        )

    def _compile_recovered_plan(
        self,
        *,
        matter_id: str,
        state: AgentRunState,
        claim: DurablePlanningClaim,
        reconciled: bool,
    ) -> WorkerStepResult:
        claim.validate()
        if (
            claim.run_id != state.run_id
            or claim.state.firm_id != self._actor.firm_id
            or claim.state.matter_id != matter_id
        ):
            raise CaseAgentWorkerBlocked(
                "planning reconciliation claim is outside this firm or run"
            )
        proposal = claim.recovered_proposal
        if not isinstance(proposal, CasePlanProposal):
            raise CaseAgentWorkerBlocked(
                "planning reconciliation has no validated structured proposal"
            )
        snapshot = self._snapshot_provider.build_for_run(
            state=claim.state, actor=self._actor
        )
        snapshot.validate()
        if snapshot.case_snapshot != claim.state.snapshot:
            raise CaseAgentWorkerBlocked(
                "recovered plan is not bound to the current case snapshot"
            )
        if snapshot.planning_hash != claim.planning_hash:
            raise CaseAgentWorkerBlocked(
                "recovered plan is bound to an obsolete authorized planning projection"
            )
        try:
            graph = self._compiler.compile(
                graph_id=claim.graph_id,
                graph_version=claim.graph_version,
                goal=claim.state.goal,
                snapshot=snapshot,
                proposal=proposal,
                run_budget=remaining_planning_budget(claim.state),
            )
        except CasePlannerBudgetExceeded:
            code = _planning_budget_failure_code(claim.state)
            self._record_planning_failure(matter_id=matter_id, claim=claim,
                attempt_version=claim.attempt_version, status="FAILED", error_code=code)
            return WorkerStepResult(state.run_id, AgentWorkerStep.PLANNING_FAILED,
                                    claim.event_version, reason_code=code)
        except CasePlannerAdmissionBlocked as error:
            self._record_planning_failure(
                matter_id=matter_id,
                claim=claim,
                attempt_version=claim.attempt_version,
                status="FAILED",
                error_code=error.error_code,
            )
            return WorkerStepResult(
                state.run_id,
                AgentWorkerStep.PLANNING_FAILED,
                claim.event_version,
                reason_code=error.error_code,
            )
        event_version = self._store.accept_planning_graph(
            matter_id=matter_id,
            actor=self._actor,
            claim=claim,
            expected_attempt_version=claim.attempt_version,
            idempotency_key=f"agent-plan-reconcile-accept:{claim.planning_attempt_id}",
            graph=graph,
        )
        return WorkerStepResult(
            state.run_id,
            (
                AgentWorkerStep.PLANNING_RECONCILED
                if reconciled
                else AgentWorkerStep.PLANNED
            ),
            event_version,
            reason_code=(
                "PLANNER_PROVIDER_RESULT_RECONCILED"
                if reconciled
                else "PLANNER_RESULT_RECOVERED_AFTER_RESTART"
            ),
        )

    def _execute_claim(
        self, *, matter_id: str, claim: DurableTaskClaim, reconcile: bool
    ) -> WorkerStepResult:
        adapter = self._adapter_for(claim.task)
        approval_id = self._approval_id(
            matter_id=matter_id, run_id=claim.run_id, task=claim.task
        )
        context = TaskExecutionContext(
            store=self._store,
            matter_id=matter_id,
            actor=self._actor,
            claim=claim,
            approval_id=approval_id,
            lease_seconds=self._lease_seconds,
        )
        try:
            with _HeartbeatPump(context.heartbeat, self._heartbeat_interval) as pump:
                outcome = (
                    adapter.reconcile(context=context)
                    if reconcile
                    else adapter.execute(context=context)
                )
            pump.raise_if_failed()
            # Prove the lease is still ours immediately before accepting an
            # adapter outcome.  This closes the gap where a very short task
            # finishes before the periodic heartbeat, or the lease changes
            # between adapter return and receipt persistence.
            context.heartbeat()
        except CaseAgentWorkerBlocked:
            raise
        except CaseAgentKnownExternalFailure as error:
            if (
                claim.task.capability.network_policy
                is not NetworkPolicy.EXACT_ALLOWLIST
                or not context.external_boundary_committed
                or error.external_request_id != context.external_request_id
            ):
                raise CaseAgentWorkerBlocked(
                    "known external failure differs from its durable boundary"
                ) from error
            outcome = TaskAdapterOutcome(
                status=ResultStatus.FAILED,
                external_submission_state=ExternalSubmissionState.SUBMITTED,
                output_hash=None,
                error_code=error.error_code,
                external_request_id=error.external_request_id,
                runtime_seconds=0,
                cost_minor_units=0,
                external_calls=1,
            )
        except Exception as error:
            if claim.task.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST:
                if context.external_boundary_committed:
                    outcome = TaskAdapterOutcome(
                        status=ResultStatus.UNKNOWN,
                        external_submission_state=ExternalSubmissionState.UNKNOWN,
                        output_hash=None,
                        error_code=None,
                        external_request_id=context.external_request_id,
                        runtime_seconds=0,
                        cost_minor_units=0,
                        external_calls=1,
                    )
                else:
                    outcome = TaskAdapterOutcome(
                        status=ResultStatus.FAILED,
                        external_submission_state=ExternalSubmissionState.NOT_SUBMITTED,
                        output_hash=None,
                        error_code="ADAPTER_FAILED_BEFORE_SUBMISSION",
                        external_request_id=str(uuid5(UUID(claim.attempt_id), "not-submitted")),
                        runtime_seconds=0,
                        cost_minor_units=0,
                        external_calls=0,
                    )
            else:
                outcome = TaskAdapterOutcome(
                    status=ResultStatus.FAILED,
                    external_submission_state=ExternalSubmissionState.NOT_APPLICABLE,
                    output_hash=None,
                    error_code="LOCAL_ADAPTER_FAILED",
                    external_request_id=None,
                    runtime_seconds=0,
                    cost_minor_units=0,
                    external_calls=0,
                )
            # The exception text is deliberately not persisted; it can contain
            # case material or secrets.
            # Report only code locations, never exception text, local variables,
            # document contents or provider bodies.
            locations = tuple(
                (frame.name, frame.lineno)
                for frame in traceback.extract_tb(error.__traceback__)
            )
            logging.getLogger(__name__).warning(
                "Agent adapter failure class=%s locations=%s",
                type(error).__name__, locations,
            )
        if context.lease_lost:
            raise CaseAgentWorkerBlocked("adapter result was discarded after lease loss")
        self._validate_outcome(claim=claim, context=context, outcome=outcome, reconcile=reconcile)
        receipt = self._receipt(claim=claim, outcome=outcome)
        latest = self._projection(matter_id=matter_id, run_id=claim.run_id)
        runtime = next(
            (item for item in latest.state.tasks if item.spec.task_id == claim.task_id),
            None,
        )
        if (
            runtime is None
            or runtime.active_attempt_id != claim.attempt_id
            or runtime.status not in {AgentTaskStatus.RUNNING, AgentTaskStatus.UNKNOWN}
        ):
            raise CaseAgentWorkerBlocked("task attempt changed before its result was recorded")
        self._store.record_receipt(
            matter_id=matter_id,
            actor=self._actor,
            run_id=claim.run_id,
            expected_event_version=latest.state.event_version,
            idempotency_key=f"agent-task-receipt:{claim.attempt_id}:{receipt.receipt_id}",
            receipt=receipt,
        )
        if outcome.status is ResultStatus.SUCCEEDED:
            step = AgentWorkerStep.TASK_SUCCEEDED
        elif outcome.status is ResultStatus.UNKNOWN:
            step = AgentWorkerStep.TASK_RECONCILIATION_REQUIRED
        else:
            step = AgentWorkerStep.TASK_FAILED
        return WorkerStepResult(
            claim.run_id,
            step,
            latest.state.event_version + 1,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            reason_code=outcome.error_code,
        )

    def _adapter_for(self, task: AgentTaskSpec) -> CaseAgentTaskAdapter:
        adapter = self._adapters.get(task.skill.tool_id)
        if adapter is None:
            raise CaseAgentWorkerBlocked("compiled task adapter is not registered in this worker")
        manifest = adapter.manifest
        if (
            manifest.tool_id != task.skill.tool_id
            or manifest.adapter_id != task.skill.adapter_id
            or manifest.adapter_version != task.skill.adapter_version
            or manifest.execution_mode is not task.capability.execution_mode
            or manifest.sandbox_policy_version != task.capability.sandbox_policy_version
            or manifest.sandbox_policy_hash != task.capability.sandbox_policy_hash
        ):
            raise CaseAgentWorkerBlocked("runtime adapter differs from the compiled task binding")
        if task.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST:
            if not manifest.network_capable or not manifest.supports_reconciliation:
                raise CaseAgentWorkerBlocked("network adapter lacks reconciliation support")
        elif manifest.network_capable:
            raise CaseAgentWorkerBlocked("local task was bound to a network-capable adapter")
        return adapter

    def _approval_id(
        self, *, matter_id: str, run_id: str, task: AgentTaskSpec
    ) -> str | None:
        if task.capability.network_policy is NetworkPolicy.DENY:
            return None
        state = self._projection(matter_id=matter_id, run_id=run_id).state
        if state.graph is None:
            raise CaseAgentWorkerBlocked("network task has no current graph")
        matches = tuple(
            approval
            for approval in state.approvals
            if approval.task_id == task.task_id
            and approval.task_input_hash == task.input_hash
            and approval.graph_hash == state.graph.graph_hash
            and approval.gate == task.approval_gate
        )
        if len(matches) != 1:
            raise CaseAgentWorkerBlocked("network task lacks one exact current approval")
        return matches[0].approval_id

    def _projection(self, *, matter_id: str, run_id: str) -> WorkerRunProjection:
        projection = self._store.read_projection(
            matter_id=matter_id, actor=self._actor, run_id=run_id
        )
        if not projection.checkpoint_verified:
            raise CaseAgentWorkerBlocked("Agent checkpoint differs from event replay")
        if projection.state.run_id != run_id or projection.state.matter_id != matter_id:
            raise CaseAgentWorkerBlocked("Agent projection is outside the requested run")
        return projection

    def _record_planning_failure(
        self,
        *,
        matter_id: str,
        claim: DurablePlanningClaim,
        attempt_version: int,
        status: str,
        error_code: str,
    ) -> None:
        try:
            self._store.record_planning_failure(
                matter_id=matter_id,
                actor=self._actor,
                claim=claim,
                expected_attempt_version=attempt_version,
                status=status,
                error_code=error_code,
            )
        except Exception as error:
            raise CaseAgentWorkerBlocked(
                "planning outcome could not be durably recorded"
            ) from error

    @staticmethod
    def _validate_outcome(
        *,
        claim: DurableTaskClaim,
        context: TaskExecutionContext,
        outcome: TaskAdapterOutcome,
        reconcile: bool,
    ) -> None:
        if not isinstance(outcome, TaskAdapterOutcome):
            raise CaseAgentWorkerBlocked("adapter returned an invalid outcome")
        if reconcile and outcome.status is ResultStatus.UNKNOWN:
            raise CaseAgentWorkerBlocked("reconciliation cannot return another unknown result")
        network = claim.task.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST
        if network:
            # The generic 0031 boundary precedes the exact 0045 ledger
            # exchange. If 0045 never commits, send_raw was unreachable even
            # though the generic boundary exists. Preserve that narrower fact
            # instead of turning a known zero-call failure back into UNKNOWN.
            known_pre_dispatch_failure = (
                context.external_boundary_committed
                and outcome.status is ResultStatus.FAILED
                and outcome.external_submission_state
                is ExternalSubmissionState.NOT_SUBMITTED
                and outcome.external_calls == 0
                and (
                    (
                        _is_exact_ledger_extraction_task(claim.task)
                        and outcome.error_code
                        in {
                            "LEDGER_EXCHANGE_NOT_CREATED",
                            "LEDGER_PROVIDER_DNS_FAILED",
                            "LEDGER_PROVIDER_CONNECT_FAILED",
                        }
                    )
                    or (
                        _is_exact_lawyer_analysis_task(claim.task)
                        and outcome.error_code
                        in {
                            "LAWYER_ANALYSIS_BINDING_REJECTED",
                            "LAWYER_ANALYSIS_BINDING_UNAVAILABLE",
                            "LAWYER_ANALYSIS_EXCHANGE_NOT_CREATED",
                            "LAWYER_ANALYSIS_DNS_FAILED",
                            "LAWYER_ANALYSIS_CONNECT_FAILED",
                        }
                    )
                )
            )
            if outcome.status is ResultStatus.SUCCEEDED and not context.external_boundary_committed:
                raise CaseAgentWorkerBlocked("network success lacks a persisted submission boundary")
            if context.external_boundary_committed and (
                outcome.external_request_id != context.external_request_id
                or (
                    outcome.external_submission_state
                    is ExternalSubmissionState.NOT_SUBMITTED
                    and not known_pre_dispatch_failure
                )
            ):
                raise CaseAgentWorkerBlocked("network outcome differs from its external boundary")
            if outcome.status is ResultStatus.UNKNOWN and (
                not context.external_boundary_committed
                or outcome.external_submission_state is not ExternalSubmissionState.UNKNOWN
            ):
                raise CaseAgentWorkerBlocked("unknown network result lacks an uncertain boundary")
        elif (
            outcome.external_submission_state is not ExternalSubmissionState.NOT_APPLICABLE
            or outcome.external_request_id is not None
        ):
            raise CaseAgentWorkerBlocked("local adapter returned external-request fields")

    @staticmethod
    def _receipt(
        *, claim: DurableTaskClaim, outcome: TaskAdapterOutcome
    ) -> TaskResultReceipt:
        digest = _canonical_hash(
            {
                "attempt_id": claim.attempt_id,
                "task_id": claim.task_id,
                "input_hash": claim.task.input_hash,
                "adapter_id": claim.task.skill.adapter_id,
                "adapter_version": claim.task.skill.adapter_version,
                "status": outcome.status.value,
                "external_submission_state": outcome.external_submission_state.value,
                "output_hash": outcome.output_hash,
                "error_code": outcome.error_code,
                "external_request_id": outcome.external_request_id,
                "runtime_seconds": outcome.runtime_seconds,
                "cost_minor_units": outcome.cost_minor_units,
                "external_calls": outcome.external_calls,
                "artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "artifact_kind": artifact.artifact_kind,
                        "content_hash": artifact.content_hash,
                        "byte_size": artifact.byte_size,
                        "source_input_hash": artifact.source_input_hash,
                        "managed_derivative": artifact.managed_derivative,
                    }
                    for artifact in outcome.artifacts
                ],
            }
        )
        receipt_id = str(uuid5(UUID(claim.attempt_id), digest))
        return TaskResultReceipt(
            receipt_id=receipt_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            input_hash=claim.task.input_hash,
            adapter_id=claim.task.skill.adapter_id,
            adapter_version=claim.task.skill.adapter_version,
            status=outcome.status,
            external_submission_state=outcome.external_submission_state,
            output_hash=outcome.output_hash,
            error_code=outcome.error_code,
            external_request_id=outcome.external_request_id,
            runtime_seconds=outcome.runtime_seconds,
            cost_minor_units=outcome.cost_minor_units,
            external_calls=outcome.external_calls,
            artifacts=outcome.artifacts,
        )


class _PlanningLeaseContext:
    def __init__(
        self,
        *,
        store: CaseAgentWorkerStore,
        matter_id: str,
        actor: Actor,
        claim: DurablePlanningClaim,
        lease_seconds: int,
    ) -> None:
        self._store = store
        self._matter_id = matter_id
        self._actor = actor
        self._claim = claim
        self._lease_seconds = lease_seconds
        self._attempt_version = claim.attempt_version
        self._lock = RLock()

    @property
    def attempt_version(self) -> int:
        with self._lock:
            return self._attempt_version

    def heartbeat(self) -> None:
        with self._lock:
            next_version, _ = self._store.heartbeat_planning_attempt(
                matter_id=self._matter_id,
                actor=self._actor,
                planning_attempt_id=self._claim.planning_attempt_id,
                expected_attempt_version=self._attempt_version,
                lease_owner=self._claim.lease_owner,
                extend_seconds=self._lease_seconds,
            )
            if next_version != self._attempt_version + 1:
                raise CaseAgentWorkerBlocked(
                    "planning heartbeat returned a non-contiguous version"
                )
            self._attempt_version = next_version


class _HeartbeatPump:
    """Refresh a durable lease independently from adapter/provider code."""

    def __init__(self, heartbeat: Callable[[], None], interval_seconds: int) -> None:
        self._heartbeat = heartbeat
        self._interval = interval_seconds
        self._stop = Event()
        self._error: Exception | None = None
        self._thread = Thread(target=self._run, name="case-agent-lease-heartbeat", daemon=True)

    def __enter__(self) -> "_HeartbeatPump":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> bool:
        self._stop.set()
        self._thread.join(timeout=min(5, self._interval + 1))
        return False

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._heartbeat()
            except Exception as error:
                self._error = error
                self._stop.set()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise CaseAgentWorkerBlocked("durable work lease heartbeat failed") from self._error


def _validated_adapters(
    adapters: Mapping[str, CaseAgentTaskAdapter], *, allow_empty: bool
) -> dict[str, CaseAgentTaskAdapter]:
    values = dict(adapters)
    if not values and not allow_empty:
        raise ValueError("worker adapter registry cannot be empty")
    for tool_id, adapter in values.items():
        _code(tool_id, "adapter registry tool_id")
        manifest = getattr(adapter, "manifest", None)
        if not isinstance(manifest, RuntimeAdapterManifest):
            raise ValueError("worker adapter requires an exact runtime manifest")
        manifest.validate()
        if manifest.tool_id != tool_id:
            raise ValueError("worker adapter registry key differs from its manifest")
        if not callable(getattr(adapter, "execute", None)):
            raise ValueError("worker adapter lacks its bounded execute method")
        if manifest.supports_reconciliation and not callable(
            getattr(adapter, "reconcile", None)
        ):
            raise ValueError("reconcilable adapter lacks a reconcile method")
    return values


def _adapter_catalog_hash(adapters: Mapping[str, CaseAgentTaskAdapter]) -> str:
    return _canonical_hash(
        [
            {
                "tool_id": tool_id,
                "adapter_id": adapter.manifest.adapter_id,
                "adapter_version": adapter.manifest.adapter_version,
                "execution_mode": adapter.manifest.execution_mode.value,
                "sandbox_policy_version": adapter.manifest.sandbox_policy_version,
                "sandbox_policy_hash": adapter.manifest.sandbox_policy_hash,
                "network_capable": adapter.manifest.network_capable,
                "supports_reconciliation": adapter.manifest.supports_reconciliation,
            }
            for tool_id, adapter in sorted(adapters.items())
        ]
    )


def _reextraction_set_intent(state: AgentRunState) -> str:
    if state.graph is None:
        raise CaseAgentWorkerBlocked(
            "re-extraction completion requires a current graph"
        )
    return "\n".join(
        (
            state.matter_id,
            str(state.snapshot.matter_version),
            state.run_id,
            state.graph.graph_id,
        )
    )


def _is_exact_ledger_extraction_task(task: AgentTaskSpec) -> bool:
    return (
        task.skill.skill_id == "case_ledger_extraction"
        and task.skill.skill_version == "1.0.0"
        and task.skill.tool_id == "extract_case_ledger"
        and task.skill.tool_version == "1.0.0"
    )


def _is_exact_lawyer_analysis_task(task: AgentTaskSpec) -> bool:
    return (
        task.skill.skill_id == "lawyer_decision_package"
        and task.skill.skill_version == "1.0.0"
        and task.skill.tool_id == "analyze_lawyer_decision_package"
        and task.skill.tool_version == "1.0.0"
    )


def _worker_actor(actor: Actor) -> None:
    if actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise PermissionError("case Agent worker requires a dedicated SYSTEM_WORKER identity")
    _uuid(actor.actor_id, "worker actor_id")
    _uuid(actor.firm_id, "worker firm_id")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _uuid(value: object, label: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise CaseAgentWorkerBlocked(f"{label} must be a UUID") from error


def _sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CaseAgentWorkerBlocked(f"{label} must be a SHA-256 digest")


def _code(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 200
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value) is None
    ):
        raise CaseAgentWorkerBlocked(f"{label} is invalid")


def _positive(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CaseAgentWorkerBlocked(f"{label} must be positive")


def _aware(value: object, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CaseAgentWorkerBlocked(f"{label} must be timezone-aware")


__all__ = [
    "AgentWorkerHeartbeat",
    "AgentWorkerReadinessProbe",
    "AgentWorkerReadinessSnapshot",
    "AgentWorkerStep",
    "CaseAgentTaskAdapter",
    "CaseAgentWorker",
    "CaseAgentWorkerBlocked",
    "CaseAgentReconciliationUnavailable",
    "CaseAgentWorkerStore",
    "CaseAgentVerificationStore",
    "CaseLedgerExceptionFollowupAutomationPort",
    "CaseLedgerExtractionStagingPort",
    "CaseWorkPlanPromotionPort",
    "CasePlanningSnapshotProvider",
    "DurablePlanningClaim",
    "DurableTaskClaim",
    "DurableVerificationClaim",
    "ReapedAttemptResult",
    "ReapedPlanningAttemptResult",
    "SemanticPlanner",
    "TaskAdapterOutcome",
    "TaskExecutionContext",
    "WorkerHealthStore",
    "WorkerStepResult",
]
