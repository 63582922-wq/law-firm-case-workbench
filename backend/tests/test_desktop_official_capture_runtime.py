from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from case_api.desktop_official_capture_runtime import (
    OFFICIAL_CAPTURE_INTERVAL_SECONDS_ENV,
    OFFICIAL_CAPTURE_WORKER_ENABLED_ENV,
    OFFICIAL_CAPTURE_WORK_ROOT_ENV,
    DesktopOfficialCaptureRuntimeBlocked,
    build_desktop_official_capture_runtime,
)
from case_api.desktop_persistent_runtime import build_desktop_persistent_runtime
from case_api.desktop_identity_runtime import DesktopIdentityRuntime
from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.artifact_key_provider import ArtifactKeyMaterial, MANAGED_ARTIFACT_KEY_PURPOSE
from case_kernel.models import Actor, Role


@dataclass(frozen=True)
class SyntheticKeyProvider:
    key_bytes: bytes = b"k" * 32

    def get_key(self, *, purpose: str) -> ArtifactKeyMaterial:
        if purpose != MANAGED_ARTIFACT_KEY_PURPOSE:
            raise AssertionError(purpose)
        return ArtifactKeyMaterial(
            key_id="test-managed-key-v1", purpose=purpose, key_bytes=self.key_bytes
        )


def enrolled_identity() -> DesktopIdentityRuntime:
    now = datetime.now(timezone.utc)
    firm_id = "22222222-2222-4222-8222-222222222222"
    authority = DesktopSessionAuthority(
        actor=Actor(
            actor_id="11111111-1111-4111-8111-111111111111",
            firm_id=firm_id,
            roles=frozenset({Role.LEAD_LAWYER}),
        ),
        bootstrap_token="b" * 64,
        bootstrap_expires_at=now + timedelta(seconds=30),
        session_expires_at=now + timedelta(minutes=10),
    )
    return DesktopIdentityRuntime(
        phase="ENROLLED", message="synthetic", enrollment_id=str(uuid4()),
        firm_id=firm_id, session_authority=authority,
    )


class DesktopOfficialCaptureRuntimeTests(unittest.TestCase):
    def _environment(self, *, managed_root: Path, work_root: Path) -> dict[str, str]:
        return {
            "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
            "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
            "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://worker:secret@127.0.0.1/lawcase_preview",
            "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT": str(managed_root),
            OFFICIAL_CAPTURE_WORKER_ENABLED_ENV: "YES",
            OFFICIAL_CAPTURE_WORK_ROOT_ENV: str(work_root),
            "CASE_WORKBENCH_SYSTEM_WORKER_ID": str(uuid4()),
        }

    def test_worker_is_opt_in_and_uses_a_private_separate_work_root(self) -> None:
        with TemporaryDirectory(prefix="desktop-official-capture-") as temporary:
            parent = Path(temporary)
            managed_root, work_root = parent / "managed", parent / "worker"
            managed_root.mkdir(mode=0o700)
            work_root.mkdir(mode=0o700)
            environment = self._environment(managed_root=managed_root, work_root=work_root)
            persistent_runtime = build_desktop_persistent_runtime(
                identity=enrolled_identity(), environ=environment, key_provider=SyntheticKeyProvider()
            )
            assembled = build_desktop_official_capture_runtime(
                identity=enrolled_identity(), environ=environment, persistent_runtime=persistent_runtime
            )
            self.assertIsNotNone(assembled)
            assert assembled is not None
            self.assertEqual(assembled.work_root, work_root.resolve())
            self.assertFalse(assembled.stop.is_set())

            disabled = dict(environment)
            disabled.pop(OFFICIAL_CAPTURE_WORKER_ENABLED_ENV)
            self.assertIsNone(
                build_desktop_official_capture_runtime(
                    identity=enrolled_identity(), environ=disabled, persistent_runtime=persistent_runtime
                )
            )

    def test_worker_rejects_unsafe_directory_or_ambiguous_enablement(self) -> None:
        with TemporaryDirectory(prefix="desktop-official-capture-") as temporary:
            parent = Path(temporary)
            managed_root = parent / "managed"
            managed_root.mkdir(mode=0o700)
            environment = self._environment(managed_root=managed_root, work_root=managed_root)
            persistent_runtime = build_desktop_persistent_runtime(
                identity=enrolled_identity(), environ=environment, key_provider=SyntheticKeyProvider()
            )
            with self.assertRaisesRegex(DesktopOfficialCaptureRuntimeBlocked, "separate"):
                build_desktop_official_capture_runtime(
                    identity=enrolled_identity(), environ=environment, persistent_runtime=persistent_runtime
                )

            misconfigured = dict(environment)
            misconfigured[OFFICIAL_CAPTURE_WORKER_ENABLED_ENV] = "true"
            with self.assertRaisesRegex(DesktopOfficialCaptureRuntimeBlocked, "explicit YES"):
                build_desktop_official_capture_runtime(
                    identity=enrolled_identity(), environ=misconfigured, persistent_runtime=persistent_runtime
                )

    def test_worker_rejects_invalid_interval_before_starting_a_loop(self) -> None:
        with TemporaryDirectory(prefix="desktop-official-capture-") as temporary:
            parent = Path(temporary)
            managed_root, work_root = parent / "managed", parent / "worker"
            managed_root.mkdir(mode=0o700)
            work_root.mkdir(mode=0o700)
            environment = self._environment(managed_root=managed_root, work_root=work_root)
            environment[OFFICIAL_CAPTURE_INTERVAL_SECONDS_ENV] = "0.5"
            persistent_runtime = build_desktop_persistent_runtime(
                identity=enrolled_identity(), environ=environment, key_provider=SyntheticKeyProvider()
            )
            with self.assertRaisesRegex(DesktopOfficialCaptureRuntimeBlocked, "1 to 300"):
                build_desktop_official_capture_runtime(
                    identity=enrolled_identity(), environ=environment, persistent_runtime=persistent_runtime
                )


if __name__ == "__main__":
    unittest.main()
