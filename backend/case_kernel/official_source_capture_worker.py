"""One-job system-worker entry point for an already-authorized official capture.

This is intentionally not a queue scanner or a generic browser.  A desktop
supervisor must provide the exact matter and queued run selected from the
protected task queue.  The worker first claims that one run under a dedicated
SYSTEM_WORKER actor, then delegates to the bounded HTTPS capture coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol
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
    def find_next_claimable_capture(self, *, actor: Actor) -> tuple[str, str, int] | None: ...

    def claim_capture(self, **kwargs) -> OfficialSourceCaptureRunLease: ...

    def complete_capture(self, **kwargs): ...

    def fail_capture(self, **kwargs): ...


class OfficialSourceCaptureArtifactStoreFactory(Protocol):
    """Issue a per-matter artifact capability after the queue selects a run."""

    def __call__(self, matter_id: str) -> object: ...


def run_authorized_official_source_capture(
    *,
    matter_id: str,
    run_id: str,
    expected_version: int,
    worker: Actor,
    claim_idempotency_key: str,
    case_root: str | Path,
    artifact_store: object,
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


def run_next_authorized_official_source_capture(
    *,
    worker: Actor,
    case_root: str | Path,
    artifact_store: object | None = None,
    artifact_store_factory: OfficialSourceCaptureArtifactStoreFactory | None = None,
    store: OfficialSourceCaptureWorkerStore,
    transport: OfficialSourceTransport | None = None,
) -> OfficialSourceCaptureCoordinationResult | None:
    """Run at most one eligible capture; never bulk-drain a queue.

    Production callers provide a factory so storage is created only after the
    store returns the one lease candidate, bound to that candidate's matter.
    The legacy fixed store remains available for the local command-line flow.
    """
    if (artifact_store is None) == (artifact_store_factory is None):
        raise OfficialSourceCaptureWorkerBlocked(
            "official capture requires exactly one artifact-store capability"
        )
    candidate = store.find_next_claimable_capture(actor=worker)
    if candidate is None:
        return None
    matter_id, run_id, version = candidate
    selected_store = (
        artifact_store_factory(matter_id)
        if artifact_store_factory is not None
        else artifact_store
    )
    if selected_store is None:
        raise OfficialSourceCaptureWorkerBlocked(
            "official capture artifact-store capability is unavailable"
        )
    return run_authorized_official_source_capture(
        matter_id=matter_id, run_id=run_id, expected_version=version, worker=worker,
        claim_idempotency_key=f"official-worker:{run_id}:claim", case_root=case_root,
        artifact_store=selected_store, store=store, transport=transport,
    )


class BoundedOfficialSourceCaptureWorker:
    """One bounded auxiliary cycle for the existing single Agent Worker process."""

    def __init__(
        self,
        *,
        worker: Actor,
        case_root: str | Path,
        store: OfficialSourceCaptureWorkerStore,
        artifact_store_factory: OfficialSourceCaptureArtifactStoreFactory,
        transport: OfficialSourceTransport | None = None,
    ) -> None:
        if worker.roles != frozenset({Role.SYSTEM_WORKER}):
            raise ValueError("official capture requires a dedicated SYSTEM_WORKER")
        if not callable(artifact_store_factory):
            raise ValueError("official capture artifact-store factory is required")
        self._worker = worker
        self._case_root = case_root
        self._store = store
        self._artifact_store_factory = artifact_store_factory
        self._transport = transport

    def run_cycle(self) -> bool:
        result = run_next_authorized_official_source_capture(
            worker=self._worker,
            case_root=self._case_root,
            artifact_store_factory=self._artifact_store_factory,
            store=self._store,
            transport=self._transport,
        )
        return result is not None


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
