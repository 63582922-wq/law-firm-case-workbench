"""Fail-closed process boundary for a firm-scoped production Agent Worker.

This module fixes the server-only configuration contract now.  It cannot start
until deployment injects the real atomic planning-projection repository; there
is no demo repository, browser API-key route or synthetic snapshot fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Callable, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict

from case_kernel.case_agent_planning_snapshot import CasePlanningProjectionRepository
from case_kernel.case_agent_planning_memory import (
    DynamicPlanningMemorySearchRequestFactory,
)
from case_kernel.case_agent_planning_memory_postgres import (
    PostgresPlanningMemoryEnrichmentStore,
)
from case_kernel.brave_public_search import (
    BravePublicSearchProvider,
    BraveSearchCredentials,
)
from case_kernel.case_agent_research_postgres import (
    PinnedBraveHttpsTransport,
    PostgresDurablePublicSearchExchange,
    PostgresPublicResearchBindingPort,
)
from case_kernel.qwen_visual_ocr_postgres import (
    PostgresDurableQwenVisualOcrBroker,
    PostgresVisualOcrBindingPort,
    VisualOcrProjectionPolicy,
)
from case_kernel.qwen_visual_ocr_transport import (
    PinnedQwenVisualOcrHttpsBroker,
    QwenVisualOcrCredentials,
    QwenVisualOcrRecoverableExchange,
)
from case_kernel.case_agent_lawyer_analysis_transport import (
    PostgresBoundRecoverableLawyerAnalysisExchange,
    QwenLawyerAnalysisCredentials,
)
from case_kernel.case_agent_document_binding_postgres import (
    PostgresDynamicDocumentBindingPort,
    PostgresVerifiedLawyerDecisionPackagePort,
    VerifiedOfficialSourceTextPort,
)
from case_kernel.case_agent_runtime_postgres import (
    PostgresManagedArtifactAccessPort,
)
from case_kernel.case_agent_document_delivery_postgres import (
    PostgresReviewableDocumentPackageAccessPort,
    PostgresReviewableDocumentPackageStore,
    preflight_case_agent_document_delivery_runtime_contract,
)
from case_kernel.case_agent_document_delivery import (
    first_release_reviewable_document_templates,
)
from case_kernel.case_agent_document_revisions import (
    PostgresDocumentRevisionWorker,
    PostgresContentRevisionWorker,
    PostgresContentGenerationJobStore,
    PostgresDocumentRevisionReceiptStore,
    DocumentRevisionWorkerGroup,
    preflight_content_revision_runtime,
    preflight_content_recovery_runtime,
    PostgresContentRevisionRecoveryStore,
)
from case_kernel.case_agent_document_exchange_postgres import (
    DeepSeekDocumentRawHttpsTransport,
    PostgresRecoverableDocumentDraftExchange,
    S3DocumentDraftRawResponseStore,
    preflight_case_agent_document_exchange_runtime_contract,
)
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MODEL,
)
from case_kernel.case_agent_ledger_extraction_exchange_postgres import (
    DeepSeekLedgerExtractionCredentials,
    DeepSeekLedgerExtractionRawHttpsTransport,
    PostgresRecoverableLedgerExtractionExchange,
    S3LedgerExtractionRawResponseStore,
    preflight_case_agent_ledger_extraction_runtime_contract,
)
from case_kernel.case_agent_ledger_extraction_postgres import (
    preflight_case_agent_ledger_extraction_staging_runtime_contract,
)
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    preflight_case_agent_ledger_exception_followup_schema,
)
from case_kernel.deepseek_document_drafting import (
    DeepSeekDocumentDraftConfig,
    DeepSeekDocumentDraftCredentials,
    DeepSeekDocumentDraftProvider,
)
from case_kernel.isolated_document_renderer import (
    IsolatedDocumentRendererBlocked,
    IsolatedDocumentRendererClient,
    IsolatedDocumentRendererClientSettings,
)
from case_kernel.case_agent_runtime_identity import case_agent_worker_id
from case_kernel.deepseek_case_agent_planner import (
    DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
    DeepSeekCaseAgentPlanner,
    DeepSeekPlannerCredentials,
    DeepSeekPlannerProviderConfig,
)
from case_kernel.controlled_defence_case_agent_planner import (
    ControlledDefencePlanningRouter,
)
from case_kernel.models import Actor, Role
from case_kernel.web_object_store import (
    S3CompatiblePrivateObjectStore,
    S3PrivateObjectStoreConfig,
)
from case_kernel.official_source_capture_worker import (
    BoundedOfficialSourceCaptureWorker,
)
from case_kernel.official_source_private_store import (
    compose_official_source_s3_adapters,
)

from .case_agent_worker_runtime import (
    CaseAgentWorkerRuntimeSettings,
    ComposedCaseAgentWorker,
    RunMemoryCheckpointPort,
    RunnerIncidentSink,
    compose_case_agent_worker_from_repository,
    run_composed_worker,
)


class CaseAgentWorkerConfigurationBlocked(RuntimeError):
    """Production Worker configuration is missing or unsafe."""


@dataclass(frozen=True, repr=False)
class CaseAgentWorkerProcessSettings:
    runtime: CaseAgentWorkerRuntimeSettings
    database_role: str
    verifier_database_role: str
    object_store: S3PrivateObjectStoreConfig
    deepseek_credentials: DeepSeekPlannerCredentials
    deepseek_config: DeepSeekPlannerProviderConfig
    brave_credentials: BraveSearchCredentials | None = field(
        default=None, repr=False
    )
    qwen_credentials: QwenVisualOcrCredentials | None = field(
        default=None, repr=False
    )
    qwen_workspace_id: str | None = None
    qwen_pdftoppm_executable: Path | None = field(default=None, repr=False)
    lawyer_analysis_credentials: QwenLawyerAnalysisCredentials | None = field(
        default=None, repr=False
    )
    document_delivery_enabled: bool = False
    document_content_revisions_enabled: bool = False
    document_renderer_settings: IsolatedDocumentRendererClientSettings | None = field(
        default=None, repr=False
    )
    ledger_extraction_credentials: DeepSeekLedgerExtractionCredentials | None = field(
        default=None, repr=False
    )

    def __repr__(self) -> str:
        return (
            "CaseAgentWorkerProcessSettings("
            f"worker_id={self.runtime.worker_id!r}, "
            f"firm_id={self.runtime.actor.firm_id!r}, secrets=<redacted>)"
        )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str]
    ) -> "CaseAgentWorkerProcessSettings":
        """Read only administrator/server environment, never browser state."""

        if not isinstance(environ, Mapping):
            raise CaseAgentWorkerConfigurationBlocked("Worker environment is invalid")
        if _required(environ, "LAWCASE_AGENT_WORKER_RUNTIME_MODE") != "PRODUCTION_AGENT_WORKER":
            raise CaseAgentWorkerConfigurationBlocked("Worker runtime mode is not enabled")
        firm_id = _uuid(_required(environ, "LAWCASE_AGENT_WORKER_FIRM_ID"), "Worker firm")
        actor_id = _uuid(_required(environ, "LAWCASE_AGENT_WORKER_ACTOR_ID"), "Worker actor")
        verifier_actor_id = _uuid(
            _required(environ, "LAWCASE_AGENT_VERIFIER_ACTOR_ID"),
            "Verifier actor",
        )
        if verifier_actor_id == actor_id:
            raise CaseAgentWorkerConfigurationBlocked(
                "Verifier actor must differ from execution Worker"
            )
        database_role = _required(environ, "LAWCASE_AGENT_WORKER_DATABASE_ROLE")
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database_role) is None:
            raise CaseAgentWorkerConfigurationBlocked("Worker database role is invalid")
        dsn = _required(environ, "LAWCASE_AGENT_WORKER_POSTGRES_DSN")
        _postgres_dsn(dsn, expected_role=database_role)
        verifier_database_role = _required(
            environ, "LAWCASE_AGENT_VERIFIER_DATABASE_ROLE"
        )
        if (
            re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", verifier_database_role) is None
            or verifier_database_role == database_role
        ):
            raise CaseAgentWorkerConfigurationBlocked(
                "Verifier database role is invalid"
            )
        verifier_dsn = _required(environ, "LAWCASE_AGENT_VERIFIER_POSTGRES_DSN")
        _postgres_dsn(verifier_dsn, expected_role=verifier_database_role)
        if verifier_dsn == dsn:
            raise CaseAgentWorkerConfigurationBlocked(
                "Verifier requires a distinct PostgreSQL principal"
            )
        root = Path(_required(environ, "LAWCASE_AGENT_WORKER_PRIVATE_ROOT"))
        if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts:
            raise CaseAgentWorkerConfigurationBlocked("Worker private root is invalid")

        endpoint = _required(environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT")
        if urlsplit(endpoint).scheme != "https":
            raise CaseAgentWorkerConfigurationBlocked("Worker object store requires HTTPS")
        encryption = _required(environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENCRYPTION")
        kms_key = _optional(environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_KMS_KEY_ID")
        try:
            object_store = S3PrivateObjectStoreConfig(
                endpoint_url=endpoint,
                region_name=_required(environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_REGION"),
                bucket=_required(environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_BUCKET"),
                access_key_id=_required(
                    environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_ACCESS_KEY_ID"
                ),
                secret_access_key=_required(
                    environ, "LAWCASE_AGENT_WORKER_OBJECT_STORE_SECRET_ACCESS_KEY"
                ),
                server_side_encryption=encryption,
                kms_key_id=kms_key,
                allow_insecure_internal_endpoint=False,
            )
            credentials = DeepSeekPlannerCredentials(
                api_key=_required(environ, "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY")
            )
            model = _required(environ, "LAWCASE_AGENT_WORKER_DEEPSEEK_MODEL")
            allowed_models = tuple(
                sorted(
                    set(
                        _csv(
                            _required(
                                environ,
                                "LAWCASE_AGENT_WORKER_DEEPSEEK_ALLOWED_MODELS",
                            )
                        )
                    )
                )
            )
            deepseek_config = DeepSeekPlannerProviderConfig(
                endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
                model=model,
                allowed_models=allowed_models,
            )
            brave_key = _optional(
                environ, "LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY"
            )
            brave_credentials = (
                BraveSearchCredentials(brave_key) if brave_key is not None else None
            )
            qwen_key = _optional(
                environ, "LAWCASE_AGENT_WORKER_QWEN_API_KEY"
            )
            qwen_workspace_id = _optional(
                environ, "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID"
            )
            qwen_renderer = _optional(
                environ, "LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE"
            )
            qwen_values = (qwen_key, qwen_workspace_id, qwen_renderer)
            if any(value is not None for value in qwen_values) and not all(
                value is not None for value in qwen_values
            ):
                raise CaseAgentWorkerConfigurationBlocked(
                    "Qwen OCR requires the server key, workspace and renderer together"
                )
            if qwen_key is not None:
                assert qwen_workspace_id is not None
                assert qwen_renderer is not None
                if re.fullmatch(
                    r"[a-z0-9][a-z0-9-]{2,62}", qwen_workspace_id
                ) is None:
                    raise CaseAgentWorkerConfigurationBlocked(
                        "Qwen OCR workspace identity is invalid"
                    )
                qwen_credentials = QwenVisualOcrCredentials(api_key=qwen_key)
                qwen_pdftoppm_executable = _server_executable(
                    Path(qwen_renderer), label="Qwen OCR pdftoppm"
                )
            else:
                qwen_credentials = None
                qwen_workspace_id = None
                qwen_pdftoppm_executable = None
            lawyer_analysis_key = _optional(
                environ, "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY"
            )
            lawyer_analysis_workspace = _optional(
                environ, "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID"
            )
            if (lawyer_analysis_key is None) != (
                lawyer_analysis_workspace is None
            ):
                raise CaseAgentWorkerConfigurationBlocked(
                    "lawyer analysis requires its server key and workspace together"
                )
            lawyer_analysis_credentials = (
                QwenLawyerAnalysisCredentials(
                    api_key=lawyer_analysis_key,
                    workspace_id=lawyer_analysis_workspace,
                )
                if lawyer_analysis_key is not None
                and lawyer_analysis_workspace is not None
                else None
            )
            document_delivery_enabled = _optional_feature_flag(
                environ, "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED"
            )
            document_content_revisions_enabled = _optional_feature_flag(
                environ, "LAWCASE_AGENT_WORKER_DOCUMENT_CONTENT_REVISIONS_ENABLED"
            )
            if document_content_revisions_enabled and not document_delivery_enabled:
                raise CaseAgentWorkerConfigurationBlocked("content revisions require document delivery")
            legacy_document_soffice = _optional(
                environ, "LAWCASE_AGENT_WORKER_DOCUMENT_SOFFICE_EXECUTABLE"
            )
            legacy_document_renderer = _optional(
                environ, "LAWCASE_AGENT_WORKER_DOCUMENT_PDFTOPPM_EXECUTABLE"
            )
            if legacy_document_soffice is not None or legacy_document_renderer is not None:
                raise CaseAgentWorkerConfigurationBlocked(
                    "document conversion must use the isolated renderer service"
                )
            if document_delivery_enabled:
                document_renderer_settings = (
                    IsolatedDocumentRendererClientSettings.from_worker_environment(
                        environ
                    )
                )
            else:
                document_renderer_settings = None
            ledger_extraction_key = _optional(
                environ,
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY",
            )
            ledger_extraction_model = _optional(
                environ,
                "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL",
            )
            if (ledger_extraction_key is None) != (
                ledger_extraction_model is None
            ):
                raise CaseAgentWorkerConfigurationBlocked(
                    "ledger extraction requires its server key and fixed model together"
                )
            if ledger_extraction_key is not None:
                if ledger_extraction_model != DEEPSEEK_LEDGER_EXTRACTION_MODEL:
                    raise CaseAgentWorkerConfigurationBlocked(
                        "ledger extraction model differs from the fixed contract"
                    )
                ledger_extraction_credentials = (
                    DeepSeekLedgerExtractionCredentials(
                        api_key=ledger_extraction_key
                    )
                )
            else:
                ledger_extraction_credentials = None
        except CaseAgentWorkerConfigurationBlocked:
            raise
        except IsolatedDocumentRendererBlocked:
            raise CaseAgentWorkerConfigurationBlocked(
                "isolated document renderer configuration is invalid"
            ) from None
        except Exception:
            raise CaseAgentWorkerConfigurationBlocked(
                "Worker provider or object-store configuration is invalid"
            ) from None
        actor = Actor(actor_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
        verifier_actor = Actor(
            verifier_actor_id, firm_id, frozenset({Role.SYSTEM_WORKER})
        )
        runtime = CaseAgentWorkerRuntimeSettings(
            worker_id=case_agent_worker_id(firm_id),
            actor=actor,
            postgres_dsn=dsn,
            verifier_actor=verifier_actor,
            verifier_postgres_dsn=verifier_dsn,
            worker_root=str(root),
        )
        runtime.validate()
        return cls(
            runtime=runtime,
            database_role=database_role,
            verifier_database_role=verifier_database_role,
            object_store=object_store,
            deepseek_credentials=credentials,
            deepseek_config=deepseek_config,
            brave_credentials=brave_credentials,
            qwen_credentials=qwen_credentials,
            qwen_workspace_id=qwen_workspace_id,
            qwen_pdftoppm_executable=qwen_pdftoppm_executable,
            lawyer_analysis_credentials=lawyer_analysis_credentials,
            document_delivery_enabled=document_delivery_enabled,
            document_content_revisions_enabled=document_content_revisions_enabled,
            document_renderer_settings=document_renderer_settings,
            ledger_extraction_credentials=ledger_extraction_credentials,
        )


def compose_production_case_agent_worker(
    *,
    settings: CaseAgentWorkerProcessSettings,
    planning_repository: CasePlanningProjectionRepository,
    memory_checkpoint: RunMemoryCheckpointPort | None = None,
    incident_sink: RunnerIncidentSink | None = None,
    document_official_source_text: VerifiedOfficialSourceTextPort | None = None,
    object_store_factory: Callable[
        [S3PrivateObjectStoreConfig], S3CompatiblePrivateObjectStore
    ] = S3CompatiblePrivateObjectStore,
) -> ComposedCaseAgentWorker:
    """Build only with a real injected atomic repository and server secrets."""

    if not isinstance(settings, CaseAgentWorkerProcessSettings):
        raise CaseAgentWorkerConfigurationBlocked("Worker settings are required")
    if (type(settings.document_content_revisions_enabled) is not bool
            or (settings.document_content_revisions_enabled and not settings.document_delivery_enabled)):
        raise CaseAgentWorkerConfigurationBlocked("content revisions require explicit document delivery configuration")
    if not callable(getattr(planning_repository, "read_atomic_projection", None)):
        raise CaseAgentWorkerConfigurationBlocked(
            "atomic planning projection repository is required"
        )
    # This process cannot rely on the Web process having started first.  Prove
    # the complete 0049 owner, RLS, privilege and trigger boundary using the
    # Worker DSN before constructing any adapter or publishing a heartbeat.
    try:
        preflight_case_agent_ledger_exception_followup_schema(
            dsn=settings.runtime.postgres_dsn,
            firm_id=settings.runtime.actor.firm_id,
        )
    except Exception as error:
        raise CaseAgentWorkerConfigurationBlocked(
            "exception follow-up lifecycle database boundary is unavailable"
        ) from error
    object_store = object_store_factory(settings.object_store)
    # Official-source capture is deliberately an auxiliary, one-job cycle in
    # this existing Worker process.  It shares no model credential or client
    # material path: only an already-authorized public-source lease can obtain
    # a fresh, matter-bound S3 capability.
    official_source_adapters = compose_official_source_s3_adapters(
        settings.object_store
    )
    official_source_capture_worker = BoundedOfficialSourceCaptureWorker(
        worker=settings.runtime.actor,
        case_root=Path(settings.runtime.worker_root),
        store=official_source_adapters.capture_store(
            dsn=settings.runtime.postgres_dsn
        ),
        artifact_store_factory=lambda matter_id: (
            official_source_adapters.capture_artifact_store(
                firm_id=settings.runtime.actor.firm_id,
                matter_id=matter_id,
            )
        ),
    )
    memory_enrichment = PostgresPlanningMemoryEnrichmentStore(
        settings.runtime.postgres_dsn
    )
    memory_request_factory = DynamicPlanningMemorySearchRequestFactory(
        # Planning memory is context, not an executable Tool input.  The
        # compiler has no Skill with this sentinel id and therefore rejects a
        # model attempt to turn memory into a task source.
        allowed_skill_ids=("planning_memory_context",),
    )
    research_binding = None
    research_provider = None
    research_exchange = None
    if settings.brave_credentials is not None:
        research_binding = PostgresPublicResearchBindingPort(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
        )
        research_provider = BravePublicSearchProvider(
            credentials=settings.brave_credentials,
            transport=None,
        )
        research_exchange = PostgresDurablePublicSearchExchange(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            object_store=object_store,
            transport=PinnedBraveHttpsTransport(),
        )
    visual_binding = None
    visual_exchange = None
    if settings.qwen_credentials is not None:
        assert settings.qwen_workspace_id is not None
        assert settings.qwen_pdftoppm_executable is not None
        projection_policy = VisualOcrProjectionPolicy(
            worker_root=Path(settings.runtime.worker_root),
            pdftoppm_executable=settings.qwen_pdftoppm_executable,
        )
        visual_binding = PostgresVisualOcrBindingPort(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            object_store=object_store,
            policy=projection_policy,
            workspace_id=settings.qwen_workspace_id,
        )
        durable_visual_broker = PostgresDurableQwenVisualOcrBroker(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=PinnedQwenVisualOcrHttpsBroker(),
        )
        visual_exchange = QwenVisualOcrRecoverableExchange(
            credentials=settings.qwen_credentials,
            egress=durable_visual_broker,
            recovery=durable_visual_broker,
        )
    lawyer_analysis_exchange = None
    if settings.lawyer_analysis_credentials is not None:
        lawyer_analysis_exchange = (
            PostgresBoundRecoverableLawyerAnalysisExchange(
                dsn=settings.runtime.postgres_dsn,
                worker_actor=settings.runtime.actor,
                credentials=settings.lawyer_analysis_credentials,
                object_store=object_store,
            )
        )
    document_binding = None
    document_provider = None
    document_exchange = None
    document_converter = None
    document_staging = None
    document_artifact_access = None
    document_revision_worker = None
    if settings.document_delivery_enabled:
        if not callable(
            getattr(document_official_source_text, "read_verified_source_text", None)
        ):
            raise CaseAgentWorkerConfigurationBlocked(
                "dynamic document delivery requires verified official-source access"
            )
        assert settings.document_renderer_settings is not None
        document_converter = IsolatedDocumentRendererClient(
            settings=settings.document_renderer_settings
        )
        try:
            document_converter.preflight()
        except Exception as error:
            raise CaseAgentWorkerConfigurationBlocked(
                "isolated document renderer is unavailable"
            ) from error
        document_credentials = DeepSeekDocumentDraftCredentials(
            api_key=settings.deepseek_credentials.api_key
        )
        document_config = DeepSeekDocumentDraftConfig(
            endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
            model=settings.deepseek_config.model,
            allowed_models=settings.deepseek_config.allowed_models,
        )
        document_provider = DeepSeekDocumentDraftProvider(
            credentials=document_credentials,
            config=document_config,
        )
        raw_transport = DeepSeekDocumentRawHttpsTransport(
            credentials=document_credentials,
            config=document_config,
        )
        response_store = S3DocumentDraftRawResponseStore(settings.object_store)
        preflight_case_agent_document_delivery_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            verifier_actor=settings.runtime.verifier_actor,
            object_store=object_store,
        )
        preflight_case_agent_document_exchange_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=raw_transport,
            response_store=response_store,
        )
        document_exchange = PostgresRecoverableDocumentDraftExchange(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=raw_transport,
            response_store=response_store,
        )
        document_templates = first_release_reviewable_document_templates()
        verified_lawyer_package_access = PostgresManagedArtifactAccessPort(
            dsn=settings.runtime.verifier_postgres_dsn,
            verifier_actor=settings.runtime.verifier_actor,
            execution_actor_id=settings.runtime.actor.actor_id,
            object_store=object_store,
        )
        verified_lawyer_package = PostgresVerifiedLawyerDecisionPackagePort(
            dsn=settings.runtime.verifier_postgres_dsn,
            verifier_actor=settings.runtime.verifier_actor,
            execution_actor_id=settings.runtime.actor.actor_id,
            artifact_access=verified_lawyer_package_access,
        )
        document_binding = PostgresDynamicDocumentBindingPort(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            templates=document_templates,
            official_source_text=document_official_source_text,
            verified_lawyer_package=verified_lawyer_package,
        )
        if settings.document_content_revisions_enabled:
            preflight_content_revision_runtime(
                execution_dsn=settings.runtime.postgres_dsn,
                verifier_dsn=settings.runtime.verifier_postgres_dsn,
                worker_actor=settings.runtime.actor,
                verifier_actor=settings.runtime.verifier_actor,
            )
        document_staging = PostgresReviewableDocumentPackageStore(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            object_store=object_store,
            template_registry=document_templates,
            enable_content_revisions=settings.document_content_revisions_enabled,
        )
        document_artifact_access = PostgresReviewableDocumentPackageAccessPort(
            dsn=settings.runtime.verifier_postgres_dsn,
            verifier_actor=settings.runtime.verifier_actor,
            execution_actor_id=settings.runtime.actor.actor_id,
            object_store=object_store,
            template_registry=document_templates,
        )
        document_revision_worker = PostgresDocumentRevisionWorker(
            execution_dsn=settings.runtime.postgres_dsn,
            verifier_dsn=settings.runtime.verifier_postgres_dsn,
            worker_actor=settings.runtime.actor,
            verifier_actor=settings.runtime.verifier_actor,
            binding=document_binding,
            converter=document_converter,
            package_store=document_staging,
            package_access=document_artifact_access,
        )
        if settings.document_content_revisions_enabled:
            content_worker = PostgresContentRevisionWorker(
                worker_actor=settings.runtime.actor,
                jobs=PostgresContentGenerationJobStore(
                    dsn=settings.runtime.postgres_dsn, worker_actor=settings.runtime.actor),
                binding=document_binding, converter=document_converter,
                package_store=document_staging, package_access=document_artifact_access,
                receipts=PostgresDocumentRevisionReceiptStore(
                    execution_dsn=settings.runtime.postgres_dsn,
                    verifier_dsn=settings.runtime.verifier_postgres_dsn,
                    worker_actor=settings.runtime.actor,
                    verifier_actor=settings.runtime.verifier_actor),
            )
            document_revision_worker = DocumentRevisionWorkerGroup(document_revision_worker, content_worker)
    ledger_extraction_exchange = None
    if settings.ledger_extraction_credentials is not None:
        ledger_transport = DeepSeekLedgerExtractionRawHttpsTransport(
            credentials=settings.ledger_extraction_credentials,
        )
        ledger_response_store = S3LedgerExtractionRawResponseStore(
            settings.object_store
        )
        # Both halves are required before the executable Skill can enter the
        # adapter catalog/heartbeat: 0045 proves the one-call external
        # boundary, while 0042 proves the private lawyer-review landing path.
        preflight_case_agent_ledger_extraction_staging_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
        )
        preflight_case_agent_ledger_extraction_runtime_contract(
            dsn=settings.runtime.postgres_dsn,
            worker_actor=settings.runtime.actor,
            transport=ledger_transport,
            response_store=ledger_response_store,
        )
        ledger_extraction_exchange = (
            PostgresRecoverableLedgerExtractionExchange(
                dsn=settings.runtime.postgres_dsn,
                worker_actor=settings.runtime.actor,
                transport=ledger_transport,
                response_store=ledger_response_store,
            )
        )

    def planner_factory(request_guard):
        return ControlledDefencePlanningRouter(
            fallback=DeepSeekCaseAgentPlanner(
                credentials=settings.deepseek_credentials,
                config=settings.deepseek_config,
                request_guard=request_guard,
            ),
            outcome_recorder=request_guard,
        )

    return compose_case_agent_worker_from_repository(
        settings=settings.runtime,
        object_store=object_store,
        planning_repository=planning_repository,
        planner_factory=planner_factory,
        planning_memory_enrichment=memory_enrichment,
        planning_memory_request_factory=memory_request_factory,
        public_research_binding=research_binding,
        public_research_provider=research_provider,
        public_research_exchange=research_exchange,
        visual_ocr_binding=visual_binding,
        visual_ocr_exchange=visual_exchange,
        visual_ocr_workspace_id=settings.qwen_workspace_id,
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
        controlled_defence_snapshot_filter=True,
        memory_checkpoint=memory_checkpoint,
        incident_sink=incident_sink,
    )


def recover_one_production_document_content(
    *, settings: CaseAgentWorkerProcessSettings, matter_id: str, run_id: str, request_id: str,
    enabled: bool = False,
    object_store_factory: Callable[[S3PrivateObjectStoreConfig], S3CompatiblePrivateObjectStore] = S3CompatiblePrivateObjectStore,
) -> str:
    """Explicit single-request maintenance entrypoint, never the worker loop.

    Does not construct planners, model transports, converters or writable
    document staging. New generation may remain disabled during recovery.
    """
    if enabled is not True:
        raise CaseAgentWorkerConfigurationBlocked("single document recovery is not explicitly enabled")
    try:
        if any(not isinstance(value, str) or str(UUID(value)) != value for value in (matter_id, run_id, request_id)):
            raise ValueError("noncanonical recovery scope")
    except (ValueError, TypeError, AttributeError) as error:
        raise CaseAgentWorkerConfigurationBlocked("single document recovery scope is invalid") from error
    preflight_content_recovery_runtime(verifier_dsn=settings.runtime.verifier_postgres_dsn,
                                      verifier_actor=settings.runtime.verifier_actor)
    access = PostgresReviewableDocumentPackageAccessPort(
        dsn=settings.runtime.verifier_postgres_dsn, verifier_actor=settings.runtime.verifier_actor,
        execution_actor_id=settings.runtime.actor.actor_id,
        object_store=object_store_factory(settings.object_store),
        template_registry=first_release_reviewable_document_templates(),
    )
    recovery = PostgresContentRevisionRecoveryStore(
        verifier_dsn=settings.runtime.verifier_postgres_dsn, worker_actor=settings.runtime.actor,
        verifier_actor=settings.runtime.verifier_actor, package_access=access, enabled=True,
    )
    return recovery.recover(matter_id=matter_id, run_id=run_id, request_id=request_id)


def run_production_case_agent_worker(
    *,
    planning_repository: CasePlanningProjectionRepository | None = None,
    environ: Mapping[str, str] | None = None,
    memory_checkpoint: RunMemoryCheckpointPort | None = None,
    incident_sink: RunnerIncidentSink | None = None,
    document_official_source_text: VerifiedOfficialSourceTextPort | None = None,
) -> None:
    """Deployment entrypoint.  Missing repository is a startup failure."""

    if planning_repository is None:
        raise CaseAgentWorkerConfigurationBlocked(
            "production atomic planning repository is not installed"
        )
    settings = CaseAgentWorkerProcessSettings.from_environment(
        os.environ if environ is None else environ
    )
    runtime = compose_production_case_agent_worker(
        settings=settings,
        planning_repository=planning_repository,
        memory_checkpoint=memory_checkpoint,
        incident_sink=incident_sink,
        document_official_source_text=document_official_source_text,
    )
    run_composed_worker(runtime)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or value.lower() in {"none", "null", "changeme", "placeholder"}
        or value.lower().startswith(("replace", "your-", "your_", "${"))
    ):
        raise CaseAgentWorkerConfigurationBlocked(f"required Worker setting {name} is invalid")
    return value


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value in {None, ""}:
        return None
    return _required(environ, name)


def _optional_feature_flag(environ: Mapping[str, str], name: str) -> bool:
    value = environ.get(name)
    if value in {None, "", "false"}:
        return False
    if value == "true":
        return True
    raise CaseAgentWorkerConfigurationBlocked(
        f"optional Worker feature flag {name} must be true, false or empty"
    )


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise CaseAgentWorkerConfigurationBlocked(f"{label} id is invalid") from None


def _postgres_dsn(value: str, *, expected_role: str) -> None:
    try:
        details = conninfo_to_dict(value)
    except Exception:
        raise CaseAgentWorkerConfigurationBlocked("Worker PostgreSQL DSN is invalid") from None
    if (
        details.get("user") != expected_role
        or not details.get("host")
        or not details.get("dbname")
        or str(details.get("sslmode", "")).lower() != "verify-full"
    ):
        raise CaseAgentWorkerConfigurationBlocked("Worker PostgreSQL DSN is invalid")


def _csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(","))
    if not items or any(not item for item in items) or len(items) != len(set(items)):
        raise CaseAgentWorkerConfigurationBlocked("Worker model allowlist is invalid")
    return items


def _server_executable(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise CaseAgentWorkerConfigurationBlocked(f"{label} path is invalid")
    try:
        metadata = path.lstat()
    except OSError:
        raise CaseAgentWorkerConfigurationBlocked(f"{label} is unavailable") from None
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise CaseAgentWorkerConfigurationBlocked(f"{label} is unavailable")
    return path.resolve(strict=True)


__all__ = (
    "CaseAgentWorkerConfigurationBlocked",
    "CaseAgentWorkerProcessSettings",
    "compose_production_case_agent_worker",
    "run_production_case_agent_worker",
    "recover_one_production_document_content",
)
