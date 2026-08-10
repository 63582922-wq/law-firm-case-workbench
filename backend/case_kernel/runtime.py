"""Fail-closed runtime selection for the local workbench.

Importing the API never reads a database URL or opens a connection. The desktop
launcher must explicitly load these settings and inject the returned services.
This keeps the synthetic Alpha from silently becoming a persistent case API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from typing import Mapping

from psycopg.conninfo import conninfo_to_dict

from .case_ledger_postgres import PostgresCaseLedgerStore
from .evidence_intake_postgres import PostgresEvidenceIntakeStore
from .formal_calculation_postgres import PostgresFormalCalculationStore
from .legal_source_postgres import PostgresLegalSourceStore
from .managed_artifact_store import LocalEncryptedArtifactStore
from .office_pdf_conversion_worker import OfficePdfConversionBlocked, SandboxedOfficePdfConverter
from .official_source_capture_postgres import PostgresOfficialSourceCaptureStore
from .postgres_store import PostgresMatterStore
from .reviewable_draft_postgres import PostgresReviewableDraftStore
from .agent_execution_postgres import PostgresAgentExecutionStore
from .external_request_postgres import PostgresExternalRequestStore
from .submission_postgres import PostgresSubmissionStore
from .store import InMemoryMatterStore, MatterStore


class RuntimeConfigurationBlocked(ValueError):
    """The requested runtime could expose a persistence boundary unsafely."""


class RuntimeMode(str, Enum):
    SYNTHETIC_ALPHA = "synthetic-alpha"
    POSTGRES_INTERNAL_PREVIEW = "postgres-internal-preview"


@dataclass(frozen=True)
class RuntimeSettings:
    mode: RuntimeMode
    _postgres_dsn: str | None = field(default=None, repr=False, compare=False)
    _office_soffice_executable: str | None = field(default=None, repr=False, compare=False)
    _office_pdf_renderer_executable: str | None = field(default=None, repr=False, compare=False)

    @property
    def postgres_dsn(self) -> str | None:
        return self._postgres_dsn

    @property
    def office_conversion_enabled(self) -> bool:
        return self._office_soffice_executable is not None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "RuntimeSettings":
        raw_mode = environ.get("CASE_WORKBENCH_RUNTIME_MODE", RuntimeMode.SYNTHETIC_ALPHA.value).strip()
        try:
            mode = RuntimeMode(raw_mode)
        except ValueError as error:
            raise RuntimeConfigurationBlocked("unsupported CASE_WORKBENCH_RUNTIME_MODE") from error
        dsn = environ.get("CASE_WORKBENCH_POSTGRES_DSN", "").strip()
        acknowledgement = environ.get("CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW", "")
        office_acknowledgement = environ.get("CASE_WORKBENCH_ENABLE_OFFICE_CONVERSION", "").strip()
        office_soffice = environ.get("CASE_WORKBENCH_OFFICE_SOFFICE", "").strip()
        office_renderer = environ.get("CASE_WORKBENCH_OFFICE_PDF_RENDERER", "").strip()
        office_configured = bool(office_acknowledgement or office_soffice or office_renderer)

        if mode is RuntimeMode.SYNTHETIC_ALPHA:
            if dsn or acknowledgement or office_configured:
                raise RuntimeConfigurationBlocked(
                    "persistent or Office-conversion settings cannot be present while runtime mode is synthetic-alpha"
                )
            return cls(mode=mode)

        if acknowledgement != "YES":
            raise RuntimeConfigurationBlocked(
                "postgres-internal-preview requires CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW=YES"
            )
        if not dsn:
            raise RuntimeConfigurationBlocked("postgres-internal-preview requires CASE_WORKBENCH_POSTGRES_DSN")
        try:
            database_name = conninfo_to_dict(dsn).get("dbname", "")
        except Exception as error:
            raise RuntimeConfigurationBlocked("CASE_WORKBENCH_POSTGRES_DSN is not a valid PostgreSQL connection string") from error
        if not (database_name.endswith("_preview") or database_name.endswith("_test")):
            raise RuntimeConfigurationBlocked(
                "internal preview only accepts a dedicated database name ending in _preview or _test"
            )
        if office_configured:
            if office_acknowledgement != "YES":
                raise RuntimeConfigurationBlocked(
                    "Office conversion requires CASE_WORKBENCH_ENABLE_OFFICE_CONVERSION=YES"
                )
            if not office_soffice or not office_renderer:
                raise RuntimeConfigurationBlocked(
                    "Office conversion requires CASE_WORKBENCH_OFFICE_SOFFICE and CASE_WORKBENCH_OFFICE_PDF_RENDERER"
                )
        return cls(
            mode=mode,
            _postgres_dsn=dsn,
            _office_soffice_executable=office_soffice or None,
            _office_pdf_renderer_executable=office_renderer or None,
        )


@dataclass(frozen=True)
class RuntimeServices:
    settings: RuntimeSettings
    matter_store: MatterStore
    case_ledger_store: PostgresCaseLedgerStore | None
    evidence_manifest_store: PostgresEvidenceIntakeStore | None
    formal_calculation_store: PostgresFormalCalculationStore | None
    legal_source_store: PostgresLegalSourceStore | None
    official_source_capture_store: PostgresOfficialSourceCaptureStore | None
    submission_store: PostgresSubmissionStore | None
    reviewable_draft_store: PostgresReviewableDraftStore | None
    agent_execution_store: PostgresAgentExecutionStore | None
    external_request_store: PostgresExternalRequestStore | None
    artifact_store: LocalEncryptedArtifactStore | None
    office_pdf_converter: SandboxedOfficePdfConverter | None
    persistence_label: str


def load_runtime_settings() -> RuntimeSettings:
    """The launcher calls this explicitly; API module import never does."""
    return RuntimeSettings.from_environment(os.environ)


def build_runtime_services(
    settings: RuntimeSettings,
    *,
    artifact_store: LocalEncryptedArtifactStore | None = None,
) -> RuntimeServices:
    if settings.mode is RuntimeMode.SYNTHETIC_ALPHA:
        if artifact_store is not None:
            raise RuntimeConfigurationBlocked(
                "synthetic-alpha cannot receive the persistent encrypted artifact store"
            )
        return RuntimeServices(
            settings=settings,
            matter_store=InMemoryMatterStore(),
            case_ledger_store=None,
            evidence_manifest_store=None,
            formal_calculation_store=None,
            legal_source_store=None,
            official_source_capture_store=None,
            submission_store=None,
            reviewable_draft_store=None,
            agent_execution_store=None,
            external_request_store=None,
            artifact_store=None,
            office_pdf_converter=None,
            persistence_label="in-memory-synthetic-only",
        )
    dsn = settings.postgres_dsn
    if dsn is None:
        raise RuntimeConfigurationBlocked("persistent runtime settings lost their PostgreSQL DSN")
    office_converter = _build_office_converter(settings, artifact_store=artifact_store)
    artifact_reader = (
        (lambda object_key, expected_hash: artifact_store.read_bytes(
            object_key, expected_sha256=expected_hash
        ))
        if artifact_store is not None
        else None
    )
    return RuntimeServices(
        settings=settings,
        matter_store=PostgresMatterStore(dsn),
        case_ledger_store=PostgresCaseLedgerStore(dsn),
        evidence_manifest_store=PostgresEvidenceIntakeStore(dsn),
        formal_calculation_store=PostgresFormalCalculationStore(dsn),
        legal_source_store=PostgresLegalSourceStore(
            dsn, official_source_reader=artifact_reader
        ),
        official_source_capture_store=PostgresOfficialSourceCaptureStore(
            dsn, artifact_reader=artifact_reader
        ),
        submission_store=PostgresSubmissionStore(dsn, artifact_reader=artifact_reader),
        reviewable_draft_store=PostgresReviewableDraftStore(
            dsn, artifact_reader=artifact_reader
        ),
        agent_execution_store=PostgresAgentExecutionStore(dsn),
        external_request_store=PostgresExternalRequestStore(dsn),
        artifact_store=artifact_store,
        office_pdf_converter=office_converter,
        persistence_label="postgres-internal-preview",
    )


def _build_office_converter(
    settings: RuntimeSettings, *, artifact_store: LocalEncryptedArtifactStore | None
) -> SandboxedOfficePdfConverter | None:
    if not settings.office_conversion_enabled:
        return None
    if artifact_store is None:
        raise RuntimeConfigurationBlocked("Office conversion requires the persistent encrypted artifact store")
    if settings._office_soffice_executable is None or settings._office_pdf_renderer_executable is None:
        raise RuntimeConfigurationBlocked("Office conversion runtime configuration is incomplete")
    try:
        return SandboxedOfficePdfConverter(
            soffice_executable=settings._office_soffice_executable,
            pdf_renderer_executable=settings._office_pdf_renderer_executable,
        )
    except OfficePdfConversionBlocked as error:
        raise RuntimeConfigurationBlocked("Office conversion executables failed the local safety check") from error
