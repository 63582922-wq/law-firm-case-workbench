from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from case_api.desktop_agent_draft_runtime import (
    AGENT_DRAFT_WORKER_ENABLED_ENV,
    AGENT_DRAFT_WORK_ROOT_ENV,
    DesktopAgentDraftRuntimeBlocked,
    build_desktop_agent_draft_runtime,
)
from case_api.desktop_identity_runtime import DesktopIdentityRuntime
from case_api.desktop_persistent_runtime import DesktopPersistentRuntime
from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.managed_artifact_store import LocalEncryptedArtifactStore
from case_kernel.models import Actor, Role


def enrolled_identity() -> DesktopIdentityRuntime:
    now = datetime.now(timezone.utc)
    firm_id = str(uuid4())
    authority = DesktopSessionAuthority(
        actor=Actor(str(uuid4()), firm_id, frozenset({Role.LEAD_LAWYER})),
        bootstrap_token="b" * 64,
        bootstrap_expires_at=now + timedelta(seconds=30),
        session_expires_at=now + timedelta(minutes=10),
    )
    return DesktopIdentityRuntime(
        phase="ENROLLED",
        message="synthetic",
        enrollment_id=str(uuid4()),
        firm_id=firm_id,
        session_authority=authority,
    )


class DesktopAgentDraftRuntimeTests(unittest.TestCase):
    def _persistent_runtime(self, managed_root: Path) -> DesktopPersistentRuntime:
        store = LocalEncryptedArtifactStore(managed_root, key_id="test", encryption_key=b"k" * 32)
        return DesktopPersistentRuntime(
            dependencies=None,  # type: ignore[arg-type]
            services=SimpleNamespace(
                agent_execution_store=object(),
                reviewable_draft_store=object(),
                artifact_store=store,
                office_pdf_converter=object(),
            ),
        )

    def test_worker_is_opt_in_and_has_a_private_root(self) -> None:
        with TemporaryDirectory(prefix="desktop-agent-draft-") as temporary:
            parent = Path(temporary)
            managed_root, work_root = parent / "managed", parent / "worker"
            managed_root.mkdir(mode=0o700)
            work_root.mkdir(mode=0o700)
            environment = {
                AGENT_DRAFT_WORKER_ENABLED_ENV: "YES",
                AGENT_DRAFT_WORK_ROOT_ENV: str(work_root),
                "CASE_WORKBENCH_SYSTEM_WORKER_ID": str(uuid4()),
            }
            with patch("case_api.desktop_agent_draft_runtime.SandboxedOfficePdfConverter", object):
                runtime = build_desktop_agent_draft_runtime(
                    identity=enrolled_identity(),
                    environ=environment,
                    persistent_runtime=self._persistent_runtime(managed_root),
                )
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.work_root, work_root.resolve())

            disabled = dict(environment)
            disabled.pop(AGENT_DRAFT_WORKER_ENABLED_ENV)
            self.assertIsNone(
                build_desktop_agent_draft_runtime(
                    identity=enrolled_identity(),
                    environ=disabled,
                    persistent_runtime=self._persistent_runtime(managed_root),
                )
            )

    def test_worker_rejects_unsafe_root_and_ambiguous_enablement(self) -> None:
        with TemporaryDirectory(prefix="desktop-agent-draft-") as temporary:
            managed_root = Path(temporary) / "managed"
            managed_root.mkdir(mode=0o700)
            environment = {
                AGENT_DRAFT_WORKER_ENABLED_ENV: "YES",
                AGENT_DRAFT_WORK_ROOT_ENV: str(managed_root),
                "CASE_WORKBENCH_SYSTEM_WORKER_ID": str(uuid4()),
            }
            with patch("case_api.desktop_agent_draft_runtime.SandboxedOfficePdfConverter", object):
                with self.assertRaisesRegex(DesktopAgentDraftRuntimeBlocked, "separate"):
                    build_desktop_agent_draft_runtime(
                        identity=enrolled_identity(),
                        environ=environment,
                        persistent_runtime=self._persistent_runtime(managed_root),
                    )
                bad = dict(environment)
                bad[AGENT_DRAFT_WORKER_ENABLED_ENV] = "true"
                with self.assertRaisesRegex(DesktopAgentDraftRuntimeBlocked, "explicit YES"):
                    build_desktop_agent_draft_runtime(
                        identity=enrolled_identity(),
                        environ=bad,
                        persistent_runtime=self._persistent_runtime(managed_root),
                    )


if __name__ == "__main__":
    unittest.main()
