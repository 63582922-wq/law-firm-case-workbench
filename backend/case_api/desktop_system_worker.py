"""Derive a desktop SYSTEM_WORKER identity without trusting browser input.

The worker account is an operator-configured UUID, but its firm scope always
comes from the currently re-verified desktop enrollment. PostgreSQL still
checks that this account has the dedicated SYSTEM_WORKER role for each matter.
"""

from __future__ import annotations

from typing import Mapping
from uuid import UUID

from case_kernel.models import Actor, Role

from .desktop_identity_runtime import DesktopIdentityRuntime


SYSTEM_WORKER_ID_ENV = "CASE_WORKBENCH_SYSTEM_WORKER_ID"


class DesktopSystemWorkerBlocked(PermissionError):
    """The desktop cannot safely assemble a dedicated worker identity."""


def load_desktop_system_worker(
    *, identity: DesktopIdentityRuntime,
    environ: Mapping[str, str],
) -> Actor:
    if identity.phase != "ENROLLED" or identity.session_authority is None:
        raise DesktopSystemWorkerBlocked("desktop system worker requires an enrolled desktop identity")
    worker_id = environ.get(SYSTEM_WORKER_ID_ENV, "").strip()
    if not worker_id:
        raise DesktopSystemWorkerBlocked("desktop system worker is not configured")
    if identity.firm_id is None:
        raise DesktopSystemWorkerBlocked("desktop enrollment has no firm scope")
    try:
        UUID(worker_id)
        UUID(identity.firm_id)
    except (TypeError, ValueError) as error:
        raise DesktopSystemWorkerBlocked("desktop system worker identity must use UUIDs") from error
    return Actor(
        actor_id=worker_id,
        firm_id=identity.firm_id,
        roles=frozenset({Role.SYSTEM_WORKER}),
    )
