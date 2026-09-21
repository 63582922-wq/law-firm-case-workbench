from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_api.desktop_identity_runtime import DesktopIdentityRuntime
from case_api.desktop_persistent_runtime import DesktopPersistentRuntimeBlocked, build_desktop_persistent_runtime
from case_api.desktop_sidecar import create_desktop_sidecar_app
from case_api.persistent_identity import DesktopSessionAuthority
from case_kernel.artifact_key_provider import ArtifactKeyMaterial, MANAGED_ARTIFACT_KEY_PURPOSE
from case_kernel.models import Actor, Role
from fastapi.testclient import TestClient


@dataclass(frozen=True)
class SyntheticKeyProvider:
    key_bytes: bytes = b"k" * 32

    def get_key(self, *, purpose: str) -> ArtifactKeyMaterial:
        if purpose != MANAGED_ARTIFACT_KEY_PURPOSE:
            raise AssertionError(purpose)
        return ArtifactKeyMaterial(
            key_id="test-managed-key-v1",
            purpose=purpose,
            key_bytes=self.key_bytes,
        )


def enrolled_identity() -> DesktopIdentityRuntime:
    now = datetime.now(timezone.utc)
    authority = DesktopSessionAuthority(
        actor=Actor(
            actor_id="11111111-1111-4111-8111-111111111111",
            firm_id="22222222-2222-4222-8222-222222222222",
            roles=frozenset({Role.LEAD_LAWYER}),
        ),
        bootstrap_token="b" * 64,
        bootstrap_expires_at=now + timedelta(seconds=30),
        session_expires_at=now + timedelta(minutes=10),
    )
    return DesktopIdentityRuntime(
        phase="ENROLLED",
        message="synthetic enrolled identity",
        enrollment_id="33333333-3333-4333-8333-333333333333",
        firm_id="22222222-2222-4222-8222-222222222222",
        session_authority=authority,
    )


class DesktopPersistentRuntimeTests(unittest.TestCase):
    def test_preview_runtime_requires_enrolled_identity_and_private_managed_root(self) -> None:
        with TemporaryDirectory(prefix="desktop-persistent-runtime-") as temporary:
            root = Path(temporary) / "managed"
            root.mkdir(mode=0o700)
            environment = {
                "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
                "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
                "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://worker:secret@127.0.0.1/lawcase_preview",
                "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT": str(root),
            }
            runtime = build_desktop_persistent_runtime(
                identity=enrolled_identity(),
                environ=environment,
                key_provider=SyntheticKeyProvider(),
            )
            self.assertIs(runtime.dependencies.identity_resolver, runtime.dependencies.desktop_session_authority)
            self.assertEqual(runtime.services.persistence_label, "postgres-internal-preview")
            self.assertEqual(runtime.services.artifact_store.managed_root, root.resolve())  # type: ignore[union-attr]
            health = TestClient(
                create_desktop_sidecar_app(
                    persistent_dependencies=runtime.dependencies,
                )
            ).get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["persistence"], "configured-not-probed")
            self.assertEqual(health.json()["desktop_session"], "configured")

            with self.assertRaises(DesktopPersistentRuntimeBlocked):
                build_desktop_persistent_runtime(
                    identity=DesktopIdentityRuntime(phase="NOT_ENROLLED", message="missing"),
                    environ=environment,
                    key_provider=SyntheticKeyProvider(),
                )

    def test_commercial_runtime_keeps_the_existing_identity_keychain_and_private_root_gates(self) -> None:
        with TemporaryDirectory(prefix="desktop-commercial-runtime-") as temporary:
            root = Path(temporary) / "managed"
            root.mkdir(mode=0o700)
            environment = {
                "CASE_WORKBENCH_RUNTIME_MODE": "commercial-production",
                "CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION": "YES",
                "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://worker:synthetic-secret@127.0.0.1/lawcase_production",
                "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT": str(root),
            }
            runtime = build_desktop_persistent_runtime(
                identity=enrolled_identity(),
                environ=environment,
                key_provider=SyntheticKeyProvider(),
            )
            self.assertEqual(runtime.services.persistence_label, "commercial-production")
            health = TestClient(
                create_desktop_sidecar_app(
                    persistent_dependencies=runtime.dependencies,
                )
            ).get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["mode"], "commercial-production")
            self.assertEqual(health.json()["desktop_session"], "configured")

            missing_confirmation = dict(environment)
            missing_confirmation.pop("CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION")
            with self.assertRaises(DesktopPersistentRuntimeBlocked):
                build_desktop_persistent_runtime(
                    identity=enrolled_identity(),
                    environ=missing_confirmation,
                    key_provider=SyntheticKeyProvider(),
                )

    def test_preview_runtime_rejects_missing_or_insecure_artifact_root(self) -> None:
        environment = {
            "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
            "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
            "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://worker:secret@127.0.0.1/lawcase_preview",
        }
        with self.assertRaises(DesktopPersistentRuntimeBlocked):
            build_desktop_persistent_runtime(
                identity=enrolled_identity(),
                environ=environment,
                key_provider=SyntheticKeyProvider(),
            )

        with TemporaryDirectory(prefix="desktop-persistent-runtime-") as temporary:
            root = Path(temporary) / "insecure"
            root.mkdir(mode=0o755)
            environment["CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT"] = str(root)
            with self.assertRaises(DesktopPersistentRuntimeBlocked):
                build_desktop_persistent_runtime(
                    identity=enrolled_identity(),
                    environ=environment,
                    key_provider=SyntheticKeyProvider(),
                )


if __name__ == "__main__":
    unittest.main()
