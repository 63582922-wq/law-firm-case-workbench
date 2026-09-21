"""Bounded Web Agent workflow for page-level material review candidates.

The model is an untrusted classifier.  It receives only a server-built page
projection and returns one candidate for every supplied evidence page.  The
state machine owns run lifecycle, idempotency and optimistic concurrency; a
successful model response can only move a run to ``NEEDS_REVIEW``.  It never
creates a formal fact, page disposition, duplicate resolution, transaction or
monetary calculation.

Production persistence implements :class:`AgentMaterialRunStore`.  The
in-memory implementation in this module is intentionally limited to unit tests
and development composition, but exercises the same command contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from threading import RLock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import Actor, Role


MATERIAL_AGENT_SCHEMA_VERSION = "web-agent-material-review-v1"
MATERIAL_AGENT_PLAN_VERSION = "material-review-plan-v1"
MAX_AGENT_PAGES_PER_RUN = 64
MAX_AGENT_TEXT_BYTES_PER_RUN = 1536 * 1024


class AgentMaterialReviewBlocked(ValueError):
    """The Agent command or untrusted model result violates the contract."""


class AgentProviderRejected(RuntimeError):
    """The provider returned a known terminal rejection before a valid result."""


class AgentProviderUnknownSubmission(RuntimeError):
    """The provider submission may have occurred and must not be retried."""


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class AgentTaskStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class AgentTaskKind(StrEnum):
    MATERIAL_READING = "MATERIAL_READING"
    PAGE_CLASSIFICATION = "PAGE_CLASSIFICATION"
    RELEVANT_PAGE_EXTRACTION = "RELEVANT_PAGE_EXTRACTION"
    EXCEPTION_ROUTING = "EXCEPTION_ROUTING"


class PageCandidateKind(StrEnum):
    RELEVANT_PAGE = "RELEVANT_PAGE"
    UNRELATED_PAGE = "UNRELATED_PAGE"
    OCR_REQUIRED = "OCR_REQUIRED"
    DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
    UNCERTAIN = "UNCERTAIN"


class ReviewPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AgentCandidateStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"


class AgentIntent(StrEnum):
    MATERIAL_NEUTRAL_REVIEW = "MATERIAL_NEUTRAL_REVIEW"
    MATERIAL_TO_PLAINTIFF_LEDGER = "MATERIAL_TO_PLAINTIFF_LEDGER"
    MATERIAL_TO_DEFENSE_LEDGER = "MATERIAL_TO_DEFENSE_LEDGER"


@dataclass(frozen=True)
class AgentPlanContext:
    intent: AgentIntent
    representation_profile_version: int | None
    representation_profile_hash: str | None

    @classmethod
    def neutral(
        cls,
        *,
        representation_profile_version: int | None = None,
        representation_profile_hash: str | None = None,
    ) -> "AgentPlanContext":
        return cls(
            intent=AgentIntent.MATERIAL_NEUTRAL_REVIEW,
            representation_profile_version=representation_profile_version,
            representation_profile_hash=representation_profile_hash,
        )


class CandidateReasonCode(StrEnum):
    PARTY_NAME_MATCH = "PARTY_NAME_MATCH"
    TRANSACTION_ENTRY = "TRANSACTION_ENTRY"
    COURT_DOCUMENT = "COURT_DOCUMENT"
    LOAN_DOCUMENT = "LOAN_DOCUMENT"
    TARGET_ALIAS_MATCH = "TARGET_ALIAS_MATCH"
    NO_CASE_SIGNAL = "NO_CASE_SIGNAL"
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    CONFLICTING_CONTEXT = "CONFLICTING_CONTEXT"
    INSTRUCTION_LIKE_TEXT = "INSTRUCTION_LIKE_TEXT"


class AgentFailureCode(StrEnum):
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    PROVIDER_RESULT_UNKNOWN = "PROVIDER_RESULT_UNKNOWN"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"


@dataclass(frozen=True)
class AgentEvidencePageProjection:
    """Server-built minimum projection of one registered original page."""

    evidence_page_id: str
    source_file_sha256: str
    page_number: int
    extracted_text: str
    extracted_text_sha256: str

    @classmethod
    def build(
        cls,
        *,
        evidence_page_id: str,
        source_file_sha256: str,
        page_number: int,
        extracted_text: str,
    ) -> "AgentEvidencePageProjection":
        if not isinstance(extracted_text, str):
            raise AgentMaterialReviewBlocked("page text must be server-decoded text")
        encoded = extracted_text.encode("utf-8")
        return cls(
            evidence_page_id=evidence_page_id,
            source_file_sha256=source_file_sha256,
            page_number=page_number,
            extracted_text=extracted_text,
            extracted_text_sha256=sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class AgentEvidencePageBinding:
    evidence_page_id: str
    source_file_sha256: str
    page_number: int
    extracted_text_sha256: str


@dataclass(frozen=True)
class AgentMaterialAnalysisRequest:
    matter_id: str
    matter_version: int
    input_hash: str
    pages: tuple[AgentEvidencePageProjection, ...]
    # Server-side authorization metadata.  It is deliberately excluded from
    # the model body and the evidence-content input hash.
    external_request_id: str | None = None
    run_id: str | None = None
    claim_lease_id: str | None = None
    plan_context: AgentPlanContext = AgentPlanContext.neutral()
    external_ledger_version: int | None = None


@dataclass(frozen=True)
class AgentMaterialTask:
    task_id: str
    sequence: int
    kind: AgentTaskKind
    status: AgentTaskStatus


@dataclass(frozen=True)
class ModelPageCandidate:
    evidence_page_id: str
    source_file_sha256: str
    page_number: int
    kind: PageCandidateKind
    confidence: float
    review_priority: ReviewPriority
    reason_codes: tuple[CandidateReasonCode, ...]
    supporting_excerpt: str
    duplicate_of_page_id: str | None


@dataclass(frozen=True)
class AgentPageCandidate:
    candidate_id: str
    matter_id: str
    evidence_page_id: str
    source_file_sha256: str
    page_number: int
    kind: PageCandidateKind
    confidence: float
    review_priority: ReviewPriority
    reason_codes: tuple[CandidateReasonCode, ...]
    supporting_excerpt: str
    duplicate_of_page_id: str | None
    input_hash: str
    status: AgentCandidateStatus = AgentCandidateStatus.NEEDS_REVIEW


@dataclass(frozen=True)
class AgentMaterialRunSnapshot:
    run_id: str
    firm_id: str
    matter_id: str
    requested_by: str
    matter_version: int
    input_hash: str
    request_hash: str
    page_bindings: tuple[AgentEvidencePageBinding, ...]
    run_version: int
    status: AgentRunStatus
    tasks: tuple[AgentMaterialTask, ...]
    candidates: tuple[AgentPageCandidate, ...]
    output_hash: str | None
    failure_code: AgentFailureCode | None
    created_at: datetime
    updated_at: datetime
    external_request_id: str | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    plan_context: AgentPlanContext = AgentPlanContext.neutral()


class AgentMaterialAnalysisProvider(Protocol):
    """Return raw untrusted JSON for one already-authorised page projection."""

    def analyze_materials(self, request: AgentMaterialAnalysisRequest) -> str | bytes: ...


class AgentEvidenceProjectionSource(Protocol):
    """Load registered evidence pages; browser text/hashes are never accepted."""

    def load_pages(
        self,
        *,
        actor: Actor,
        matter_id: str,
        evidence_page_ids: tuple[str, ...],
    ) -> tuple[AgentEvidencePageProjection, ...]: ...


class AgentMaterialRunStore(Protocol):
    """Persistence boundary for Agent lifecycle commands.

    A PostgreSQL implementation must enforce the same firm/matter scope,
    idempotency and version checks transactionally.  It must not store model
    credentials or silently recover a ``RUNNING`` request by resubmitting it.
    """

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
    ) -> AgentMaterialRunSnapshot: ...

    def claim_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> AgentMaterialRunSnapshot: ...

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
    ) -> AgentMaterialRunSnapshot: ...

    def bind_external_request(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        authorized_matter_version: int,
        external_request_id: str,
        idempotency_key: str,
    ) -> AgentMaterialRunSnapshot: ...

    def save_candidates(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        candidates: tuple[AgentPageCandidate, ...],
        output_hash: str,
    ) -> AgentMaterialRunSnapshot: ...

    def fail_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        failure_code: AgentFailureCode,
    ) -> AgentMaterialRunSnapshot: ...

    def get_run(self, *, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot: ...


class InMemoryAgentMaterialRunStore:
    """Thread-safe contract implementation for tests and local development."""

    _QUEUE_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )
    _READ_ROLES = _QUEUE_ROLES | frozenset({Role.SYSTEM_WORKER})

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._runs: dict[str, AgentMaterialRunSnapshot] = {}
        self._commands: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._lock = RLock()

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
        _require_actor(actor, self._QUEUE_ROLES)
        _require_identifier(matter_id, "matter_id")
        _require_positive_int(matter_version, "matter_version")
        _require_sha256(input_hash, "input_hash")
        _require_sha256(request_hash, "request_hash")
        _validate_plan_context(plan_context, require_profile=False)
        _validate_page_bindings(matter_id, matter_version, input_hash, page_bindings)
        _require_idempotency_key(idempotency_key)
        command_hash = _canonical_hash(
            {
                "command": "CREATE_AGENT_MATERIAL_RUN",
                "firm_id": actor.firm_id,
                "matter_id": matter_id,
                "matter_version": matter_version,
                "input_hash": input_hash,
                "request_hash": request_hash,
                "page_bindings": [_binding_payload(item) for item in page_bindings],
                "plan_context": _plan_context_payload(plan_context),
            }
        )
        command_key = (actor.actor_id, "CREATE_AGENT_MATERIAL_RUN", idempotency_key)
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            now = _aware_time(self._clock())
            run_id = self._id_factory()
            _require_identifier(run_id, "run_id")
            tasks = tuple(
                AgentMaterialTask(
                    task_id=self._id_factory(),
                    sequence=sequence,
                    kind=kind,
                    status=AgentTaskStatus.QUEUED,
                )
                for sequence, kind in enumerate(_fixed_task_plan(), start=1)
            )
            snapshot = AgentMaterialRunSnapshot(
                run_id=run_id,
                firm_id=actor.firm_id,
                matter_id=matter_id,
                requested_by=actor.actor_id,
                matter_version=matter_version,
                input_hash=input_hash,
                request_hash=request_hash,
                page_bindings=tuple(page_bindings),
                run_version=1,
                status=AgentRunStatus.QUEUED,
                tasks=tasks,
                candidates=(),
                output_hash=None,
                failure_code=None,
                created_at=now,
                updated_at=now,
                plan_context=plan_context,
            )
            self._runs[run_id] = snapshot
            self._commands[command_key] = (command_hash, run_id)
            return snapshot

    def claim_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        lease_seconds: int = 120,
    ) -> AgentMaterialRunSnapshot:
        _require_actor(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        if not 30 <= lease_seconds <= 300:
            raise AgentMaterialReviewBlocked("Agent claim lease must be between 30 and 300 seconds")
        command_hash = _canonical_hash(
            {
                "command": "CLAIM_AGENT_MATERIAL_RUN",
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "lease_seconds": lease_seconds,
            }
        )
        command_key = (actor.actor_id, "CLAIM_AGENT_MATERIAL_RUN", idempotency_key)
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            current = self._scoped_run(actor, run_id)
            _require_run_version(current, expected_run_version)
            now = _aware_time(self._clock())
            reclaimable = (
                current.status is AgentRunStatus.CLAIMED
                and current.lease_expires_at is not None
                and current.lease_expires_at <= now
            )
            if current.status is not AgentRunStatus.QUEUED and not reclaimable:
                raise AgentMaterialReviewBlocked(
                    "only a queued or expired claimed Agent run can be claimed"
                )
            if current.external_request_id is None:
                raise AgentMaterialReviewBlocked(
                    "Agent run requires an exact lawyer-authorized external request before execution"
                )
            _validate_plan_context(current.plan_context, require_profile=True)
            if current.attempt_count >= 3:
                raise AgentMaterialReviewBlocked("Agent claim attempt limit is exhausted")
            lease_id = self._id_factory()
            _require_identifier(lease_id, "lease_id")
            updated = replace(
                current,
                run_version=current.run_version + 1,
                status=AgentRunStatus.CLAIMED,
                lease_id=lease_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=current.attempt_count + 1,
                updated_at=now,
            )
            self._runs[run_id] = updated
            self._commands[command_key] = (command_hash, run_id)
            return updated

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
        _require_actor(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_identifier(lease_id, "lease_id")
        _require_identifier(external_request_id, "external_request_id")
        _require_sha256(request_hash, "request_hash")
        _require_idempotency_key(idempotency_key)
        command_hash = _canonical_hash(
            {
                "command": "MARK_AGENT_MATERIAL_SUBMISSION_STARTED",
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "lease_id": lease_id,
                "external_request_id": external_request_id,
                "request_hash": request_hash,
            }
        )
        command_key = (
            actor.actor_id,
            "MARK_AGENT_MATERIAL_SUBMISSION_STARTED",
            idempotency_key,
        )
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            current = self._scoped_run(actor, run_id)
            _require_run_version(current, expected_run_version)
            if (
                current.status is not AgentRunStatus.CLAIMED
                or current.lease_id != lease_id
                or current.external_request_id != external_request_id
            ):
                raise AgentMaterialReviewBlocked(
                    "Agent submission start does not match the active authorized claim"
                )
            tasks = tuple(replace(task, status=AgentTaskStatus.RUNNING) for task in current.tasks)
            updated = replace(
                current,
                run_version=current.run_version + 1,
                status=AgentRunStatus.RUNNING,
                tasks=tasks,
                lease_id=None,
                lease_expires_at=None,
                updated_at=_aware_time(self._clock()),
            )
            self._runs[run_id] = updated
            self._commands[command_key] = (command_hash, run_id)
            return updated

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
        _require_actor(actor, frozenset({Role.LEAD_LAWYER, Role.REVIEWER}))
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_positive_int(authorized_matter_version, "authorized_matter_version")
        _require_identifier(external_request_id, "external_request_id")
        _require_idempotency_key(idempotency_key)
        command_hash = _canonical_hash(
            {
                "command": "BIND_AGENT_MATERIAL_EXTERNAL_REQUEST",
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "authorized_matter_version": authorized_matter_version,
                "external_request_id": external_request_id,
            }
        )
        command_key = (
            actor.actor_id,
            "BIND_AGENT_MATERIAL_EXTERNAL_REQUEST",
            idempotency_key,
        )
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            current = self._scoped_run(actor, run_id)
            _require_run_version(current, expected_run_version)
            if current.status is not AgentRunStatus.QUEUED or current.external_request_id is not None:
                raise AgentMaterialReviewBlocked(
                    "only an unbound queued Agent run can bind an external request"
                )
            _validate_plan_context(current.plan_context, require_profile=True)
            if authorized_matter_version != current.matter_version + 1:
                raise AgentMaterialReviewBlocked(
                    "external authorization must be the next exact matter version after Agent queueing"
                )
            updated = replace(
                current,
                matter_version=authorized_matter_version,
                external_request_id=external_request_id,
                run_version=current.run_version + 1,
                updated_at=_aware_time(self._clock()),
            )
            self._runs[run_id] = updated
            self._commands[command_key] = (command_hash, run_id)
            return updated

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
        _require_actor(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        _require_sha256(output_hash, "output_hash")
        candidate_hash = agent_candidate_output_hash(candidates)
        if candidate_hash != output_hash:
            raise AgentMaterialReviewBlocked("candidate output hash does not match candidate content")
        command_hash = _canonical_hash(
            {
                "command": "SAVE_AGENT_MATERIAL_CANDIDATES",
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "output_hash": output_hash,
            }
        )
        command_key = (actor.actor_id, "SAVE_AGENT_MATERIAL_CANDIDATES", idempotency_key)
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            current = self._scoped_run(actor, run_id)
            _require_run_version(current, expected_run_version)
            if current.status is not AgentRunStatus.RUNNING:
                raise AgentMaterialReviewBlocked("only a running Agent run can publish candidates")
            _validate_persisted_candidates(current, candidates)
            tasks = tuple(
                replace(
                    task,
                    status=(
                        AgentTaskStatus.NEEDS_REVIEW
                        if task.kind is AgentTaskKind.EXCEPTION_ROUTING
                        else AgentTaskStatus.COMPLETED
                    ),
                )
                for task in current.tasks
            )
            updated = replace(
                current,
                run_version=current.run_version + 1,
                status=AgentRunStatus.NEEDS_REVIEW,
                tasks=tasks,
                candidates=tuple(candidates),
                output_hash=output_hash,
                failure_code=None,
                updated_at=_aware_time(self._clock()),
            )
            self._runs[run_id] = updated
            self._commands[command_key] = (command_hash, run_id)
            return updated

    def fail_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
        failure_code: AgentFailureCode,
    ) -> AgentMaterialRunSnapshot:
        _require_actor(actor, frozenset({Role.SYSTEM_WORKER}))
        _require_positive_int(expected_run_version, "expected_run_version")
        _require_idempotency_key(idempotency_key)
        if not isinstance(failure_code, AgentFailureCode):
            raise AgentMaterialReviewBlocked("failure code is invalid")
        command_hash = _canonical_hash(
            {
                "command": "FAIL_AGENT_MATERIAL_RUN",
                "run_id": run_id,
                "expected_run_version": expected_run_version,
                "failure_code": failure_code.value,
            }
        )
        command_key = (actor.actor_id, "FAIL_AGENT_MATERIAL_RUN", idempotency_key)
        with self._lock:
            replay = self._replay(command_key, command_hash)
            if replay is not None:
                return replay
            current = self._scoped_run(actor, run_id)
            _require_run_version(current, expected_run_version)
            if current.status not in {AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING}:
                raise AgentMaterialReviewBlocked("only a claimed or running Agent run can fail")
            tasks = tuple(replace(task, status=AgentTaskStatus.FAILED) for task in current.tasks)
            updated = replace(
                current,
                run_version=current.run_version + 1,
                status=AgentRunStatus.FAILED,
                tasks=tasks,
                candidates=(),
                output_hash=None,
                failure_code=failure_code,
                lease_id=None,
                lease_expires_at=None,
                updated_at=_aware_time(self._clock()),
            )
            self._runs[run_id] = updated
            self._commands[command_key] = (command_hash, run_id)
            return updated

    def get_run(self, *, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot:
        _require_actor(actor, self._READ_ROLES)
        with self._lock:
            return self._scoped_run(actor, run_id)

    def _scoped_run(self, actor: Actor, run_id: str) -> AgentMaterialRunSnapshot:
        _require_identifier(run_id, "run_id")
        current = self._runs.get(run_id)
        if current is None or current.firm_id != actor.firm_id:
            raise KeyError(run_id)
        return current

    def _replay(
        self,
        command_key: tuple[str, str, str],
        command_hash: str,
    ) -> AgentMaterialRunSnapshot | None:
        prior = self._commands.get(command_key)
        if prior is None:
            return None
        prior_hash, run_id = prior
        if prior_hash != command_hash:
            raise AgentMaterialReviewBlocked("idempotency key was already used for another Agent command")
        return self._runs[run_id]


class WebMaterialAgentCoordinator:
    """Run the fixed material-review plan through an injected provider/store."""

    def __init__(
        self,
        *,
        store: AgentMaterialRunStore,
        provider: AgentMaterialAnalysisProvider,
        page_source: AgentEvidenceProjectionSource,
    ) -> None:
        self._store = store
        self._provider = provider
        self._page_source = page_source

    def queue_run(
        self,
        *,
        actor: Actor,
        matter_id: str,
        matter_version: int,
        idempotency_key: str,
        evidence_page_ids: Sequence[str],
        plan_context: AgentPlanContext,
    ) -> AgentMaterialRunSnapshot:
        page_ids = _normalize_page_ids(evidence_page_ids)
        pages = self._page_source.load_pages(
            actor=actor,
            matter_id=matter_id,
            evidence_page_ids=page_ids,
        )
        _require_source_returned_exact_pages(page_ids, pages)
        request = build_material_analysis_request(
            matter_id=matter_id,
            matter_version=matter_version,
            pages=pages,
            plan_context=plan_context,
        )
        request_hash = _canonical_hash(
            {
                "plan_version": MATERIAL_AGENT_PLAN_VERSION,
                "matter_id": request.matter_id,
                "matter_version": request.matter_version,
                "input_hash": request.input_hash,
                "task_plan": [item.value for item in _fixed_task_plan()],
                "plan_context": _plan_context_payload(request.plan_context),
            }
        )
        return self._store.create_run(
            actor=actor,
            matter_id=request.matter_id,
            matter_version=request.matter_version,
            input_hash=request.input_hash,
            request_hash=request_hash,
            page_bindings=_request_page_bindings(request),
            plan_context=request.plan_context,
            idempotency_key=idempotency_key,
        )

    def execute_run(
        self,
        *,
        actor: Actor,
        run_id: str,
        expected_run_version: int,
        idempotency_key: str,
    ) -> AgentMaterialRunSnapshot:
        current = self._store.get_run(actor=actor, run_id=run_id)
        page_ids = tuple(binding.evidence_page_id for binding in current.page_bindings)
        pages = self._page_source.load_pages(
            actor=actor,
            matter_id=current.matter_id,
            evidence_page_ids=page_ids,
        )
        _require_source_returned_exact_pages(page_ids, pages)
        request = build_material_analysis_request(
            matter_id=current.matter_id,
            matter_version=current.matter_version,
            pages=pages,
            external_request_id=current.external_request_id,
            run_id=current.run_id,
            claim_lease_id=current.lease_id,
            plan_context=current.plan_context,
            external_ledger_version=current.matter_version,
        )
        if (
            current.matter_id != request.matter_id
            or current.matter_version != request.matter_version
            or current.input_hash != request.input_hash
        ):
            raise AgentMaterialReviewBlocked("Agent run input no longer matches its queued evidence snapshot")
        if request.external_request_id is None:
            raise AgentMaterialReviewBlocked(
                "Agent run is not bound to an exact lawyer-authorized external request"
            )
        # A persisted RUNNING status means submission may already have
        # happened.  Returning it for reconciliation is safer than a silent
        # second provider call.  Terminal states are idempotent reads too.
        if current.status not in {AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED}:
            return current
        if current.status is AgentRunStatus.CLAIMED:
            now = datetime.now(timezone.utc)
            if current.lease_expires_at is None or current.lease_expires_at > now:
                return current
        claimed = self._store.claim_run(
            actor=actor,
            run_id=run_id,
            expected_run_version=expected_run_version,
            idempotency_key=idempotency_key,
        )
        if claimed.status is not AgentRunStatus.CLAIMED:
            return claimed
        request = replace(
            request,
            run_id=claimed.run_id,
            claim_lease_id=claimed.lease_id,
        )
        try:
            raw = self._provider.analyze_materials(request)
            started = self._store.get_run(actor=actor, run_id=run_id)
            if started.status is not AgentRunStatus.RUNNING:
                raise AgentProviderUnknownSubmission(
                    "provider returned without an exact submission-started run transition"
                )
            model_candidates = parse_material_candidate_response(raw, request=request)
        except AgentProviderUnknownSubmission:
            return self._fail_started_run(
                actor=actor,
                started=self._store.get_run(actor=actor, run_id=run_id),
                idempotency_key=idempotency_key,
                failure_code=AgentFailureCode.PROVIDER_RESULT_UNKNOWN,
            )
        except AgentProviderRejected:
            return self._fail_started_run(
                actor=actor,
                started=self._store.get_run(actor=actor, run_id=run_id),
                idempotency_key=idempotency_key,
                failure_code=AgentFailureCode.PROVIDER_REJECTED,
            )
        except AgentMaterialReviewBlocked:
            return self._fail_started_run(
                actor=actor,
                started=self._store.get_run(actor=actor, run_id=run_id),
                idempotency_key=idempotency_key,
                failure_code=AgentFailureCode.MODEL_RESPONSE_INVALID,
            )
        except Exception:
            return self._fail_started_run(
                actor=actor,
                started=self._store.get_run(actor=actor, run_id=run_id),
                idempotency_key=idempotency_key,
                failure_code=AgentFailureCode.INTERNAL_FAILURE,
            )
        candidates = _materialize_candidates(
            run_id=started.run_id,
            matter_id=started.matter_id,
            input_hash=started.input_hash,
            candidates=model_candidates,
        )
        output_hash = agent_candidate_output_hash(candidates)
        # Persistence failure is intentionally not translated into a fake
        # model failure.  The run remains RUNNING and requires reconciliation.
        return self._store.save_candidates(
            actor=actor,
            run_id=run_id,
            expected_run_version=started.run_version,
            idempotency_key=_outcome_idempotency_key(idempotency_key),
            candidates=candidates,
            output_hash=output_hash,
        )

    def _fail_started_run(
        self,
        *,
        actor: Actor,
        started: AgentMaterialRunSnapshot,
        idempotency_key: str,
        failure_code: AgentFailureCode,
    ) -> AgentMaterialRunSnapshot:
        return self._store.fail_run(
            actor=actor,
            run_id=started.run_id,
            expected_run_version=started.run_version,
            idempotency_key=_outcome_idempotency_key(idempotency_key),
            failure_code=failure_code,
        )

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
        return self._store.bind_external_request(
            actor=actor,
            run_id=run_id,
            expected_run_version=expected_run_version,
            authorized_matter_version=authorized_matter_version,
            external_request_id=external_request_id,
            idempotency_key=idempotency_key,
        )


def build_material_analysis_request(
    *,
    matter_id: str,
    matter_version: int,
    pages: Sequence[AgentEvidencePageProjection],
    external_request_id: str | None = None,
    run_id: str | None = None,
    claim_lease_id: str | None = None,
    plan_context: AgentPlanContext | None = None,
    external_ledger_version: int | None = None,
) -> AgentMaterialAnalysisRequest:
    _require_identifier(matter_id, "matter_id")
    _require_positive_int(matter_version, "matter_version")
    normalized_context = plan_context or AgentPlanContext.neutral()
    _validate_plan_context(normalized_context, require_profile=False)
    normalized = tuple(pages)
    if not 1 <= len(normalized) <= MAX_AGENT_PAGES_PER_RUN:
        raise AgentMaterialReviewBlocked(
            f"one Agent material run must contain 1 to {MAX_AGENT_PAGES_PER_RUN} pages"
        )
    page_ids: set[str] = set()
    page_locations: set[tuple[str, int]] = set()
    total_text_bytes = 0
    for page in normalized:
        if not isinstance(page, AgentEvidencePageProjection):
            raise AgentMaterialReviewBlocked("Agent page projection type is invalid")
        _require_identifier(page.evidence_page_id, "evidence_page_id")
        _require_sha256(page.source_file_sha256, "source_file_sha256")
        _require_positive_int(page.page_number, "page_number")
        if not isinstance(page.extracted_text, str):
            raise AgentMaterialReviewBlocked("page extracted text is invalid")
        encoded = page.extracted_text.encode("utf-8")
        if len(encoded) > 24 * 1024:
            raise AgentMaterialReviewBlocked("one page text projection exceeds the Agent boundary")
        total_text_bytes += len(encoded)
        expected_text_hash = sha256(encoded).hexdigest()
        if page.extracted_text_sha256 != expected_text_hash:
            raise AgentMaterialReviewBlocked("page text hash does not match the page projection")
        if page.evidence_page_id in page_ids:
            raise AgentMaterialReviewBlocked("Agent page projection contains duplicate page ids")
        location = (page.source_file_sha256, page.page_number)
        if location in page_locations:
            raise AgentMaterialReviewBlocked("Agent page projection contains duplicate source locations")
        page_ids.add(page.evidence_page_id)
        page_locations.add(location)
    if total_text_bytes > MAX_AGENT_TEXT_BYTES_PER_RUN:
        raise AgentMaterialReviewBlocked("Agent page text projection exceeds the run boundary")
    if external_request_id is not None:
        _require_identifier(external_request_id, "external_request_id")
    if run_id is not None:
        _require_identifier(run_id, "run_id")
    if claim_lease_id is not None:
        _require_identifier(claim_lease_id, "claim_lease_id")
    if external_ledger_version is not None:
        _require_positive_int(external_ledger_version, "external_ledger_version")
    ordered = tuple(sorted(normalized, key=lambda page: (page.source_file_sha256, page.page_number, page.evidence_page_id)))
    input_hash = _input_hash_from_bindings(
        matter_id=matter_id,
        bindings=tuple(
            AgentEvidencePageBinding(
                evidence_page_id=page.evidence_page_id,
                source_file_sha256=page.source_file_sha256,
                page_number=page.page_number,
                extracted_text_sha256=page.extracted_text_sha256,
            )
            for page in ordered
        ),
    )
    return AgentMaterialAnalysisRequest(
        matter_id,
        matter_version,
        input_hash,
        ordered,
        external_request_id,
        run_id,
        claim_lease_id,
        normalized_context,
        external_ledger_version,
    )


def parse_material_candidate_response(
    raw: str | bytes,
    *,
    request: AgentMaterialAnalysisRequest,
) -> tuple[ModelPageCandidate, ...]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or not 2 <= len(encoded) <= 512 * 1024:
        raise AgentMaterialReviewBlocked("model material response size is invalid")
    try:
        value = json.loads(
            encoded,
            parse_constant=lambda _: (_ for _ in ()).throw(
                AgentMaterialReviewBlocked("model material response contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentMaterialReviewBlocked("model material response must be one JSON object") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "input_hash", "candidates"}:
        raise AgentMaterialReviewBlocked("model material response schema is invalid")
    if value.get("schema_version") != MATERIAL_AGENT_SCHEMA_VERSION or value.get("input_hash") != request.input_hash:
        raise AgentMaterialReviewBlocked("model material response is not bound to this Agent input")
    items = value.get("candidates")
    if not isinstance(items, list) or len(items) != len(request.pages):
        raise AgentMaterialReviewBlocked("model must return exactly one candidate for every supplied page")
    expected = {page.evidence_page_id: page for page in request.pages}
    candidates: list[ModelPageCandidate] = []
    seen: set[str] = set()
    for item in items:
        candidate = _parse_model_candidate(item, expected=expected)
        if candidate.evidence_page_id in seen:
            raise AgentMaterialReviewBlocked("model returned duplicate page candidates")
        seen.add(candidate.evidence_page_id)
        candidates.append(candidate)
    if seen != set(expected):
        raise AgentMaterialReviewBlocked("model omitted or invented an evidence page")
    return tuple(sorted(candidates, key=lambda item: item.evidence_page_id))


def _parse_model_candidate(
    item: object,
    *,
    expected: Mapping[str, AgentEvidencePageProjection],
) -> ModelPageCandidate:
    keys = {
        "evidence_page_id",
        "source_file_sha256",
        "page_number",
        "kind",
        "confidence",
        "review_priority",
        "reason_codes",
        "supporting_excerpt",
        "duplicate_of_page_id",
    }
    if not isinstance(item, dict) or set(item) != keys:
        raise AgentMaterialReviewBlocked("model page candidate schema is invalid")
    page_id = item.get("evidence_page_id")
    page = expected.get(page_id) if isinstance(page_id, str) else None
    if page is None:
        raise AgentMaterialReviewBlocked("model page candidate references an unknown page")
    if item.get("source_file_sha256") != page.source_file_sha256 or item.get("page_number") != page.page_number:
        raise AgentMaterialReviewBlocked("model page candidate source binding is invalid")
    try:
        kind = PageCandidateKind(item.get("kind"))
        priority = ReviewPriority(item.get("review_priority"))
    except (TypeError, ValueError) as error:
        raise AgentMaterialReviewBlocked("model page candidate classification is invalid") from error
    confidence = item.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise AgentMaterialReviewBlocked("model page candidate confidence is invalid")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise AgentMaterialReviewBlocked("model page candidate confidence is invalid")
    reason_values = item.get("reason_codes")
    if not isinstance(reason_values, list) or not 1 <= len(reason_values) <= 8:
        raise AgentMaterialReviewBlocked("model page candidate reason codes are invalid")
    try:
        reason_codes = tuple(CandidateReasonCode(value) for value in reason_values)
    except (TypeError, ValueError) as error:
        raise AgentMaterialReviewBlocked("model page candidate reason code is invalid") from error
    if len(set(reason_codes)) != len(reason_codes):
        raise AgentMaterialReviewBlocked("model page candidate reason codes contain duplicates")
    excerpt = item.get("supporting_excerpt")
    if not isinstance(excerpt, str) or len(excerpt) > 500 or excerpt != excerpt.strip():
        raise AgentMaterialReviewBlocked("model page candidate supporting excerpt is invalid")
    if excerpt and excerpt not in page.extracted_text:
        raise AgentMaterialReviewBlocked("model supporting excerpt is not present in the bound page text")
    duplicate_of = item.get("duplicate_of_page_id")
    if kind is PageCandidateKind.DUPLICATE_CANDIDATE:
        if not isinstance(duplicate_of, str) or duplicate_of not in expected or duplicate_of == page_id:
            raise AgentMaterialReviewBlocked("duplicate candidate lacks another bound source page")
        if CandidateReasonCode.POSSIBLE_DUPLICATE not in reason_codes:
            raise AgentMaterialReviewBlocked("duplicate candidate lacks its required reason code")
    elif duplicate_of is not None:
        raise AgentMaterialReviewBlocked("non-duplicate candidate cannot name a duplicate source")
    if kind in {PageCandidateKind.OCR_REQUIRED, PageCandidateKind.UNCERTAIN} and priority is not ReviewPriority.HIGH:
        raise AgentMaterialReviewBlocked("uncertain or OCR candidates must be routed to high-priority review")
    if confidence < 0.8 and priority is ReviewPriority.LOW:
        raise AgentMaterialReviewBlocked("low-confidence candidates cannot be routed to low-priority review")
    if kind is PageCandidateKind.OCR_REQUIRED:
        if CandidateReasonCode.LOW_OCR_CONFIDENCE not in reason_codes:
            raise AgentMaterialReviewBlocked("OCR candidate lacks its required reason code")
    elif not page.extracted_text:
        raise AgentMaterialReviewBlocked("a page without extracted text must be routed to OCR review")
    elif not excerpt and page.extracted_text:
        raise AgentMaterialReviewBlocked("text-backed candidate requires an exact supporting excerpt")
    return ModelPageCandidate(
        evidence_page_id=page_id,
        source_file_sha256=page.source_file_sha256,
        page_number=page.page_number,
        kind=kind,
        confidence=confidence,
        review_priority=priority,
        reason_codes=reason_codes,
        supporting_excerpt=excerpt,
        duplicate_of_page_id=duplicate_of,
    )


def _materialize_candidates(
    *,
    run_id: str,
    matter_id: str,
    input_hash: str,
    candidates: tuple[ModelPageCandidate, ...],
) -> tuple[AgentPageCandidate, ...]:
    return tuple(
        AgentPageCandidate(
            candidate_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"cn.lawcase.web-agent-candidate/v1/{run_id}/{candidate.evidence_page_id}",
                )
            ),
            matter_id=matter_id,
            evidence_page_id=candidate.evidence_page_id,
            source_file_sha256=candidate.source_file_sha256,
            page_number=candidate.page_number,
            kind=candidate.kind,
            confidence=candidate.confidence,
            review_priority=candidate.review_priority,
            reason_codes=candidate.reason_codes,
            supporting_excerpt=candidate.supporting_excerpt,
            duplicate_of_page_id=candidate.duplicate_of_page_id,
            input_hash=input_hash,
        )
        for candidate in candidates
    )


def agent_candidate_output_hash(candidates: tuple[AgentPageCandidate, ...]) -> str:
    return _canonical_hash(
        {
            "schema_version": "agent-material-candidate-output-v1",
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "matter_id": candidate.matter_id,
                    "evidence_page_id": candidate.evidence_page_id,
                    "source_file_sha256": candidate.source_file_sha256,
                    "page_number": candidate.page_number,
                    "kind": candidate.kind.value,
                    "confidence": candidate.confidence,
                    "review_priority": candidate.review_priority.value,
                    "reason_codes": [code.value for code in candidate.reason_codes],
                    "supporting_excerpt": candidate.supporting_excerpt,
                    "duplicate_of_page_id": candidate.duplicate_of_page_id,
                    "input_hash": candidate.input_hash,
                    "status": candidate.status.value,
                }
                for candidate in candidates
            ],
        }
    )


def _validate_persisted_candidates(
    run: AgentMaterialRunSnapshot,
    candidates: tuple[AgentPageCandidate, ...],
) -> None:
    if not candidates:
        raise AgentMaterialReviewBlocked("an Agent review run cannot complete without review candidates")
    ids: set[str] = set()
    page_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, AgentPageCandidate):
            raise AgentMaterialReviewBlocked("Agent candidate type is invalid")
        if candidate.matter_id != run.matter_id or candidate.input_hash != run.input_hash:
            raise AgentMaterialReviewBlocked("Agent candidate is not bound to the queued matter input")
        _require_sha256(candidate.source_file_sha256, "candidate source_file_sha256")
        if candidate.status is not AgentCandidateStatus.NEEDS_REVIEW:
            raise AgentMaterialReviewBlocked("Agent candidate cannot bypass lawyer review")
        if candidate.candidate_id in ids or candidate.evidence_page_id in page_ids:
            raise AgentMaterialReviewBlocked("Agent candidate identifiers must be unique")
        ids.add(candidate.candidate_id)
        page_ids.add(candidate.evidence_page_id)


def _fixed_task_plan() -> tuple[AgentTaskKind, ...]:
    return (
        AgentTaskKind.MATERIAL_READING,
        AgentTaskKind.PAGE_CLASSIFICATION,
        AgentTaskKind.RELEVANT_PAGE_EXTRACTION,
        AgentTaskKind.EXCEPTION_ROUTING,
    )


def _request_page_bindings(
    request: AgentMaterialAnalysisRequest,
) -> tuple[AgentEvidencePageBinding, ...]:
    return tuple(
        AgentEvidencePageBinding(
            evidence_page_id=page.evidence_page_id,
            source_file_sha256=page.source_file_sha256,
            page_number=page.page_number,
            extracted_text_sha256=page.extracted_text_sha256,
        )
        for page in request.pages
    )


def _normalize_page_ids(values: Sequence[str]) -> tuple[str, ...]:
    page_ids = tuple(values)
    if not 1 <= len(page_ids) <= MAX_AGENT_PAGES_PER_RUN:
        raise AgentMaterialReviewBlocked(
            f"one Agent material run must contain 1 to {MAX_AGENT_PAGES_PER_RUN} pages"
        )
    for page_id in page_ids:
        _require_identifier(page_id, "evidence_page_id")
    if len(set(page_ids)) != len(page_ids):
        raise AgentMaterialReviewBlocked("Agent page selection contains duplicate page ids")
    return tuple(sorted(page_ids))


def _require_source_returned_exact_pages(
    requested_page_ids: tuple[str, ...],
    pages: Sequence[AgentEvidencePageProjection],
) -> None:
    returned = tuple(sorted(page.evidence_page_id for page in pages))
    if returned != requested_page_ids:
        raise AgentMaterialReviewBlocked("evidence source omitted or invented a requested page")


def _validate_page_bindings(
    matter_id: str,
    matter_version: int,
    input_hash: str,
    bindings: tuple[AgentEvidencePageBinding, ...],
) -> None:
    if not 1 <= len(bindings) <= MAX_AGENT_PAGES_PER_RUN:
        raise AgentMaterialReviewBlocked("Agent page bindings are invalid")
    for binding in bindings:
        if not isinstance(binding, AgentEvidencePageBinding):
            raise AgentMaterialReviewBlocked("Agent page binding type is invalid")
        _require_identifier(binding.evidence_page_id, "evidence_page_id")
        _require_sha256(binding.source_file_sha256, "source_file_sha256")
        _require_positive_int(binding.page_number, "page_number")
        _require_sha256(binding.extracted_text_sha256, "extracted_text_sha256")
    ordered = tuple(
        sorted(bindings, key=lambda item: (item.source_file_sha256, item.page_number, item.evidence_page_id))
    )
    if tuple(bindings) != ordered or len({item.evidence_page_id for item in bindings}) != len(bindings):
        raise AgentMaterialReviewBlocked("Agent page bindings must be unique and canonical")
    if _input_hash_from_bindings(
        matter_id=matter_id,
        bindings=bindings,
    ) != input_hash:
        raise AgentMaterialReviewBlocked("Agent page bindings do not match input_hash")


def _input_hash_from_bindings(
    *,
    matter_id: str,
    bindings: tuple[AgentEvidencePageBinding, ...],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "agent-material-input-v1",
            "matter_id": matter_id,
            "pages": [_binding_payload(binding) for binding in bindings],
        }
    )


def _binding_payload(binding: AgentEvidencePageBinding) -> dict[str, str | int]:
    return {
        "evidence_page_id": binding.evidence_page_id,
        "source_file_sha256": binding.source_file_sha256,
        "page_number": binding.page_number,
        "extracted_text_sha256": binding.extracted_text_sha256,
    }


def _validate_plan_context(value: AgentPlanContext, *, require_profile: bool) -> None:
    if not isinstance(value, AgentPlanContext) or not isinstance(value.intent, AgentIntent):
        raise AgentMaterialReviewBlocked("Agent plan context is invalid")
    version = value.representation_profile_version
    profile_hash = value.representation_profile_hash
    if (version is None) != (profile_hash is None):
        raise AgentMaterialReviewBlocked("Agent representation profile binding is incomplete")
    if version is not None:
        _require_positive_int(version, "representation_profile_version")
        _require_sha256(profile_hash, "representation_profile_hash")
    if require_profile and version is None:
        raise AgentMaterialReviewBlocked(
            "external Agent execution requires a confirmed representation profile"
        )
    if value.intent is not AgentIntent.MATERIAL_NEUTRAL_REVIEW:
        raise AgentMaterialReviewBlocked(
            "party-position Agent plans are reserved until the representation profile ledger is implemented"
        )


def _plan_context_payload(value: AgentPlanContext) -> dict[str, str | int | None]:
    return {
        "intent": value.intent.value,
        "representation_profile_version": value.representation_profile_version,
        "representation_profile_hash": value.representation_profile_hash,
    }


def _outcome_idempotency_key(execution_key: str) -> str:
    return f"agent-outcome:{sha256(execution_key.encode('utf-8')).hexdigest()}"


def _require_actor(actor: Actor, roles: frozenset[Role]) -> None:
    if not isinstance(actor, Actor) or not actor.roles.intersection(roles):
        raise AgentMaterialReviewBlocked("actor is not allowed to perform this Agent command")
    _require_identifier(actor.actor_id, "actor_id")
    _require_identifier(actor.firm_id, "firm_id")


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise AgentMaterialReviewBlocked(f"{label} is invalid")


def _require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise AgentMaterialReviewBlocked("Agent idempotency key is invalid")


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentMaterialReviewBlocked(f"{label} must be a positive integer")


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AgentMaterialReviewBlocked(f"{label} must be a lowercase SHA-256")


def _require_run_version(run: AgentMaterialRunSnapshot, expected: int) -> None:
    if run.run_version != expected:
        raise AgentMaterialReviewBlocked("Agent run version conflict")


def _aware_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AgentMaterialReviewBlocked("Agent store clock must return an aware datetime")
    return value


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = (
    "AgentEvidencePageProjection",
    "AgentIntent",
    "AgentPlanContext",
    "AgentEvidencePageBinding",
    "AgentEvidenceProjectionSource",
    "AgentCandidateStatus",
    "AgentFailureCode",
    "AgentMaterialAnalysisProvider",
    "AgentMaterialAnalysisRequest",
    "AgentMaterialReviewBlocked",
    "AgentMaterialRunSnapshot",
    "AgentMaterialRunStore",
    "AgentPageCandidate",
    "AgentProviderRejected",
    "AgentProviderUnknownSubmission",
    "AgentRunStatus",
    "AgentTaskKind",
    "AgentTaskStatus",
    "CandidateReasonCode",
    "InMemoryAgentMaterialRunStore",
    "MATERIAL_AGENT_PLAN_VERSION",
    "MATERIAL_AGENT_SCHEMA_VERSION",
    "MAX_AGENT_PAGES_PER_RUN",
    "MAX_AGENT_TEXT_BYTES_PER_RUN",
    "PageCandidateKind",
    "ReviewPriority",
    "WebMaterialAgentCoordinator",
    "agent_candidate_output_hash",
    "build_material_analysis_request",
    "parse_material_candidate_response",
)
