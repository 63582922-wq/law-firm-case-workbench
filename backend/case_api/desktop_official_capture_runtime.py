"""Assemble the optional, bounded official-source capture worker for desktop.

The worker is intentionally separate from the HTTP application.  It receives
only a re-verified desktop-derived SYSTEM_WORKER identity, the persistent
stores assembled at process startup, and a private work-root chosen by the
desktop operator.  It never receives a browser URL, case folder, query, or
credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
from threading import Event
from typing import Mapping

from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.official_source_capture_supervisor import OfficialCaptureSupervisor
from case_kernel.official_source_capture_worker import run_next_authorized_official_source_capture

from .desktop_identity_runtime import DesktopIdentityRuntime
from .desktop_persistent_runtime import DesktopPersistentRuntime
from .desktop_system_worker import DesktopSystemWorkerBlocked, load_desktop_system_worker


OFFICIAL_CAPTURE_WORKER_ENABLED_ENV = "CASE_WORKBENCH_ENABLE_OFFICIAL_CAPTURE_WORKER"
OFFICIAL_CAPTURE_WORK_ROOT_ENV = "CASE_WORKBENCH_OFFICIAL_CAPTURE_WORK_ROOT"
OFFICIAL_CAPTURE_INTERVAL_SECONDS_ENV = "CASE_WORKBENCH_OFFICIAL_CAPTURE_INTERVAL_SECONDS"


class DesktopOfficialCaptureRuntimeBlocked(RuntimeError):
    """The optional desktop capture worker has not met its local safeguards."""


@dataclass(frozen=True)
class DesktopOfficialCaptureRuntime:
    """A ready-but-not-yet-started bounded worker loop.

    ``stop`` belongs to the sidecar process lifecycle.  The caller starts the
    supervisor only after the loopback server has been composed, and always
    sets it before closing the process.
    """

    supervisor: OfficialCaptureSupervisor
    stop: Event
    work_root: Path


def build_desktop_official_capture_runtime(
    *,
    identity: DesktopIdentityRuntime,
    environ: Mapping[str, str],
    persistent_runtime: DesktopPersistentRuntime,
) -> DesktopOfficialCaptureRuntime | None:
    """Return an explicitly enabled, one-at-a-time worker or ``None``.

    Leaving the enable variable unset is the safe default and is not an error.
    A present value other than ``YES`` is rejected so an operator cannot mistake
    a misspelling for a running worker.
    """

    enabled = environ.get(OFFICIAL_CAPTURE_WORKER_ENABLED_ENV)
    if enabled is None or not enabled.strip():
        return None
    if enabled != "YES":
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture worker requires explicit YES enablement"
        )
    if persistent_runtime.services.official_source_capture_store is None:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture worker requires the persistent capture store"
        )
    artifact_store = persistent_runtime.services.artifact_store
    if not isinstance(artifact_store, LocalEncryptedArtifactStore):
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture worker requires the encrypted artifact store"
        )
    try:
        worker = load_desktop_system_worker(identity=identity, environ=environ)
    except DesktopSystemWorkerBlocked as error:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture worker identity is unavailable"
        ) from error
    work_root = _configured_private_work_root(environ, artifact_store)
    interval_seconds = _configured_interval_seconds(environ)
    stop = Event()

    def run_once() -> object | None:
        return run_next_authorized_official_source_capture(
            worker=worker,
            case_root=work_root,
            artifact_store=artifact_store,
            store=persistent_runtime.services.official_source_capture_store,
        )

    return DesktopOfficialCaptureRuntime(
        supervisor=OfficialCaptureSupervisor(
            run_once=run_once,
            stop=stop,
            interval_seconds=interval_seconds,
        ),
        stop=stop,
        work_root=work_root,
    )


def _configured_private_work_root(
    environ: Mapping[str, str], artifact_store: LocalEncryptedArtifactStore
) -> Path:
    raw_root = environ.get(OFFICIAL_CAPTURE_WORK_ROOT_ENV, "").strip()
    if not raw_root:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture worker requires a pre-created private work root"
        )
    root = Path(raw_root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture work root must be an absolute non-symlink directory"
        )
    try:
        resolved = root.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture work root is unavailable"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or resolved == Path("/"):
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture work root must be a private directory"
        )
    if metadata.st_mode & 0o077:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture work root must not be readable by group or other users"
        )
    managed_root = artifact_store.managed_root.resolve(strict=True)
    if (
        resolved == managed_root
        or resolved.is_relative_to(managed_root)
        or managed_root.is_relative_to(resolved)
    ):
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture work root must be separate from encrypted artifacts"
        )
    # ``put_bytes`` repeats this check before each encrypted write.  Calling it
    # here catches unsafe roots during setup, before the loop is started.
    artifact_store.assert_separate_from_case_root(resolved)
    return resolved


def _configured_interval_seconds(environ: Mapping[str, str]) -> float:
    raw = environ.get(OFFICIAL_CAPTURE_INTERVAL_SECONDS_ENV, "5").strip()
    try:
        interval = float(raw)
    except ValueError as error:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture interval is invalid"
        ) from error
    if not 1 <= interval <= 300:
        raise DesktopOfficialCaptureRuntimeBlocked(
            "official capture interval must be 1 to 300 seconds"
        )
    return interval
