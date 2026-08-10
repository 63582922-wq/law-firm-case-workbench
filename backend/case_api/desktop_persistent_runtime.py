"""Assemble the guarded persistent desktop runtime without opening case routes by default.

This module is deliberately a narrow composition root.  It connects only an
already-enrolled desktop identity, an explicitly acknowledged preview database,
and a Keychain-protected encrypted artifact directory.  It never provisions a
key, accepts a key from HTTP, or accepts an artifact directory from a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
from typing import Mapping

from case_kernel.artifact_access import EphemeralArtifactAccessBroker
from case_kernel.artifact_key_provider import ArtifactKeyProvider, MacOSKeychainArtifactKeyProvider
from case_kernel.local_access_grants import LocalFolderGrantRegistry
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.original_page_access import OriginalPageAccessBroker
from case_kernel.reviewable_draft_access import ReviewableOfficeDraftAccessBroker
from case_kernel.runtime import RuntimeConfigurationBlocked, RuntimeMode, RuntimeServices, RuntimeSettings, build_runtime_services
from case_kernel.submission_access import SubmissionExportAccessBroker

from .desktop_identity_runtime import DesktopIdentityRuntime
from .persistent_app import PersistentApiDependencies


MACOS_KEYCHAIN_ARTIFACT_SERVICE = "cn.lawcase.workbench.managed-artifacts"
MACOS_KEYCHAIN_ARTIFACT_ACCOUNT = "aes256-gcm-v1"
MACOS_KEYCHAIN_ARTIFACT_KEY_ID = "macos-keychain-aes256-gcm-v1"
_MANAGED_ROOT_ENV = "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT"


class DesktopPersistentRuntimeBlocked(RuntimeError):
    """A desktop preview runtime has not met every local security prerequisite."""


@dataclass(frozen=True)
class DesktopPersistentRuntime:
    """The only permitted production-like composition for the desktop sidecar."""

    dependencies: PersistentApiDependencies
    services: RuntimeServices


def build_desktop_persistent_runtime(
    *,
    identity: DesktopIdentityRuntime,
    environ: Mapping[str, str],
    key_provider: ArtifactKeyProvider | None = None,
) -> DesktopPersistentRuntime:
    """Build persistent dependencies or reject the desktop launch before routes open."""

    if identity.phase != "ENROLLED" or identity.session_authority is None:
        raise DesktopPersistentRuntimeBlocked(
            "persistent desktop runtime requires an enrolled OS-bound identity"
        )
    try:
        settings = RuntimeSettings.from_environment(environ)
    except RuntimeConfigurationBlocked as error:
        raise DesktopPersistentRuntimeBlocked("persistent runtime configuration is invalid") from error
    if settings.mode is not RuntimeMode.POSTGRES_INTERNAL_PREVIEW:
        raise DesktopPersistentRuntimeBlocked(
            "persistent desktop runtime requires postgres-internal-preview mode"
        )

    managed_root = _configured_managed_root(environ)
    provider = key_provider or MacOSKeychainArtifactKeyProvider(
        service=MACOS_KEYCHAIN_ARTIFACT_SERVICE,
        account=MACOS_KEYCHAIN_ARTIFACT_ACCOUNT,
        key_id=MACOS_KEYCHAIN_ARTIFACT_KEY_ID,
    )
    try:
        artifact_store = LocalEncryptedArtifactStore.from_key_provider(
            managed_root,
            key_provider=provider,
        )
        services = build_runtime_services(settings, artifact_store=artifact_store)
    except Exception as error:
        # Do not expose Keychain, filesystem, or DSN details through the local
        # service startup channel.  The desktop setup diagnostic can inspect
        # its own configured prerequisites without disclosing secrets to HTTP.
        raise DesktopPersistentRuntimeBlocked(
            "persistent desktop dependencies are unavailable"
        ) from error

    if not all(
        (
            services.case_ledger_store,
            services.evidence_manifest_store,
            services.formal_calculation_store,
            services.legal_source_store,
            services.official_source_capture_store,
            services.submission_store,
            services.reviewable_draft_store,
            services.agent_execution_store,
            services.agent_draft_candidate_store,
            services.document_consistency_store,
            services.external_request_store,
            services.artifact_store,
        )
    ):
        raise DesktopPersistentRuntimeBlocked(
            "persistent desktop dependencies were assembled incompletely"
        )

    folder_grants = LocalFolderGrantRegistry()
    dependencies = PersistentApiDependencies(
        settings=settings,
        case_ledger_store=services.case_ledger_store,
        identity_resolver=identity.session_authority,
        desktop_session_authority=identity.session_authority,
        evidence_manifest_store=services.evidence_manifest_store,
        formal_calculation_store=services.formal_calculation_store,
        legal_source_store=services.legal_source_store,
        official_source_capture_store=services.official_source_capture_store,
        submission_store=services.submission_store,
        reviewable_draft_store=services.reviewable_draft_store,
        agent_execution_store=services.agent_execution_store,
        document_consistency_store=services.document_consistency_store,
        external_request_store=services.external_request_store,
        artifact_access_broker=EphemeralArtifactAccessBroker(),
        submission_access_broker=SubmissionExportAccessBroker(),
        reviewable_draft_access_broker=ReviewableOfficeDraftAccessBroker(),
        artifact_store=services.artifact_store,
        local_folder_grants=folder_grants,
        original_page_access_broker=OriginalPageAccessBroker(
            folder_grants=folder_grants,
            artifact_store=services.artifact_store,
        ),
    )
    try:
        dependencies.validate()
    except ValueError as error:
        raise DesktopPersistentRuntimeBlocked(
            "persistent desktop dependency validation failed"
        ) from error
    return DesktopPersistentRuntime(dependencies=dependencies, services=services)


def _configured_managed_root(environ: Mapping[str, str]) -> Path:
    raw_root = environ.get(_MANAGED_ROOT_ENV, "").strip()
    if not raw_root:
        raise DesktopPersistentRuntimeBlocked(
            f"{_MANAGED_ROOT_ENV} must name a pre-created private directory"
        )
    root = Path(raw_root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise DesktopPersistentRuntimeBlocked("managed artifact root must be an absolute non-symlink directory")
    try:
        resolved = root.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise DesktopPersistentRuntimeBlocked("managed artifact root is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or resolved == Path("/"):
        raise DesktopPersistentRuntimeBlocked("managed artifact root must be a private directory")
    if metadata.st_mode & 0o077:
        raise DesktopPersistentRuntimeBlocked(
            "managed artifact root must not be readable by group or other users"
        )
    return resolved
