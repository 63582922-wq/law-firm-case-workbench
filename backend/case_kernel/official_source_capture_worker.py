"""One-job system-worker entry point for an already-authorized official capture.

This is intentionally not a queue scanner or a generic browser.  A desktop
supervisor must provide the exact matter and queued run selected from the
protected task queue.  The worker first claims that one run under a dedicated
SYSTEM_WORKER actor, then delegates to the bounded HTTPS capture coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from .case_ledger_postgres import CaseLedgerPersistenceBlocked
from .managed_artifact_store import LocalEncryptedArtifactStore
from .models import Actor, Role
from .official_source_capture import OfficialSourceTransport
from .official_source_capture_coordinator import (
    OfficialSourceCaptureCoordinationResult,
    execute_claimed_official_source_capture,
)
from .official_source_capture_postgres import OfficialSourceCaptureRunLease


class OfficialSourceCaptureWorkerBlocked(PermissionError):
    """The supervisor attempted to run an official capture outside its lease boundary."""


class OfficialSourceCaptureWorkerStore(Protocol):
    def claim_capture(self, **kwargs) -> OfficialSourceCaptureRunLease: ...

    def complete_capture(self, **kwargs): ...

    def fail_capture(self, **kwargs): ...


def run_authorized_official_source_capture(
    *,
    matter_id: str,
    run_id: str,
    expected_version: int,
    worker: Actor,
    claim_idempotency_key: str,
    case_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore,
    store: OfficialSourceCaptureWorkerStore,
    transport: OfficialSourceTransport | None = None,
) -> OfficialSourceCaptureCoordinationResult:
    """Claim exactly one approved run, then execute it exactly once.

    The function accepts no URL, query, provider credential, browser payload or
    case text.  Those values are reconstructed from the approved run after the
    store grants the short lease.  Network errors are converted by the
    coordinator into a terminal, auditable failure record.
    """

    _validate_worker_request(
        matter_id=matter_id,
        run_id=run_id,
        expected_version=expected_version,
        worker=worker,
        claim_idempotency_key=claim_idempotency_key,
    )
    try:
        lease = store.claim_capture(
            matter_id=matter_id,
            run_id=run_id,
            actor=worker,
            expected_version=expected_version,
            idempotency_key=claim_idempotency_key,
        )
    except (CaseLedgerPersistenceBlocked, KeyError) as error:
        raise OfficialSourceCaptureWorkerBlocked(
            "official source capture run could not be claimed"
        ) from error
    if lease.matter_id != matter_id or lease.run_id != run_id:
        raise OfficialSourceCaptureWorkerBlocked(
            "official source capture store returned a lease outside the requested run"
        )
    if lease.matter_version < expected_version:
        raise OfficialSourceCaptureWorkerBlocked(
            "official source capture lease version predates the claimed task"
        )
    return execute_claimed_official_source_capture(
        lease=lease,
        case_root=case_root,
        artifact_store=artifact_store,
        persistence=store,
        system_actor=worker,
        transport=transport,
    )


def _validate_worker_request(
    *,
    matter_id: str,
    run_id: str,
    expected_version: int,
    worker: Actor,
    claim_idempotency_key: str,
) -> None:
    for label, value in (("matter_id", matter_id), ("run_id", run_id), ("worker actor_id", worker.actor_id), ("worker firm_id", worker.firm_id)):
        try:
            UUID(value)
        except (TypeError, ValueError) as error:
            raise OfficialSourceCaptureWorkerBlocked(f"official capture {label} must be a UUID") from error
    if worker.roles != frozenset({Role.SYSTEM_WORKER}):
        raise OfficialSourceCaptureWorkerBlocked(
            "official capture requires a dedicated SYSTEM_WORKER identity"
        )
    if expected_version < 1:
        raise OfficialSourceCaptureWorkerBlocked("official capture expected version is invalid")
    if not 16 <= len(claim_idempotency_key) <= 200 or not claim_idempotency_key.isascii():
        raise OfficialSourceCaptureWorkerBlocked("official capture claim idempotency key is invalid")
