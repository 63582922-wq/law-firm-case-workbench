from __future__ import annotations

import subprocess
import sys
from time import monotonic
import unittest
from pathlib import Path

from scripts.manage_local_managed_test import (
    _MANAGED_BUILD_HEADROOM_BYTES,
    _bounded_run,
    _canonical_runtime_source_check,
    _compose_validation_allowed,
    _compose_status_read_failure_check,
    _managed_build_headroom_check,
    _runtime_python_source_overlays,
)


class LocalManagedCommandBoundaryTests(unittest.TestCase):
    def test_local_acceptance_worker_and_api_share_complete_packages(self) -> None:
        overlay = (
            Path(__file__).resolve().parents[2]
            / "deployment/local-managed-test/compose.sealed-recovery-overlay.yaml"
        ).read_text()
        api = overlay.split("\n  api:\n", 1)[1].split("\n  case-agent-worker:\n", 1)[0]
        worker = overlay.split("\n  case-agent-worker:\n", 1)[1]
        for package in ("case_kernel", "case_api"):
            binding = f"../../backend/{package}:/app/backend/{package}:ro"
            self.assertIn(binding, api)
            self.assertIn(binding, worker)
        self.assertNotIn("/case_kernel/official_source_private_store.py:", worker)
        self.assertNotIn("/case_kernel/legal_source_postgres.py:", worker)

    def test_worker_receives_all_visual_ocr_configuration(self) -> None:
        compose = (Path(__file__).resolve().parents[2] / "deployment/local-managed-test/compose.yaml").read_text()
        worker = compose.split("\n  case-agent-worker:\n", 1)[1].split("\n  gateway:\n", 1)[0]
        for binding in (
            "LAWCASE_AGENT_WORKER_QWEN_API_KEY: ${LAWCASE_AGENT_WORKER_QWEN_API_KEY}",
            "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID: ${LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID}",
            "LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE: /usr/bin/pdftoppm",
        ):
            self.assertIn(binding, worker)

    def test_full_rebuild_requires_five_gib_host_headroom_before_compose(self) -> None:
        blocked = _managed_build_headroom_check(
            free_bytes=_MANAGED_BUILD_HEADROOM_BYTES - 1
        )
        ready = _managed_build_headroom_check(
            free_bytes=_MANAGED_BUILD_HEADROOM_BYTES
        )
        self.assertFalse(blocked.ready)
        self.assertIn("未停止消费者", blocked.message)
        self.assertTrue(ready.ready)

    def test_low_headroom_skips_compose_validation_too(self) -> None:
        self.assertFalse(
            _compose_validation_allowed(
                docker_available=True,
                files_ready=True,
                headroom_ready=False,
            )
        )
        self.assertTrue(
            _compose_validation_allowed(
                docker_available=True,
                files_ready=True,
                headroom_ready=True,
            )
        )

    def test_canonical_compose_rejects_host_python_source_overlays(self) -> None:
        blocked = _canonical_runtime_source_check(
            compose_text=(
                "services:\n"
                "  api:\n"
                "    volumes:\n"
                "      - ../../backend/case_api/web_app.py:/app/backend/case_api/web_app.py:ro\n"
            )
        )
        ready = _canonical_runtime_source_check(
            compose_text="services:\n  api:\n    volumes:\n      - ca_bundle:/run/lawcase-ca:ro\n"
        )
        self.assertFalse(blocked.ready)
        self.assertIn("完整启动已拒绝", blocked.message)
        self.assertTrue(ready.ready)
        self.assertTrue(_canonical_runtime_source_check().ready)

    def test_runtime_source_overlay_reports_only_container_destinations(self) -> None:
        overlays = _runtime_python_source_overlays(
            {
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": "/host/private/path/web_app.py",
                        "Destination": "/app/backend/case_api/web_app.py",
                    },
                    {
                        "Type": "volume",
                        "Source": "lawcase-data",
                        "Destination": "/app/backend/case_api/web_runtime.py",
                    },
                    {
                        "Type": "bind",
                        "Source": "/host/private/path/config.py",
                        "Destination": "/opt/other/config.py",
                    },
                ]
            }
        )
        self.assertEqual(overlays, ("/app/backend/case_api/web_app.py",))

    def test_unavailable_docker_status_is_not_reported_as_stopped_services(self) -> None:
        timed_out = _compose_status_read_failure_check(124)
        failed = _compose_status_read_failure_check(1)
        self.assertFalse(timed_out.ready)
        self.assertIn("超时", timed_out.message)
        self.assertIn("无法判断", timed_out.message)
        self.assertFalse(failed.ready)
        self.assertIn("读取失败", failed.message)

    def test_infrastructure_diagnostic_cannot_wait_forever(self) -> None:
        started = monotonic()
        result = _bounded_run(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
            timeout_seconds=1,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 124)
        self.assertLess(monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
