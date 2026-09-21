from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "start_local_web",
    ROOT / "scripts" / "start_local_web.py",
)
assert SPEC is not None and SPEC.loader is not None
start_local_web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_local_web)


class _RunningProcess:
    @staticmethod
    def poll() -> None:
        return None


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format: str, *args: object) -> None:
        del args


class _FailingProxyHandler(BaseHTTPRequestHandler):
    request_count = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        type(self).request_count += 1
        self.send_response(502)
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        del args


class LocalWebLauncherTests(unittest.TestCase):
    def test_executable_resolution_accepts_path_command_and_absolute_file(self) -> None:
        with tempfile.NamedTemporaryFile() as executable:
            self.assertEqual(
                start_local_web._resolve_executable(executable.name),
                executable.name,
            )
        with patch.object(start_local_web.shutil, "which", return_value="/tools/pnpm") as which:
            self.assertEqual(start_local_web._resolve_executable("pnpm"), "/tools/pnpm")
        which.assert_called_once_with("pnpm")

    def test_readiness_probe_bypasses_configured_http_proxy(self) -> None:
        healthy = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _FailingProxyHandler)
        _FailingProxyHandler.request_count = 0
        threads = [
            threading.Thread(target=healthy.serve_forever, daemon=True),
            threading.Thread(target=proxy.serve_forever, daemon=True),
        ]
        for thread in threads:
            thread.start()
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        try:
            with patch.dict(
                os.environ,
                {
                    "http_proxy": proxy_url,
                    "HTTP_PROXY": proxy_url,
                    "https_proxy": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
                clear=False,
            ):
                ready = start_local_web._wait_for_url(
                    f"http://127.0.0.1:{healthy.server_address[1]}/healthz",
                    _RunningProcess(),
                    timeout_seconds=1,
                )
            self.assertTrue(ready)
            self.assertEqual(_FailingProxyHandler.request_count, 0)
        finally:
            healthy.shutdown()
            proxy.shutdown()
            healthy.server_close()
            proxy.server_close()


if __name__ == "__main__":
    unittest.main()
