"""Fail-closed composition root for the self-hosted Web workbench.

This module is deliberately the only place that reads the Web deployment
environment.  Importing it opens no network connection, database connection,
object-store client request, browser route, desktop bridge, or local-case
folder.  A process must explicitly call :func:`create_web_runtime_app` (for
example through ``uvicorn --factory``) after every required production setting
has been supplied.

The v1 OIDC authorization-state implementation is process-local by design.
Consequently this composition accepts exactly one API process; it refuses an
otherwise-dangerous "replica" configuration instead of silently losing PKCE
state across workers.  A future multi-node implementation must inject a shared
*ephemeral*, atomic state store before that boundary is relaxed.

No desktop, Tauri, Keychain, loopback, bearer-token, public-object URL, or
browser filesystem capability is imported into this composition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI
from case_kernel.case_agent_fact_correction_postgres import PostgresFactCorrectionProposalStore
from .web_fact_correction import WebFactCorrectionOriginalReader
from psycopg.conninfo import conninfo_to_dict

from case_kernel.evidence_intake_worker import ClamAvCommandScanner
from case_kernel.case_ledger_postgres import PostgresCaseLedgerStore
from case_kernel.case_agent_postgres import PostgresCaseAgentStore
from case_kernel.legal_source_postgres import PostgresLegalSourceStore
from case_kernel.official_source_private_store import (
    OfficialSourceS3ProductionAdapters,
    compose_official_source_s3_adapters,
)
from case_kernel.evidence_manifest_postgres import PostgresEvidenceManifestStore
from case_kernel.models import Actor, Role
from case_kernel.postgres_store import (
    PostgresMatterStore,
    preflight_case_agent_matter_provisioning_contract,
)
from case_kernel.formal_calculation_postgres import PostgresFormalCalculationStore
from case_kernel.submission_postgres import PostgresSubmissionStore
from case_kernel.web_object_store import S3CompatiblePrivateObjectStore, S3PrivateObjectStoreConfig
from case_kernel.common_material_object_store import S3CommonMaterialPrivateObjectStore
from case_kernel.case_agent_runtime_identity import ExactCaseAgentRuntimeReadiness
from case_kernel.case_agent_runtime_postgres import (
    PostgresCaseAgentMatterPrincipalReadiness,
    PostgresEvidenceProjectionAuthorizationPort,
    PostgresLedgerExtractionProjectionPort,
)
from case_kernel.case_agent_skill_adapters import WebEvidencePageTaskProjectionPort
from case_kernel.case_agent_ledger_extraction_adapters import (
    DEEPSEEK_LEDGER_EXTRACTION_MANIFEST,
)
from case_kernel.case_agent_ledger_extraction_postgres import (
    PostgresCaseLedgerExtractionPromotionStore,
    TaskBoundEvidencePageTextReader,
)
from case_kernel.case_agent_document_delivery_postgres import (
    PostgresReviewableDocumentPackageAccessPort,
    ReviewableDocumentPackageRead,
)
from case_kernel.case_agent_document_delivery import (
    first_release_reviewable_document_templates,
    DynamicDocumentTaskBinding,
)
from case_kernel.case_agent_document_revisions import (
    PostgresDocumentRevisionCommandStore,
)
from case_kernel.case_agent_document_binding_postgres import (
    PostgresDynamicDocumentBindingPort, PostgresVerifiedLawyerDecisionPackagePort,
    VerifiedOfficialSourceTextPort,
)
from case_kernel.case_agent_runtime_postgres import PostgresManagedArtifactAccessPort
from case_kernel.case_posture_postgres import PostgresCasePostureStore
from case_kernel.case_work_plan_postgres import PostgresCaseWorkPlanStore
from case_kernel.web_pdf_page_preview import WebPdfPagePreviewPolicy, WebPdfPagePreviewService
from case_kernel.web_upload_staging import WebUploadStagingArea
from case_kernel.web_common_material_admission import CommonMaterialStagingArea
from case_kernel.web_zip_staging import WebZipStagingArea
from case_kernel.reviewable_draft_postgres import PostgresReviewableDraftStore
from case_kernel.isolated_document_renderer import (
    IsolatedDocumentRendererBlocked,
    IsolatedDocumentRendererClient,
    IsolatedDocumentRendererClientSettings,
    decode_shared_secret_base64url,
)
from case_kernel.web_agent_evidence_projection import (
    WebAgentEvidenceProjectionPolicy,
    WebAgentEvidenceProjectionSource,
)
from .web_document_drafts import WebDocumentDraftService
from .web_document_draft_delivery import WebDocumentDraftDeliveryService
from .web_case_agent_control import WebCaseAgentControlService
from .web_case_agent_artifacts import (
    PostgresWebCaseAgentArtifactReviewService,
)
from .web_case_agent_documents import (
    PostgresWebCaseAgentDocumentReviewService,
)
from .web_case_agent_final_review import WebCaseAgentFinalReviewReadiness

from .web_app import WebApiDependencies, WebApiSettings, create_web_app
from .web_identity import MfaClaimRequirement, OidcVerificationPolicy, TokenTransportPolicy, WebOidcIdentityResolver
from .web_identity_directory import PostgresOidcIdentityDirectory
from .web_jwks import CachedHttpsJwksProvider, JwksFetchPolicy
from .web_material_upload import WebMaterialUploadService
from .web_material_upload_postgres import PostgresWebMaterialUploadSlotStore
from .web_common_material_upload import WebCommonMaterialUploadService
from .web_common_material_upload_postgres import PostgresCommonMaterialUploadStore
from .web_case_posture import WebCasePostureService
from .web_dynamic_case_plan import WebDynamicCasePlanService
from .web_agent_ledger_extraction_review import (
    WebAgentLedgerExtractionReviewService,
)
from .web_agent_ledger_extraction_review_postgres import (
    PostgresAgentLedgerExtractionReviewStore,
    preflight_web_ledger_confirmation_session_authority,
)
from case_kernel.case_agent_ledger_exception_followup_postgres import (
    PostgresCaseLedgerExceptionFollowupStore,
    preflight_case_agent_ledger_exception_followup_schema,
)
from .web_agent_ledger_exception_followup import (
    WebAgentLedgerExceptionFollowupService,
)
from .web_archive_upload import WebMaterialArchiveUploadService
from .web_archive_upload_postgres import PostgresWebMaterialArchiveStore
from .web_evidence_review import WebEvidenceReviewService
from .web_derivative_worker import WebEvidenceDerivativeWorker
from .web_derivative_delivery import WebDerivativeDeliveryService
from .web_oidc_login import (
    EphemeralOidcAuthorizationStateStore,
    OidcAuthorizationCodeLogin,
    OidcAuthorizationCodePolicy,
    UrlLibOidcTokenEndpointClient,
)
from .web_session import WebSessionAuthority, WebSessionPolicy
from .web_session_postgres import PostgresWebSessionActorDirectory, PostgresWebSessionStore
from .local_managed_acceptance_auth import LocalManagedAcceptanceSessionBootstrap


__all__ = (
    "WebPrivateRoots",
    "WebRuntimeAssemblyAdapters",
    "WebRuntimeComposition",
    "WebRuntimeConfigurationBlocked",
    "WebRuntimeSettings",
    "build_web_runtime_composition",
    "create_web_runtime_app",
    "load_web_runtime_settings",
)


_DATABASE_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_CLAIM_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")
_MAX_DSN_LENGTH = 4_096
_PLACEHOLDER_WORDS = frozenset(
    {
        "",
        "-",
        "changeme",
        "change-me",
        "example",
        "none",
        "null",
        "placeholder",
        "replace",
        "replace-me",
        "todo",
        "unset",
    }
)
_PLACEHOLDER_PREFIXES = (
    "${",
    "<",
    "replace_",
    "replace-",
    "replace with",
    "replace-with",
    "your_",
    "your-",
)
_RESERVED_PUBLIC_HOSTS = ("localhost", ".localhost", ".example", ".invalid", ".test")
_SINGLE_API_TOPOLOGY = "SINGLE_API_PROCESS"
_IN_PROCESS_STATE_STORE = "IN_PROCESS_EPHEMERAL"
_PRODUCTION_MODE = "PRODUCTION_WEB"


class WebRuntimeConfigurationBlocked(RuntimeError):
    """Web production composition is incomplete, unsafe, or not explicitly enabled.

    Messages deliberately identify a configuration *category* only.  They do
    not echo a DSN, password, client secret, object-store credential, object
    key, path inside a case, OIDC state, JWT, or scanner output.
    """


class _FirmScopedContentBinding:
    """Server-selected, read-only source reconstruction after human authorization."""

    def __init__(self, *, dsn: str, execution_actor_ids: Mapping[str, str], verifier_actor_ids: Mapping[str, str], object_store: object, official_source_text: VerifiedOfficialSourceTextPort) -> None:
        self._ports = {}
        if set(execution_actor_ids) != set(verifier_actor_ids):
            raise WebRuntimeConfigurationBlocked("content binding principal mappings differ")
        for firm_id, execution_id in execution_actor_ids.items():
            worker = Actor(execution_id, firm_id, frozenset({Role.SYSTEM_WORKER}))
            verifier = Actor(verifier_actor_ids[firm_id], firm_id, frozenset({Role.SYSTEM_WORKER}))
            access = PostgresManagedArtifactAccessPort(dsn=dsn, verifier_actor=verifier, execution_actor_id=execution_id, object_store=object_store)
            self._ports[firm_id] = PostgresDynamicDocumentBindingPort(
                dsn=dsn, worker_actor=worker, templates=first_release_reviewable_document_templates(),
                official_source_text=official_source_text,
                verified_lawyer_package=PostgresVerifiedLawyerDecisionPackagePort(
                    dsn=dsn, verifier_actor=verifier, execution_actor_id=execution_id, artifact_access=access,
                ),
            )

    def __call__(self, *, actor: Actor, package: ReviewableDocumentPackageRead, matter_id: str, run_id: str) -> DynamicDocumentTaskBinding:
        port = self._ports.get(actor.firm_id)
        if port is None or package.run_id != run_id:
            raise PermissionError("content binding firm is not configured")
        return port.resolve_content_proposal_binding(
            actor=actor, package_id=package.package_id,
            matter_id=matter_id, run_id=run_id,
        )


class _FirmScopedDocumentPackageAccess:
    """Dispatch 0039 package reads to the exact firm verifier identity.

    The browser can never choose a verifier.  The mapping is the same
    administrator-owned execution/verifier mapping used by runtime readiness,
    and each concrete access port independently reauthorizes the current run,
    graph, task, work plan, posture and object bytes.
    """

    def __init__(
        self,
        *,
        dsn: str,
        execution_actor_ids: Mapping[str, str],
        verifier_actor_ids: Mapping[str, str],
        object_store: object,
    ) -> None:
        if set(execution_actor_ids) != set(verifier_actor_ids):
            raise WebRuntimeConfigurationBlocked(
                "Agent execution and verifier firm mappings differ"
            )
        self._ports = {
            firm_id: PostgresReviewableDocumentPackageAccessPort(
                dsn=dsn,
                verifier_actor=Actor(
                    verifier_actor_ids[firm_id],
                    firm_id,
                    frozenset({Role.SYSTEM_WORKER}),
                ),
                execution_actor_id=execution_actor_ids[firm_id],
                object_store=object_store,  # type: ignore[arg-type]
            )
            for firm_id in sorted(execution_actor_ids)
        }

    def read_package(
        self,
        *,
        firm_id: str,
        matter_id: str,
        run_id: str,
        artifact_id: str,
    ):
        port = self._ports.get(firm_id)
        if port is None:
            raise PermissionError("document package firm is not configured")
        return port.read_package(
            firm_id=firm_id,
            matter_id=matter_id,
            run_id=run_id,
            artifact_id=artifact_id,
        )


@dataclass(frozen=True)
class WebPrivateRoots:
    """Two server-owned private filesystem roots.

    ``staging_root`` is for bytes which have not yet passed scanning.  The
    materialization root is reserved for a later, dedicated derivative worker;
    keeping the roots distinct prevents a browser upload from becoming a worker
    input merely because it shares a directory.
    """

    staging_root: Path = field(repr=False)
    worker_materialization_root: Path = field(repr=False)


@dataclass(frozen=True)
class WebRuntimeSettings:
    """All explicit, production-only inputs for the browser workbench.

    The browser never receives this object.  Secret-bearing fields are hidden
    from repr so an accidental diagnostics print cannot expose credentials.
    The values are intentionally not read from ``os.environ`` until
    :meth:`from_environment` is called by an explicit runtime factory.
    """

    runtime_mode: str
    deployment_topology: str
    oidc_state_store: str
    public_origin: str
    oidc_issuer: str
    oidc_authorization_endpoint: str
    oidc_token_endpoint: str
    oidc_jwks_url: str
    oidc_client_id: str
    oidc_client_secret: str = field(repr=False)
    oidc_audience: str = ""
    oidc_scopes: frozenset[str] = frozenset({"openid"})
    oidc_required_amr: frozenset[str] = frozenset({"mfa"})
    oidc_accepted_acr: frozenset[str] = frozenset()
    app_postgres_dsn: str = field(default="", repr=False)
    app_database_role: str = "lawcase_web_application"
    identity_directory_postgres_dsn: str = field(default="", repr=False)
    identity_directory_database_role: str = "lawcase_identity_directory"
    session_gateway_postgres_dsn: str = field(default="", repr=False)
    session_gateway_database_role: str = "lawcase_web_session_gateway"
    object_store_endpoint: str = ""
    object_store_region: str = ""
    object_store_bucket: str = ""
    object_store_access_key_id: str = field(default="", repr=False)
    object_store_secret_access_key: str = field(default="", repr=False)
    object_store_encryption: str = "aws:kms"
    object_store_kms_key_id: str | None = field(default=None, repr=False)
    clamav_executable: Path = field(default_factory=lambda: Path("/unconfigured"), repr=False)
    clamav_timeout_seconds: int = 120
    pdftoppm_executable: Path = field(default_factory=lambda: Path("/unconfigured-pdftoppm"), repr=False)
    pdftoppm_timeout_seconds: int = 30
    document_worker_enabled: bool = False
    document_renderer_settings: IsolatedDocumentRendererClientSettings | None = field(
        default=None, repr=False
    )
    private_roots: WebPrivateRoots = field(
        default_factory=lambda: WebPrivateRoots(Path("/unconfigured-staging"), Path("/unconfigured-worker")),
        repr=False,
    )
    system_worker_ids_by_firm: Mapping[str, str] = field(default_factory=dict, repr=False)
    system_verifier_ids_by_firm: Mapping[str, str] = field(default_factory=dict, repr=False)
    local_managed_acceptance_auth_bypass: bool = False
    local_managed_acceptance_firm_id: str | None = None
    local_managed_acceptance_lead_actor_id: str | None = None

    def __post_init__(self) -> None:
        if self.runtime_mode != _PRODUCTION_MODE:
            raise WebRuntimeConfigurationBlocked("Web runtime mode is not explicitly enabled")
        if self.deployment_topology != _SINGLE_API_TOPOLOGY:
            raise WebRuntimeConfigurationBlocked("Web runtime currently requires one API process")
        if self.oidc_state_store != _IN_PROCESS_STATE_STORE:
            raise WebRuntimeConfigurationBlocked("Web OIDC state store is not the supported ephemeral single-process store")

        _validate_production_public_origin(self.public_origin)
        _validate_configured_text(self.oidc_issuer, label="OIDC issuer", maximum=1_024)
        _validate_configured_text(self.oidc_authorization_endpoint, label="OIDC authorization endpoint", maximum=2_048)
        _validate_configured_text(self.oidc_token_endpoint, label="OIDC token endpoint", maximum=2_048)
        _validate_configured_text(self.oidc_jwks_url, label="OIDC JWKS endpoint", maximum=2_048)
        _validate_configured_text(self.oidc_client_id, label="OIDC client identifier", maximum=255)
        _validate_secret(self.oidc_client_secret, label="OIDC client secret", minimum=16, maximum=4_096)
        _validate_configured_text(self.oidc_audience, label="OIDC audience", maximum=255)
        _validate_claim_collection(self.oidc_scopes, label="OIDC scopes", required_value="openid")
        _validate_claim_collection(self.oidc_required_amr, label="OIDC MFA AMR", required_value="mfa")
        _validate_claim_collection(self.oidc_accepted_acr, label="OIDC accepted ACR", allow_empty=True)

        _validate_database_role(self.app_database_role, label="Web application database role")
        _validate_database_role(self.identity_directory_database_role, label="OIDC identity directory database role")
        _validate_database_role(self.session_gateway_database_role, label="Web session gateway database role")
        if len(
            {
                self.app_database_role,
                self.identity_directory_database_role,
                self.session_gateway_database_role,
            }
        ) != 3:
            raise WebRuntimeConfigurationBlocked("Web PostgreSQL roles must be distinct")
        _validate_postgres_dsn(
            self.app_postgres_dsn,
            label="Web application PostgreSQL DSN",
            expected_role=self.app_database_role,
        )
        _validate_postgres_dsn(
            self.identity_directory_postgres_dsn,
            label="OIDC identity directory PostgreSQL DSN",
            expected_role=self.identity_directory_database_role,
        )
        _validate_postgres_dsn(
            self.session_gateway_postgres_dsn,
            label="Web session gateway PostgreSQL DSN",
            expected_role=self.session_gateway_database_role,
        )
        if len(
            {
                self.app_postgres_dsn,
                self.identity_directory_postgres_dsn,
                self.session_gateway_postgres_dsn,
            }
        ) != 3:
            raise WebRuntimeConfigurationBlocked("Web PostgreSQL connection identities must be separate")

        _validate_configured_text(self.object_store_endpoint, label="private object-store endpoint", maximum=1_024)
        _validate_configured_text(self.object_store_region, label="private object-store region", maximum=32)
        _validate_configured_text(self.object_store_bucket, label="private object-store bucket", maximum=63)
        _validate_configured_text(self.object_store_access_key_id, label="private object-store access key", maximum=256)
        _validate_secret(self.object_store_secret_access_key, label="private object-store secret", minimum=16, maximum=1_024)
        if self.object_store_encryption not in {"AES256", "aws:kms"}:
            raise WebRuntimeConfigurationBlocked("private object-store encryption mode is invalid")
        if self.object_store_encryption == "aws:kms":
            _validate_configured_text(self.object_store_kms_key_id, label="private object-store KMS key", maximum=512)
        elif self.object_store_kms_key_id is not None:
            raise WebRuntimeConfigurationBlocked("private object-store KMS key is inconsistent with encryption mode")
        try:
            S3PrivateObjectStoreConfig(
                endpoint_url=self.object_store_endpoint,
                region_name=self.object_store_region,
                bucket=self.object_store_bucket,
                access_key_id=self.object_store_access_key_id,
                secret_access_key=self.object_store_secret_access_key,
                server_side_encryption=self.object_store_encryption,
                kms_key_id=self.object_store_kms_key_id,
                # A browser-facing commercial runtime never falls back to an
                # unencrypted internal HTTP object-store hop.  The current
                # Compose MinIO service remains setup-gated for this reason.
                allow_insecure_internal_endpoint=False,
            )
        except Exception:
            raise WebRuntimeConfigurationBlocked("private object-store configuration is invalid") from None

        normalized_clamav = _declared_absolute_path(self.clamav_executable, label="ClamAV executable")
        object.__setattr__(self, "clamav_executable", normalized_clamav)
        if type(self.clamav_timeout_seconds) is not int or not 10 <= self.clamav_timeout_seconds <= 600:
            raise WebRuntimeConfigurationBlocked("ClamAV timeout is invalid")
        normalized_pdftoppm = _declared_absolute_path(self.pdftoppm_executable, label="pdftoppm executable")
        object.__setattr__(self, "pdftoppm_executable", normalized_pdftoppm)
        if type(self.pdftoppm_timeout_seconds) is not int or not 1 <= self.pdftoppm_timeout_seconds <= 60:
            raise WebRuntimeConfigurationBlocked("pdftoppm timeout is invalid")
        if type(self.document_worker_enabled) is not bool:
            raise WebRuntimeConfigurationBlocked("document worker flag is invalid")
        if self.document_worker_enabled:
            if not isinstance(
                self.document_renderer_settings,
                IsolatedDocumentRendererClientSettings,
            ):
                raise WebRuntimeConfigurationBlocked(
                    "document worker requires the isolated document renderer"
                )
        elif self.document_renderer_settings is not None:
            raise WebRuntimeConfigurationBlocked(
                "isolated document renderer requires document worker enablement"
            )
        roots = _normalize_private_roots(self.private_roots)
        object.__setattr__(self, "private_roots", roots)
        workers = _normalize_system_workers(self.system_worker_ids_by_firm)
        verifiers = _normalize_system_workers(self.system_verifier_ids_by_firm)
        if set(workers) != set(verifiers) or any(
            workers[firm_id] == verifiers[firm_id] for firm_id in workers
        ):
            raise WebRuntimeConfigurationBlocked(
                "Web execution/verifier mappings are inconsistent"
            )
        object.__setattr__(self, "system_worker_ids_by_firm", workers)
        object.__setattr__(self, "system_verifier_ids_by_firm", verifiers)
        if self.local_managed_acceptance_auth_bypass:
            if self.public_origin != "https://workbench.127.0.0.1.nip.io":
                raise WebRuntimeConfigurationBlocked(
                    "local managed acceptance authentication requires the fixed loopback origin"
                )
            if not self.oidc_issuer.startswith("https://identity.127.0.0.1.nip.io/"):
                raise WebRuntimeConfigurationBlocked(
                    "local managed acceptance authentication requires the fixed loopback issuer"
                )
            try:
                acceptance_firm = str(UUID(str(self.local_managed_acceptance_firm_id)))
                acceptance_actor = str(UUID(str(self.local_managed_acceptance_lead_actor_id)))
            except (TypeError, ValueError, AttributeError):
                raise WebRuntimeConfigurationBlocked(
                    "local managed acceptance authentication identity is invalid"
                ) from None
            if acceptance_firm not in workers or acceptance_actor in {
                workers[acceptance_firm],
                verifiers[acceptance_firm],
            }:
                raise WebRuntimeConfigurationBlocked(
                    "local managed acceptance authentication identity is inconsistent"
                )
            object.__setattr__(self, "local_managed_acceptance_firm_id", acceptance_firm)
            object.__setattr__(self, "local_managed_acceptance_lead_actor_id", acceptance_actor)
        elif (
            self.local_managed_acceptance_firm_id is not None
            or self.local_managed_acceptance_lead_actor_id is not None
        ):
            raise WebRuntimeConfigurationBlocked(
                "local managed acceptance authentication identity requires the explicit bypass gate"
            )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "WebRuntimeSettings":
        """Parse one explicit deployment environment without logging it.

        This parser intentionally accepts no defaults for production inputs.
        A typo, missing secret, placeholder, duplicate database identity,
        insecure public origin, test hostname, or unacknowledged topology stops
        the process before :func:`create_web_app` can mount case routes.
        """

        if not isinstance(environ, Mapping):
            raise WebRuntimeConfigurationBlocked("Web runtime environment is invalid")
        _reject_parallel_process_environment(environ)
        document_worker_enabled = _parse_boolean_environment(
            environ, "LAWCASE_WEB_DOCUMENT_WORKER_ENABLED", default=False
        )
        document_renderer_settings = _parse_web_document_renderer_settings(
            environ, enabled=document_worker_enabled
        )
        return cls(
            runtime_mode=_required_environment_value(environ, "LAWCASE_WEB_RUNTIME_MODE"),
            deployment_topology=_required_environment_value(environ, "LAWCASE_WEB_DEPLOYMENT_TOPOLOGY"),
            oidc_state_store=_required_environment_value(environ, "LAWCASE_WEB_OIDC_STATE_STORE"),
            public_origin=_required_environment_value(environ, "LAWCASE_WEB_PUBLIC_ORIGIN"),
            oidc_issuer=_required_environment_value(environ, "LAWCASE_WEB_OIDC_ISSUER"),
            oidc_authorization_endpoint=_required_environment_value(environ, "LAWCASE_WEB_OIDC_AUTHORIZATION_ENDPOINT"),
            oidc_token_endpoint=_required_environment_value(environ, "LAWCASE_WEB_OIDC_TOKEN_ENDPOINT"),
            oidc_jwks_url=_required_environment_value(environ, "LAWCASE_WEB_OIDC_JWKS_URL"),
            oidc_client_id=_required_environment_value(environ, "LAWCASE_WEB_OIDC_CLIENT_ID"),
            oidc_client_secret=_required_environment_value(environ, "LAWCASE_WEB_OIDC_CLIENT_SECRET"),
            oidc_audience=_required_environment_value(environ, "LAWCASE_WEB_OIDC_AUDIENCE"),
            oidc_scopes=_parse_csv_environment(environ, "LAWCASE_WEB_OIDC_SCOPES"),
            oidc_required_amr=_parse_csv_environment(environ, "LAWCASE_WEB_OIDC_REQUIRED_AMR"),
            oidc_accepted_acr=_parse_csv_environment(environ, "LAWCASE_WEB_OIDC_ACCEPTED_ACR", allow_empty=True),
            app_postgres_dsn=_required_environment_value(environ, "LAWCASE_WEB_APP_POSTGRES_DSN"),
            app_database_role=_required_environment_value(environ, "LAWCASE_WEB_APP_DATABASE_ROLE"),
            identity_directory_postgres_dsn=_required_environment_value(
                environ, "LAWCASE_WEB_IDENTITY_DIRECTORY_POSTGRES_DSN"
            ),
            identity_directory_database_role=_required_environment_value(
                environ, "LAWCASE_WEB_IDENTITY_DIRECTORY_DATABASE_ROLE"
            ),
            session_gateway_postgres_dsn=_required_environment_value(
                environ, "LAWCASE_WEB_SESSION_GATEWAY_POSTGRES_DSN"
            ),
            session_gateway_database_role=_required_environment_value(
                environ, "LAWCASE_WEB_SESSION_GATEWAY_DATABASE_ROLE"
            ),
            object_store_endpoint=_required_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_ENDPOINT"),
            object_store_region=_required_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_REGION"),
            object_store_bucket=_required_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_BUCKET"),
            object_store_access_key_id=_required_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_ACCESS_KEY_ID"),
            object_store_secret_access_key=_required_environment_value(
                environ, "LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY"
            ),
            object_store_encryption=_required_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_ENCRYPTION"),
            object_store_kms_key_id=_optional_environment_value(environ, "LAWCASE_WEB_OBJECT_STORE_KMS_KEY_ID"),
            clamav_executable=Path(_required_environment_value(environ, "LAWCASE_WEB_CLAMAV_EXECUTABLE")),
            clamav_timeout_seconds=_parse_positive_integer_environment(
                environ, "LAWCASE_WEB_CLAMAV_TIMEOUT_SECONDS", minimum=10, maximum=600
            ),
            pdftoppm_executable=Path(_required_environment_value(environ, "LAWCASE_WEB_PDFTOPPM_EXECUTABLE")),
            pdftoppm_timeout_seconds=_parse_positive_integer_environment(
                environ, "LAWCASE_WEB_PDFTOPPM_TIMEOUT_SECONDS", minimum=1, maximum=60
            ),
            document_worker_enabled=document_worker_enabled,
            document_renderer_settings=document_renderer_settings,
            private_roots=WebPrivateRoots(
                staging_root=Path(_required_environment_value(environ, "LAWCASE_WEB_UPLOAD_STAGING_ROOT")),
                worker_materialization_root=Path(
                    _required_environment_value(environ, "LAWCASE_WEB_WORKER_MATERIALIZATION_ROOT")
                ),
            ),
            system_worker_ids_by_firm=_parse_system_worker_environment(environ),
            system_verifier_ids_by_firm=_parse_system_verifier_environment(environ),
            local_managed_acceptance_auth_bypass=_parse_boolean_environment(
                environ,
                "LAWCASE_LOCAL_MANAGED_ACCEPTANCE_AUTH_BYPASS",
                default=False,
            ),
            local_managed_acceptance_firm_id=_optional_environment_value(
                environ, "LAWCASE_LOCAL_MANAGED_ACCEPTANCE_FIRM_ID"
            ),
            local_managed_acceptance_lead_actor_id=_optional_environment_value(
                environ, "LAWCASE_LOCAL_MANAGED_ACCEPTANCE_LEAD_ACTOR_ID"
            ),
        )

    def object_store_config(self) -> S3PrivateObjectStoreConfig:
        """Build the redacted S3 configuration only for server composition."""

        return S3PrivateObjectStoreConfig(
            endpoint_url=self.object_store_endpoint,
            region_name=self.object_store_region,
            bucket=self.object_store_bucket,
            access_key_id=self.object_store_access_key_id,
            secret_access_key=self.object_store_secret_access_key,
            server_side_encryption=self.object_store_encryption,
            kms_key_id=self.object_store_kms_key_id,
            allow_insecure_internal_endpoint=False,
        )


def _default_object_store_factory(config: S3PrivateObjectStoreConfig) -> S3CompatiblePrivateObjectStore:
    return S3CompatiblePrivateObjectStore(config)


def _default_common_material_object_store_factory(
    config: S3PrivateObjectStoreConfig,
) -> S3CommonMaterialPrivateObjectStore:
    return S3CommonMaterialPrivateObjectStore(config)


def _default_scanner_factory(executable: Path, timeout_seconds: int) -> ClamAvCommandScanner:
    return ClamAvCommandScanner(executable, timeout_seconds=timeout_seconds)


def _default_official_source_factory(
    config: S3PrivateObjectStoreConfig,
) -> OfficialSourceS3ProductionAdapters:
    return compose_official_source_s3_adapters(config)


def _default_document_renderer_factory(
    settings: IsolatedDocumentRendererClientSettings,
) -> IsolatedDocumentRendererClient:
    return IsolatedDocumentRendererClient(settings=settings)


def _default_document_renderer_preflight(renderer: object) -> None:
    preflight = getattr(renderer, "preflight", None)
    if not callable(preflight):
        raise WebRuntimeConfigurationBlocked("isolated document renderer client is invalid")
    preflight()


@dataclass(frozen=True)
class WebRuntimeAssemblyAdapters:
    """Small test seam around environment-bound infrastructure adapters.

    Production uses all defaults.  Tests may replace only the S3 client and
    scanner/preflight boundary, avoiding a live database, object store, or
    antivirus dependency while still exercising the real OIDC/session/upload
    composition.  This seam must not be exposed by an HTTP route.
    """

    object_store_factory: Callable[[S3PrivateObjectStoreConfig], object] = _default_object_store_factory
    common_material_object_store_factory: Callable[
        [S3PrivateObjectStoreConfig], object
    ] = _default_common_material_object_store_factory
    official_source_factory: Callable[
        [S3PrivateObjectStoreConfig], OfficialSourceS3ProductionAdapters
    ] = _default_official_source_factory
    scanner_factory: Callable[[Path, int], object] = _default_scanner_factory
    clamav_preflight: Callable[[Path, int], None] = field(
        default=lambda executable, timeout_seconds: _preflight_clamav(executable, timeout_seconds)
    )
    pdftoppm_preflight: Callable[[Path, int], None] = field(
        default=lambda executable, timeout_seconds: _preflight_pdftoppm(executable, timeout_seconds)
    )
    document_renderer_factory: Callable[[IsolatedDocumentRendererClientSettings], object] = (
        _default_document_renderer_factory
    )
    document_renderer_preflight: Callable[[object], None] = (
        _default_document_renderer_preflight
    )
    ledger_confirmation_preflight: Callable[[str], None] = field(
        default=lambda dsn: preflight_web_ledger_confirmation_session_authority(
            dsn=dsn
        )
    )
    ledger_exception_followup_preflight: Callable[[str, str], None] = field(
        default=lambda dsn, firm_id: (
            preflight_case_agent_ledger_exception_followup_schema(
                dsn=dsn, firm_id=firm_id
            )
        )
    )
    matter_provisioning_preflight: Callable[
        [str, Mapping[str, str], Mapping[str, str]], None
    ] = field(
        default=lambda dsn, workers, verifiers: (
            preflight_case_agent_matter_provisioning_contract(
                dsn=dsn,
                system_worker_ids_by_firm=workers,
                system_verifier_ids_by_firm=verifiers,
            )
        )
    )

    def __post_init__(self) -> None:
        for label, value in (
            ("object-store factory", self.object_store_factory),
            ("common-material object-store factory", self.common_material_object_store_factory),
            ("official-source factory", self.official_source_factory),
            ("scanner factory", self.scanner_factory),
            ("ClamAV preflight", self.clamav_preflight),
            ("pdftoppm preflight", self.pdftoppm_preflight),
            ("isolated document renderer factory", self.document_renderer_factory),
            ("isolated document renderer preflight", self.document_renderer_preflight),
            ("ledger confirmation database preflight", self.ledger_confirmation_preflight),
            (
                "ledger exception follow-up database preflight",
                self.ledger_exception_followup_preflight,
            ),
            ("matter provisioning database preflight", self.matter_provisioning_preflight),
        ):
            if not callable(value):
                raise ValueError(f"Web runtime {label} is invalid")


@dataclass(frozen=True)
class WebRuntimeComposition:
    """Server-only assembled dependencies for the narrow first Web vertical."""

    settings: WebRuntimeSettings = field(repr=False)
    api_dependencies: WebApiDependencies = field(repr=False)
    evidence_manifest_store: PostgresEvidenceManifestStore = field(repr=False)
    case_ledger_store: PostgresCaseLedgerStore = field(repr=False)
    legal_store: PostgresLegalSourceStore = field(repr=False)
    official_source_capture_store: object = field(repr=False)
    formal_calculation_store: PostgresFormalCalculationStore = field(repr=False)
    submission_store: PostgresSubmissionStore = field(repr=False)
    object_store: object = field(repr=False)
    official_source_adapters: OfficialSourceS3ProductionAdapters = field(repr=False)
    scanner: object = field(repr=False)
    page_preview_service: WebPdfPagePreviewService = field(repr=False)
    private_roots: WebPrivateRoots = field(repr=False)
    system_worker_for_firm: Callable[[str], Actor] = field(repr=False)
    material_upload_service: WebMaterialUploadService = field(repr=False)
    derivative_worker: WebEvidenceDerivativeWorker = field(repr=False)
    archive_upload_service: WebMaterialArchiveUploadService = field(repr=False)
    common_material_upload_service: WebCommonMaterialUploadService = field(repr=False)
    case_posture_service: WebCasePostureService = field(repr=False)


def load_web_runtime_settings(environ: Mapping[str, str] | None = None) -> WebRuntimeSettings:
    """Explicitly load Web production settings; importing this module is inert."""

    return WebRuntimeSettings.from_environment(os.environ if environ is None else environ)


def build_web_runtime_composition(
    settings: WebRuntimeSettings,
    *,
    adapters: WebRuntimeAssemblyAdapters | None = None,
) -> WebRuntimeComposition:
    """Assemble the real browser runtime without mounting routes yet.

    There is intentionally no memory store, test identity, synthetic case,
    implicit object store, or scanner bypass.  Configuration errors are
    normalized to non-sensitive startup failures before a caller hands the
    dependencies to FastAPI.
    """

    if not isinstance(settings, WebRuntimeSettings):
        raise WebRuntimeConfigurationBlocked("Web runtime settings are required")
    adapters = adapters or WebRuntimeAssemblyAdapters()
    if not isinstance(adapters, WebRuntimeAssemblyAdapters):
        raise WebRuntimeConfigurationBlocked("Web runtime assembly adapters are invalid")

    try:
        staging_root = _prepare_private_root(settings.private_roots.staging_root, label="Web upload staging root")
        worker_root = _prepare_private_root(
            settings.private_roots.worker_materialization_root,
            label="Web worker materialization root",
        )
        if _paths_overlap(staging_root, worker_root):
            raise WebRuntimeConfigurationBlocked("Web private roots must not overlap")

        # This runs as the same unprivileged API account which will receive
        # uploads.  Constructing a scanner object alone is insufficient: an
        # image with a missing binary, incompatible loader, or no execute bit
        # must be rejected before any Web route becomes available.
        # The isolated local acceptance run admits its frozen 88-page,
        # 11-file synthetic source set through server-only services and
        # deliberately exposes no browser upload routes. Do not spend the
        # 4 GB Docker VM loading ClamAV definitions solely for a route that is
        # absent from that fixture environment. Every production composition
        # still executes the scanner preflight before mounting any intake
        # route, and the acceptance services still require a scanner receipt.
        if not settings.local_managed_acceptance_auth_bypass:
            adapters.clamav_preflight(
                settings.clamav_executable, settings.clamav_timeout_seconds
            )
        scanner = adapters.scanner_factory(settings.clamav_executable, settings.clamav_timeout_seconds)
        if not callable(getattr(scanner, "scan", None)):
            raise WebRuntimeConfigurationBlocked("Web ClamAV scanner is invalid")
        # The PNG renderer is an equally real executable boundary.  Do not
        # start a Web server whose first evidence preview would discover that
        # the container omitted Poppler or lost execute permission.
        adapters.pdftoppm_preflight(settings.pdftoppm_executable, settings.pdftoppm_timeout_seconds)
        document_converter = None
        if settings.document_worker_enabled:
            assert settings.document_renderer_settings is not None
            document_converter = adapters.document_renderer_factory(
                settings.document_renderer_settings
            )
            if not callable(
                getattr(document_converter, "convert_generated_document", None)
            ):
                raise WebRuntimeConfigurationBlocked(
                    "isolated document renderer client is invalid"
                )
            adapters.document_renderer_preflight(document_converter)
        # The lawyer review capability is authoritative, not an optional UI
        # decoration.  Refuse to mount any managed Web routes unless migration
        # 0048's live-session definer owner, FORCE-RLS policy, narrow EXECUTE
        # grants and direct-DML revocations all exist on the application DSN.
        adapters.ledger_confirmation_preflight(settings.app_postgres_dsn)
        # 0048 proves the lawyer's live review-session boundary.  The 0049
        # lifecycle is a separate capability with Worker commands, immutable
        # history and replan wakeups, so it receives its own hard startup gate
        # for every configured tenant instead of borrowing the 0048 result.
        for firm_id in sorted(settings.system_worker_ids_by_firm):
            adapters.ledger_exception_followup_preflight(
                settings.app_postgres_dsn, firm_id
            )
        # A healthy page is not enough: new matters must be able to bind the
        # server-owned execution and verifier identities atomically.  This
        # takes the same row locks used by CREATE_MATTER and fails closed if a
        # later privilege hardening step omitted that narrow entitlement.
        adapters.matter_provisioning_preflight(
            settings.app_postgres_dsn,
            settings.system_worker_ids_by_firm,
            settings.system_verifier_ids_by_firm,
        )

        object_store_config = settings.object_store_config()
        object_store = adapters.object_store_factory(object_store_config)
        common_material_object_store = adapters.common_material_object_store_factory(
            object_store_config
        )
        official_sources = adapters.official_source_factory(object_store_config)
        if not isinstance(official_sources, OfficialSourceS3ProductionAdapters):
            raise WebRuntimeConfigurationBlocked(
                "official-source private-store adapter is invalid"
            )
        for method in (
            "put_verified_pdf",
            "put_verified_zip",
            "delete_unbound_upload_object",
            "materialize_verified_pdf",
            "put_verified_derivative",
            "materialize_verified_derivative",
            "read_case_agent_review_candidate",
            "read_reviewable_document_object",
        ):
            if not callable(getattr(object_store, method, None)):
                raise WebRuntimeConfigurationBlocked("private object-store adapter is invalid")
        for method in (
            "put_immutable_common_material",
            "recover_immutable_common_material",
            "materialize_common_material",
        ):
            if not callable(getattr(common_material_object_store, method, None)):
                raise WebRuntimeConfigurationBlocked(
                    "common-material private object-store adapter is invalid"
                )
        if settings.document_worker_enabled:
            for method in ("put_verified_office_artifact", "put_verified_review_pdf", "read_verified_review_artifact"):
                if not callable(getattr(object_store, method, None)):
                    raise WebRuntimeConfigurationBlocked("document worker object-store adapter is invalid")

        identity_directory = PostgresOidcIdentityDirectory(
            settings.identity_directory_postgres_dsn,
            database_role=settings.identity_directory_database_role,
        )
        jwks_provider = CachedHttpsJwksProvider(
            policy=JwksFetchPolicy(
                issuer=settings.oidc_issuer,
                jwks_url=settings.oidc_jwks_url,
            )
        )
        identity_resolver = WebOidcIdentityResolver(
            policy=OidcVerificationPolicy(
                issuer=settings.oidc_issuer,
                audience=settings.oidc_audience,
                mfa=MfaClaimRequirement(
                    required_amr=settings.oidc_required_amr,
                    accepted_acr=settings.oidc_accepted_acr,
                ),
            ),
            jwks_provider=jwks_provider,
            actor_mapping=identity_directory,
            # The Web session authority, not a JWT, authenticates API calls.
            # Keep this resolver's transport policy token-free by default.
            transport=TokenTransportPolicy(),
        )
        session_authority = WebSessionAuthority(
            store=PostgresWebSessionStore(
                settings.session_gateway_postgres_dsn,
                database_role=settings.session_gateway_database_role,
            ),
            actor_directory=PostgresWebSessionActorDirectory(
                settings.identity_directory_postgres_dsn,
                database_role=settings.identity_directory_database_role,
            ),
            policy=WebSessionPolicy(public_origin=settings.public_origin),
        )
        local_managed_acceptance_session_bootstrap = (
            LocalManagedAcceptanceSessionBootstrap(
                public_origin=settings.public_origin,
                issuer=settings.oidc_issuer,
                actor=Actor(
                    actor_id=settings.local_managed_acceptance_lead_actor_id,
                    firm_id=settings.local_managed_acceptance_firm_id,
                    roles=frozenset({Role.LEAD_LAWYER}),
                ),
                session_authority=session_authority,
            )
            if settings.local_managed_acceptance_auth_bypass
            else None
        )
        oidc_login = OidcAuthorizationCodeLogin(
            policy=OidcAuthorizationCodePolicy(
                public_origin=settings.public_origin,
                issuer=settings.oidc_issuer,
                authorization_endpoint=settings.oidc_authorization_endpoint,
                token_endpoint=settings.oidc_token_endpoint,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                scopes=settings.oidc_scopes,
            ),
            state_store=EphemeralOidcAuthorizationStateStore(),
            token_client=UrlLibOidcTokenEndpointClient(),
            identity_resolver=identity_resolver,
            session_issuer=session_authority,
        )
        matter_store = PostgresMatterStore(
            settings.app_postgres_dsn,
            system_worker_ids_by_firm=settings.system_worker_ids_by_firm,
            system_verifier_ids_by_firm=settings.system_verifier_ids_by_firm,
        )
        case_ledger_store = PostgresCaseLedgerStore(settings.app_postgres_dsn)
        legal_store = official_sources.legal_source_store(
            dsn=settings.app_postgres_dsn
        )
        official_source_capture_store = official_sources.capture_store(
            dsn=settings.app_postgres_dsn
        )
        formal_calculation_store = PostgresFormalCalculationStore(settings.app_postgres_dsn)
        submission_store = PostgresSubmissionStore(settings.app_postgres_dsn)
        case_agent_store = PostgresCaseAgentStore(settings.app_postgres_dsn)
        evidence_manifest_store = PostgresEvidenceManifestStore(settings.app_postgres_dsn)
        case_posture_service = WebCasePostureService(
            store=PostgresCasePostureStore(settings.app_postgres_dsn)
        )
        system_worker_for_firm = _system_worker_resolver(settings.system_worker_ids_by_firm)
        ledger_confirmation_root = _prepare_private_root(
            worker_root / "ledger-confirmation",
            label="Agent ledger confirmation worker root",
        )
        ledger_evidence_projection_source = WebAgentEvidenceProjectionSource(
            evidence_store=evidence_manifest_store,
            object_store=object_store,  # type: ignore[arg-type]
            system_worker_for_firm=system_worker_for_firm,
            policy=WebAgentEvidenceProjectionPolicy(
                worker_root=ledger_confirmation_root
            ),
        )

        def ledger_confirmation_store_for_actor(actor: Actor):
            """Build the exact task-bound reader for the actor's verified firm.

            ``actor.firm_id`` comes from the OIDC/MFA server session.  It is
            never accepted from a route parameter or browser body.  A firm
            without an administrator-owned worker mapping therefore cannot
            acquire a source reader or a confirmation store.
            """

            worker_actor = system_worker_for_firm(actor.firm_id)
            projection = WebEvidencePageTaskProjectionPort(
                authorization_port=PostgresEvidenceProjectionAuthorizationPort(
                    dsn=settings.app_postgres_dsn,
                    worker_actor=worker_actor,
                    required_tool=DEEPSEEK_LEDGER_EXTRACTION_MANIFEST.tool_id,
                ),
                projection_source=ledger_evidence_projection_source,
            )
            ledger_projection = PostgresLedgerExtractionProjectionPort(
                dsn=settings.app_postgres_dsn,
                worker_actor=worker_actor,
                native_projection_port=projection,
                object_store=object_store,
            )
            return PostgresCaseLedgerExtractionPromotionStore(
                settings.app_postgres_dsn,
                evidence_page_text_reader=TaskBoundEvidencePageTextReader(
                    projection_port=ledger_projection
                ),
            )

        agent_ledger_extraction_review_service = (
            WebAgentLedgerExtractionReviewService(
                store=PostgresAgentLedgerExtractionReviewStore(
                    settings.app_postgres_dsn,
                    confirmation_store_factory=ledger_confirmation_store_for_actor,
                    configured_firm_ids=frozenset(
                        settings.system_worker_ids_by_firm
                    ),
                )
            )
        )
        case_agent_document_review_service = (
            PostgresWebCaseAgentDocumentReviewService(
                dsn=settings.app_postgres_dsn,
                package_access=_FirmScopedDocumentPackageAccess(
                    dsn=settings.app_postgres_dsn,
                    execution_actor_ids=settings.system_worker_ids_by_firm,
                    verifier_actor_ids=settings.system_verifier_ids_by_firm,
                    object_store=object_store,
                ),
                revision_store=PostgresDocumentRevisionCommandStore(
                    dsn=settings.app_postgres_dsn,
                    templates=first_release_reviewable_document_templates(),
                ),
                content_binding_resolver=_FirmScopedContentBinding(
                    dsn=settings.app_postgres_dsn,
                    execution_actor_ids=settings.system_worker_ids_by_firm,
                    verifier_actor_ids=settings.system_verifier_ids_by_firm,
                    object_store=object_store, official_source_text=official_sources.verified_text,
                ),
            )
        )
        case_agent_artifact_review_service = (
            PostgresWebCaseAgentArtifactReviewService(
                dsn=settings.app_postgres_dsn,
                object_store=object_store,  # type: ignore[arg-type]
            )
        )
        case_agent_control_service = WebCaseAgentControlService(
            store=case_agent_store,
            snapshot_reader=case_ledger_store,
            posture_reader=case_posture_service,
            final_review_readiness=WebCaseAgentFinalReviewReadiness(
                artifact_review=case_agent_artifact_review_service,
                document_review=case_agent_document_review_service,
            ),
            sealed_recovery_artifact_reader=case_agent_artifact_review_service,
        )
        # This service is assembled only after the per-firm 0049 preflight
        # above succeeds.  Merely having the migration module on disk never
        # turns on the browser capability.
        agent_ledger_exception_followup_service = (
            WebAgentLedgerExceptionFollowupService(
                store=PostgresCaseLedgerExceptionFollowupStore(
                    settings.app_postgres_dsn
                ),
                matter_version_reader=case_ledger_store,
                recovery_run_service=case_agent_control_service,
                configured_firm_ids=frozenset(
                    settings.system_worker_ids_by_firm
                ),
            )
        )
        upload_service = WebMaterialUploadService(
            slot_store=PostgresWebMaterialUploadSlotStore(settings.app_postgres_dsn),
            staging=WebUploadStagingArea(staging_root),
            scanner=scanner,
            object_store=object_store,  # type: ignore[arg-type]
            evidence_store=evidence_manifest_store,
            system_worker_for_firm=system_worker_for_firm,
        )
        common_material_upload_service = WebCommonMaterialUploadService(
            store=PostgresCommonMaterialUploadStore(settings.app_postgres_dsn),
            staging=CommonMaterialStagingArea(staging_root / "common-materials"),
            scanner=scanner,
            object_store=common_material_object_store,  # type: ignore[arg-type]
        )
        archive_upload_service = WebMaterialArchiveUploadService(
            store=PostgresWebMaterialArchiveStore(settings.app_postgres_dsn),
            staging=WebZipStagingArea(staging_root / "archives"),
            object_store=object_store,  # type: ignore[arg-type]
        )
        page_preview_service = WebPdfPagePreviewService(
            evidence_store=evidence_manifest_store,
            object_store=object_store,  # type: ignore[arg-type]
            system_worker_for_firm=system_worker_for_firm,
            policy=WebPdfPagePreviewPolicy(
                worker_root=worker_root,
                pdftoppm_executable=settings.pdftoppm_executable,
                render_timeout_seconds=settings.pdftoppm_timeout_seconds,
            ),
        )
        evidence_review_service = WebEvidenceReviewService(evidence_store=evidence_manifest_store)
        derivative_worker = WebEvidenceDerivativeWorker(
            evidence_store=evidence_manifest_store,
            object_store=object_store,  # type: ignore[arg-type]
            worker_root=worker_root,
            system_worker_for_firm=system_worker_for_firm,
        )
        derivative_delivery_service = WebDerivativeDeliveryService(
            evidence_store=evidence_manifest_store,
            object_store=object_store,  # type: ignore[arg-type]
            worker_root=worker_root,
        )
        document_draft_service = None
        document_draft_delivery_service = None
        if settings.document_worker_enabled:
            for method in ("put_verified_office_artifact", "put_verified_review_pdf", "read_verified_review_artifact"):
                if not callable(getattr(object_store, method, None)):
                    raise WebRuntimeConfigurationBlocked("document worker object-store adapter is invalid")
            assert document_converter is not None
            reviewable_store = PostgresReviewableDraftStore(
                settings.app_postgres_dsn,
                artifact_reader=lambda key, expected_hash: object_store.read_verified_review_artifact(key, expected_hash),
            )
            document_draft_service = WebDocumentDraftService(
                case_ledger_store=case_ledger_store,
                reviewable_store=reviewable_store,
                object_store=object_store,
                system_worker_for_firm=system_worker_for_firm,
                converter=document_converter,
            )
            document_draft_delivery_service = WebDocumentDraftDeliveryService(
                reviewable_store=reviewable_store,
                object_store=object_store,
            )
        # One exact heartbeat/principal/store probe owns both capability
        # decisions.  General Agent execution accepts every supported catalog;
        # ledger extraction additionally requires the ledger adapter catalog.
        case_agent_runtime_readiness = ExactCaseAgentRuntimeReadiness(
            store=case_agent_store,
            matter_principal_probe=PostgresCaseAgentMatterPrincipalReadiness(
                settings.app_postgres_dsn
            ),
            worker_actor_ids_by_firm=settings.system_worker_ids_by_firm,
            verifier_actor_ids_by_firm=settings.system_verifier_ids_by_firm,
        )
        fact_correction_store=PostgresFactCorrectionProposalStore(
            settings.app_postgres_dsn,
            original_reader=WebFactCorrectionOriginalReader(
                artifact_review_service=case_agent_artifact_review_service,
                evidence_projection=ledger_evidence_projection_source,matter_store=matter_store),
        )
        # Earlier artifact services retain a stateless snapshot reader. The
        # exposed ledger command adapter additionally owns decision verification.
        case_ledger_store=PostgresCaseLedgerStore(settings.app_postgres_dsn,
            fact_correction_verifier=fact_correction_store)
        api_dependencies = WebApiDependencies(
            settings=WebApiSettings(public_origin=settings.public_origin),
            oidc_login=oidc_login,
            session_authority=session_authority,
            matter_store=matter_store,
            fact_correction_store=fact_correction_store,
            case_ledger_store=case_ledger_store,
            legal_store=legal_store,
            official_source_capture_store=official_source_capture_store,
            formal_calculation_store=formal_calculation_store,
            submission_store=submission_store,
            upload_service=(
                None
                if settings.local_managed_acceptance_auth_bypass
                else upload_service
            ),
            common_material_upload_service=(
                None
                if settings.local_managed_acceptance_auth_bypass
                else common_material_upload_service
            ),
            case_posture_service=case_posture_service,
            archive_upload_service=(
                None
                if settings.local_managed_acceptance_auth_bypass
                else archive_upload_service
            ),
            page_preview_service=page_preview_service,
            evidence_review_service=evidence_review_service,
            derivative_worker=derivative_worker,
            derivative_delivery_service=derivative_delivery_service,
            document_draft_service=document_draft_service,
            document_draft_delivery_service=document_draft_delivery_service,
            dynamic_case_plan_service=WebDynamicCasePlanService(
                store=PostgresCaseWorkPlanStore(settings.app_postgres_dsn)
            ),
            agent_ledger_extraction_review_service=(
                agent_ledger_extraction_review_service
            ),
            agent_ledger_exception_followup_service=(
                agent_ledger_exception_followup_service
            ),
            # Creating and inspecting a durable lawyer goal is available in
            # production composition.  Planning/execution remain separate
            # workers; the UI therefore shows a truthful CREATED state until
            # those workers advance the event log.
            case_agent_control_service=case_agent_control_service,
            case_agent_artifact_review_service=(
                case_agent_artifact_review_service
            ),
            case_agent_document_review_service=(
                case_agent_document_review_service
            ),
            case_agent_runtime_ready=case_agent_runtime_readiness,
            case_agent_ledger_runtime_ready=(
                case_agent_runtime_readiness.ledger_ready
            ),
            case_agent_document_runtime_ready=(
                case_agent_runtime_readiness.document_ready
            ),
            local_managed_acceptance_session_bootstrap=(
                local_managed_acceptance_session_bootstrap
            ),
        )
        # Keep this final validate before callers can mount the routes.  It
        # catches cross-origin/login/session dependency swaps even if a future
        # refactor changes one construction above.
        api_dependencies.validate()
    except WebRuntimeConfigurationBlocked:
        raise
    except Exception as error:
        # Keep the public error stable while retaining a private exception
        # chain for container diagnostics.  Without the cause, an operator
        # cannot distinguish a missing release module from a bad adapter or a
        # stale configuration and is pushed toward blind restarts.
        raise WebRuntimeConfigurationBlocked(
            "Web runtime components could not be assembled safely"
        ) from error

    return WebRuntimeComposition(
        settings=settings,
        api_dependencies=api_dependencies,
        evidence_manifest_store=evidence_manifest_store,
        case_ledger_store=case_ledger_store,
        legal_store=legal_store,
        official_source_capture_store=official_source_capture_store,
        formal_calculation_store=formal_calculation_store,
        submission_store=submission_store,
        object_store=object_store,
        official_source_adapters=official_sources,
        scanner=scanner,
        page_preview_service=page_preview_service,
        private_roots=WebPrivateRoots(staging_root=staging_root, worker_materialization_root=worker_root),
        system_worker_for_firm=system_worker_for_firm,
        material_upload_service=upload_service,
        derivative_worker=derivative_worker,
        archive_upload_service=archive_upload_service,
        common_material_upload_service=common_material_upload_service,
        case_posture_service=case_posture_service,
    )


def create_web_runtime_app(
    environ: Mapping[str, str] | None = None,
    *,
    adapters: WebRuntimeAssemblyAdapters | None = None,
) -> FastAPI:
    """Explicit ASGI factory for ``uvicorn --factory``.

    There is deliberately no module-level ``app`` initialized from the
    process environment.  A missing or unsafe configuration therefore fails
    the worker process instead of leaving a health-only service that could be
    mistaken for a live case API.
    """

    composition = build_web_runtime_composition(load_web_runtime_settings(environ), adapters=adapters)
    return create_web_app(composition.api_dependencies)


def _required_environment_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str):
        raise WebRuntimeConfigurationBlocked(f"required Web runtime setting {name} is missing")
    normalized = value.strip()
    if value != normalized or not normalized or "\x00" in normalized or _looks_like_placeholder(normalized):
        raise WebRuntimeConfigurationBlocked(f"required Web runtime setting {name} is invalid")
    return normalized


def _optional_environment_value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WebRuntimeConfigurationBlocked(f"optional Web runtime setting {name} is invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if value != normalized or "\x00" in normalized or _looks_like_placeholder(normalized):
        raise WebRuntimeConfigurationBlocked(f"optional Web runtime setting {name} is invalid")
    return normalized


def _parse_csv_environment(
    environ: Mapping[str, str],
    name: str,
    *,
    allow_empty: bool = False,
) -> frozenset[str]:
    value = environ.get(name)
    if value is None:
        if allow_empty:
            return frozenset()
        raise WebRuntimeConfigurationBlocked(f"required Web runtime setting {name} is missing")
    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")
    if not value:
        if allow_empty:
            return frozenset()
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")
    pieces = tuple(part.strip().lower() for part in value.split(","))
    if not pieces or any(not piece or not _CLAIM_VALUE.fullmatch(piece) for piece in pieces):
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")
    result = frozenset(pieces)
    if len(result) != len(pieces):
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} contains duplicate values")
    return result


def _parse_boolean_environment(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")


def _parse_positive_integer_environment(
    environ: Mapping[str, str],
    name: str,
    *,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int:
    raw = _optional_environment_value(environ, name)
    if raw is None:
        if default is not None:
            return default
        raise WebRuntimeConfigurationBlocked(f"required Web runtime setting {name} is missing")
    if not raw.isascii() or not raw.isdecimal():
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise WebRuntimeConfigurationBlocked(f"Web runtime setting {name} is invalid")
    return value


def _parse_web_document_renderer_settings(
    environ: Mapping[str, str], *, enabled: bool
) -> IsolatedDocumentRendererClientSettings | None:
    """Read the fixed Web-to-renderer contract without accepting local Office.

    The browser API can request only the existing authenticated sidecar.  A
    legacy local `soffice` setting is rejected even when the sidecar is also
    configured, so an accidental image change cannot silently move Office
    parsing back into the browser-facing process.
    """

    legacy_names = (
        "LAWCASE_WEB_SOFFICE_EXECUTABLE",
        "LAWCASE_WEB_SOFFICE_TIMEOUT_SECONDS",
    )
    if any(_optional_environment_value(environ, name) is not None for name in legacy_names):
        raise WebRuntimeConfigurationBlocked(
            "Web document drafts must use the isolated document renderer"
        )
    if not enabled:
        # Deployments may keep a fixed internal endpoint and secret mounted
        # while this capability is disabled. They are inert until the explicit
        # capability flag is true, and rejecting that otherwise-safe standby
        # configuration would prevent a normal setup-gated Web deployment.
        return None
    try:
        return IsolatedDocumentRendererClientSettings(
            endpoint=_required_environment_value(
                environ, "LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT"
            ),
            shared_secret=decode_shared_secret_base64url(
                _required_environment_value(
                    environ, "LAWCASE_WEB_DOCUMENT_RENDERER_SHARED_SECRET"
                )
            ),
            timeout_seconds=_parse_positive_integer_environment(
                environ,
                "LAWCASE_WEB_DOCUMENT_RENDERER_TIMEOUT_SECONDS",
                minimum=10,
                maximum=180,
            ),
        )
    except IsolatedDocumentRendererBlocked:
        raise WebRuntimeConfigurationBlocked(
            "isolated document renderer configuration is invalid"
        ) from None


def _parse_system_worker_environment(environ: Mapping[str, str]) -> Mapping[str, str]:
    raw = _required_environment_value(environ, "LAWCASE_WEB_SYSTEM_WORKERS_JSON")
    if len(raw) > 16_384:
        raise WebRuntimeConfigurationBlocked("Web system-worker mapping is invalid")
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_json_members)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise WebRuntimeConfigurationBlocked("Web system-worker mapping is invalid") from None
    if not isinstance(decoded, dict):
        raise WebRuntimeConfigurationBlocked("Web system-worker mapping is invalid")
    return decoded


def _parse_system_verifier_environment(environ: Mapping[str, str]) -> Mapping[str, str]:
    raw = _required_environment_value(environ, "LAWCASE_WEB_SYSTEM_VERIFIERS_JSON")
    if len(raw) > 16_384:
        raise WebRuntimeConfigurationBlocked("Web system-verifier mapping is invalid")
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_json_members)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise WebRuntimeConfigurationBlocked("Web system-verifier mapping is invalid") from None
    if not isinstance(decoded, dict):
        raise WebRuntimeConfigurationBlocked("Web system-verifier mapping is invalid")
    return decoded


def _reject_parallel_process_environment(environ: Mapping[str, str]) -> None:
    """Reject common multi-worker launch controls for in-process PKCE state."""

    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        value = environ.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or value.strip() != "1":
            raise WebRuntimeConfigurationBlocked("Web OIDC state requires exactly one API process")


def _validate_production_public_origin(value: object) -> None:
    try:
        normalized = WebApiSettings(public_origin=str(value)).public_origin
    except Exception:
        raise WebRuntimeConfigurationBlocked("Web public origin must be canonical HTTPS") from None
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _RESERVED_PUBLIC_HOSTS or any(hostname.endswith(suffix) for suffix in _RESERVED_PUBLIC_HOSTS[1:]):
        raise WebRuntimeConfigurationBlocked("Web public origin cannot use a local or reserved hostname")


def _validate_configured_text(value: object, *, label: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _looks_like_placeholder(value)
    ):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")


def _validate_secret(value: object, *, label: str, minimum: int, maximum: int) -> None:
    _validate_configured_text(value, label=label, maximum=maximum)
    if not isinstance(value, str) or len(value) < minimum:
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")


def _validate_claim_collection(
    value: object,
    *,
    label: str,
    required_value: str | None = None,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, frozenset) or (not value and not allow_empty):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")
    if not all(isinstance(item, str) and _CLAIM_VALUE.fullmatch(item) for item in value):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")
    if any(item != item.lower() for item in value):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")
    if required_value is not None and required_value not in value:
        raise WebRuntimeConfigurationBlocked(f"{label} must include {required_value}")


def _validate_database_role(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not _DATABASE_ROLE.fullmatch(value):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")


def _validate_postgres_dsn(value: object, *, label: str, expected_role: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_DSN_LENGTH
        or "\x00" in value
        or _looks_like_placeholder(value)
    ):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")
    try:
        details = conninfo_to_dict(value)
    except Exception:
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid") from None
    user = details.get("user")
    database = details.get("dbname")
    host = details.get("host")
    sslmode = str(details.get("sslmode", "")).lower()
    if (
        not isinstance(user, str)
        or user != expected_role
        or not isinstance(database, str)
        or not database
        or _looks_like_placeholder(database)
        or not isinstance(host, str)
        or not host
        or sslmode != "verify-full"
    ):
        raise WebRuntimeConfigurationBlocked(f"{label} is invalid")


def _declared_absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, Path):
        value = Path(str(value))
    if not value.is_absolute() or value == Path(value.anchor) or ".." in value.parts:
        raise WebRuntimeConfigurationBlocked(f"{label} must be a non-root absolute path")
    # macOS commonly exposes a system-owned ``/var -> /private/var`` alias.
    # Canonicalize such existing *ancestor* aliases once at startup, while
    # continuing to reject a configured leaf which is itself a symlink.  The
    # resulting root is then checked/owned directly by the API account.
    try:
        if value.exists() and value.is_symlink():
            raise WebRuntimeConfigurationBlocked(f"{label} cannot be a symbolic link")
        normalized = value.resolve(strict=False)
    except OSError:
        raise WebRuntimeConfigurationBlocked(f"{label} is unavailable") from None
    if normalized == Path(normalized.anchor):
        raise WebRuntimeConfigurationBlocked(f"{label} must be a non-root absolute path")
    return normalized


def _normalize_private_roots(value: object) -> WebPrivateRoots:
    if not isinstance(value, WebPrivateRoots):
        raise WebRuntimeConfigurationBlocked("Web private roots are invalid")
    staging = _declared_absolute_path(value.staging_root, label="Web upload staging root")
    worker = _declared_absolute_path(value.worker_materialization_root, label="Web worker materialization root")
    if _paths_overlap_declared(staging, worker):
        raise WebRuntimeConfigurationBlocked("Web private roots must be distinct and non-overlapping")
    source_root = Path(__file__).resolve().parents[2]
    if _is_within(staging, source_root) or _is_within(worker, source_root):
        raise WebRuntimeConfigurationBlocked("Web private roots cannot be inside application source")
    return WebPrivateRoots(staging_root=staging, worker_materialization_root=worker)


def _normalize_system_workers(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise WebRuntimeConfigurationBlocked("Web system-worker mapping is invalid")
    result: dict[str, str] = {}
    worker_ids: set[str] = set()
    for firm_id, worker_id in value.items():
        try:
            normalized_firm = str(UUID(str(firm_id)))
            normalized_worker = str(UUID(str(worker_id)))
        except (TypeError, ValueError, AttributeError):
            raise WebRuntimeConfigurationBlocked("Web system-worker mapping is invalid") from None
        if normalized_firm in result or normalized_worker in worker_ids:
            raise WebRuntimeConfigurationBlocked("Web system-worker mapping must use one dedicated worker per firm")
        result[normalized_firm] = normalized_worker
        worker_ids.add(normalized_worker)
    return MappingProxyType(result)


def _system_worker_resolver(worker_ids_by_firm: Mapping[str, str]) -> Callable[[str], Actor]:
    workers = _normalize_system_workers(worker_ids_by_firm)

    def resolve(firm_id: str) -> Actor:
        try:
            normalized_firm = str(UUID(firm_id))
        except (TypeError, ValueError):
            raise PermissionError("Web system worker firm is invalid") from None
        worker_id = workers.get(normalized_firm)
        if worker_id is None:
            # Never fall back to a global worker identity.  A firm not listed
            # here cannot acquire a private object locator on a later recovery
            # path, which is safer than implicit cross-tenant worker reuse.
            raise PermissionError("Web system worker is not provisioned for this firm")
        return Actor(
            actor_id=worker_id,
            firm_id=normalized_firm,
            roles=frozenset({Role.SYSTEM_WORKER}),
        )

    return resolve


def _prepare_private_root(path: Path, *, label: str) -> Path:
    declared = _declared_absolute_path(path, label=label)
    try:
        declared.mkdir(parents=True, mode=0o700, exist_ok=True)
        if declared.is_symlink():
            raise OSError("symbolic link")
        resolved = declared.resolve(strict=True)
        if not resolved.is_dir() or resolved.is_symlink():
            raise OSError("not a directory")
        if resolved.stat().st_uid != os.geteuid():
            raise OSError("not owned by runtime account")
        resolved.chmod(0o700)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode != 0o700:
            raise OSError("unsafe mode")
    except OSError:
        raise WebRuntimeConfigurationBlocked(f"{label} is unavailable or not private") from None
    return resolved


def _preflight_clamav(executable: Path, timeout_seconds: int) -> None:
    """Require the configured scanner to execute in the API image now.

    stdout/stderr are discarded, so version output, package metadata and a
    loader error cannot leak through an application log or browser response.
    This is intentionally stricter than constructing ``ClamAvCommandScanner``
    alone, which checks the path but defers executable invocation until the
    first upload.
    """

    _preflight_absolute_executable(
        executable,
        label="ClamAV",
        version_argument="--version",
        timeout_seconds=timeout_seconds,
    )


def _preflight_pdftoppm(executable: Path, timeout_seconds: int) -> None:
    """Require Poppler's ``pdftoppm`` renderer in the same API image."""

    _preflight_absolute_executable(
        executable,
        label="pdftoppm",
        version_argument="-v",
        timeout_seconds=timeout_seconds,
    )


def _preflight_absolute_executable(
    executable: Path,
    *,
    label: str,
    version_argument: str,
    timeout_seconds: int,
) -> None:
    declared = _declared_absolute_path(executable, label=f"{label} executable")
    try:
        if declared.is_symlink():
            raise OSError("symbolic link")
        resolved = declared.resolve(strict=True)
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
            raise OSError("not executable")
        result = subprocess.run(
            [str(resolved), version_argument],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=min(timeout_seconds, 15),
        )
        if result.returncode != 0:
            raise OSError("version check failed")
    except (OSError, subprocess.TimeoutExpired):
        raise WebRuntimeConfigurationBlocked(f"{label} is unavailable in the Web API runtime") from None


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _paths_overlap_declared(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _looks_like_placeholder(value: str) -> bool:
    lower = value.strip().lower()
    return lower in _PLACEHOLDER_WORDS or lower.startswith(_PLACEHOLDER_PREFIXES)


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result
