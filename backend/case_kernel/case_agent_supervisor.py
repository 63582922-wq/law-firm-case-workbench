"""Deterministic control plane for the lawyer case Agent.

The supervisor is an event-sourced domain aggregate, not an LLM loop.  A
lawyer's natural-language goal is stored as untrusted business intent.  It can
be used by a bounded planner, but can never become a shell command, URL, file
path or Tool payload by itself.  Only a compiled task graph may execute, and a
task graph may bind only an IMPLEMENTED Skill/Tool pair with a concrete runtime
adapter and an exact capability contract.

Persistence, leases, HTTP and worker composition intentionally live outside
this module.  Every transition here is derived exclusively from the prior
state and an immutable, sequenced event so the aggregate can be rebuilt after
a process restart without inventing work or repeating an uncertain external
request.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping
from uuid import UUID

from .skill_registry import (
    ApprovalGate,
    CapabilityScope,
    CaseSkillRegistry,
    SkillMaturity,
    SkillRegistryBlocked,
)


class AgentSupervisorBlocked(ValueError):
    """The proposed graph, event or state transition is not safe to apply."""


class AgentRunStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    WAITING_INPUT = "WAITING_INPUT"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERIFYING = "VERIFYING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AgentTaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    RETRYABLE = "RETRYABLE"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class AgentRiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    PROHIBITED = "PROHIBITED"


class AgentAutonomyLevel(StrEnum):
    A0_OBSERVE = "A0_OBSERVE"
    A1_PROPOSE = "A1_PROPOSE"
    A2_INTERNAL_REVERSIBLE = "A2_INTERNAL_REVERSIBLE"
    A3_LAWYER_APPROVAL = "A3_LAWYER_APPROVAL"
    A4_PROHIBITED = "A4_PROHIBITED"


class RetryMode(StrEnum):
    IDEMPOTENT = "IDEMPOTENT"
    BEFORE_EXTERNAL_SUBMISSION_ONLY = "BEFORE_EXTERNAL_SUBMISSION_ONLY"
    NEVER_AUTOMATIC = "NEVER_AUTOMATIC"


class ResultStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ExternalSubmissionState(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTED = "SUBMITTED"
    UNKNOWN = "UNKNOWN"


class AdapterExecutionMode(StrEnum):
    IN_PROCESS = "IN_PROCESS"
    ISOLATED_CONTAINER = "ISOLATED_CONTAINER"
    NETWORK_CONNECTOR = "NETWORK_CONNECTOR"


class NetworkPolicy(StrEnum):
    DENY = "DENY"
    EXACT_ALLOWLIST = "EXACT_ALLOWLIST"


class AgentEventType(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    PLANNING_STARTED = "PLANNING_STARTED"
    PLANNING_FAILED = "PLANNING_FAILED"
    PLANNING_BUDGET_REVIEWED = "PLANNING_BUDGET_REVIEWED"
    PLANNING_MATERIAL_SCOPE_REVIEWED = "PLANNING_MATERIAL_SCOPE_REVIEWED"
    SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED = "SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED"
    CASE_ANALYSIS_STAGE_REVIEWED = "CASE_ANALYSIS_STAGE_REVIEWED"
    CASE_ANALYSIS_REVISION_REVIEWED = "CASE_ANALYSIS_REVISION_REVIEWED"
    CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED = "CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED"
    PLANNING_RESULT_UNKNOWN = "PLANNING_RESULT_UNKNOWN"
    TASK_GRAPH_ACCEPTED = "TASK_GRAPH_ACCEPTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    LAWYER_PLAN_CORRECTION_RECORDED = "LAWYER_PLAN_CORRECTION_RECORDED"
    TASK_STARTED = "TASK_STARTED"
    TASK_RESULT_RECORDED = "TASK_RESULT_RECORDED"
    CASE_SNAPSHOT_CHANGED = "CASE_SNAPSHOT_CHANGED"
    RUN_PAUSED = "RUN_PAUSED"
    RUN_RESUMED = "RUN_RESUMED"
    RUN_CANCELLED = "RUN_CANCELLED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    RUN_COMPLETED = "RUN_COMPLETED"


class SupervisorCommandKind(StrEnum):
    REQUEST_PLAN = "REQUEST_PLAN"
    REQUEST_REPLAN = "REQUEST_REPLAN"
    RECONCILE_PLAN_RESULT = "RECONCILE_PLAN_RESULT"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    DISPATCH_TASK = "DISPATCH_TASK"
    RECONCILE_EXTERNAL_RESULT = "RECONCILE_EXTERNAL_RESULT"
    START_VERIFICATION = "START_VERIFICATION"
    REQUEST_FINAL_REVIEW = "REQUEST_FINAL_REVIEW"
    COMPLETE_RUN = "COMPLETE_RUN"


class AgentDeliverableKind(StrEnum):
    """Exact first-release outputs a lawyer may request from the Agent."""

    CASE_REVIEW_MEMO = "CASE_REVIEW_MEMO"
    DEFENCE_STATEMENT = "DEFENCE_STATEMENT"
    EVIDENCE_CATALOGUE = "EVIDENCE_CATALOGUE"
    SUPPLEMENTARY_EVIDENCE_CHECKLIST = "SUPPLEMENTARY_EVIDENCE_CHECKLIST"
    PAYMENT_LEDGER = "PAYMENT_LEDGER"


class AgentDeliverableFormat(StrEnum):
    DOCX = "DOCX"
    XLSX = "XLSX"


_DELIVERABLE_FORMAT = {
    AgentDeliverableKind.CASE_REVIEW_MEMO: AgentDeliverableFormat.DOCX,
    AgentDeliverableKind.DEFENCE_STATEMENT: AgentDeliverableFormat.DOCX,
    AgentDeliverableKind.EVIDENCE_CATALOGUE: AgentDeliverableFormat.XLSX,
    AgentDeliverableKind.SUPPLEMENTARY_EVIDENCE_CHECKLIST: AgentDeliverableFormat.DOCX,
    AgentDeliverableKind.PAYMENT_LEDGER: AgentDeliverableFormat.XLSX,
}


@dataclass(frozen=True)
class ActivePlanDeliverableRef:
    """One exact actionable document item selected from an ACTIVE plan."""

    item_id: str
    item_hash: str
    deliverable_kind: AgentDeliverableKind
    output_format: AgentDeliverableFormat

    def validate(self) -> None:
        _require_uuid(self.item_id, "active-plan deliverable item_id")
        _require_sha256(self.item_hash, "active-plan deliverable item_hash")
        if not isinstance(self.deliverable_kind, AgentDeliverableKind):
            raise AgentSupervisorBlocked("active-plan deliverable kind is invalid")
        if not isinstance(self.output_format, AgentDeliverableFormat):
            raise AgentSupervisorBlocked("active-plan deliverable format is invalid")
        if self.output_format is not _DELIVERABLE_FORMAT[self.deliverable_kind]:
            raise AgentSupervisorBlocked(
                "active-plan deliverable format differs from the server template catalogue"
            )


@dataclass(frozen=True)
class ActivePlanExecutionRef:
    """Server-derived authority for one explicit execution of an ACTIVE plan."""

    plan_id: str
    plan_hash: str
    source_run_id: str
    items: tuple[ActivePlanDeliverableRef, ...]

    def validate(self) -> None:
        _require_uuid(self.plan_id, "active-plan execution plan_id")
        _require_sha256(self.plan_hash, "active-plan execution plan_hash")
        _require_uuid(self.source_run_id, "active-plan execution source_run_id")
        if not self.items or len(self.items) > len(AgentDeliverableKind):
            raise AgentSupervisorBlocked(
                "active-plan execution requires a bounded deliverable set"
            )
        for item in self.items:
            if not isinstance(item, ActivePlanDeliverableRef):
                raise AgentSupervisorBlocked("active-plan deliverable reference is invalid")
            item.validate()
        canonical = tuple(
            sorted(self.items, key=lambda item: (item.deliverable_kind.value, item.item_id))
        )
        if self.items != canonical:
            raise AgentSupervisorBlocked(
                "active-plan deliverable references must be canonical"
            )
        if len({item.item_id for item in self.items}) != len(self.items):
            raise AgentSupervisorBlocked("active-plan deliverable item ids are duplicated")
        if len({item.deliverable_kind for item in self.items}) != len(self.items):
            raise AgentSupervisorBlocked("active-plan deliverable kinds are duplicated")


@dataclass(frozen=True)
class AgentGoal:
    goal_id: str
    objective: str
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    requested_by: str
    goal_hash: str
    requested_deliverables: tuple[AgentDeliverableKind, ...] = ()
    active_plan_execution: ActivePlanExecutionRef | None = None
    material_read_refs: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        goal_id: str,
        objective: str,
        success_criteria: Iterable[str],
        constraints: Iterable[str],
        requested_by: str,
        requested_deliverables: Iterable[AgentDeliverableKind | str] = (),
        active_plan_execution: ActivePlanExecutionRef | None = None,
        material_read_refs: tuple[str, ...] = (),
    ) -> "AgentGoal":
        """Create an opaque business goal; it never authorizes a Tool call."""

        _require_uuid(goal_id, "goal_id")
        _require_identifier(requested_by, "requested_by")
        normalized_objective = _require_business_text(objective, "objective", 4000)
        normalized_criteria = _bounded_text_tuple(
            success_criteria, "success criteria", maximum_items=30, maximum_length=1000
        )
        normalized_constraints = _optional_bounded_text_tuple(
            constraints, "goal constraints", maximum_items=30, maximum_length=1000
        )
        try:
            normalized_deliverables = tuple(
                sorted(
                    {AgentDeliverableKind(value) for value in requested_deliverables},
                    key=lambda value: value.value,
                )
            )
        except (TypeError, ValueError) as error:
            raise AgentSupervisorBlocked(
                "requested deliverables are outside the installed server catalogue"
            ) from error
        if len(normalized_deliverables) > len(AgentDeliverableKind):
            raise AgentSupervisorBlocked("requested deliverable set is too large")
        if active_plan_execution is not None:
            if not isinstance(active_plan_execution, ActivePlanExecutionRef):
                raise AgentSupervisorBlocked("active-plan execution authority is invalid")
            active_plan_execution.validate()
            execution_kinds = tuple(
                sorted(
                    (item.deliverable_kind for item in active_plan_execution.items),
                    key=lambda value: value.value,
                )
            )
            if execution_kinds != normalized_deliverables:
                raise AgentSupervisorBlocked(
                    "active-plan execution items differ from requested deliverables"
                )
        if not isinstance(material_read_refs, tuple) or len(material_read_refs) > 200:
            raise AgentSupervisorBlocked("material reading scope must be a bounded tuple")
        for ref in material_read_refs:
            if not isinstance(ref, str) or ":" not in ref:
                raise AgentSupervisorBlocked("material reading reference is invalid")
            kind, identifier = ref.split(":", 1)
            if kind not in {"evidence-page", "material-object"}:
                raise AgentSupervisorBlocked("material reading scope accepts only original inputs")
            _require_uuid(identifier, "material reading reference")
        if material_read_refs != tuple(sorted(set(material_read_refs))):
            raise AgentSupervisorBlocked("material reading references must be canonical")
        if material_read_refs and (normalized_deliverables or active_plan_execution is not None):
            raise AgentSupervisorBlocked("material reading cannot authorize case deliverables")
        schema_version = (
            "lawyer-agent-goal-v2"
            if normalized_deliverables or active_plan_execution is not None
            else "lawyer-agent-goal-v1"
        )
        payload: dict[str, object] = {
            "schema_version": schema_version,
            "goal_id": goal_id,
            "objective": normalized_objective,
            "success_criteria": normalized_criteria,
            "constraints": normalized_constraints,
            "requested_by": requested_by,
        }
        if schema_version == "lawyer-agent-goal-v2":
            payload["requested_deliverables"] = tuple(
                item.value for item in normalized_deliverables
            )
            payload["active_plan_execution"] = (
                None
                if active_plan_execution is None
                else {
                    "plan_id": active_plan_execution.plan_id,
                    "plan_hash": active_plan_execution.plan_hash,
                    "source_run_id": active_plan_execution.source_run_id,
                    "items": tuple(
                        {
                            "item_id": item.item_id,
                            "item_hash": item.item_hash,
                            "deliverable_kind": item.deliverable_kind.value,
                            "output_format": item.output_format.value,
                        }
                        for item in active_plan_execution.items
                    ),
                }
            )
        if material_read_refs:
            payload["schema_version"] = "lawyer-agent-goal-v3"
            payload["material_read_refs"] = material_read_refs
        goal_hash = _canonical_hash(payload)
        return cls(
            goal_id=goal_id,
            objective=normalized_objective,
            success_criteria=normalized_criteria,
            constraints=normalized_constraints,
            requested_by=requested_by,
            goal_hash=goal_hash,
            requested_deliverables=normalized_deliverables,
            active_plan_execution=active_plan_execution,
            material_read_refs=material_read_refs,
        )


@dataclass(frozen=True)
class CaseSnapshotRef:
    matter_id: str
    matter_version: int
    snapshot_hash: str
    schema_version: str

    def validate(self) -> None:
        _require_uuid(self.matter_id, "snapshot matter_id")
        _require_positive(self.matter_version, "snapshot matter_version")
        _require_sha256(self.snapshot_hash, "snapshot_hash")
        _require_code(self.schema_version, "snapshot schema_version")


@dataclass(frozen=True)
class RunResourceBudget:
    max_tasks: int
    max_total_attempts: int
    max_external_calls: int
    max_runtime_seconds: int
    max_cost_minor_units: int
    max_output_bytes: int

    def validate(self) -> None:
        for name, value, maximum in (
            ("max_tasks", self.max_tasks, 500),
            ("max_total_attempts", self.max_total_attempts, 2000),
            ("max_external_calls", self.max_external_calls, 1000),
            ("max_runtime_seconds", self.max_runtime_seconds, 7 * 24 * 60 * 60),
            ("max_cost_minor_units", self.max_cost_minor_units, 100_000_000),
            ("max_output_bytes", self.max_output_bytes, 100 * 1024 * 1024 * 1024),
        ):
            _require_non_negative(value, name)
            if value > maximum:
                raise AgentSupervisorBlocked(f"{name} exceeds the supervisor safety boundary")
        if self.max_tasks < 1 or self.max_total_attempts < 1:
            raise AgentSupervisorBlocked("run budget must allow at least one task and one attempt")


@dataclass(frozen=True)
class TaskResourceBudget:
    max_attempts: int
    timeout_seconds: int
    max_external_calls: int
    max_cost_minor_units: int
    max_output_bytes: int

    def validate(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise AgentSupervisorBlocked("task max_attempts must be between 1 and 10")
        if not 1 <= self.timeout_seconds <= 24 * 60 * 60:
            raise AgentSupervisorBlocked("task timeout exceeds the worker boundary")
        if not 0 <= self.max_external_calls <= 1000:
            raise AgentSupervisorBlocked("task external-call budget exceeds the worker boundary")
        _require_non_negative(self.max_cost_minor_units, "task max_cost_minor_units")
        _require_non_negative(self.max_output_bytes, "task max_output_bytes")
        if self.max_output_bytes > 10 * 1024 * 1024 * 1024:
            raise AgentSupervisorBlocked("task output budget exceeds the worker boundary")


@dataclass(frozen=True)
class RuntimeAdapterManifest:
    tool_id: str
    adapter_id: str
    adapter_version: str
    execution_mode: AdapterExecutionMode
    supports_idempotency: bool
    supports_reconciliation: bool
    network_capable: bool
    sandbox_policy_version: str
    sandbox_policy_hash: str

    def validate(self) -> None:
        _require_code(self.tool_id, "adapter tool_id")
        _require_code(self.adapter_id, "adapter_id")
        _require_semver(self.adapter_version, "adapter_version")
        _require_semver(self.sandbox_policy_version, "sandbox_policy_version")
        _require_sha256(self.sandbox_policy_hash, "sandbox_policy_hash")


@dataclass(frozen=True)
class TaskCapabilityContract:
    execution_mode: AdapterExecutionMode
    network_policy: NetworkPolicy
    allowed_domains: tuple[str, ...]
    sandbox_profile: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    reads_case_objects: tuple[str, ...]
    writes_managed_derivatives: bool
    external_request_approval_required: bool

    def validate(self) -> None:
        _require_code(self.sandbox_profile, "sandbox_profile")
        _require_semver(self.sandbox_policy_version, "sandbox_policy_version")
        _require_sha256(self.sandbox_policy_hash, "sandbox_policy_hash")
        if len(self.reads_case_objects) > 500:
            raise AgentSupervisorBlocked("one task cannot bind more than 500 case objects")
        for object_id in self.reads_case_objects:
            _require_identifier(object_id, "case object reference")
        if len(set(self.reads_case_objects)) != len(self.reads_case_objects):
            raise AgentSupervisorBlocked("case object references must be unique")
        if self.network_policy is NetworkPolicy.DENY:
            if self.allowed_domains or self.external_request_approval_required:
                raise AgentSupervisorBlocked("a deny-network contract cannot contain network grants")
        else:
            if not self.allowed_domains or not self.external_request_approval_required:
                raise AgentSupervisorBlocked(
                    "network access requires exact destinations and external-request approval"
                )
            if len(self.allowed_domains) > 20:
                raise AgentSupervisorBlocked("network destination allowlist is too broad")
            for domain in self.allowed_domains:
                if _DOMAIN_RE.fullmatch(domain) is None or "*" in domain:
                    raise AgentSupervisorBlocked("network destinations must be exact DNS names")


@dataclass(frozen=True)
class SkillBinding:
    skill_id: str
    skill_version: str
    tool_id: str
    tool_version: str
    adapter_id: str
    adapter_version: str


@dataclass(frozen=True)
class AgentTaskSpec:
    task_id: str
    sequence: int
    title: str
    purpose: str
    rationale: str
    dependency_ids: tuple[str, ...]
    input_refs: tuple[str, ...]
    input_hash: str
    skill: SkillBinding
    granted_scopes: frozenset[CapabilityScope]
    capability: TaskCapabilityContract
    risk_level: AgentRiskLevel
    autonomy_level: AgentAutonomyLevel
    approval_gate: ApprovalGate
    retry_mode: RetryMode
    budget: TaskResourceBudget


@dataclass(frozen=True)
class AgentTaskGraph:
    graph_id: str
    graph_version: int
    goal_hash: str
    snapshot: CaseSnapshotRef
    tasks: tuple[AgentTaskSpec, ...]
    graph_hash: str


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    task_id: str
    task_input_hash: str
    graph_hash: str
    gate: ApprovalGate
    approved_by: str
    approval_hash: str

    @classmethod
    def build(
        cls,
        *,
        approval_id: str,
        task: AgentTaskSpec,
        graph_hash: str,
        gate: ApprovalGate,
        approved_by: str,
    ) -> "ApprovalRecord":
        _require_uuid(approval_id, "approval_id")
        _require_identifier(approved_by, "approved_by")
        _require_sha256(graph_hash, "approval graph_hash")
        approval_hash = _canonical_hash(
            {
                "schema_version": "agent-task-approval-v1",
                "approval_id": approval_id,
                "task_id": task.task_id,
                "task_input_hash": task.input_hash,
                "graph_hash": graph_hash,
                "gate": gate.value,
                "approved_by": approved_by,
            }
        )
        return cls(
            approval_id=approval_id,
            task_id=task.task_id,
            task_input_hash=task.input_hash,
            graph_hash=graph_hash,
            gate=gate,
            approved_by=approved_by,
            approval_hash=approval_hash,
        )


@dataclass(frozen=True)
class ArtifactReceipt:
    artifact_id: str
    artifact_kind: str
    content_hash: str
    byte_size: int
    source_input_hash: str
    managed_derivative: bool

    def validate(self) -> None:
        _require_uuid(self.artifact_id, "artifact_id")
        _require_code(self.artifact_kind, "artifact_kind")
        _require_sha256(self.content_hash, "artifact content_hash")
        _require_sha256(self.source_input_hash, "artifact source_input_hash")
        _require_non_negative(self.byte_size, "artifact byte_size")


@dataclass(frozen=True)
class TaskResultReceipt:
    receipt_id: str
    task_id: str
    attempt_id: str
    input_hash: str
    adapter_id: str
    adapter_version: str
    status: ResultStatus
    external_submission_state: ExternalSubmissionState
    output_hash: str | None
    error_code: str | None
    external_request_id: str | None
    runtime_seconds: int
    cost_minor_units: int
    external_calls: int
    artifacts: tuple[ArtifactReceipt, ...] = ()


@dataclass(frozen=True)
class TaskRuntimeState:
    spec: AgentTaskSpec
    status: AgentTaskStatus
    attempt_count: int = 0
    active_attempt_id: str | None = None
    receipts: tuple[TaskResultReceipt, ...] = ()


@dataclass(frozen=True)
class BudgetUsage:
    attempts: int = 0
    external_calls: int = 0
    runtime_seconds: int = 0
    cost_minor_units: int = 0
    output_bytes: int = 0


@dataclass(frozen=True)
class AgentRunState:
    run_id: str
    firm_id: str
    matter_id: str
    event_version: int
    goal: AgentGoal
    snapshot: CaseSnapshotRef
    budget: RunResourceBudget
    status: AgentRunStatus
    graph: AgentTaskGraph | None
    tasks: tuple[TaskRuntimeState, ...]
    approvals: tuple[ApprovalRecord, ...]
    artifacts: tuple[ArtifactReceipt, ...]
    budget_usage: BudgetUsage
    paused_from: AgentRunStatus | None = None
    stale: bool = False
    cancelled: bool = False
    verification_hash: str | None = None
    failure_code: str | None = None
    task_failure_policy: str = "STOP_ON_TASK_FAILURE_V1"
    material_stage: object | None = None
    analysis_stage: object | None = None


@dataclass(frozen=True)
class RunFinalReviewApproval:
    approval_id: str
    run_id: str
    graph_hash: str
    verification_hash: str
    artifact_manifest_hash: str
    approved_by: str
    approval_hash: str
    document_review_versions: tuple[tuple[str, str], ...] = ()

    @classmethod
    def build(
        cls,
        *,
        approval_id: str,
        state: AgentRunState,
        approved_by: str,
        document_review_versions: tuple[tuple[str, str], ...] = (),
    ) -> "RunFinalReviewApproval":
        _require_uuid(approval_id, "final review approval_id")
        _require_identifier(approved_by, "final review approved_by")
        if (
            state.graph is None
            or state.status is not AgentRunStatus.READY_FOR_REVIEW
            or state.verification_hash is None
            or state.cancelled
            or state.stale
        ):
            raise AgentSupervisorBlocked("run is not ready for final lawyer review")
        artifact_manifest_hash = _artifact_manifest_hash(state.artifacts)
        if type(document_review_versions) is not tuple or len(document_review_versions) > 128:
            raise AgentSupervisorBlocked("final document review bindings are invalid")
        for binding in document_review_versions:
            if type(binding) is not tuple or len(binding) != 2:
                raise AgentSupervisorBlocked("final document review binding is invalid")
            _require_uuid(binding[0], "reviewed document artifact")
            _require_sha256(binding[1], "reviewed document version")
        if document_review_versions:
            expected_documents = {artifact.artifact_id for artifact in state.artifacts
                                  if artifact.artifact_kind == "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"}
            supplied_documents = {item[0] for item in document_review_versions}
            if supplied_documents != expected_documents or len(supplied_documents) != len(document_review_versions):
                raise AgentSupervisorBlocked("final document review bindings do not cover the exact document set")
        document_review_versions = tuple(sorted(document_review_versions))
        approval_hash = _canonical_hash(
            {
                "schema_version": "lawyer-agent-final-review-v2" if document_review_versions else "lawyer-agent-final-review-v1",
                "approval_id": approval_id,
                "run_id": state.run_id,
                "graph_hash": state.graph.graph_hash,
                "verification_hash": state.verification_hash,
                "artifact_manifest_hash": artifact_manifest_hash,
                "approved_by": approved_by,
                **({"document_review_versions": document_review_versions} if document_review_versions else {}),
            }
        )
        return cls(
            approval_id=approval_id,
            run_id=state.run_id,
            graph_hash=state.graph.graph_hash,
            verification_hash=state.verification_hash,
            artifact_manifest_hash=artifact_manifest_hash,
            approved_by=approved_by,
            approval_hash=approval_hash,
            document_review_versions=document_review_versions,
        )


@dataclass(frozen=True)
class RunCreatedPayload:
    goal: AgentGoal
    snapshot: CaseSnapshotRef
    budget: RunResourceBudget
    task_failure_policy: str = "STOP_ON_TASK_FAILURE_V1"


@dataclass(frozen=True)
class TaskGraphPayload:
    graph: AgentTaskGraph


@dataclass(frozen=True)
class ApprovalPayload:
    approval: ApprovalRecord


@dataclass(frozen=True)
class LawyerPlanCorrectionPayload:
    """Hash-bound server-built correction; prose remains untrusted case data."""

    signal_id: str
    task_id: str
    decision_hash: str
    subject_hash: str
    decision_code: str


@dataclass(frozen=True)
class TaskStartedPayload:
    task_id: str
    attempt_id: str
    graph_hash: str
    input_hash: str


@dataclass(frozen=True)
class TaskResultPayload:
    receipt: TaskResultReceipt


@dataclass(frozen=True)
class SnapshotChangedPayload:
    snapshot: CaseSnapshotRef


@dataclass(frozen=True)
class VerificationPayload:
    verification_hash: str
    error_code: str | None = None


@dataclass(frozen=True)
class PlanningFailurePayload:
    """A known terminal planner failure; no provider text reaches the aggregate."""

    error_code: str


@dataclass(frozen=True)
class PlanningBudgetReviewPayload:
    snapshot: CaseSnapshotRef
    previous_runtime_seconds: int
    approved_runtime_seconds: int
    request_hash: str
    planning_hash: str
    proposal_hash: str
    approved_by: str


@dataclass(frozen=True)
class PlanningMaterialScopeReviewPayload:
    snapshot: CaseSnapshotRef
    original_goal_hash: str
    original_proposal_hash: str
    request_hash: str
    planning_hash: str
    material_read_refs: tuple[str, ...]
    previous_output_bytes: int
    approved_output_bytes: int
    effective_goal_hash: str
    derived_proposal_hash: str
    compiled_graph_hash: str
    approved_by: str


@dataclass(frozen=True)
class SupplementaryMaterialStageReviewPayload:
    stage: object


@dataclass(frozen=True)
class CaseAnalysisStageReviewPayload:
    stage: object


@dataclass(frozen=True)
class RunCompletedPayload:
    final_review: RunFinalReviewApproval


AgentEventPayload = (
    RunCreatedPayload
    | TaskGraphPayload
    | ApprovalPayload
    | LawyerPlanCorrectionPayload
    | TaskStartedPayload
    | TaskResultPayload
    | SnapshotChangedPayload
    | VerificationPayload
    | PlanningFailurePayload
    | PlanningBudgetReviewPayload
    | PlanningMaterialScopeReviewPayload
    | SupplementaryMaterialStageReviewPayload
    | CaseAnalysisStageReviewPayload
    | RunCompletedPayload
    | None
)


@dataclass(frozen=True)
class AgentSupervisorEvent:
    event_id: str
    run_id: str
    firm_id: str
    matter_id: str
    sequence: int
    event_type: AgentEventType
    occurred_at: datetime
    actor_id: str
    payload: AgentEventPayload = None


@dataclass(frozen=True)
class SupervisorCommand:
    command_id: str
    kind: SupervisorCommandKind
    run_id: str
    task_id: str | None
    attempt_id: str | None
    graph_hash: str | None
    reason_code: str


def compile_task_graph(
    *,
    graph_id: str,
    graph_version: int,
    goal: AgentGoal,
    snapshot: CaseSnapshotRef,
    tasks: Iterable[AgentTaskSpec],
    registry: CaseSkillRegistry,
    adapters: Mapping[str, RuntimeAdapterManifest],
    run_budget: RunResourceBudget,
) -> AgentTaskGraph:
    """Compile an untrusted proposed DAG against the active server runtime."""

    _require_uuid(graph_id, "graph_id")
    _require_positive(graph_version, "graph_version")
    _validate_goal(goal)
    snapshot.validate()
    run_budget.validate()
    normalized = tuple(sorted(tasks, key=lambda item: item.sequence))
    if not normalized or len(normalized) > run_budget.max_tasks:
        raise AgentSupervisorBlocked("task graph is empty or exceeds the run budget")
    task_ids: set[str] = set()
    sequences: set[int] = set()
    for task in normalized:
        _validate_task(
            task,
            registry=registry,
            adapters=adapters,
            run_budget=run_budget,
        )
        if task.task_id in task_ids:
            raise AgentSupervisorBlocked("task ids must be unique")
        if task.sequence in sequences:
            raise AgentSupervisorBlocked("task sequences must be unique")
        task_ids.add(task.task_id)
        sequences.add(task.sequence)
    if sequences != set(range(1, len(normalized) + 1)):
        raise AgentSupervisorBlocked("task sequences must be contiguous")
    _validate_dag(normalized, task_ids)
    graph_hash = _task_graph_hash(
        graph_id=graph_id,
        graph_version=graph_version,
        goal_hash=goal.goal_hash,
        snapshot=snapshot,
        tasks=normalized,
    )
    return AgentTaskGraph(
        graph_id=graph_id,
        graph_version=graph_version,
        goal_hash=goal.goal_hash,
        snapshot=snapshot,
        tasks=normalized,
        graph_hash=graph_hash,
    )


def reduce_agent_event(
    state: AgentRunState | None,
    event: AgentSupervisorEvent,
) -> AgentRunState:
    """Apply exactly one persisted event without clock, UUID or I/O access."""

    _validate_event_envelope(state, event)
    if state is None:
        if event.event_type is not AgentEventType.RUN_CREATED or not isinstance(
            event.payload, RunCreatedPayload
        ):
            raise AgentSupervisorBlocked("the first event must create the Agent run")
        _validate_goal(event.payload.goal)
        event.payload.snapshot.validate()
        event.payload.budget.validate()
        if event.payload.task_failure_policy not in (
            "STOP_ON_TASK_FAILURE_V1", "ISOLATE_KNOWN_TASK_FAILURES_V1"
        ):
            raise AgentSupervisorBlocked("task failure policy is invalid")
        if event.matter_id != event.payload.snapshot.matter_id:
            raise AgentSupervisorBlocked("run event matter differs from its case snapshot")
        if event.actor_id != event.payload.goal.requested_by:
            raise AgentSupervisorBlocked("run creator differs from the lawyer goal owner")
        return AgentRunState(
            run_id=event.run_id,
            firm_id=event.firm_id,
            matter_id=event.matter_id,
            event_version=event.sequence,
            goal=event.payload.goal,
            snapshot=event.payload.snapshot,
            budget=event.payload.budget,
            status=AgentRunStatus.CREATED,
            graph=None,
            tasks=(),
            approvals=(),
            artifacts=(),
            budget_usage=BudgetUsage(),
            task_failure_policy=event.payload.task_failure_policy,
        )

    if state.status in {AgentRunStatus.FAILED, AgentRunStatus.CANCELLED} and not (
        state.status is AgentRunStatus.CANCELLED
        and event.event_type is AgentEventType.TASK_RESULT_RECORDED
    ):
        raise AgentSupervisorBlocked("a terminal Agent run cannot accept this event")
    if state.status is AgentRunStatus.COMPLETED and event.event_type is not AgentEventType.CASE_SNAPSHOT_CHANGED:
        raise AgentSupervisorBlocked("a terminal Agent run cannot accept this event")

    updated = state
    if event.event_type is AgentEventType.PLANNING_STARTED:
        _require_no_payload(event)
        if state.cancelled or state.status not in {
            AgentRunStatus.CREATED,
            AgentRunStatus.STALE,
        }:
            raise AgentSupervisorBlocked("planning cannot start from the current run state")
        # A replacement plan is allowed to supersede stale/failed work, but no
        # old task can dispatch while the aggregate is in PLANNING.
        updated = replace(
            state,
            status=AgentRunStatus.PLANNING,
            failure_code=None,
        )
    elif event.event_type is AgentEventType.PLANNING_FAILED:
        if not isinstance(event.payload, PlanningFailurePayload):
            raise AgentSupervisorBlocked("planning failure payload is invalid")
        _require_code(event.payload.error_code, "planning error_code")
        if state.status not in {
            AgentRunStatus.PLANNING,
            AgentRunStatus.RECONCILIATION_REQUIRED,
        }:
            raise AgentSupervisorBlocked("planning failure is not expected")
        updated = replace(
            state,
            status=AgentRunStatus.WAITING_INPUT,
            failure_code=event.payload.error_code,
            stale=state.graph is not None,
        )
    elif event.event_type is AgentEventType.PLANNING_BUDGET_REVIEWED:
        payload = event.payload
        if not isinstance(payload, PlanningBudgetReviewPayload):
            raise AgentSupervisorBlocked("planning budget review payload is invalid")
        if (state.status is not AgentRunStatus.WAITING_INPUT or state.graph is not None
                or state.tasks or state.stale or state.failure_code != "PLANNER_PROPOSAL_REJECTED"):
            raise AgentSupervisorBlocked("planning budget review requires an unaccepted rejected plan")
        payload.snapshot.validate()
        if payload.snapshot != state.snapshot:
            raise AgentSupervisorBlocked("planning budget review snapshot differs")
        _require_uuid(payload.approved_by, "planning budget reviewer")
        if payload.approved_by != event.actor_id:
            raise AgentSupervisorBlocked("planning budget reviewer differs from event actor")
        for label in ("request_hash", "planning_hash", "proposal_hash"):
            _require_sha256(getattr(payload, label), label)
        if (type(payload.previous_runtime_seconds) is not int
                or type(payload.approved_runtime_seconds) is not int
                or payload.previous_runtime_seconds != state.budget.max_runtime_seconds
                or payload.approved_runtime_seconds <= payload.previous_runtime_seconds):
            raise AgentSupervisorBlocked("planning runtime budget review is not a bounded extension")
        budget = replace(state.budget, max_runtime_seconds=payload.approved_runtime_seconds)
        budget.validate()
        updated = replace(state, budget=budget, status=AgentRunStatus.PLANNING, failure_code=None)
    elif event.event_type is AgentEventType.PLANNING_MATERIAL_SCOPE_REVIEWED:
        payload = event.payload
        if not isinstance(payload, PlanningMaterialScopeReviewPayload):
            raise AgentSupervisorBlocked("material scope review payload is invalid")
        if (state.status is not AgentRunStatus.WAITING_INPUT or state.graph is not None
                or state.tasks or state.stale or state.cancelled
                or state.failure_code != "PLANNER_PROPOSAL_REJECTED"
                or state.goal.material_read_refs or state.goal.requested_deliverables
                or state.goal.active_plan_execution is not None):
            raise AgentSupervisorBlocked("material scope review requires an unaccepted initial plan")
        payload.snapshot.validate()
        if payload.snapshot != state.snapshot or payload.original_goal_hash != state.goal.goal_hash:
            raise AgentSupervisorBlocked("material scope review original goal or snapshot differs")
        _require_uuid(payload.approved_by, "material scope reviewer")
        if payload.approved_by != event.actor_id:
            raise AgentSupervisorBlocked("material scope reviewer differs from event actor")
        for field in ("original_goal_hash", "original_proposal_hash", "request_hash", "planning_hash",
                      "effective_goal_hash", "derived_proposal_hash", "compiled_graph_hash"):
            _require_sha256(getattr(payload, field), field)
        if (not payload.material_read_refs or type(payload.previous_output_bytes) is not int
                or type(payload.approved_output_bytes) is not int
                or payload.previous_output_bytes != state.budget.max_output_bytes
                or not payload.previous_output_bytes <= payload.approved_output_bytes <= 64 * 1024 * 1024):
            raise AgentSupervisorBlocked("material scope output budget is invalid")
        goal = AgentGoal.build(goal_id=state.goal.goal_id, objective=state.goal.objective,
            success_criteria=state.goal.success_criteria, constraints=state.goal.constraints,
            requested_by=state.goal.requested_by, material_read_refs=payload.material_read_refs)
        if goal.goal_hash != payload.effective_goal_hash:
            raise AgentSupervisorBlocked("material scope effective goal fingerprint differs")
        budget = replace(state.budget, max_output_bytes=payload.approved_output_bytes)
        budget.validate()
        updated = replace(state, goal=goal, budget=budget, status=AgentRunStatus.PLANNING, failure_code=None)
    elif event.event_type is AgentEventType.SUPPLEMENTARY_MATERIAL_STAGE_REVIEWED:
        from .case_agent_material_stage import validate_supplementary_stage_review
        if not isinstance(event.payload, SupplementaryMaterialStageReviewPayload):
            raise AgentSupervisorBlocked("supplementary material review payload is invalid")
        stage = event.payload.stage
        validate_supplementary_stage_review(state=state, stage=stage, actor_id=event.actor_id)
        updated = replace(state, budget=stage.proposed_budget, material_stage=stage,
            status=AgentRunStatus.STALE, stale=True, failure_code=None)
    elif event.event_type in {AgentEventType.CASE_ANALYSIS_STAGE_REVIEWED, AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED,
                             AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED}:
        from .case_agent_analysis_stage import validate_case_analysis_stage_review
        if not isinstance(event.payload, CaseAnalysisStageReviewPayload):
            raise AgentSupervisorBlocked("analysis stage payload differs")
        stage = event.payload.stage
        from .case_agent_analysis_stage import CaseAnalysisRevisionStage, CaseAnalysisRequestRepairStage
        if isinstance(stage, CaseAnalysisRevisionStage) != (event.event_type is AgentEventType.CASE_ANALYSIS_REVISION_REVIEWED):
            raise AgentSupervisorBlocked("analysis stage event type differs")
        if isinstance(stage, CaseAnalysisRequestRepairStage) != (event.event_type is AgentEventType.CASE_ANALYSIS_REQUEST_REPAIR_REVIEWED):
            raise AgentSupervisorBlocked("analysis repair event type differs")
        validate_case_analysis_stage_review(state=state, stage=stage, actor_id=event.actor_id)
        updated = replace(state, budget=stage.proposed_budget, analysis_stage=stage,
            material_stage=None, status=AgentRunStatus.STALE, stale=True, failure_code=None)
    elif event.event_type is AgentEventType.PLANNING_RESULT_UNKNOWN:
        _require_no_payload(event)
        if state.status is not AgentRunStatus.PLANNING:
            raise AgentSupervisorBlocked("unknown planning result is not expected")
        updated = replace(
            state,
            status=AgentRunStatus.RECONCILIATION_REQUIRED,
            failure_code="PLANNING_RESULT_UNKNOWN",
            stale=state.graph is not None,
        )
    elif event.event_type is AgentEventType.TASK_GRAPH_ACCEPTED:
        if not isinstance(event.payload, TaskGraphPayload):
            raise AgentSupervisorBlocked("task graph event payload is invalid")
        graph = event.payload.graph
        _validate_compiled_graph(graph, state)
        if state.material_stage is not None:
            from .case_agent_material_stage import validate_supplementary_stage_graph
            validate_supplementary_stage_graph(stage=state.material_stage, graph=graph)
        if state.analysis_stage is not None:
            from .case_agent_analysis_stage import validate_case_analysis_stage_graph
            validate_case_analysis_stage_graph(stage=state.analysis_stage, graph=graph)
        if state.cancelled or state.status not in {
            AgentRunStatus.CREATED,
            AgentRunStatus.PLANNING,
            AgentRunStatus.STALE,
            AgentRunStatus.WAITING_INPUT,
            AgentRunStatus.RECONCILIATION_REQUIRED,
        }:
            raise AgentSupervisorBlocked("a task graph cannot replace the current run state")
        if (
            state.status is AgentRunStatus.RECONCILIATION_REQUIRED
            and state.failure_code != "PLANNING_RESULT_UNKNOWN"
        ):
            raise AgentSupervisorBlocked(
                "only a reconciled planning result may replace this run state"
            )
        tasks = tuple(
            TaskRuntimeState(spec=task, status=AgentTaskStatus.PENDING) for task in graph.tasks
        )
        updated = replace(
            state,
            graph=graph,
            snapshot=graph.snapshot,
            tasks=_refresh_task_readiness(tasks, (), graph.graph_hash),
            approvals=(),
            artifacts=(),
            status=AgentRunStatus.EXECUTING,
            stale=False,
            verification_hash=None,
            failure_code=None,
        )
        updated = _derive_run_status(updated)
    elif event.event_type is AgentEventType.APPROVAL_GRANTED:
        if not isinstance(event.payload, ApprovalPayload):
            raise AgentSupervisorBlocked("approval event payload is invalid")
        if state.graph is None or state.cancelled or state.stale:
            raise AgentSupervisorBlocked("approval requires a current task graph")
        approval = event.payload.approval
        _validate_approval(approval, state, event_actor_id=event.actor_id)
        if any(item.approval_id == approval.approval_id for item in state.approvals):
            raise AgentSupervisorBlocked("approval id was already recorded")
        approvals = (*state.approvals, approval)
        updated = replace(
            state,
            approvals=approvals,
            tasks=_refresh_task_readiness(state.tasks, approvals, state.graph.graph_hash),
        )
        updated = _derive_run_status(updated)
    elif event.event_type is AgentEventType.LAWYER_PLAN_CORRECTION_RECORDED:
        if not isinstance(event.payload, LawyerPlanCorrectionPayload):
            raise AgentSupervisorBlocked("lawyer correction event payload is invalid")
        if state.graph is None or state.cancelled:
            raise AgentSupervisorBlocked("lawyer correction requires a current task graph")
        for value, label in (
            (event.payload.signal_id, "lawyer correction signal_id"),
            (event.payload.task_id, "lawyer correction task_id"),
        ):
            _require_uuid(value, label)
        _require_sha256(event.payload.decision_hash, "lawyer correction decision_hash")
        _require_sha256(event.payload.subject_hash, "lawyer correction subject_hash")
        _require_code(event.payload.decision_code, "lawyer correction decision_code")
        if not event.payload.decision_code.startswith("LAWYER_REJECT_"):
            raise AgentSupervisorBlocked("lawyer correction code is outside policy")
        if not any(item.spec.task_id == event.payload.task_id for item in state.tasks):
            raise AgentSupervisorBlocked("lawyer correction task is outside the current graph")
        # A correction changes the planning input, not the formal case ledger.
        # It invalidates every non-running result and drives a fresh snapshot
        # and REPLAN through the durable Worker.  Running/unknown external
        # attempts remain reconcilable and can never be wished away.
        tasks = tuple(
            replace(item, status=AgentTaskStatus.STALE)
            if item.status not in {
                AgentTaskStatus.RUNNING,
                AgentTaskStatus.UNKNOWN,
                AgentTaskStatus.CANCELLED,
            }
            else item
            for item in state.tasks
        )
        next_status = (
            AgentRunStatus.RECONCILIATION_REQUIRED
            if any(item.status is AgentTaskStatus.UNKNOWN for item in tasks)
            else AgentRunStatus.STALE
        )
        updated = replace(
            state,
            status=next_status,
            tasks=tasks,
            stale=True,
            verification_hash=None,
            failure_code="LAWYER_PLAN_CORRECTION",
        )
    elif event.event_type is AgentEventType.TASK_STARTED:
        if not isinstance(event.payload, TaskStartedPayload):
            raise AgentSupervisorBlocked("task-start event payload is invalid")
        updated = _apply_task_started(state, event.payload)
    elif event.event_type is AgentEventType.TASK_RESULT_RECORDED:
        if not isinstance(event.payload, TaskResultPayload):
            raise AgentSupervisorBlocked("task-result event payload is invalid")
        updated = _apply_task_result(state, event.payload.receipt)
    elif event.event_type is AgentEventType.CASE_SNAPSHOT_CHANGED:
        if not isinstance(event.payload, SnapshotChangedPayload):
            raise AgentSupervisorBlocked("snapshot-change event payload is invalid")
        updated = _apply_snapshot_change(state, event.payload.snapshot)
    elif event.event_type is AgentEventType.RUN_PAUSED:
        _require_no_payload(event)
        if state.status in {
            AgentRunStatus.PAUSED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
        }:
            raise AgentSupervisorBlocked("run cannot be paused from its current state")
        updated = replace(state, status=AgentRunStatus.PAUSED, paused_from=state.status)
    elif event.event_type is AgentEventType.RUN_RESUMED:
        _require_no_payload(event)
        if state.status is not AgentRunStatus.PAUSED or state.paused_from is None:
            raise AgentSupervisorBlocked("only a paused run may resume")
        updated = _derive_run_status(replace(state, status=state.paused_from, paused_from=None))
    elif event.event_type is AgentEventType.RUN_CANCELLED:
        _require_no_payload(event)
        if state.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}:
            raise AgentSupervisorBlocked("a completed or failed run cannot be cancelled")
        # Dispatched work cannot be wished away.  Pending work is cancelled,
        # while RUNNING/UNKNOWN attempts remain eligible for a late receipt or
        # reconciliation.  Any late success is recorded as STALE, never used.
        tasks = tuple(
            replace(task, status=AgentTaskStatus.CANCELLED)
            if task.status
            not in {
                AgentTaskStatus.SUCCEEDED,
                AgentTaskStatus.RUNNING,
                AgentTaskStatus.UNKNOWN,
            }
            else task
            for task in state.tasks
        )
        updated = replace(
            state,
            status=AgentRunStatus.CANCELLED,
            tasks=tasks,
            cancelled=True,
            paused_from=None,
        )
    elif event.event_type is AgentEventType.VERIFICATION_STARTED:
        _require_no_payload(event)
        if state.status is not AgentRunStatus.VERIFYING or state.cancelled or state.stale:
            raise AgentSupervisorBlocked("verification prerequisites are not satisfied")
    elif event.event_type is AgentEventType.VERIFICATION_PASSED:
        if not isinstance(event.payload, VerificationPayload) or event.payload.error_code is not None:
            raise AgentSupervisorBlocked("verification success payload is invalid")
        _require_sha256(event.payload.verification_hash, "verification_hash")
        if state.status is not AgentRunStatus.VERIFYING:
            raise AgentSupervisorBlocked("verification result is not expected")
        updated = replace(
            state,
            status=AgentRunStatus.READY_FOR_REVIEW,
            verification_hash=event.payload.verification_hash,
        )
    elif event.event_type is AgentEventType.VERIFICATION_FAILED:
        if not isinstance(event.payload, VerificationPayload) or not event.payload.error_code:
            raise AgentSupervisorBlocked("verification failure payload is invalid")
        _require_code(event.payload.error_code, "verification error_code")
        if state.status is not AgentRunStatus.VERIFYING:
            raise AgentSupervisorBlocked("verification result is not expected")
        updated = replace(
            state,
            status=AgentRunStatus.FAILED,
            failure_code=event.payload.error_code,
        )
    elif event.event_type is AgentEventType.RUN_COMPLETED:
        if not isinstance(event.payload, RunCompletedPayload):
            raise AgentSupervisorBlocked("run completion requires an exact final review")
        if (
            state.status is not AgentRunStatus.READY_FOR_REVIEW
            or state.verification_hash is None
            or state.cancelled
            or state.stale
        ):
            raise AgentSupervisorBlocked("run is not ready to complete")
        _validate_final_review(event.payload.final_review, state, event.actor_id)
        updated = replace(state, status=AgentRunStatus.COMPLETED)
    else:
        raise AgentSupervisorBlocked("event type is not implemented by the supervisor")
    return replace(updated, event_version=event.sequence)


def replay_agent_events(events: Iterable[AgentSupervisorEvent]) -> AgentRunState:
    state: AgentRunState | None = None
    count = 0
    for event in events:
        state = reduce_agent_event(state, event)
        count += 1
    if state is None or count == 0:
        raise AgentSupervisorBlocked("an Agent run requires at least one event")
    return state


def decide_next_commands(state: AgentRunState) -> tuple[SupervisorCommand, ...]:
    """Return deterministic, idempotent commands for the durable dispatcher."""

    commands: list[SupervisorCommand] = []
    unknown = tuple(task for task in state.tasks if task.status is AgentTaskStatus.UNKNOWN)
    for task in unknown:
        receipt = task.receipts[-1]
        commands.append(
            _command(
                state,
                SupervisorCommandKind.RECONCILE_EXTERNAL_RESULT,
                task_id=task.spec.task_id,
                attempt_id=receipt.attempt_id,
                reason_code="EXTERNAL_RESULT_UNKNOWN",
            )
        )
    if unknown:
        return tuple(commands)
    if state.cancelled or state.status in {
        AgentRunStatus.PAUSED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
    }:
        return ()
    if (
        state.status is AgentRunStatus.RECONCILIATION_REQUIRED
        and state.failure_code == "PLANNING_RESULT_UNKNOWN"
    ):
        return (
            _command(
                state,
                SupervisorCommandKind.RECONCILE_PLAN_RESULT,
                reason_code="PLANNER_PROVIDER_RESULT_UNKNOWN",
            ),
        )
    if state.status is AgentRunStatus.WAITING_INPUT:
        # WAITING_INPUT is a human/operator gate for every terminal task or
        # planner failure.  Automatically asking the model for another graph
        # here creates an unbounded provider loop, and an old run can then
        # starve newer current-version work.  A fresh plan is only requested
        # after an explicit snapshot-change/correction event makes the run
        # STALE, or through a separately authorized replacement run.
        return ()
    if state.status is AgentRunStatus.PLANNING:
        # PLANNING_STARTED already owns one durable provider attempt.  Its
        # lease/outcome is recovered from the planning-attempt ledger; the
        # reducer must never emit a second provider request while it is live.
        return ()
    if state.stale or state.status is AgentRunStatus.STALE:
        return (
            _command(
                state,
                SupervisorCommandKind.REQUEST_REPLAN,
                reason_code="CASE_SNAPSHOT_CHANGED",
            ),
        )
    if state.graph is None:
        return (
            _command(
                state,
                SupervisorCommandKind.REQUEST_PLAN,
                reason_code="TASK_GRAPH_REQUIRED",
            ),
        )
    for task in state.tasks:
        if task.status is AgentTaskStatus.WAITING_APPROVAL:
            commands.append(
                _command(
                    state,
                    SupervisorCommandKind.REQUEST_APPROVAL,
                    task_id=task.spec.task_id,
                    reason_code=f"APPROVAL_{task.spec.approval_gate.value}_REQUIRED",
                )
            )
    for task in state.tasks:
        if task.status in {AgentTaskStatus.READY, AgentTaskStatus.RETRYABLE}:
            _require_dispatch_budget(state, task)
            commands.append(
                _command(
                    state,
                    SupervisorCommandKind.DISPATCH_TASK,
                    task_id=task.spec.task_id,
                    attempt_id=_next_attempt_id(state, task),
                    reason_code=(
                        "DEPENDENCIES_AND_GATES_SATISFIED"
                        if task.status is AgentTaskStatus.READY
                        else "SAFE_RETRY_ALLOWED"
                    ),
                )
            )
    if commands:
        return tuple(commands)
    if state.status is AgentRunStatus.VERIFYING:
        return (
            _command(
                state,
                SupervisorCommandKind.START_VERIFICATION,
                reason_code="ALL_TASKS_SUCCEEDED",
            ),
        )
    if state.status is AgentRunStatus.READY_FOR_REVIEW:
        return (
            _command(
                state,
                SupervisorCommandKind.REQUEST_FINAL_REVIEW,
                reason_code="FINAL_LAWYER_REVIEW_REQUIRED",
            ),
        )
    return ()


def _validate_task(
    task: AgentTaskSpec,
    *,
    registry: CaseSkillRegistry,
    adapters: Mapping[str, RuntimeAdapterManifest],
    run_budget: RunResourceBudget,
) -> None:
    if not isinstance(task, AgentTaskSpec):
        raise AgentSupervisorBlocked("task graph contains an invalid task")
    _require_uuid(task.task_id, "task_id")
    _require_positive(task.sequence, "task sequence")
    _require_business_text(task.title, "task title", 500)
    _require_business_text(task.purpose, "task purpose", 2000)
    _require_business_text(task.rationale, "task rationale", 4000)
    _require_sha256(task.input_hash, "task input_hash")
    if not task.input_refs or len(task.input_refs) > 500:
        raise AgentSupervisorBlocked("task requires bounded server-owned input references")
    for input_ref in task.input_refs:
        _require_identifier(input_ref, "task input reference")
    if len(set(task.input_refs)) != len(task.input_refs):
        raise AgentSupervisorBlocked("task input references must be unique")
    task.budget.validate()
    if task.budget.max_cost_minor_units > run_budget.max_cost_minor_units:
        raise AgentSupervisorBlocked("task cost budget exceeds the run budget")
    if task.budget.max_external_calls > run_budget.max_external_calls:
        raise AgentSupervisorBlocked("task external-call budget exceeds the run budget")
    if task.budget.max_output_bytes > run_budget.max_output_bytes:
        raise AgentSupervisorBlocked("task output budget exceeds the run budget")
    task.capability.validate()
    if task.risk_level is AgentRiskLevel.PROHIBITED or task.autonomy_level is AgentAutonomyLevel.A4_PROHIBITED:
        raise AgentSupervisorBlocked("prohibited legal or external action cannot enter the task graph")
    if task.risk_level is AgentRiskLevel.HIGH and task.autonomy_level is not AgentAutonomyLevel.A3_LAWYER_APPROVAL:
        raise AgentSupervisorBlocked("high-risk work cannot be configured for autonomous execution")
    if task.autonomy_level is AgentAutonomyLevel.A3_LAWYER_APPROVAL and task.approval_gate not in {
        ApprovalGate.LAWYER_REVIEW,
        ApprovalGate.RELEASE_LOCK,
    }:
        raise AgentSupervisorBlocked("A3 work requires an exact lawyer or release approval gate")
    try:
        skill = registry.get_skill(task.skill.skill_id)
        tool = registry.authorize_tool(
            skill_id=task.skill.skill_id,
            tool_id=task.skill.tool_id,
            granted_scopes=task.granted_scopes,
            lawyer_approved=True,
            release_locked=True,
        )
    except SkillRegistryBlocked as error:
        raise AgentSupervisorBlocked(f"task Skill/Tool is not executable: {error}") from error
    if skill.maturity is not SkillMaturity.IMPLEMENTED:
        raise AgentSupervisorBlocked("task Skill is not implemented in this runtime")
    if (
        skill.version != task.skill.skill_version
        or tool.version != task.skill.tool_version
        or not _gate_satisfies(task.approval_gate, skill.approval_gate)
    ):
        raise AgentSupervisorBlocked("task binding differs from the active Skill policy")
    adapter = adapters.get(task.skill.tool_id)
    if adapter is None:
        raise AgentSupervisorBlocked("task Tool has no concrete runtime adapter")
    adapter.validate()
    if (
        adapter.tool_id != task.skill.tool_id
        or adapter.adapter_id != task.skill.adapter_id
        or adapter.adapter_version != task.skill.adapter_version
        or adapter.execution_mode is not task.capability.execution_mode
        or adapter.sandbox_policy_version != task.capability.sandbox_policy_version
        or adapter.sandbox_policy_hash != task.capability.sandbox_policy_hash
    ):
        raise AgentSupervisorBlocked("task binding differs from the concrete runtime adapter")
    if not adapter.supports_idempotency:
        raise AgentSupervisorBlocked("runtime adapter lacks an idempotent execution contract")
    if task.retry_mode is not RetryMode.NEVER_AUTOMATIC and task.budget.max_attempts > 1:
        if not adapter.supports_idempotency:
            raise AgentSupervisorBlocked("retryable task requires an idempotent adapter")
    if tool.allows_external_network:
        if (
            not adapter.network_capable
            or task.capability.network_policy is not NetworkPolicy.EXACT_ALLOWLIST
            or not task.capability.external_request_approval_required
            or not adapter.supports_reconciliation
        ):
            raise AgentSupervisorBlocked(
                "external Tool requires allowlisted network, approval and reconciliation"
            )
    elif task.capability.network_policy is not NetworkPolicy.DENY or adapter.network_capable:
        raise AgentSupervisorBlocked("a local Tool cannot receive an implicit network capability")
    if tool.writes_only_managed_derivatives != task.capability.writes_managed_derivatives:
        raise AgentSupervisorBlocked("task derivative-write contract differs from the Tool policy")
    if task.retry_mode is RetryMode.BEFORE_EXTERNAL_SUBMISSION_ONLY and not adapter.supports_reconciliation:
        raise AgentSupervisorBlocked("external-safe retry policy requires reconciliation support")


def _validate_dag(tasks: tuple[AgentTaskSpec, ...], task_ids: set[str]) -> None:
    graph = {task.task_id: task.dependency_ids for task in tasks}
    for task in tasks:
        if len(set(task.dependency_ids)) != len(task.dependency_ids):
            raise AgentSupervisorBlocked("task dependencies must be unique")
        for dependency in task.dependency_ids:
            _require_uuid(dependency, "dependency task_id")
            if dependency == task.task_id or dependency not in task_ids:
                raise AgentSupervisorBlocked("task dependency is missing or self-referential")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise AgentSupervisorBlocked("task graph contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def _validate_event_envelope(
    state: AgentRunState | None, event: AgentSupervisorEvent
) -> None:
    _require_uuid(event.event_id, "event_id")
    _require_uuid(event.run_id, "run_id")
    _require_uuid(event.firm_id, "firm_id")
    _require_uuid(event.matter_id, "matter_id")
    _require_identifier(event.actor_id, "event actor_id")
    if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
        raise AgentSupervisorBlocked("event occurred_at must include a timezone")
    expected_sequence = 1 if state is None else state.event_version + 1
    if event.sequence != expected_sequence:
        raise AgentSupervisorBlocked("event sequence is not contiguous")
    if state is not None and (
        event.run_id != state.run_id
        or event.firm_id != state.firm_id
        or event.matter_id != state.matter_id
    ):
        raise AgentSupervisorBlocked("event is outside this Agent run")


def _validate_compiled_graph(graph: AgentTaskGraph, state: AgentRunState) -> None:
    if graph.goal_hash != state.goal.goal_hash or graph.snapshot.matter_id != state.matter_id:
        raise AgentSupervisorBlocked("task graph is outside this goal or matter")
    if graph.snapshot != state.snapshot:
        raise AgentSupervisorBlocked("task graph is not bound to the current case snapshot")
    if state.graph is not None and graph.graph_version <= state.graph.graph_version:
        raise AgentSupervisorBlocked("replacement task graph version must increase")
    expected = _task_graph_hash(
        graph_id=graph.graph_id,
        graph_version=graph.graph_version,
        goal_hash=graph.goal_hash,
        snapshot=graph.snapshot,
        tasks=graph.tasks,
    )
    if graph.graph_hash != expected:
        raise AgentSupervisorBlocked("task graph hash differs from its contents")


def _apply_task_started(state: AgentRunState, payload: TaskStartedPayload) -> AgentRunState:
    if state.graph is None or state.cancelled or state.stale or state.status is AgentRunStatus.PAUSED:
        raise AgentSupervisorBlocked("task cannot start without a current executable graph")
    _require_uuid(payload.attempt_id, "attempt_id")
    if payload.graph_hash != state.graph.graph_hash:
        raise AgentSupervisorBlocked("task start is bound to another graph")
    task = _task_by_id(state.tasks, payload.task_id)
    if task.status not in {AgentTaskStatus.READY, AgentTaskStatus.RETRYABLE}:
        raise AgentSupervisorBlocked("task is not ready to start")
    if payload.input_hash != task.spec.input_hash:
        raise AgentSupervisorBlocked("task start input hash changed")
    if payload.attempt_id != _next_attempt_id(state, task):
        raise AgentSupervisorBlocked("task attempt id is not the deterministic dispatcher id")
    _require_dispatch_budget(state, task)
    updated_task = replace(
        task,
        status=AgentTaskStatus.RUNNING,
        attempt_count=task.attempt_count + 1,
        active_attempt_id=payload.attempt_id,
    )
    return replace(
        state,
        status=AgentRunStatus.EXECUTING,
        tasks=_replace_task(state.tasks, updated_task),
        budget_usage=replace(
            state.budget_usage,
            attempts=state.budget_usage.attempts + 1,
        ),
    )


def _apply_task_result(state: AgentRunState, receipt: TaskResultReceipt) -> AgentRunState:
    task = _task_by_id(state.tasks, receipt.task_id)
    _validate_result_receipt(receipt, task)
    reconciling = task.status is AgentTaskStatus.UNKNOWN
    if task.status is not AgentTaskStatus.RUNNING and not reconciling:
        raise AgentSupervisorBlocked("task result has no running or unknown attempt")
    if receipt.attempt_id != task.active_attempt_id:
        raise AgentSupervisorBlocked("task result attempt does not match the active attempt")
    if reconciling and receipt.status is ResultStatus.UNKNOWN:
        raise AgentSupervisorBlocked("reconciliation cannot record another unknown result")
    receipts = (*task.receipts, receipt)
    artifacts = state.artifacts
    output_bytes = 0
    if receipt.status is ResultStatus.SUCCEEDED:
        if state.cancelled or state.stale:
            next_status = AgentTaskStatus.STALE
        else:
            next_status = AgentTaskStatus.SUCCEEDED
        artifacts = (*artifacts, *receipt.artifacts)
        output_bytes = sum(item.byte_size for item in receipt.artifacts)
    elif receipt.status is ResultStatus.UNKNOWN:
        next_status = AgentTaskStatus.UNKNOWN
    elif state.cancelled:
        next_status = AgentTaskStatus.CANCELLED
    elif state.stale:
        next_status = AgentTaskStatus.STALE
    else:
        next_status = (
            AgentTaskStatus.RETRYABLE
            if _may_retry(task, receipt, state)
            else AgentTaskStatus.FAILED
        )
    updated_task = replace(task, status=next_status, receipts=receipts)
    usage = replace(
        state.budget_usage,
        external_calls=state.budget_usage.external_calls + receipt.external_calls,
        runtime_seconds=state.budget_usage.runtime_seconds + receipt.runtime_seconds,
        cost_minor_units=state.budget_usage.cost_minor_units + receipt.cost_minor_units,
        output_bytes=state.budget_usage.output_bytes + output_bytes,
    )
    _validate_budget_usage(state.budget, usage)
    updated = replace(
        state,
        tasks=_replace_task(state.tasks, updated_task),
        artifacts=artifacts,
        budget_usage=usage,
    )
    if state.cancelled:
        return replace(updated, status=AgentRunStatus.CANCELLED)
    if state.stale:
        return replace(updated, status=AgentRunStatus.STALE)
    updated = replace(
        updated,
        tasks=_refresh_task_readiness(updated.tasks, updated.approvals, state.graph.graph_hash),
    )
    derived = _derive_run_status(updated)
    if state.status is AgentRunStatus.PAUSED:
        return replace(derived, status=AgentRunStatus.PAUSED, paused_from=derived.status)
    return derived


def _validate_result_receipt(receipt: TaskResultReceipt, task: TaskRuntimeState) -> None:
    _require_uuid(receipt.receipt_id, "receipt_id")
    _require_uuid(receipt.task_id, "receipt task_id")
    _require_uuid(receipt.attempt_id, "receipt attempt_id")
    _require_sha256(receipt.input_hash, "receipt input_hash")
    _require_code(receipt.adapter_id, "receipt adapter_id")
    _require_semver(receipt.adapter_version, "receipt adapter_version")
    if (
        receipt.input_hash != task.spec.input_hash
        or receipt.adapter_id != task.spec.skill.adapter_id
        or receipt.adapter_version != task.spec.skill.adapter_version
    ):
        raise AgentSupervisorBlocked("task receipt differs from its immutable execution binding")
    for name, value in (
        ("runtime_seconds", receipt.runtime_seconds),
        ("cost_minor_units", receipt.cost_minor_units),
        ("external_calls", receipt.external_calls),
    ):
        _require_non_negative(value, name)
    if receipt.runtime_seconds > task.spec.budget.timeout_seconds:
        raise AgentSupervisorBlocked("task receipt exceeds its runtime budget")
    if receipt.cost_minor_units > task.spec.budget.max_cost_minor_units:
        raise AgentSupervisorBlocked("task receipt exceeds its cost budget")
    if receipt.external_calls > task.spec.budget.max_external_calls:
        raise AgentSupervisorBlocked("task receipt exceeds its external-call budget")
    network_task = task.spec.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST
    if receipt.external_submission_state is ExternalSubmissionState.UNKNOWN and receipt.status is not ResultStatus.UNKNOWN:
        raise AgentSupervisorBlocked("an uncertain external submission must remain an unknown result")
    if receipt.status is ResultStatus.UNKNOWN and not network_task:
        raise AgentSupervisorBlocked("a local task cannot report an unknown external result")
    if not network_task and receipt.external_submission_state is not ExternalSubmissionState.NOT_APPLICABLE:
        raise AgentSupervisorBlocked("a local task cannot report an external submission state")
    if receipt.status is ResultStatus.SUCCEEDED:
        _require_sha256(receipt.output_hash or "", "result output_hash")
        if receipt.error_code is not None:
            raise AgentSupervisorBlocked("successful task result cannot carry an error code")
    elif receipt.status is ResultStatus.FAILED:
        if receipt.output_hash is not None:
            raise AgentSupervisorBlocked("failed task result cannot carry an output hash")
        _require_code(receipt.error_code or "", "result error_code")
    else:
        if receipt.output_hash is not None:
            raise AgentSupervisorBlocked("unknown task result cannot carry an output hash")
        if receipt.external_submission_state is not ExternalSubmissionState.UNKNOWN:
            raise AgentSupervisorBlocked("unknown result requires an unknown external submission state")
        _require_identifier(receipt.external_request_id or "", "external_request_id")
    if receipt.external_submission_state is ExternalSubmissionState.NOT_APPLICABLE:
        if receipt.external_request_id is not None or receipt.external_calls != 0:
            raise AgentSupervisorBlocked("local result cannot carry an external request")
    else:
        _require_identifier(receipt.external_request_id or "", "external_request_id")
    if receipt.status is not ResultStatus.SUCCEEDED and receipt.artifacts:
        raise AgentSupervisorBlocked("only a successful task result may register artifacts")
    artifact_ids: set[str] = set()
    output_bytes = 0
    for artifact in receipt.artifacts:
        artifact.validate()
        if artifact.artifact_id in artifact_ids:
            raise AgentSupervisorBlocked("task receipt contains duplicate artifact ids")
        if artifact.source_input_hash != task.spec.input_hash:
            raise AgentSupervisorBlocked("artifact is not bound to the task input")
        if artifact.managed_derivative != task.spec.capability.writes_managed_derivatives:
            raise AgentSupervisorBlocked("artifact storage contract differs from the task capability")
        artifact_ids.add(artifact.artifact_id)
        output_bytes += artifact.byte_size
    if output_bytes > task.spec.budget.max_output_bytes:
        raise AgentSupervisorBlocked("task artifacts exceed the output budget")


def _apply_snapshot_change(state: AgentRunState, snapshot: CaseSnapshotRef) -> AgentRunState:
    snapshot.validate()
    if snapshot.matter_id != state.matter_id:
        raise AgentSupervisorBlocked("changed snapshot belongs to another matter")
    if snapshot.matter_version < state.snapshot.matter_version:
        raise AgentSupervisorBlocked("case snapshot cannot move backwards")
    if snapshot == state.snapshot:
        raise AgentSupervisorBlocked("snapshot-change event must change version or content")
    tasks = tuple(
        replace(task, status=AgentTaskStatus.STALE)
        if task.status
        not in {
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.UNKNOWN,
            AgentTaskStatus.CANCELLED,
        }
        else task
        for task in state.tasks
    )
    next_status = (
        AgentRunStatus.RECONCILIATION_REQUIRED
        if any(task.status is AgentTaskStatus.UNKNOWN for task in tasks)
        else AgentRunStatus.STALE
    )
    if state.status is AgentRunStatus.PAUSED:
        return replace(
            state,
            snapshot=snapshot,
            status=AgentRunStatus.PAUSED,
            paused_from=next_status,
            tasks=tasks,
            stale=True,
            verification_hash=None,
        )
    return replace(
        state,
        snapshot=snapshot,
        status=next_status,
        tasks=tasks,
        stale=True,
        verification_hash=None,
    )


def _refresh_task_readiness(
    tasks: tuple[TaskRuntimeState, ...],
    approvals: tuple[ApprovalRecord, ...],
    graph_hash: str,
) -> tuple[TaskRuntimeState, ...]:
    statuses = {task.spec.task_id: task.status for task in tasks}
    refreshed: list[TaskRuntimeState] = []
    for task in tasks:
        if task.status not in {
            AgentTaskStatus.PENDING,
            AgentTaskStatus.READY,
            AgentTaskStatus.WAITING_APPROVAL,
        }:
            refreshed.append(task)
            continue
        dependencies_succeeded = all(
            statuses.get(dependency) is AgentTaskStatus.SUCCEEDED
            for dependency in task.spec.dependency_ids
        )
        if not dependencies_succeeded:
            refreshed.append(replace(task, status=AgentTaskStatus.PENDING))
            continue
        if _task_requires_approval(task.spec) and not _has_exact_approval(
            task.spec, approvals, graph_hash
        ):
            refreshed.append(replace(task, status=AgentTaskStatus.WAITING_APPROVAL))
            continue
        refreshed.append(replace(task, status=AgentTaskStatus.READY))
    return tuple(refreshed)


def _derive_run_status(state: AgentRunState) -> AgentRunState:
    if state.cancelled:
        return replace(state, status=AgentRunStatus.CANCELLED)
    if state.stale:
        return replace(state, status=AgentRunStatus.STALE)
    statuses = {task.status for task in state.tasks}
    if not statuses:
        # A newly created or still-planning run can be paused before a graph
        # exists.  Resuming must restore that exact phase, not invent an
        # EXECUTING state with zero tasks.
        return state
    if AgentTaskStatus.UNKNOWN in statuses:
        status = AgentRunStatus.RECONCILIATION_REQUIRED
    elif AgentTaskStatus.FAILED in statuses:
        if state.task_failure_policy == "ISOLATE_KNOWN_TASK_FAILURES_V1" and statuses.intersection(
            {AgentTaskStatus.READY, AgentTaskStatus.RETRYABLE, AgentTaskStatus.RUNNING}
        ):
            status = AgentRunStatus.EXECUTING
        elif state.task_failure_policy == "ISOLATE_KNOWN_TASK_FAILURES_V1" and AgentTaskStatus.WAITING_APPROVAL in statuses:
            status = AgentRunStatus.WAITING_APPROVAL
        else:
            status = AgentRunStatus.WAITING_INPUT
    elif statuses and statuses == {AgentTaskStatus.SUCCEEDED}:
        status = AgentRunStatus.VERIFYING
    elif AgentTaskStatus.WAITING_APPROVAL in statuses and not statuses.intersection(
        {AgentTaskStatus.READY, AgentTaskStatus.RETRYABLE, AgentTaskStatus.RUNNING}
    ):
        status = AgentRunStatus.WAITING_APPROVAL
    else:
        status = AgentRunStatus.EXECUTING
    return replace(state, status=status)


def _validate_approval(
    approval: ApprovalRecord,
    state: AgentRunState,
    *,
    event_actor_id: str,
) -> None:
    _require_uuid(approval.approval_id, "approval_id")
    _require_identifier(approval.approved_by, "approved_by")
    _require_sha256(approval.approval_hash, "approval_hash")
    if approval.approved_by != event_actor_id:
        raise AgentSupervisorBlocked("approval actor differs from the persisted event actor")
    task = _task_by_id(state.tasks, approval.task_id).spec
    if (
        approval.task_input_hash != task.input_hash
        or approval.graph_hash != state.graph.graph_hash
        or approval.gate is not task.approval_gate
    ):
        raise AgentSupervisorBlocked("approval is not bound to the exact current task and graph")
    expected = ApprovalRecord.build(
        approval_id=approval.approval_id,
        task=task,
        graph_hash=state.graph.graph_hash,
        gate=approval.gate,
        approved_by=approval.approved_by,
    )
    if approval.approval_hash != expected.approval_hash:
        raise AgentSupervisorBlocked("approval hash differs from its exact contents")


def _validate_final_review(
    approval: RunFinalReviewApproval,
    state: AgentRunState,
    event_actor_id: str,
) -> None:
    if approval.approved_by != event_actor_id:
        raise AgentSupervisorBlocked("final review actor differs from the persisted event actor")
    expected = RunFinalReviewApproval.build(
        approval_id=approval.approval_id,
        state=state,
        approved_by=approval.approved_by,
        document_review_versions=approval.document_review_versions,
    )
    if approval != expected:
        raise AgentSupervisorBlocked(
            "final review differs from the current graph, verification or artifact manifest"
        )


def _may_retry(
    task: TaskRuntimeState,
    receipt: TaskResultReceipt,
    state: AgentRunState,
) -> bool:
    if task.attempt_count >= task.spec.budget.max_attempts:
        return False
    if state.budget_usage.attempts >= state.budget.max_total_attempts:
        return False
    if task.spec.retry_mode is RetryMode.NEVER_AUTOMATIC:
        return False
    if receipt.external_submission_state in {
        ExternalSubmissionState.SUBMITTED,
        ExternalSubmissionState.UNKNOWN,
    }:
        return False
    if task.spec.retry_mode is RetryMode.BEFORE_EXTERNAL_SUBMISSION_ONLY:
        return receipt.external_submission_state is ExternalSubmissionState.NOT_SUBMITTED
    return receipt.external_submission_state in {
        ExternalSubmissionState.NOT_APPLICABLE,
        ExternalSubmissionState.NOT_SUBMITTED,
    }


def _require_dispatch_budget(state: AgentRunState, task: TaskRuntimeState) -> None:
    if state.budget_usage.attempts >= state.budget.max_total_attempts:
        raise AgentSupervisorBlocked("run attempt budget is exhausted")
    if task.attempt_count >= task.spec.budget.max_attempts:
        raise AgentSupervisorBlocked("task attempt budget is exhausted")
    if state.budget_usage.external_calls >= state.budget.max_external_calls and (
        task.spec.capability.network_policy is NetworkPolicy.EXACT_ALLOWLIST
    ):
        raise AgentSupervisorBlocked("run external-call budget is exhausted")


def _validate_budget_usage(budget: RunResourceBudget, usage: BudgetUsage) -> None:
    for value, maximum, label in (
        (usage.attempts, budget.max_total_attempts, "attempt"),
        (usage.external_calls, budget.max_external_calls, "external-call"),
        (usage.runtime_seconds, budget.max_runtime_seconds, "runtime"),
        (usage.cost_minor_units, budget.max_cost_minor_units, "cost"),
        (usage.output_bytes, budget.max_output_bytes, "output"),
    ):
        if value > maximum:
            raise AgentSupervisorBlocked(f"run {label} budget would be exceeded")


def _task_requires_approval(task: AgentTaskSpec) -> bool:
    return task.approval_gate is not ApprovalGate.NONE or task.autonomy_level is AgentAutonomyLevel.A3_LAWYER_APPROVAL


def _has_exact_approval(
    task: AgentTaskSpec,
    approvals: tuple[ApprovalRecord, ...],
    graph_hash: str,
) -> bool:
    return any(
        approval.task_id == task.task_id
        and approval.task_input_hash == task.input_hash
        and approval.graph_hash == graph_hash
        and approval.gate is task.approval_gate
        for approval in approvals
    )


def _gate_satisfies(task_gate: ApprovalGate, skill_gate: ApprovalGate) -> bool:
    if task_gate is skill_gate:
        return True
    stronger = {
        ApprovalGate.NONE: {
            ApprovalGate.MATERIAL_SCOPE,
            ApprovalGate.LAWYER_REVIEW,
            ApprovalGate.RELEASE_LOCK,
        },
        ApprovalGate.MATERIAL_SCOPE: {ApprovalGate.LAWYER_REVIEW, ApprovalGate.RELEASE_LOCK},
        ApprovalGate.LAWYER_REVIEW: {ApprovalGate.RELEASE_LOCK},
        ApprovalGate.RELEASE_LOCK: set(),
    }
    return task_gate in stronger[skill_gate]


def _task_graph_hash(
    *,
    graph_id: str,
    graph_version: int,
    goal_hash: str,
    snapshot: CaseSnapshotRef,
    tasks: tuple[AgentTaskSpec, ...],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "lawyer-agent-task-graph-v1",
            "graph_id": graph_id,
            "graph_version": graph_version,
            "goal_hash": goal_hash,
            "snapshot": _snapshot_payload(snapshot),
            "tasks": [_task_payload(task) for task in tasks],
        }
    )


def _task_payload(task: AgentTaskSpec) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "sequence": task.sequence,
        "title": task.title,
        "purpose": task.purpose,
        "rationale": task.rationale,
        "dependency_ids": task.dependency_ids,
        "input_refs": task.input_refs,
        "input_hash": task.input_hash,
        "skill": {
            "skill_id": task.skill.skill_id,
            "skill_version": task.skill.skill_version,
            "tool_id": task.skill.tool_id,
            "tool_version": task.skill.tool_version,
            "adapter_id": task.skill.adapter_id,
            "adapter_version": task.skill.adapter_version,
        },
        "granted_scopes": sorted(scope.value for scope in task.granted_scopes),
        "capability": {
            "execution_mode": task.capability.execution_mode.value,
            "network_policy": task.capability.network_policy.value,
            "allowed_domains": task.capability.allowed_domains,
            "sandbox_profile": task.capability.sandbox_profile,
            "sandbox_policy_version": task.capability.sandbox_policy_version,
            "sandbox_policy_hash": task.capability.sandbox_policy_hash,
            "reads_case_objects": task.capability.reads_case_objects,
            "writes_managed_derivatives": task.capability.writes_managed_derivatives,
            "external_request_approval_required": task.capability.external_request_approval_required,
        },
        "risk_level": task.risk_level.value,
        "autonomy_level": task.autonomy_level.value,
        "approval_gate": task.approval_gate.value,
        "retry_mode": task.retry_mode.value,
        "budget": {
            "max_attempts": task.budget.max_attempts,
            "timeout_seconds": task.budget.timeout_seconds,
            "max_external_calls": task.budget.max_external_calls,
            "max_cost_minor_units": task.budget.max_cost_minor_units,
            "max_output_bytes": task.budget.max_output_bytes,
        },
    }


def _snapshot_payload(snapshot: CaseSnapshotRef) -> dict[str, object]:
    return {
        "matter_id": snapshot.matter_id,
        "matter_version": snapshot.matter_version,
        "snapshot_hash": snapshot.snapshot_hash,
        "schema_version": snapshot.schema_version,
    }


def _artifact_manifest_hash(artifacts: tuple[ArtifactReceipt, ...]) -> str:
    return _canonical_hash(
        {
            "schema_version": "lawyer-agent-artifact-manifest-v1",
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_kind": artifact.artifact_kind,
                    "content_hash": artifact.content_hash,
                    "byte_size": artifact.byte_size,
                    "source_input_hash": artifact.source_input_hash,
                    "managed_derivative": artifact.managed_derivative,
                }
                for artifact in sorted(artifacts, key=lambda item: item.artifact_id)
            ],
        }
    )


def _command(
    state: AgentRunState,
    kind: SupervisorCommandKind,
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
    reason_code: str,
) -> SupervisorCommand:
    command_id = _canonical_hash(
        {
            "schema_version": "lawyer-agent-supervisor-command-v1",
            "run_id": state.run_id,
            "event_version": state.event_version,
            "kind": kind.value,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "graph_hash": state.graph.graph_hash if state.graph else None,
            "reason_code": reason_code,
        }
    )
    return SupervisorCommand(
        command_id=command_id,
        kind=kind,
        run_id=state.run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        graph_hash=state.graph.graph_hash if state.graph else None,
        reason_code=reason_code,
    )


def _next_attempt_id(state: AgentRunState, task: TaskRuntimeState) -> str:
    digest = _canonical_hash(
        {
            "schema_version": "lawyer-agent-attempt-v1",
            "run_id": state.run_id,
            "graph_hash": state.graph.graph_hash if state.graph else None,
            "task_id": task.spec.task_id,
            "attempt_number": task.attempt_count + 1,
            "input_hash": task.spec.input_hash,
        }
    )
    # UUID v5 shape derived without any ambient randomness.  The digest is
    # reformatted only for interoperability with event-store UUID columns.
    return f"{digest[:8]}-{digest[8:12]}-5{digest[13:16]}-a{digest[17:20]}-{digest[20:32]}"


def _replace_task(
    tasks: tuple[TaskRuntimeState, ...], updated: TaskRuntimeState
) -> tuple[TaskRuntimeState, ...]:
    return tuple(updated if task.spec.task_id == updated.spec.task_id else task for task in tasks)


def _task_by_id(tasks: tuple[TaskRuntimeState, ...], task_id: str) -> TaskRuntimeState:
    for task in tasks:
        if task.spec.task_id == task_id:
            return task
    raise AgentSupervisorBlocked("task does not exist in the current graph")


def _validate_goal(goal: AgentGoal) -> None:
    expected = AgentGoal.build(
        goal_id=goal.goal_id,
        objective=goal.objective,
        success_criteria=goal.success_criteria,
        constraints=goal.constraints,
        requested_by=goal.requested_by,
        requested_deliverables=goal.requested_deliverables,
        active_plan_execution=goal.active_plan_execution,
        material_read_refs=goal.material_read_refs,
    )
    if goal != expected:
        raise AgentSupervisorBlocked("goal hash differs from its exact business intent")


def _require_no_payload(event: AgentSupervisorEvent) -> None:
    if event.payload is not None:
        raise AgentSupervisorBlocked("event must not contain a payload")


def _bounded_text_tuple(
    values: Iterable[str], label: str, *, maximum_items: int, maximum_length: int
) -> tuple[str, ...]:
    result = tuple(_require_business_text(value, label, maximum_length) for value in values)
    if not result or len(result) > maximum_items:
        raise AgentSupervisorBlocked(f"{label} must contain 1 to {maximum_items} items")
    return result


def _optional_bounded_text_tuple(
    values: Iterable[str], label: str, *, maximum_items: int, maximum_length: int
) -> tuple[str, ...]:
    result = tuple(_require_business_text(value, label, maximum_length) for value in values)
    if len(result) > maximum_items:
        raise AgentSupervisorBlocked(f"{label} cannot exceed {maximum_items} items")
    return result


def _require_business_text(value: str, label: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise AgentSupervisorBlocked(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum_length:
        raise AgentSupervisorBlocked(f"{label} is missing or too long")
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise AgentSupervisorBlocked(f"{label} contains control characters")
    return normalized


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise AgentSupervisorBlocked(f"{label} must be a UUID") from error


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise AgentSupervisorBlocked(f"{label} is invalid")


def _require_code(value: str, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise AgentSupervisorBlocked(f"{label} must be a stable code")


def _require_semver(value: str, label: str) -> None:
    if not isinstance(value, str) or _SEMVER_RE.fullmatch(value) is None:
        raise AgentSupervisorBlocked(f"{label} must be a semantic version")


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AgentSupervisorBlocked(f"{label} must be a lowercase SHA-256")


def _require_positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentSupervisorBlocked(f"{label} must be positive")


def _require_non_negative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentSupervisorBlocked(f"{label} must be a non-negative integer")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
        ).encode("utf-8")
    ).hexdigest()


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,199}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
)


__all__ = (
    "AdapterExecutionMode",
    "ActivePlanDeliverableRef",
    "ActivePlanExecutionRef",
    "AgentAutonomyLevel",
    "AgentDeliverableFormat",
    "AgentDeliverableKind",
    "AgentEventType",
    "AgentGoal",
    "AgentRiskLevel",
    "AgentRunState",
    "AgentRunStatus",
    "AgentSupervisorBlocked",
    "AgentSupervisorEvent",
    "AgentTaskGraph",
    "AgentTaskSpec",
    "AgentTaskStatus",
    "ApprovalPayload",
    "ApprovalRecord",
    "LawyerPlanCorrectionPayload",
    "ArtifactReceipt",
    "BudgetUsage",
    "CaseSnapshotRef",
    "ExternalSubmissionState",
    "NetworkPolicy",
    "PlanningFailurePayload",
    "ResultStatus",
    "RetryMode",
    "RunCreatedPayload",
    "RunCompletedPayload",
    "RunFinalReviewApproval",
    "RunResourceBudget",
    "RuntimeAdapterManifest",
    "SkillBinding",
    "SnapshotChangedPayload",
    "SupervisorCommand",
    "SupervisorCommandKind",
    "TaskCapabilityContract",
    "TaskGraphPayload",
    "TaskResourceBudget",
    "TaskResultPayload",
    "TaskResultReceipt",
    "TaskStartedPayload",
    "VerificationPayload",
    "compile_task_graph",
    "decide_next_commands",
    "reduce_agent_event",
    "replay_agent_events",
)
