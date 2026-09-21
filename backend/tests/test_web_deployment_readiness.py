from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.web_deployment_readiness import assess_web_deployment_readiness
from scripts.web_deployment_readiness import REQUIRED_MIGRATIONS
from scripts.web_deployment_readiness import _check_migrations
from backend.tests.test_web_runtime import WebRuntimeTests


class WebDeploymentReadinessTests(unittest.TestCase):
    def test_static_check_requires_the_complete_migration_chain(self) -> None:
        self.assertEqual(REQUIRED_MIGRATIONS[0], "0001_core.sql")
        self.assertEqual(
            REQUIRED_MIGRATIONS,
            tuple(
                path.name
                for path in sorted(
                    (Path(__file__).resolve().parents[1] / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
                )
            ),
        )
        self.assertEqual(
            tuple(int(name[:4]) for name in REQUIRED_MIGRATIONS),
            tuple(range(1, len(REQUIRED_MIGRATIONS) + 1)),
        )

    def test_migration_check_blocks_a_non_contiguous_discovered_chain(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "0001_core.sql").write_text("-- one", encoding="utf-8")
            (root / "0003_gap.sql").write_text("-- three", encoding="utf-8")
            check = _check_migrations(root)
        self.assertEqual(check.status.value, "BLOCKED")
        self.assertIn("不连续", check.message)

    def test_default_environment_is_blocked_without_echoing_secrets(self) -> None:
        report = assess_web_deployment_readiness(environ={})
        self.assertFalse(report.static_preflight_passed)
        payload = str(report.as_dict())
        self.assertNotIn("password", payload.lower())
        self.assertFalse(report.as_dict()["release_authorized"])

    def test_complete_static_configuration_passes_without_external_connection(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            environment, _, _ = WebRuntimeTests()._environment(root)
            scanner = root / "clamdscan"
            scanner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            scanner.chmod(0o700)
            environment["LAWCASE_WEB_CLAMAV_EXECUTABLE"] = str(scanner)
            report = assess_web_deployment_readiness(environ=environment)
        self.assertTrue(report.static_preflight_passed)
        self.assertFalse(report.as_dict()["release_authorized"])
        self.assertEqual(report.target, "commercial-production-web")
        self.assertTrue(all("password" not in check.message.lower() for check in report.checks))


if __name__ == "__main__":
    unittest.main()
