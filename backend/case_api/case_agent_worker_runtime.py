"""Composition root and long-running process for one firm-scoped Agent Worker.

The Web API creates the authoritative run event.  Migration 0033 wakes the
durable inbox in that same database transaction; this independent process
leases the projection, lets ``CaseAgentWorker`` execute one supervisor command,
and settles only when the event version has not advanced underneath it.

There is intentionally no implicit planner or planning-snapshot fallback.
Production must inject both, so missing provider configuration cannot look
like an available Agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import signal
from threading import Event
from time import monotonic
from typing import Callable, Mapping, Protocol

from case_kernel.case_agent_planner import (
    CaseAgentPlannerCompiler,
    ServerSkillExecutionPolicy,
)
from case_kernel.controlled_defence_case_agent_planner import (
    ControlledDefencePlanningSnapshotProvider,
)
from case_kernel.case_agent_postgres import PostgresCaseAgentStore
from case_kernel.case_agent_planning_snapshot import (
    AuthoritativeCasePlanningSnapshotProvider,
    ExecutablePlanningSkill,
    PlanningProjectionObjectType,
)
from case_kernel.case_agent_planning_memory import (
    MemoryEnrichedPlanningSnapshotProvider,
    PlanningMemoryEnrichmentPort,
    PlanningMemorySearchRequestFactory,
)
from case_kernel.case_agent_research_adapters import (
    PUBLIC_WEB_RESEARCH_MANIFEST,
    PublicResearchBindingPort,
    DurablePublicSearchExchange,
    PublicWebResearchTaskAdapter,
)
from case_kernel.qwen_visual_ocr_adapter import (
    QWEN_VISUAL_OCR_COST_RESERVE_MINOR_UNITS,
    QWEN_VISUAL_OCR_MANIFEST,
    DurableQwenVisualOcrExchange,
    VisualOcrBindingPort,
    configured_qwen_visual_ocr_adapter,
    qwen_visual_ocr_server_policy,
)
from case_kernel.brave_public_search import (
    BRAVE_SEARCH_HOST,
    BravePublicSearchProvider,
)
from case_kernel.case_agent_skill_adapters import (
    CommonDocumentTaskAdapter,
    PdfTextTaskAdapter,
    WebEvidencePageTaskProjectionPort,
)
from case_kernel.case_agent_case_context_adapters import (
    CASE_CONTEXT_REVIEW_MANIFEST,
    DeterministicCaseContextTaskAdapter,
)
from case_kernel.case_agent_legal_research_plan_adapters import (
    LEGAL_RESEARCH_PLANNING_MANIFEST,
    LEGAL_RESEARCH_PLANNING_SKILL_ID,
    DeterministicLegalResearchPlanningTaskAdapter,
)
from case_kernel.case_agent_lawyer_analysis import (
    LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
    valid_qwen_lawyer_analysis_host,
)
from case_kernel.case_agent_lawyer_analysis_adapters import (
    LAWYER_ANALYSIS_SKILL_ID,
    LAWYER_ANALYSIS_TOOL_ID,
    QWEN_LAWYER_ANALYSIS_MANIFEST,
    QwenLawyerAnalysisTaskAdapter,
    RecoverableLawyerAnalysisExchange,
)
from case_kernel.case_agent_lawyer_analysis_transport import (
    LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_HOST,
    DEEPSEEK_LEDGER_EXTRACTION_MANIFEST,
    DeepSeekLedgerExtractionTaskAdapter,
    RecoverableLedgerExtractionExchange,
)
from case_kernel.case_agent_ledger_extraction_postgres import (
    PostgresCaseLedgerExtractionStagingStore,
    TaskBoundEvidencePageTextReader,
)
from case_kernel.case_agent_ledger_exception_followup_worker import (
    PostgresCaseLedgerExceptionFollowupAutomation,
)
from case_kernel.case_agent_case_context_postgres import (
    PostgresCaseContextProjectionPort,
)
from case_kernel.case_agent_document_adapters import (
    DOCX_DOCUMENT_DELIVERY_MANIFEST,
    XLSX_DOCUMENT_DELIVERY_MANIFEST,
    DynamicDocumentBindingPort,
    DynamicDocumentTaskAdapter,
    RecoverableDocumentDraftExchange,
    ReviewableDocumentPackageStagingPort,
)
from case_kernel.case_agent_document_delivery import ReviewableDocumentFormat
from case_kernel.case_agent_document_delivery_postgres import (
    DocumentAwareManagedArtifactAccessPort,
)
from case_kernel.case_agent_supervisor import (
    AgentAutonomyLevel,
    AgentRiskLevel,
    RetryMode,
    TaskResourceBudget,
)
from case_kernel.case_agent_worker import (
    AgentWorkerReadinessProbe,
    AgentWorkerStep,
    CaseAgentTaskAdapter,
    CaseAgentWorker,
    CasePlanningSnapshotProvider,
    SemanticPlanner,
    WorkerStepResult,
)
from case_kernel.case_agent_worker_postgres import (
    PlanningResultReconciler,
    PostgresCaseAgentWorkerAdapter,
)
from case_kernel.case_work_plan_postgres import PostgresCaseWorkPlanStore
from case_kernel.case_agent_runtime_postgres import (
    AgentRunInboxClaim,
    CaseAgentRunInbox,
    PostgresCaseAgentRunInbox,
    PostgresCommonDocumentInputPort,
    PostgresEvidenceProjectionAuthorizationPort,
    PostgresLedgerExtractionProjectionPort,
    PostgresManagedArtifactAccessPort,
    PostgresReviewCandidateStagingPort,
)
from case_kernel.case_agent_runtime_identity import case_agent_worker_id
from case_kernel.evidence_manifest_postgres import PostgresEvidenceManifestStore
from case_kernel.models import Actor, Role
from case_kernel.skill_registry import ApprovalGate, default_case_skill_registry
from case_kernel.case_agent_verifier import (
    ManagedArtifactAccessPort,
    build_first_release_case_agent_run_verifier,
)
from case_kernel.deepseek_document_drafting import DeepSeekDocumentDraftProvider
from case_kernel.reviewable_draft_worker import ReviewOfficeConverter
from case_kernel.web_agent_evidence_projection import (
    WebAgentEvidenceProjectionPolicy,
    WebAgentEvidenceProjectionSource,
)
from case_kernel.web_agent_material_review import MAX_AGENT_PAGES_PER_RUN
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore


class PlannerFactory(Protocol):
    """Build the configured planner with the durable submission guard."""

    def __call__(self, request_guard: PostgresCaseAgentWorkerAdapter) -> SemanticPlanner: ...


class PlanningProjectionRepository(Protocol):
    def read_atomic_projection(self, **kwargs: object) -> object: ...


class FutureReadOnlyMaterialAdapterFactory(Protocol):
    """Extension point for later PPTX/RTF/text/mail or local image adapters.

    Implementations are not registered by this first release.  Hosted visual
    providers additionally require the durable external-submission ledger and
    explicit lawyer authorization; they cannot be inserted as a local parser.
    """

    def build(self, *, tool_id: str) -> CaseAgentTaskAdapter: ...


class RunMemoryCheckpointPort(Protocol):
    def checkpoint_current_run(
        self,
        *,
        actor: Actor,
        matter_id: str,
        run_id: str,
        occurred_at: datetime,
    ) -> object: ...


class RunnerIncidentSink(Protocol):
    """Server-only structured incident sink; no exception text enters Web."""

    def record(
        self,
        *,
        firm_id: str,
        worker_id: str,
        run_id: str | None,
        matter_id: str | None,
        code: str,
        occurred_at: datetime,
    ) -> None: ...


class DocumentRevisionWorkerPort(Protocol):
    """Consume one deterministic, zero-network document revision."""

    def run_cycle(self) -> bool: ...


@dataclass(frozen=True)
class CaseAgentWorkerRuntimeSettings:
    worker_id: str
    actor: Actor
    postgres_dsn: str
    verifier_actor: Actor
    verifier_postgres_dsn: str
    worker_root: str
    # Covers the strict lawyer-analysis maximum: 300 seconds of provider
    # transport plus a 60-second archive/receipt envelope.
    run_lease_seconds: int = 420
    task_lease_seconds: int = 420
    task_heartbeat_interval_seconds: int = 30
    poll_interval_seconds: float = 2.0
    readiness_heartbeat_interval_seconds: int = 30
    readiness_ttl_seconds: int = 90

    def validate(self) -> None:
        if self.actor.roles != frozenset({Role.SYSTEM_WORKER}):
            raise PermissionError("Worker runtime requires one dedicated SYSTEM_WORKER")
        if self.worker_id != case_agent_worker_id(self.actor.firm_id):
            raise ValueError("Worker runtime id must be the exact firm-scoped identity")
        if self.verifier_actor.roles != frozenset({Role.SYSTEM_WORKER}):
            raise PermissionError("Verifier runtime requires one dedicated SYSTEM_WORKER")
        if self.verifier_actor.firm_id != self.actor.firm_id:
            raise ValueError("Verifier must belong to the execution firm's scope")
        if self.verifier_actor.actor_id == self.actor.actor_id:
            raise ValueError("Verifier identity must differ from execution Worker")
        if not self.worker_id.strip() or len(self.worker_id) > 200:
            raise ValueError("Worker runtime id is invalid")
        if (
            not self.postgres_dsn.strip()
            or not self.verifier_postgres_dsn.strip()
            or not self.worker_root.strip()
        ):
            raise ValueError("Worker runtime persistence settings are required")
        if self.verifier_postgres_dsn == self.postgres_dsn:
            raise ValueError("Verifier requires a distinct PostgreSQL principal")
        for value, label in (
            (self.run_lease_seconds, "run lease"),
            (self.task_lease_seconds, "task lease"),
        ):
            if not 30 <= value <= 900:
                raise ValueError(f"{label} must be between 30 and 900 seconds")
        if not 1 <= self.task_heartbeat_interval_seconds < self.task_lease_seconds:
            raise ValueError("task heartbeat interval is invalid")
        if not 0.1 <= self.poll_interval_seconds <= 60:
            raise ValueError("Worker poll interval is invalid")
        if not 5 <= self.readiness_heartbeat_interval_seconds <= 300:
            raise ValueError("readiness heartbeat interval is invalid")
        if not 30 <= self.readiness_ttl_seconds <= 900:
            raise ValueError("readiness heartbeat TTL is invalid")
        if self.readiness_heartbeat_interval_seconds >= self.readiness_ttl_seconds:
            raise ValueError("readiness heartbeat interval must be shorter than its TTL")


@dataclass(frozen=True)
class ComposedCaseAgentWorker:
    runner: "CaseAgentWorkerRunner"
    worker: CaseAgentWorker
    readiness: AgentWorkerReadinessProbe
    adapters: Mapping[str, CaseAgentTaskAdapter]
    memory_ready: bool


class CaseAgentWorkerRunner:
    """Continuously consume one firm's persistent run inbox."""

    _QUIET_STEPS = frozenset(
        {
            AgentWorkerStep.IDLE,
            AgentWorkerStep.WAITING_HUMAN,
            AgentWorkerStep.VERIFICATION_REQUIRED,
            AgentWorkerStep.RECONCILIATION_DEFERRED,
        }
    )
    _CHECKPOINT_STEPS = frozenset(
        {
            AgentWorkerStep.PLANNED,
            AgentWorkerStep.PLANNING_RECONCILED,
            AgentWorkerStep.SNAPSHOT_REFRESHED,
            AgentWorkerStep.TASK_SUCCEEDED,
            AgentWorkerStep.TASK_FAILED,
            AgentWorkerStep.TASK_RECONCILIATION_REQUIRED,
            AgentWorkerStep.ATTEMPT_REAPED,
            AgentWorkerStep.VERIFICATION_REQUIRED,
            AgentWorkerStep.VERIFICATION_PASSED,
            AgentWorkerStep.VERIFICATION_FAILED,
        }
    )

    def __init__(
        self,
        *,
        settings: CaseAgentWorkerRuntimeSettings,
        inbox: CaseAgentRunInbox,
        worker: CaseAgentWorker,
        readiness: AgentWorkerReadinessProbe,
        document_revision_worker: DocumentRevisionWorkerPort | None = None,
        official_source_capture_worker: DocumentRevisionWorkerPort | None = None,
        memory_checkpoint: RunMemoryCheckpointPort | None = None,
        incident_sink: RunnerIncidentSink | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        settings.validate()
        if not callable(getattr(inbox, "claim_next_run", None)) or not callable(
            getattr(inbox, "settle_run", None)
        ):
            raise ValueError("Worker run inbox is invalid")
        if memory_checkpoint is not None and not callable(
            getattr(memory_checkpoint, "checkpoint_current_run", None)
        ):
            raise ValueError("Worker memory checkpoint port is invalid")
        if memory_checkpoint is not None and incident_sink is None:
            raise ValueError(
                "Worker memory checkpoint requires a durable incident sink"
            )
        if incident_sink is not None and not callable(getattr(incident_sink, "record", None)):
            raise ValueError("Worker incident sink is invalid")
        if document_revision_worker is not None and not callable(
            getattr(document_revision_worker, "run_cycle", None)
        ):
            raise ValueError("document revision Worker port is invalid")
        if official_source_capture_worker is not None and not callable(
            getattr(official_source_capture_worker, "run_cycle", None)
        ):
            raise ValueError("official source capture Worker port is invalid")
        self._settings = settings
        self._inbox = inbox
        self._worker = worker
        self._readiness = readiness
        self._document_revisions = document_revision_worker
        self._official_source_captures = official_source_capture_worker
        self._memory = memory_checkpoint
        self._incident_sink = incident_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock
        self._stop = Event()
        self._consumer_started = False
        self._last_readiness_heartbeat: float | None = None

    @property
    def memory_ready(self) -> bool:
        return self._memory is not None

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        """Start the actual consumer loop; only this path writes liveness."""

        self._consumer_started = True
        while not self._stop.is_set():
            self._publish_readiness_if_due()
            processed = self.run_cycle()
            if not processed:
                self._stop.wait(self._settings.poll_interval_seconds)

    def run_cycle(self) -> bool:
        if self._document_revisions is not None:
            try:
                if self._document_revisions.run_cycle():
                    return True
            except Exception:
                # A revision inbox outage must not kill the Agent consumer or
                # masquerade as a failed legal task.  The request remains
                # durable and will be retried on the next bounded poll.
                self._record_runtime_incident(
                    "CASE_AGENT_DOCUMENT_REVISION_CYCLE_BLOCKED"
                )
        if self._official_source_captures is not None:
            try:
                if self._official_source_captures.run_cycle():
                    return True
            except Exception:
                # Official source captures have their own durable lease and
                # contain no case text. An outage must not kill ordinary Agent
                # work or turn an external-source issue into a legal-task error.
                self._record_runtime_incident(
                    "CASE_AGENT_OFFICIAL_SOURCE_CAPTURE_CYCLE_BLOCKED"
                )
        claim = self._inbox.claim_next_run(
            lease_owner=self._settings.worker_id,
            lease_seconds=self._settings.run_lease_seconds,
        )
        if claim is None:
            return False
        try:
            result = self._worker.process_run_once(
                matter_id=claim.matter_id, run_id=claim.run_id
            )
        except Exception:
            self._record_incident(claim, "CASE_AGENT_RUN_STEP_BLOCKED")
            self._inbox.settle_run(claim, quiet=False, retry_after_seconds=30)
            return True

        if result.step in self._CHECKPOINT_STEPS and self._memory is not None:
            try:
                self._memory.checkpoint_current_run(
                    actor=self._settings.actor,
                    matter_id=claim.matter_id,
                    run_id=claim.run_id,
                    occurred_at=self._clock(),
                )
            except Exception:
                # The task/result event remains authoritative.  Memory failure
                # becomes a separate recoverable incident, never a fake task
                # failure and never a swallowed success.
                self._record_incident(claim, "CASE_AGENT_MEMORY_CHECKPOINT_BLOCKED")
                self._inbox.settle_run(claim, quiet=False, retry_after_seconds=30)
                return True

        self._settle_result(claim, result)
        return True

    def _settle_result(
        self, claim: AgentRunInboxClaim, result: WorkerStepResult
    ) -> None:
        if result.run_id != claim.run_id or result.event_version < claim.observed_event_version:
            self._record_incident(claim, "CASE_AGENT_RUN_RESULT_BINDING_INVALID")
            self._inbox.settle_run(claim, quiet=False, retry_after_seconds=30)
            return
        # Any worker event increments case_agent_runs.current_event_version;
        # the DB trigger already reset the row to READY.  This CAS settlement
        # then harmlessly returns False and cannot overwrite the fresh wake.
        if (
            result.step is AgentWorkerStep.VERIFICATION_REQUIRED
            and result.reason_code == "VERIFICATION_RESULT_INDETERMINATE"
        ):
            # STARTED is durable but an object-store/runtime outage is not a
            # human gate.  If a second verification attempt observes the same
            # outage without writing another event, quieting this inbox would
            # strand the run forever.  Retry the same immutable attempt; the
            # verifier never resubmits model work or guesses a terminal result.
            self._inbox.settle_run(
                claim, quiet=False, retry_after_seconds=30
            )
        elif result.step is AgentWorkerStep.RECONCILIATION_DEFERRED:
            # A lookup-only recovery already checked the immutable original
            # request. Persist a safe audit marker and stop polling; any later
            # recovery is explicit and still cannot resend the provider call.
            self._record_incident(
                claim, "CASE_AGENT_RECONCILIATION_UNRESOLVED"
            )
            self._inbox.settle_run(claim, quiet=True)
        elif result.step in self._QUIET_STEPS:
            self._inbox.settle_run(claim, quiet=True)
        elif result.step is AgentWorkerStep.WAITING_LEASE:
            self._inbox.settle_run(claim, quiet=False, retry_after_seconds=30)
        else:
            self._inbox.settle_run(claim, quiet=False, retry_after_seconds=0)

    def _publish_readiness_if_due(self) -> None:
        if not self._consumer_started:
            raise RuntimeError("readiness cannot be published before consumer start")
        now = self._monotonic()
        if (
            self._last_readiness_heartbeat is None
            or now - self._last_readiness_heartbeat
            >= self._settings.readiness_heartbeat_interval_seconds
        ):
            self._readiness.publish_heartbeat(
                ttl_seconds=self._settings.readiness_ttl_seconds
            )
            self._last_readiness_heartbeat = now

    def _record_incident(self, claim: AgentRunInboxClaim, code: str) -> None:
        if self._incident_sink is None:
            return
        self._incident_sink.record(
            firm_id=claim.firm_id,
            worker_id=self._settings.worker_id,
            run_id=claim.run_id,
            matter_id=claim.matter_id,
            code=code,
            occurred_at=self._clock(),
        )

    def _record_runtime_incident(self, code: str) -> None:
        if self._incident_sink is None:
            return
        self._incident_sink.record(
            firm_id=self._settings.actor.firm_id,
            worker_id=self._settings.worker_id,
            run_id=None,
            matter_id=None,
            code=code,
            occurred_at=self._clock(),
        )


def compose_case_agent_worker(
    *,
    settings: CaseAgentWorkerRuntimeSettings,
    object_store: S3CompatiblePrivateObjectStore,
    snapshot_provider: CasePlanningSnapshotProvider | None = None,
    planning_repository: PlanningProjectionRepository | None = None,
    planner_factory: PlannerFactory,
    planning_reconciler: PlanningResultReconciler | None = None,
    planning_memory_enrichment: PlanningMemoryEnrichmentPort | None = None,
    planning_memory_request_factory: PlanningMemorySearchRequestFactory | None = None,
    public_research_binding: PublicResearchBindingPort | None = None,
    public_research_provider: BravePublicSearchProvider | None = None,
    public_research_exchange: DurablePublicSearchExchange | None = None,
    visual_ocr_binding: VisualOcrBindingPort | None = None,
    visual_ocr_exchange: DurableQwenVisualOcrExchange | None = None,
    visual_ocr_workspace_id: str | None = None,
    lawyer_analysis_exchange: RecoverableLawyerAnalysisExchange | None = None,
    document_binding: DynamicDocumentBindingPort | None = None,
    document_provider: DeepSeekDocumentDraftProvider | None = None,
    document_exchange: RecoverableDocumentDraftExchange | None = None,
    document_converter: ReviewOfficeConverter | None = None,
    document_staging: ReviewableDocumentPackageStagingPort | None = None,
    document_artifact_access: ManagedArtifactAccessPort | None = None,
    document_revision_worker: DocumentRevisionWorkerPort | None = None,
    official_source_capture_worker: DocumentRevisionWorkerPort | None = None,
    ledger_extraction_exchange: RecoverableLedgerExtractionExchange | None = None,
    controlled_defence_snapshot_filter: bool = False,
    memory_checkpoint: RunMemoryCheckpointPort | None = None,
    incident_sink: RunnerIncidentSink | None = None,
) -> ComposedCaseAgentWorker:
    """Bind the executable runtime; optional capabilities require real ports."""

    settings.validate()
    if (snapshot_provider is None) == (planning_repository is None):
        raise ValueError(
            "provide exactly one authoritative snapshot Provider or atomic repository"
        )
    if snapshot_provider is not None and not callable(
        getattr(snapshot_provider, "build_for_run", None)
    ):
        raise ValueError("authoritative CasePlanningSnapshotProvider is required")
    if planning_repository is not None and not callable(
        getattr(planning_repository, "read_atomic_projection", None)
    ):
        raise ValueError("atomic planning projection repository is required")
    if not callable(planner_factory):
        raise ValueError("configured semantic planner factory is required")
    if not isinstance(object_store, S3CompatiblePrivateObjectStore):
        raise ValueError("private object store is required")
    if type(controlled_defence_snapshot_filter) is not bool:
        raise ValueError("controlled defence snapshot filter flag is invalid")
    if (planning_memory_enrichment is None) != (
        planning_memory_request_factory is None
    ):
        raise ValueError(
            "planning memory requires both enrichment port and dynamic request factory"
        )
    research_values = (
        public_research_binding,
        public_research_provider,
        public_research_exchange,
    )
    if any(value is not None for value in research_values) and not all(
        value is not None for value in research_values
    ):
        raise ValueError("public research requires binding, provider and durable exchange")
    visual_values = (
        visual_ocr_binding,
        visual_ocr_exchange,
        visual_ocr_workspace_id,
    )
    if any(value is not None for value in visual_values) and not all(
        value is not None for value in visual_values
    ):
        raise ValueError(
            "visual OCR requires binding, durable exchange and workspace identity"
        )
    document_values = (
        document_binding,
        document_provider,
        document_exchange,
        document_converter,
        document_staging,
        document_artifact_access,
    )
    if any(value is not None for value in document_values) and not all(
        value is not None for value in document_values
    ):
        raise ValueError(
            "dynamic document delivery requires binding, provider, durable "
            "exchange, isolated converter, staging and independent access"
        )
    documents_enabled = all(value is not None for value in document_values)
    if document_revision_worker is not None:
        if not documents_enabled or not callable(
            getattr(document_revision_worker, "run_cycle", None)
        ):
            raise ValueError(
                "document revision Worker requires the complete document delivery boundary"
            )

    event_store = PostgresCaseAgentStore(settings.postgres_dsn)
    worker_store = PostgresCaseAgentWorkerAdapter(
        store=event_store,
        actor=settings.actor,
        planning_reconciler=planning_reconciler,
    )
    planner = planner_factory(worker_store)
    verifier_store = PostgresCaseAgentWorkerAdapter(
        store=PostgresCaseAgentStore(settings.verifier_postgres_dsn),
        actor=settings.verifier_actor,
    )

    candidate_staging = PostgresReviewCandidateStagingPort(
        dsn=settings.postgres_dsn,
        worker_actor=settings.actor,
        object_store=object_store,
    )
    evidence_store = PostgresEvidenceManifestStore(settings.postgres_dsn)
    authorization_port = PostgresEvidenceProjectionAuthorizationPort(
        dsn=settings.postgres_dsn, worker_actor=settings.actor
    )
    projection_source = WebAgentEvidenceProjectionSource(
        evidence_store=evidence_store,
        object_store=object_store,
        system_worker_for_firm=lambda firm_id: (
            settings.actor
            if firm_id == settings.actor.firm_id
            else _raise_unknown_firm_worker()
        ),
        policy=WebAgentEvidenceProjectionPolicy(worker_root=settings.worker_root),
    )
    pdf_projection = WebEvidencePageTaskProjectionPort(
        authorization_port=authorization_port,
        projection_source=projection_source,
    )
    ledger_pdf_projection = None
    ledger_projection = None
    if ledger_extraction_exchange is not None:
        ledger_pdf_projection = WebEvidencePageTaskProjectionPort(
            authorization_port=PostgresEvidenceProjectionAuthorizationPort(
                dsn=settings.postgres_dsn,
                worker_actor=settings.actor,
                required_tool=DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id,
            ),
            projection_source=projection_source,
        )
        ledger_projection = PostgresLedgerExtractionProjectionPort(
            dsn=settings.postgres_dsn,
            worker_actor=settings.actor,
            native_projection_port=ledger_pdf_projection,
            object_store=object_store,
        )
    ledger_extraction_staging = (
        PostgresCaseLedgerExtractionStagingStore(
            dsn=settings.postgres_dsn,
            worker_actor=settings.actor,
            object_store=object_store,
            evidence_page_text_reader=TaskBoundEvidencePageTextReader(
                projection_port=ledger_projection
            ),
        )
        if ledger_extraction_exchange is not None
        else None
    )
    ledger_exception_followups = PostgresCaseLedgerExceptionFollowupAutomation(
        dsn=settings.postgres_dsn,
        worker_actor=settings.actor,
    )
    office_inputs = PostgresCommonDocumentInputPort(
        dsn=settings.postgres_dsn,
        worker_actor=settings.actor,
        object_store=object_store,
        worker_root=settings.worker_root,
    )
    case_context_projection = PostgresCaseContextProjectionPort(
        dsn=settings.postgres_dsn,
        worker_actor=settings.actor,
    )
    legal_research_projection = PostgresCaseContextProjectionPort(
        dsn=settings.postgres_dsn,
        worker_actor=settings.actor,
        required_tool_id=LEGAL_RESEARCH_PLANNING_MANIFEST.tool_id,
    )
    lawyer_analysis_projection = (
        PostgresCaseContextProjectionPort(
            dsn=settings.postgres_dsn,
            worker_actor=settings.actor,
            required_tool_id=LAWYER_ANALYSIS_TOOL_ID,
        )
        if lawyer_analysis_exchange is not None
        else None
    )
    adapters: dict[str, CaseAgentTaskAdapter] = {
        "extract_pdf_text": PdfTextTaskAdapter(
            projection_port=pdf_projection, staging_port=candidate_staging
        ),
        "parse_office_document": CommonDocumentTaskAdapter(
            input_port=office_inputs, staging_port=candidate_staging
        ),
        CASE_CONTEXT_REVIEW_MANIFEST.tool_id: DeterministicCaseContextTaskAdapter(
            projection_port=case_context_projection,
            staging_port=candidate_staging,
        ),
        LEGAL_RESEARCH_PLANNING_MANIFEST.tool_id: (
            DeterministicLegalResearchPlanningTaskAdapter(
                projection_port=legal_research_projection,
                staging_port=candidate_staging,
            )
        ),
    }
    if public_research_binding is not None:
        assert public_research_provider is not None
        assert public_research_exchange is not None
        adapters[PUBLIC_WEB_RESEARCH_MANIFEST.tool_id] = PublicWebResearchTaskAdapter(
            binding_port=public_research_binding,
            provider=public_research_provider,
            exchange=public_research_exchange,
            staging_port=candidate_staging,
        )
    if lawyer_analysis_exchange is not None:
        assert lawyer_analysis_projection is not None
        adapters[LAWYER_ANALYSIS_TOOL_ID] = QwenLawyerAnalysisTaskAdapter(
            projection_port=lawyer_analysis_projection,
            exchange=lawyer_analysis_exchange,
            staging_port=candidate_staging,
        )
    if visual_ocr_binding is not None:
        assert visual_ocr_exchange is not None
        visual_adapter = configured_qwen_visual_ocr_adapter(
            binding_port=visual_ocr_binding,
            exchange=visual_ocr_exchange,
            staging_port=candidate_staging,
        )
        assert visual_adapter is not None
        adapters[QWEN_VISUAL_OCR_MANIFEST.tool_id] = visual_adapter
    if documents_enabled:
        assert document_binding is not None
        assert document_provider is not None
        assert document_exchange is not None
        assert document_converter is not None
        assert document_staging is not None
        adapters[DOCX_DOCUMENT_DELIVERY_MANIFEST.tool_id] = (
            DynamicDocumentTaskAdapter(
                output_format=ReviewableDocumentFormat.DOCX,
                binding_port=document_binding,
                provider=document_provider,
                exchange=document_exchange,
                converter=document_converter,
                staging_port=document_staging,
            )
        )
        adapters[XLSX_DOCUMENT_DELIVERY_MANIFEST.tool_id] = (
            DynamicDocumentTaskAdapter(
                output_format=ReviewableDocumentFormat.XLSX,
                binding_port=document_binding,
                provider=document_provider,
                exchange=document_exchange,
                converter=document_converter,
                staging_port=document_staging,
            )
        )
    if ledger_extraction_exchange is not None:
        assert ledger_projection is not None
        adapters[DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id] = DeepSeekLedgerExtractionTaskAdapter(
            projection_port=ledger_projection, exchange=ledger_extraction_exchange,
            staging_port=candidate_staging,
        )
    artifact_access = PostgresManagedArtifactAccessPort(
        dsn=settings.verifier_postgres_dsn,
        verifier_actor=settings.verifier_actor,
        execution_actor_id=settings.actor.actor_id,
        object_store=object_store,
    )
    if documents_enabled:
        assert document_artifact_access is not None
        artifact_access = DocumentAwareManagedArtifactAccessPort(
            base_access=artifact_access,
            document_access=document_artifact_access,
        )
    verifier = build_first_release_case_agent_run_verifier(
        artifact_access=artifact_access,
    )
    if planning_repository is not None:
        executable_skills = [
            ExecutablePlanningSkill(
                skill_id="pdf_reading",
                adapter=adapters["extract_pdf_text"],
                supported_object_types=frozenset(
                    {PlanningProjectionObjectType.EVIDENCE_PAGE}
                ),
                neutral_material_inventory=True,
                supported_media_types=frozenset({"application/pdf"}),
            ),
            ExecutablePlanningSkill(
                skill_id="office_reading",
                adapter=adapters["parse_office_document"],
                supported_object_types=frozenset(
                    {PlanningProjectionObjectType.MATERIAL_OBJECT}
                ),
                neutral_material_inventory=True,
            ),
            ExecutablePlanningSkill(
                skill_id="case_context_review",
                adapter=adapters[CASE_CONTEXT_REVIEW_MANIFEST.tool_id],
                supported_object_types=frozenset(
                    {
                        PlanningProjectionObjectType.CASE_FACT,
                        PlanningProjectionObjectType.CASE_CLAIM,
                        PlanningProjectionObjectType.DISPUTE_ISSUE,
                        PlanningProjectionObjectType.CASE_TRANSACTION,
                        PlanningProjectionObjectType.POSTURE_PROFILE,
                        PlanningProjectionObjectType.WORK_PLAN_ITEM,
                        PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                        PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                        PlanningProjectionObjectType.PROCEDURAL_EVENT,
                        PlanningProjectionObjectType.REVIEW_OBLIGATION,
                        PlanningProjectionObjectType.TRANSACTION_CANDIDATE,
                        PlanningProjectionObjectType.FACT_CANDIDATE,
                    }
                ),
                neutral_material_inventory=False,
            ),
            ExecutablePlanningSkill(
                skill_id=LEGAL_RESEARCH_PLANNING_SKILL_ID,
                adapter=adapters[LEGAL_RESEARCH_PLANNING_MANIFEST.tool_id],
                supported_object_types=frozenset(
                    {
                        PlanningProjectionObjectType.CASE_FACT,
                        PlanningProjectionObjectType.CASE_CLAIM,
                        PlanningProjectionObjectType.DISPUTE_ISSUE,
                        PlanningProjectionObjectType.CASE_TRANSACTION,
                        PlanningProjectionObjectType.POSTURE_PROFILE,
                        PlanningProjectionObjectType.WORK_PLAN_ITEM,
                        PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                        PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                        PlanningProjectionObjectType.PROCEDURAL_EVENT,
                    }
                ),
                neutral_material_inventory=False,
            ),
        ]
        if public_research_binding is not None:
            executable_skills.append(
                ExecutablePlanningSkill(
                    skill_id="controlled_web_search",
                    adapter=adapters[PUBLIC_WEB_RESEARCH_MANIFEST.tool_id],
                    supported_object_types=frozenset(
                        {
                            PlanningProjectionObjectType.DISPUTE_ISSUE,
                            PlanningProjectionObjectType.WORK_PLAN_ITEM,
                            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                        }
                    ),
                    neutral_material_inventory=False,
                )
            )
        if lawyer_analysis_exchange is not None:
            executable_skills.append(
                ExecutablePlanningSkill(
                    skill_id=LAWYER_ANALYSIS_SKILL_ID,
                    adapter=adapters[LAWYER_ANALYSIS_TOOL_ID],
                    supported_object_types=frozenset(
                        {
                            PlanningProjectionObjectType.CASE_FACT,
                            PlanningProjectionObjectType.CASE_CLAIM,
                            PlanningProjectionObjectType.DISPUTE_ISSUE,
                            PlanningProjectionObjectType.CASE_TRANSACTION,
                            PlanningProjectionObjectType.POSTURE_PROFILE,
                            PlanningProjectionObjectType.WORK_PLAN_ITEM,
                            PlanningProjectionObjectType.VERIFIED_LEGAL_SOURCE,
                            PlanningProjectionObjectType.APPROVED_LEGAL_RULE,
                            PlanningProjectionObjectType.PROCEDURAL_EVENT,
                            PlanningProjectionObjectType.REVIEW_OBLIGATION,
                            PlanningProjectionObjectType.TRANSACTION_CANDIDATE,
                            PlanningProjectionObjectType.FACT_CANDIDATE,
                        }
                    ),
                    neutral_material_inventory=False,
                )
            )
        if visual_ocr_binding is not None:
            executable_skills.append(
                ExecutablePlanningSkill(
                    skill_id="image_visual_ocr",
                    adapter=adapters[QWEN_VISUAL_OCR_MANIFEST.tool_id],
                    supported_object_types=frozenset(
                        {PlanningProjectionObjectType.EVIDENCE_PAGE}
                    ),
                    neutral_material_inventory=True,
                    supported_media_types=frozenset(
                        {"application/pdf", "image/jpeg", "image/png"}
                    ),
                )
            )
        if documents_enabled:
            executable_skills.extend(
                (
                    ExecutablePlanningSkill(
                        skill_id="dynamic_document_delivery",
                        adapter=adapters[
                            DOCX_DOCUMENT_DELIVERY_MANIFEST.tool_id
                        ],
                        supported_object_types=frozenset(
                            {PlanningProjectionObjectType.WORK_PLAN_ITEM}
                        ),
                        neutral_material_inventory=False,
                    ),
                    ExecutablePlanningSkill(
                        skill_id="dynamic_spreadsheet_delivery",
                        adapter=adapters[
                            XLSX_DOCUMENT_DELIVERY_MANIFEST.tool_id
                        ],
                        supported_object_types=frozenset(
                            {PlanningProjectionObjectType.WORK_PLAN_ITEM}
                        ),
                        neutral_material_inventory=False,
                    ),
                )
            )
        if ledger_extraction_exchange is not None:
            executable_skills.append(
                ExecutablePlanningSkill(
                    skill_id="case_ledger_extraction",
                    adapter=adapters[DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id],
                    supported_object_types=frozenset({PlanningProjectionObjectType.EVIDENCE_PAGE}),
                    neutral_material_inventory=True,
                    supported_media_types=frozenset({"application/pdf"}),
                )
            )
        snapshot_provider = (
            ControlledDefencePlanningSnapshotProvider(
                repository=planning_repository,
                executable_skills=tuple(executable_skills),
            )
            if controlled_defence_snapshot_filter
            else AuthoritativeCasePlanningSnapshotProvider(
                repository=planning_repository,
                executable_skills=tuple(executable_skills),
            )
        )
    if planning_memory_enrichment is not None:
        assert planning_memory_request_factory is not None
        snapshot_provider = MemoryEnrichedPlanningSnapshotProvider(
            base_provider=snapshot_provider,
            enrichment_port=planning_memory_enrichment,
            request_factory=planning_memory_request_factory,
        )
    assert snapshot_provider is not None
    registry = default_case_skill_registry(
        pdf_reading_adapter_enabled=True,
        common_document_adapter_enabled=True,
        visual_page_adapter_enabled=visual_ocr_binding is not None,
        controlled_web_research_enabled=public_research_binding is not None,
        legal_research_planner_enabled=True,
        case_context_review_enabled=True,
        dynamic_document_delivery_enabled=documents_enabled,
        case_ledger_extraction_enabled=ledger_extraction_exchange is not None,
        lawyer_decision_package_enabled=lawyer_analysis_exchange is not None,
    )
    policies = [
        _read_policy(
            "pdf_reading",
            "extract_pdf_text",
            max_output_bytes=8 * 1024 * 1024,
            max_input_refs=50,
        ),
        _read_policy(
            "office_reading",
            "parse_office_document",
            max_output_bytes=64 * 1024 * 1024,
            max_input_refs=100,
        ),
        _case_context_policy(),
        _legal_research_planning_policy(),
    ]
    if public_research_binding is not None:
        policies.append(_public_research_policy())
    if lawyer_analysis_exchange is not None:
        policies.append(
            _lawyer_analysis_policy(lawyer_analysis_exchange.endpoint_host)
        )
    if visual_ocr_binding is not None:
        assert visual_ocr_workspace_id is not None
        policies.append(_visual_ocr_policy(visual_ocr_workspace_id))
    if documents_enabled:
        policies.extend(
            (
                _document_delivery_policy(
                    skill_id="dynamic_document_delivery",
                    tool_id=DOCX_DOCUMENT_DELIVERY_MANIFEST.tool_id,
                ),
                _document_delivery_policy(
                    skill_id="dynamic_spreadsheet_delivery",
                    tool_id=XLSX_DOCUMENT_DELIVERY_MANIFEST.tool_id,
                ),
            )
        )
    if ledger_extraction_exchange is not None:
        policies.append(_ledger_extraction_policy())
    compiler = CaseAgentPlannerCompiler(
        registry=registry,
        adapters={tool_id: adapter.manifest for tool_id, adapter in adapters.items()},
        skill_policies=tuple(policies),
    )
    # This also proves the registry is not merely paper capability metadata.
    compiler.semantic_skill_catalog()
    worker = CaseAgentWorker(
        worker_id=settings.worker_id,
        actor=settings.actor,
        store=worker_store,
        snapshot_provider=snapshot_provider,
        planner=planner,
        planner_compiler=compiler,
        adapters=adapters,
        verifier=verifier,
        verifier_actor=settings.verifier_actor,
        verifier_store=verifier_store,
        ledger_extraction_staging=ledger_extraction_staging,
        ledger_exception_followups=ledger_exception_followups,
        work_plan_promotion=PostgresCaseWorkPlanStore(settings.postgres_dsn),
        lease_seconds=settings.task_lease_seconds,
        heartbeat_interval_seconds=settings.task_heartbeat_interval_seconds,
    )
    readiness = AgentWorkerReadinessProbe(
        worker_id=settings.worker_id,
        actor=settings.actor,
        planner=planner,
        adapters=adapters,
        health_store=worker_store,
        verifier=verifier,
        verifier_actor=settings.verifier_actor,
    )
    inbox = PostgresCaseAgentRunInbox(
        dsn=settings.postgres_dsn, actor=settings.actor
    )
    runner = CaseAgentWorkerRunner(
        settings=settings,
        inbox=inbox,
        worker=worker,
        readiness=readiness,
        document_revision_worker=document_revision_worker,
        official_source_capture_worker=official_source_capture_worker,
        memory_checkpoint=memory_checkpoint,
        incident_sink=incident_sink,
    )
    return ComposedCaseAgentWorker(
        runner=runner,
        worker=worker,
        readiness=readiness,
        adapters=adapters,
        memory_ready=(
            memory_checkpoint is not None and planning_memory_enrichment is not None
        ),
    )


def compose_case_agent_worker_from_repository(
    *,
    settings: CaseAgentWorkerRuntimeSettings,
    object_store: S3CompatiblePrivateObjectStore,
    planning_repository: PlanningProjectionRepository,
    planner_factory: PlannerFactory,
    planning_reconciler: PlanningResultReconciler | None = None,
    planning_memory_enrichment: PlanningMemoryEnrichmentPort | None = None,
    planning_memory_request_factory: PlanningMemorySearchRequestFactory | None = None,
    public_research_binding: PublicResearchBindingPort | None = None,
    public_research_provider: BravePublicSearchProvider | None = None,
    public_research_exchange: DurablePublicSearchExchange | None = None,
    visual_ocr_binding: VisualOcrBindingPort | None = None,
    visual_ocr_exchange: DurableQwenVisualOcrExchange | None = None,
    visual_ocr_workspace_id: str | None = None,
    lawyer_analysis_exchange: RecoverableLawyerAnalysisExchange | None = None,
    document_binding: DynamicDocumentBindingPort | None = None,
    document_provider: DeepSeekDocumentDraftProvider | None = None,
    document_exchange: RecoverableDocumentDraftExchange | None = None,
    document_converter: ReviewOfficeConverter | None = None,
    document_staging: ReviewableDocumentPackageStagingPort | None = None,
    document_artifact_access: ManagedArtifactAccessPort | None = None,
    document_revision_worker: DocumentRevisionWorkerPort | None = None,
    official_source_capture_worker: DocumentRevisionWorkerPort | None = None,
    ledger_extraction_exchange: RecoverableLedgerExtractionExchange | None = None,
    controlled_defence_snapshot_filter: bool = False,
    memory_checkpoint: RunMemoryCheckpointPort | None = None,
    incident_sink: RunnerIncidentSink | None = None,
) -> ComposedCaseAgentWorker:
    """Bind the concrete authoritative snapshot Provider to real adapters.

    This convenience root still requires an injected atomic repository; it
    never manufactures an in-memory snapshot or enables a paper capability.
    """

    return compose_case_agent_worker(
        settings=settings,
        object_store=object_store,
        planning_repository=planning_repository,
        planner_factory=planner_factory,
        planning_reconciler=planning_reconciler,
        planning_memory_enrichment=planning_memory_enrichment,
        planning_memory_request_factory=planning_memory_request_factory,
        public_research_binding=public_research_binding,
        public_research_provider=public_research_provider,
        public_research_exchange=public_research_exchange,
        visual_ocr_binding=visual_ocr_binding,
        visual_ocr_exchange=visual_ocr_exchange,
        visual_ocr_workspace_id=visual_ocr_workspace_id,
        lawyer_analysis_exchange=lawyer_analysis_exchange,
        document_binding=document_binding,
        document_provider=document_provider,
        document_exchange=document_exchange,
        document_converter=document_converter,
        document_staging=document_staging,
        document_artifact_access=document_artifact_access,
        document_revision_worker=document_revision_worker,
        official_source_capture_worker=official_source_capture_worker,
        ledger_extraction_exchange=ledger_extraction_exchange,
        controlled_defence_snapshot_filter=controlled_defence_snapshot_filter,
        memory_checkpoint=memory_checkpoint,
        incident_sink=incident_sink,
    )


def run_composed_worker(runtime: ComposedCaseAgentWorker) -> None:
    """Install bounded stop handlers and serve until the process is stopped."""

    if not isinstance(runtime, ComposedCaseAgentWorker):
        raise ValueError("composed case-Agent Worker is required")

    def request_stop(_signum: int, _frame: object) -> None:
        runtime.runner.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    runtime.runner.serve_forever()


def _read_policy(
    skill_id: str,
    tool_id: str,
    *,
    max_output_bytes: int,
    max_input_refs: int,
) -> ServerSkillExecutionPolicy:
    return ServerSkillExecutionPolicy(
        skill_id=skill_id,
        tool_id=tool_id,
        sandbox_profile="case-agent-readonly-v1",
        allowed_domains=(),
        risk_level=AgentRiskLevel.LOW,
        autonomy_level=AgentAutonomyLevel.A1_PROPOSE,
        # The RUN_CREATED event is the lawyer's source-bound authorization
        # for local reading of this matter's already-admitted materials. A
        # second MATERIAL_SCOPE gate made the user-facing "start handling"
        # action stop before it could read anything, while adding no new
        # source, network, factual or derivative authority.
        approval_gate=ApprovalGate.NONE,
        retry_mode=RetryMode.IDEMPOTENT,
        task_budget=TaskResourceBudget(
            max_attempts=3,
            timeout_seconds=300,
            max_external_calls=0,
            max_cost_minor_units=0,
            max_output_bytes=max_output_bytes,
        ),
        max_input_refs=max_input_refs,
    )


def _case_context_policy() -> ServerSkillExecutionPolicy:
    return ServerSkillExecutionPolicy(
        skill_id="case_context_review",
        tool_id="review_case_context",
        sandbox_profile="case-agent-structured-ledger-readonly-v1",
        allowed_domains=(),
        risk_level=AgentRiskLevel.LOW,
        autonomy_level=AgentAutonomyLevel.A1_PROPOSE,
        approval_gate=ApprovalGate.NONE,
        retry_mode=RetryMode.IDEMPOTENT,
        task_budget=TaskResourceBudget(
            max_attempts=3,
            timeout_seconds=120,
            max_external_calls=0,
            max_cost_minor_units=0,
            max_output_bytes=4 * 1024 * 1024,
        ),
    )


def _legal_research_planning_policy() -> ServerSkillExecutionPolicy:
    return ServerSkillExecutionPolicy(
        skill_id=LEGAL_RESEARCH_PLANNING_SKILL_ID,
        tool_id=LEGAL_RESEARCH_PLANNING_MANIFEST.tool_id,
        sandbox_profile="case-agent-legal-research-planning-v1",
        allowed_domains=(),
        risk_level=AgentRiskLevel.LOW,
        autonomy_level=AgentAutonomyLevel.A1_PROPOSE,
        approval_gate=ApprovalGate.NONE,
        retry_mode=RetryMode.IDEMPOTENT,
        task_budget=TaskResourceBudget(
            max_attempts=3,
            timeout_seconds=120,
            max_external_calls=0,
            max_cost_minor_units=0,
            max_output_bytes=4 * 1024 * 1024,
        ),
        max_input_refs=500,
    )


def _public_research_policy() -> ServerSkillExecutionPolicy:
    return ServerSkillExecutionPolicy(
        skill_id="controlled_web_search",
        tool_id="search_public_web",
        sandbox_profile="case-agent-controlled-web-v1",
        allowed_domains=(BRAVE_SEARCH_HOST,),
        risk_level=AgentRiskLevel.HIGH,
        autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
        approval_gate=ApprovalGate.LAWYER_REVIEW,
        retry_mode=RetryMode.NEVER_AUTOMATIC,
        task_budget=TaskResourceBudget(
            max_attempts=1,
            timeout_seconds=30,
            max_external_calls=1,
            max_cost_minor_units=0,
            max_output_bytes=4 * 1024 * 1024,
        ),
    )


def _lawyer_analysis_policy(endpoint_host: str) -> ServerSkillExecutionPolicy:
    if not valid_qwen_lawyer_analysis_host(endpoint_host):
        raise ValueError("lawyer-analysis endpoint host is invalid")
    return ServerSkillExecutionPolicy(
        skill_id=LAWYER_ANALYSIS_SKILL_ID,
        tool_id=LAWYER_ANALYSIS_TOOL_ID,
        sandbox_profile="case-agent-qwen-lawyer-analysis-v1",
        allowed_domains=(endpoint_host,),
        risk_level=AgentRiskLevel.HIGH,
        autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
        approval_gate=ApprovalGate.LAWYER_REVIEW,
        retry_mode=RetryMode.NEVER_AUTOMATIC,
        task_budget=TaskResourceBudget(
            max_attempts=1,
            timeout_seconds=LAWYER_ANALYSIS_TASK_TIMEOUT_SECONDS,
            max_external_calls=1,
            max_cost_minor_units=LAWYER_ANALYSIS_MAX_COST_MINOR_UNITS,
            max_output_bytes=4 * 1024 * 1024,
        ),
        max_input_refs=500,
    )


def _visual_ocr_policy(workspace_id: str) -> ServerSkillExecutionPolicy:
    descriptor = qwen_visual_ocr_server_policy(workspace_id=workspace_id)
    allowed_domains = descriptor.get("allowed_domains")
    if (
        not isinstance(allowed_domains, tuple)
        or len(allowed_domains) != 1
        or descriptor
        != {
            "tool_id": QWEN_VISUAL_OCR_MANIFEST.tool_id,
            "adapter_id": QWEN_VISUAL_OCR_MANIFEST.adapter_id,
            "adapter_version": QWEN_VISUAL_OCR_MANIFEST.adapter_version,
            "execution_mode": "NETWORK_CONNECTOR",
            "allowed_domains": allowed_domains,
            "risk_level": "HIGH",
            "autonomy_level": "A3_LAWYER_APPROVAL",
            "approval_gate": "LAWYER_REVIEW",
            "retry_mode": "NEVER_AUTOMATIC",
            "max_external_calls": 1,
            "review_only": True,
        }
    ):
        raise ValueError("visual OCR server policy identity is invalid")
    return ServerSkillExecutionPolicy(
        skill_id="image_visual_ocr",
        tool_id=QWEN_VISUAL_OCR_MANIFEST.tool_id,
        sandbox_profile="case-agent-qwen-visual-ocr-v1",
        allowed_domains=allowed_domains,
        risk_level=AgentRiskLevel.HIGH,
        autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
        approval_gate=ApprovalGate.LAWYER_REVIEW,
        retry_mode=RetryMode.NEVER_AUTOMATIC,
        task_budget=TaskResourceBudget(
            max_attempts=1,
            timeout_seconds=120,
            max_external_calls=1,
            max_cost_minor_units=QWEN_VISUAL_OCR_COST_RESERVE_MINOR_UNITS,
            max_output_bytes=32 * 1024 * 1024,
        ),
        max_input_refs=1,
    )


def _document_delivery_policy(
    *, skill_id: str, tool_id: str
) -> ServerSkillExecutionPolicy:
    if (skill_id, tool_id) not in {
        (
            "dynamic_document_delivery",
            DOCX_DOCUMENT_DELIVERY_MANIFEST.tool_id,
        ),
        (
            "dynamic_spreadsheet_delivery",
            XLSX_DOCUMENT_DELIVERY_MANIFEST.tool_id,
        ),
    }:
        raise ValueError("dynamic document policy identity is invalid")
    return ServerSkillExecutionPolicy(
        skill_id=skill_id,
        tool_id=tool_id,
        sandbox_profile="case-agent-reviewable-document-v1",
        allowed_domains=(),
        # Creating a source-bound, managed candidate is reversible internal
        # work.  The lawyer's earlier plan activation authorizes this exact
        # local projection; review remains mandatory before any legal
        # conclusion can be adopted, released, or submitted.
        risk_level=AgentRiskLevel.MEDIUM,
        autonomy_level=AgentAutonomyLevel.A2_INTERNAL_REVERSIBLE,
        approval_gate=ApprovalGate.NONE,
        retry_mode=RetryMode.NEVER_AUTOMATIC,
        task_budget=TaskResourceBudget(
            max_attempts=1,
            # Every selected local deliverable is a source projection;
            # the allowance covers Office/PDF rendering and immutable staging.
            timeout_seconds=210,
            max_external_calls=0,
            max_cost_minor_units=0,
            max_output_bytes=128 * 1024 * 1024,
        ),
    )


def _ledger_extraction_policy() -> ServerSkillExecutionPolicy:
    from case_kernel.case_agent_ledger_extraction_adapters import LEDGER_EXTRACTION_MAX_COST_MINOR_UNITS
    return ServerSkillExecutionPolicy(
        skill_id="case_ledger_extraction",
        tool_id=DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id,
        sandbox_profile="case-agent-deepseek-ledger-extraction-v1",
        allowed_domains=(DEEPSEEK_LEDGER_EXTRACTION_HOST,),
        risk_level=AgentRiskLevel.HIGH,
        autonomy_level=AgentAutonomyLevel.A3_LAWYER_APPROVAL,
        approval_gate=ApprovalGate.LAWYER_REVIEW,
        retry_mode=RetryMode.NEVER_AUTOMATIC,
        task_budget=TaskResourceBudget(max_attempts=1, timeout_seconds=120, max_external_calls=1, max_cost_minor_units=LEDGER_EXTRACTION_MAX_COST_MINOR_UNITS, max_output_bytes=8 * 1024 * 1024),
        max_input_refs=MAX_AGENT_PAGES_PER_RUN,
    )


def _raise_unknown_firm_worker() -> Actor:
    raise PermissionError("evidence projection requested another firm's Worker")


__all__ = (
    "CaseAgentWorkerRunner",
    "CaseAgentWorkerRuntimeSettings",
    "ComposedCaseAgentWorker",
    "PlannerFactory",
    "FutureReadOnlyMaterialAdapterFactory",
    "RunMemoryCheckpointPort",
    "RunnerIncidentSink",
    "compose_case_agent_worker",
    "compose_case_agent_worker_from_repository",
    "run_composed_worker",
)
