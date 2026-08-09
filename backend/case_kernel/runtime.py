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
from .evidence_manifest_postgres import PostgresEvidenceManifestStore
from .formal_calculation_postgres import PostgresFormalCalculationStore
from .legal_source_postgres import PostgresLegalSourceStore
from .postgres_store import PostgresMatterStore
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

    @property
    def postgres_dsn(self) -> str | None:
        return self._postgres_dsn

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "RuntimeSettings":
        raw_mode = environ.get("CASE_WORKBENCH_RUNTIME_MODE", RuntimeMode.SYNTHETIC_ALPHA.value).strip()
        try:
            mode = RuntimeMode(raw_mode)
        except ValueError as error:
            raise RuntimeConfigurationBlocked("unsupported CASE_WORKBENCH_RUNTIME_MODE") from error
        dsn = environ.get("CASE_WORKBENCH_POSTGRES_DSN", "").strip()
        acknowledgement = environ.get("CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW", "")

        if mode is RuntimeMode.SYNTHETIC_ALPHA:
            if dsn or acknowledgement:
                raise RuntimeConfigurationBlocked(
                    "persistent settings cannot be present while runtime mode is synthetic-alpha"
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
        return cls(mode=mode, _postgres_dsn=dsn)


@dataclass(frozen=True)
class RuntimeServices:
    settings: RuntimeSettings
    matter_store: MatterStore
    case_ledger_store: PostgresCaseLedgerStore | None
    evidence_manifest_store: PostgresEvidenceManifestStore | None
    formal_calculation_store: PostgresFormalCalculationStore | None
    legal_source_store: PostgresLegalSourceStore | None
    persistence_label: str


def load_runtime_settings() -> RuntimeSettings:
    """The launcher calls this explicitly; API module import never does."""
    return RuntimeSettings.from_environment(os.environ)


def build_runtime_services(settings: RuntimeSettings) -> RuntimeServices:
    if settings.mode is RuntimeMode.SYNTHETIC_ALPHA:
        return RuntimeServices(
            settings=settings,
            matter_store=InMemoryMatterStore(),
            case_ledger_store=None,
            evidence_manifest_store=None,
            formal_calculation_store=None,
            legal_source_store=None,
            persistence_label="in-memory-synthetic-only",
        )
    dsn = settings.postgres_dsn
    if dsn is None:
        raise RuntimeConfigurationBlocked("persistent runtime settings lost their PostgreSQL DSN")
    return RuntimeServices(
        settings=settings,
        matter_store=PostgresMatterStore(dsn),
        case_ledger_store=PostgresCaseLedgerStore(dsn),
        evidence_manifest_store=PostgresEvidenceManifestStore(dsn),
        formal_calculation_store=PostgresFormalCalculationStore(dsn),
        legal_source_store=PostgresLegalSourceStore(dsn),
        persistence_label="postgres-internal-preview",
    )
