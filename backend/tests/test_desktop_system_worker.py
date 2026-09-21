from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from case_api.desktop_identity_runtime import DesktopIdentityRuntime
from case_api.desktop_system_worker import (
    SYSTEM_WORKER_ID_ENV,
    DesktopSystemWorkerBlocked,
    load_desktop_system_worker,
)
from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.models import Actor, Role


def enrolled_identity(*, firm_id: str) -> DesktopIdentityRuntime:
    now = datetime.now(timezone.utc)
    authority = DesktopSessionAuthority(
        actor=Actor(str(uuid4()), firm_id, frozenset({Role.LEAD_LAWYER})),
        bootstrap_token="b" * 64,
        bootstrap_expires_at=now + timedelta(seconds=30),
        session_expires_at=now + timedelta(minutes=10),
    )
    return DesktopIdentityRuntime(
        phase="ENROLLED", message="synthetic", enrollment_id=str(uuid4()),
        firm_id=firm_id, session_authority=authority,
    )


class DesktopSystemWorkerTests(unittest.TestCase):
    def test_worker_scope_is_derived_from_enrollment_not_environment(self) -> None:
        firm_id, worker_id = str(uuid4()), str(uuid4())
        worker = load_desktop_system_worker(
            identity=enrolled_identity(firm_id=firm_id),
            environ={SYSTEM_WORKER_ID_ENV: worker_id, "CASE_WORKBENCH_FIRM_ID": str(uuid4())},
        )
        self.assertEqual(worker.actor_id, worker_id)
        self.assertEqual(worker.firm_id, firm_id)
        self.assertEqual(worker.roles, frozenset({Role.SYSTEM_WORKER}))

    def test_missing_or_unenrolled_worker_identity_fails_closed(self) -> None:
        firm_id = str(uuid4())
        with self.assertRaisesRegex(DesktopSystemWorkerBlocked, "not configured"):
            load_desktop_system_worker(identity=enrolled_identity(firm_id=firm_id), environ={})
        with self.assertRaisesRegex(DesktopSystemWorkerBlocked, "enrolled"):
            load_desktop_system_worker(
                identity=DesktopIdentityRuntime(phase="NOT_ENROLLED", message="missing"),
                environ={SYSTEM_WORKER_ID_ENV: str(uuid4())},
            )


if __name__ == "__main__":
    unittest.main()
