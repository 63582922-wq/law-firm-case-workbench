from __future__ import annotations

import importlib.util
from pathlib import Path
from base64 import urlsafe_b64encode
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "manage_managed_web",
    ROOT / "scripts" / "manage_managed_web.py",
)
assert SPEC is not None and SPEC.loader is not None
manage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manage
SPEC.loader.exec_module(manage)


class ManagedWebAdminEntryTests(unittest.TestCase):
    def test_full_rebuild_requires_five_gib_host_headroom(self) -> None:
        blocked = manage._managed_build_headroom_check(
            free_bytes=manage._MANAGED_BUILD_HEADROOM_BYTES - 1
        )
        ready = manage._managed_build_headroom_check(
            free_bytes=manage._MANAGED_BUILD_HEADROOM_BYTES
        )
        self.assertFalse(blocked.ready)
        self.assertIn("不会停止旧容器", blocked.message)
        self.assertTrue(ready.ready)

    def test_missing_configuration_blocks_without_starting_compose(self) -> None:
        with TemporaryDirectory() as temporary, patch.object(manage.subprocess, "run") as run:
            checks = manage.preflight(Path(temporary) / ".env")
        self.assertFalse(all(item.ready for item in checks))
        run.assert_not_called()

    def test_init_creates_private_non_placeholder_local_secrets_without_starting(self) -> None:
        with TemporaryDirectory() as temporary, patch.object(manage.subprocess, "run") as run:
            path = Path(temporary) / ".env"
            result = manage.initialize_configuration(path)
            text = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777
        self.assertEqual(result, 0)
        self.assertNotIn("REPLACE_WITH_A_UNIQUE_32_CHARACTER_OR_LONGER_SECRET", text)
        self.assertNotIn("REPLACE_WITH_A_DIFFERENT_UNIQUE_32_CHARACTER_OR_LONGER_SECRET", text)
        self.assertNotIn(
            "REPLACE_WITH_UNPADDED_BASE64URL_OF_AT_LEAST_32_RANDOM_BYTES",
            text,
        )
        self.assertEqual(mode, 0o600)
        run.assert_not_called()

    def test_preflight_report_does_not_echo_configuration_values(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            secret = "do-not-print-this-secret-value"
            path.write_text(f"LAWCASE_WEB_RUNTIME_MODE=SETUP_GATED\nSECRET={secret}\n", encoding="utf-8")
            checks = manage.preflight(path)
        self.assertNotIn(secret, str(checks))
        self.assertTrue(any(item.name == "运行方式" and not item.ready for item in checks))

    def test_start_is_fail_closed_before_compose_when_required_sections_are_missing(self) -> None:
        with TemporaryDirectory() as temporary, patch.object(manage, "compose") as compose:
            path = Path(temporary) / ".env"
            path.write_text("LAWCASE_WEB_RUNTIME_MODE=PRODUCTION_WEB\n", encoding="utf-8")
            result = manage.start_managed_web(path)
        self.assertEqual(result, 2)
        compose.assert_not_called()

    def test_document_renderer_preflight_is_optional_but_strict_when_enabled(self) -> None:
        disabled = manage._document_renderer_check(
            {"LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED": "false"}
        )
        self.assertTrue(disabled.ready)

        invalid = manage._document_renderer_check(
            {"LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED": "true"}
        )
        self.assertFalse(invalid.ready)

        secret = urlsafe_b64encode(b"r" * 32).decode("ascii").rstrip("=")
        ready = manage._document_renderer_check(
            {
                "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED": "true",
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT": (
                    "http://document-renderer:8090"
                ),
                "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET": secret,
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                "LAWCASE_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                "LAWCASE_DOCUMENT_RENDERER_MAX_CLOCK_SKEW_SECONDS": "60",
                "LAWCASE_DOCUMENT_RENDERER_REPLAY_CAPACITY": "20000",
            }
        )
        self.assertTrue(ready.ready)

        web_ready = manage._document_renderer_check(
            {
                "LAWCASE_WEB_DOCUMENT_WORKER_ENABLED": "true",
                "LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT": (
                    "http://document-renderer:8090"
                ),
                "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET": secret,
                "LAWCASE_WEB_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                "LAWCASE_DOCUMENT_RENDERER_TIMEOUT_SECONDS": "180",
                "LAWCASE_DOCUMENT_RENDERER_MAX_CLOCK_SKEW_SECONDS": "60",
                "LAWCASE_DOCUMENT_RENDERER_REPLAY_CAPACITY": "20000",
            }
        )
        self.assertTrue(web_ready.ready)

    def test_start_enables_renderer_profile_for_agent_or_web_document_delivery(self) -> None:
        ready_checks = (
            manage.AdminCheck("管理员配置", True, "ready"),
            manage.AdminCheck("商用放行", False, "not a release claim"),
        )
        for agent_enabled, web_enabled, expected_profiles in (
            ("false", "false", ("agent-worker",)),
            ("true", "false", ("agent-worker", "document-delivery")),
            ("false", "true", ("agent-worker", "document-delivery")),
        ):
            with self.subTest(agent_enabled=agent_enabled, web_enabled=web_enabled), TemporaryDirectory() as temporary:
                path = Path(temporary) / ".env"
                path.write_text(
                    "\n".join((
                        f"LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED={agent_enabled}",
                        f"LAWCASE_WEB_DOCUMENT_WORKER_ENABLED={web_enabled}",
                    )) + "\n",
                    encoding="utf-8",
                )
                with (
                    patch.object(manage, "preflight", return_value=ready_checks),
                    patch.object(manage, "compose", return_value=0) as compose,
                ):
                    result = manage.start_managed_web(path)
                self.assertEqual(result, 0)
                compose.assert_called_once_with(
                    path,
                    "up",
                    "--build",
                    "-d",
                    profiles=expected_profiles,
                )

    def test_commercial_release_gate_does_not_falsely_fail_startup_preflight(self) -> None:
        self.assertTrue(
            manage._ready_to_start(
                (
                    manage.AdminCheck("容器编排", True, "ready"),
                    manage.AdminCheck("商用放行", False, "separate acceptance"),
                )
            )
        )
        self.assertFalse(
            manage._ready_to_start(
                (
                    manage.AdminCheck("容器编排", False, "blocked"),
                    manage.AdminCheck("商用放行", False, "separate acceptance"),
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
