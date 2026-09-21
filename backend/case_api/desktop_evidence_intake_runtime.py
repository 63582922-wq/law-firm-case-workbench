"""Optional local worker for lawyer-approved evidence intake.

This worker receives no browser path or model credential.  It can run only
while the same sidecar still holds a short-lived human folder grant and an
explicit local malware scanner.  A restart or expired grant leaves the queue
untouched and requires a fresh lawyer authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Mapping, Protocol
from uuid import uuid4

from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.evidence_intake_coordinator import coordinate_claimed_evidence_intake_item
from case_kernel.evidence_intake_postgres import EvidenceIntakeItemLease
from case_kernel.evidence_intake_worker import ClamAvCommandScanner
from case_kernel.local_intake_authorizations import (
    EvidenceIntakeAuthorization,
    LocalEvidenceIntakeAuthorizationRegistry,
)
from case_kernel.models import Actor
from case_kernel.official_source_capture_supervisor import OfficialCaptureSupervisor

from .desktop_identity_runtime import DesktopIdentityRuntime
from .desktop_persistent_runtime import DesktopPersistentRuntime
from .desktop_system_worker import DesktopSystemWorkerBlocked, load_desktop_system_worker


EVIDENCE_INTAKE_WORKER_ENABLED_ENV = "CASE_WORKBENCH_ENABLE_EVIDENCE_INTAKE_WORKER"
EVIDENCE_INTAKE_CLAMAV_ENV = "CASE_WORKBENCH_EVIDENCE_INTAKE_CLAMAV"
EVIDENCE_INTAKE_INTERVAL_SECONDS_ENV = "CASE_WORKBENCH_EVIDENCE_INTAKE_INTERVAL_SECONDS"


class DesktopEvidenceIntakeRuntimeBlocked(RuntimeError):
    """The optional local intake worker is missing a required safety gate."""


class EvidenceIntakeWorkerStore(Protocol):
    def claim_evidence_intake_item(self, **kwargs) -> EvidenceIntakeItemLease: ...

    def reap_exhausted_evidence_intake_items(self, **kwargs): ...


@dataclass(frozen=True)
class DesktopEvidenceIntakeRuntime:
    supervisor: OfficialCaptureSupervisor
    stop: Event


def build_desktop_evidence_intake_runtime(
    *,
    identity: DesktopIdentityRuntime,
    environ: Mapping[str, str],
    persistent_runtime: DesktopPersistentRuntime,
) -> DesktopEvidenceIntakeRuntime | None:
    enabled = environ.get(EVIDENCE_INTAKE_WORKER_ENABLED_ENV)
    if enabled is None or not enabled.strip():
        return None
    if enabled != "YES":
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake worker requires explicit YES enablement")
    services = persistent_runtime.services
    dependencies = persistent_runtime.dependencies
    if services.evidence_manifest_store is None or services.artifact_store is None:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake worker requires evidence persistence and encrypted artifacts")
    if dependencies.local_folder_grants is None or dependencies.local_evidence_intake_authorizations is None:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake worker requires the current process-local folder authorization registry")
    try:
        worker = load_desktop_system_worker(identity=identity, environ=environ)
        scanner = ClamAvCommandScanner(environ.get(EVIDENCE_INTAKE_CLAMAV_ENV, ""))
    except (DesktopSystemWorkerBlocked, ValueError) as error:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake worker identity or local scanner is unavailable") from error
    stop = Event()
    registry = dependencies.local_evidence_intake_authorizations

    def run_once() -> object | None:
        return _run_next_authorized_item(
            registry=registry,
            worker=worker,
            persistent_runtime=persistent_runtime,
            scanner=scanner,
        )

    return DesktopEvidenceIntakeRuntime(
        supervisor=OfficialCaptureSupervisor(
            run_once=run_once,
            stop=stop,
            interval_seconds=_configured_interval(environ),
        ),
        stop=stop,
    )


def _run_next_authorized_item(
    *,
    registry: LocalEvidenceIntakeAuthorizationRegistry,
    worker: Actor,
    persistent_runtime: DesktopPersistentRuntime,
    scanner: ClamAvCommandScanner,
) -> object | None:
    authorization = registry.next_authorized_run()
    if authorization is None:
        return None
    store = persistent_runtime.services.evidence_manifest_store
    folder_grants = persistent_runtime.dependencies.local_folder_grants
    artifact_store = persistent_runtime.services.artifact_store
    if store is None or folder_grants is None or artifact_store is None:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake worker dependencies changed after startup")
    try:
        lease = store.claim_evidence_intake_item(
            matter_id=authorization.matter_id,
            run_id=authorization.run_id,
            actor=worker,
            expected_version=authorization.expected_version,
            idempotency_key=f"local-intake-claim:{authorization.run_id}:{uuid4()}",
            lease_seconds=120,
        )
    except CaseLedgerPersistenceBlocked as error:
        if "no claimable item" in str(error):
            return _reap_or_close_authorization(
                registry=registry,
                store=store,
                worker=worker,
                authorization=authorization,
            )
        raise
    # Claiming itself advances the ledger version.  Record that immediately so
    # a safely retried lease never uses the pre-claim version.
    registry.update_expected_version(run_id=authorization.run_id, expected_version=lease.matter_version)
    try:
        result = coordinate_claimed_evidence_intake_item(
            lease=lease,
            folder_grants=folder_grants,
            folder_grant_id=authorization.folder_grant_id,
            grant_actor=authorization.grant_actor,
            grant_session=authorization.grant_session,
            scanner=scanner,
            persistence=store,
            system_actor=worker,
            artifact_store=artifact_store,
            office_converter=persistent_runtime.services.office_pdf_converter,
        )
    except Exception:
        # A sidecar must not claim the same leased item again before PostgreSQL
        # declares that lease expired.  The third expired attempt is converted
        # by the normal reaper into an explicit audit event rather than being
        # silently abandoned.
        registry.defer_until(run_id=authorization.run_id, retry_not_before=lease.lease_expires_at)
        raise
    try:
        registry.update_expected_version(
            run_id=authorization.run_id,
            expected_version=result.final_matter_version,
        )
    except Exception:
        # The completed ledger result remains authoritative.  A grant that
        # expired during a long scan cannot authorize the next source file.
        registry.remove(run_id=authorization.run_id)
    return result


def _reap_or_close_authorization(
    *,
    registry: LocalEvidenceIntakeAuthorizationRegistry,
    store: EvidenceIntakeWorkerStore,
    worker: Actor,
    authorization: EvidenceIntakeAuthorization,
) -> object | None:
    """Finish bounded recovery, or release a run with no remaining work."""
    try:
        receipt = store.reap_exhausted_evidence_intake_items(
            matter_id=authorization.matter_id,
            run_id=authorization.run_id,
            actor=worker,
            expected_version=authorization.expected_version,
            idempotency_key=f"local-intake-reap:{authorization.run_id}:{uuid4()}",
        )
    except CaseLedgerPersistenceBlocked as error:
        if "no exhausted expired items" not in str(error):
            raise
        registry.remove(run_id=authorization.run_id)
        return None
    registry.update_expected_version(run_id=authorization.run_id, expected_version=receipt.matter_version)
    return receipt


def _configured_interval(environ: Mapping[str, str]) -> float:
    raw = environ.get(EVIDENCE_INTAKE_INTERVAL_SECONDS_ENV, "3").strip()
    try:
        interval = float(raw)
    except ValueError as error:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake interval is invalid") from error
    if not 1 <= interval <= 300:
        raise DesktopEvidenceIntakeRuntimeBlocked("evidence intake interval must be 1 to 300 seconds")
    return interval
