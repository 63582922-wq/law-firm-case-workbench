from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from case_api.desktop_trust_bootstrap import DesktopEnrollmentTrustRuntime
from scripts.deployment_readiness import (
    CheckStatus,
    DeploymentTarget,
    REQUIRED_MIGRATIONS,
    assess_deployment_readiness,
)


class DeploymentReadinessTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        return {
            "CASE_WORKBENCH_RUNTIME_MODE": "postgres-internal-preview",
            "CASE_WORKBENCH_ENABLE_PERSISTENT_PREVIEW": "YES",
            "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://operator:never-print@127.0.0.1/lawcase_preview",
            "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT": str(root),
        }

    def _commercial_environment(self, root: Path) -> dict[str, str]:
        return {
            "CASE_WORKBENCH_RUNTIME_MODE": "commercial-production",
            "CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION": "YES",
            "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://operator:never-print@127.0.0.1/lawcase_production",
            "CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT": str(root),
        }

    def _migrations(self, root: Path) -> Path:
        migration_root = root / "migrations"
        migration_root.mkdir()
        for name in REQUIRED_MIGRATIONS:
            (migration_root / name).write_text("-- migration\n", encoding="utf-8")
        return migration_root

    def _sidecar(self, root: Path) -> Path:
        sidecar = root / "lawcase-local-api"
        sidecar.write_bytes(b"synthetic sidecar")
        sidecar.chmod(0o700)
        return sidecar

    def _statuses(self, report) -> dict[str, CheckStatus]:
        return {check.check_id: check.status for check in report.checks}

    def test_internal_preview_static_preflight_passes_without_database_or_keychain(self) -> None:
        with TemporaryDirectory(prefix="deployment-readiness-") as temporary:
            root = Path(temporary)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            ready_trust = DesktopEnrollmentTrustRuntime(
                phase="READY", message="synthetic checked trust"
            )
            with patch(
                "scripts.deployment_readiness.load_desktop_enrollment_trust",
                return_value=ready_trust,
            ), patch("case_kernel.postgres_store.psycopg.connect") as database_connect:
                report = assess_deployment_readiness(
                    environ=self._environment(managed),
                    target=DeploymentTarget.INTERNAL_PREVIEW,
                    migration_root=self._migrations(root),
                    sidecar=self._sidecar(root),
                    platform_name="Darwin",
                )
        self.assertTrue(report.static_preflight_passed)
        self.assertEqual(set(self._statuses(report).values()), {CheckStatus.PASS})
        database_connect.assert_not_called()
        rendered = report.as_dict()
        json.dumps(rendered, ensure_ascii=False)
        self.assertNotIn("never-print", str(rendered))
        self.assertFalse(rendered["release_authorized"])

    def test_missing_migration_or_unconfigured_trust_blocks_without_secret_echo(self) -> None:
        with TemporaryDirectory(prefix="deployment-readiness-") as temporary:
            root = Path(temporary)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            migrations = self._migrations(root)
            (migrations / REQUIRED_MIGRATIONS[-1]).unlink()
            report = assess_deployment_readiness(
                environ=self._environment(managed),
                target=DeploymentTarget.INTERNAL_PREVIEW,
                migration_root=migrations,
                sidecar=self._sidecar(root),
                trust_bootstrap=Path(__file__).resolve().parents[1]
                / "case_api"
                / "deployment"
                / "enrollment_trust_bootstrap.json",
                platform_name="Darwin",
                clock=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
            )
        statuses = self._statuses(report)
        self.assertEqual(statuses["migration_manifest"], CheckStatus.BLOCKED)
        self.assertEqual(statuses["enrollment_trust"], CheckStatus.BLOCKED)
        self.assertFalse(report.static_preflight_passed)
        self.assertNotIn("never-print", str(report.as_dict()))

    def test_commercial_target_static_preflight_passes_only_with_the_distinct_production_runtime(self) -> None:
        with TemporaryDirectory(prefix="deployment-readiness-") as temporary:
            root = Path(temporary)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            ready_trust = DesktopEnrollmentTrustRuntime(
                phase="READY", message="synthetic checked trust"
            )
            with patch(
                "scripts.deployment_readiness.load_desktop_enrollment_trust",
                return_value=ready_trust,
            ), patch("case_kernel.postgres_store.psycopg.connect") as database_connect:
                report = assess_deployment_readiness(
                    environ=self._commercial_environment(managed),
                    target=DeploymentTarget.COMMERCIAL_PRODUCTION,
                    migration_root=self._migrations(root),
                    sidecar=self._sidecar(root),
                    platform_name="Darwin",
                )
        self.assertTrue(report.static_preflight_passed)
        self.assertEqual(
            self._statuses(report)["commercial_runtime_boundary"], CheckStatus.PASS
        )
        self.assertFalse(report.as_dict()["release_authorized"])
        self.assertNotIn("never-print", str(report.as_dict()))
        database_connect.assert_not_called()

    def test_commercial_target_blocks_a_preview_runtime_even_if_other_static_checks_pass(self) -> None:
        with TemporaryDirectory(prefix="deployment-readiness-") as temporary:
            root = Path(temporary)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            ready_trust = DesktopEnrollmentTrustRuntime(
                phase="READY", message="synthetic checked trust"
            )
            with patch(
                "scripts.deployment_readiness.load_desktop_enrollment_trust",
                return_value=ready_trust,
            ):
                report = assess_deployment_readiness(
                    environ=self._environment(managed),
                    target=DeploymentTarget.COMMERCIAL_PRODUCTION,
                    migration_root=self._migrations(root),
                    sidecar=self._sidecar(root),
                    platform_name="Darwin",
                )
        statuses = self._statuses(report)
        self.assertFalse(report.static_preflight_passed)
        self.assertEqual(statuses["runtime_configuration"], CheckStatus.BLOCKED)
        self.assertEqual(statuses["commercial_runtime_boundary"], CheckStatus.BLOCKED)

    def test_test_database_and_unsafe_sidecar_are_blocked(self) -> None:
        with TemporaryDirectory(prefix="deployment-readiness-") as temporary:
            root = Path(temporary)
            managed = root / "managed"
            managed.mkdir(mode=0o700)
            environment = self._environment(managed)
            environment["CASE_WORKBENCH_POSTGRES_DSN"] = "postgresql://operator:never-print@127.0.0.1/lawcase_test"
            ready_trust = DesktopEnrollmentTrustRuntime(
                phase="READY", message="synthetic checked trust"
            )
            unsafe_sidecar = root / "wrong-name"
            unsafe_sidecar.write_bytes(b"synthetic sidecar")
            unsafe_sidecar.chmod(0o700)
            with patch(
                "scripts.deployment_readiness.load_desktop_enrollment_trust",
                return_value=ready_trust,
            ):
                report = assess_deployment_readiness(
                    environ=environment,
                    target=DeploymentTarget.INTERNAL_PREVIEW,
                    migration_root=self._migrations(root),
                    sidecar=unsafe_sidecar,
                    platform_name="Darwin",
                )
        statuses = self._statuses(report)
        self.assertEqual(statuses["runtime_configuration"], CheckStatus.BLOCKED)
        self.assertEqual(statuses["native_sidecar"], CheckStatus.BLOCKED)
        self.assertNotIn("never-print", str(report.as_dict()))


if __name__ == "__main__":
    unittest.main()
