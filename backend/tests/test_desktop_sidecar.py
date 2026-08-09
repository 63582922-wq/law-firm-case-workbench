from __future__ import annotations

from io import StringIO
import json
import unittest

from fastapi.testclient import TestClient

from case_api.desktop_sidecar import (
    DesktopSidecarBlocked,
    PROTOCOL,
    create_desktop_sidecar_app,
    read_parent_handshake,
)


class DesktopSidecarTests(unittest.TestCase):
    def test_strict_parent_handshake_accepts_only_expected_fields(self) -> None:
        payload = {"protocol": PROTOCOL, "challenge": "a" * 64, "parent_pid": 1234}
        self.assertEqual(
            read_parent_handshake(StringIO(json.dumps(payload) + "\n")),
            ("a" * 64, 1234),
        )
        for invalid in (
            "",
            "{}\n",
            json.dumps({**payload, "actor_id": "client-controlled"}) + "\n",
            json.dumps({**payload, "challenge": "short"}) + "\n",
            json.dumps({**payload, "parent_pid": True}) + "\n",
            "{" + "x" * 2048 + "\n",
        ):
            with self.subTest(invalid=invalid[:60]):
                with self.assertRaises(DesktopSidecarBlocked):
                    read_parent_handshake(StringIO(invalid))

    def test_disabled_health_exposes_no_case_docs_or_identity_routes(self) -> None:
        client = TestClient(create_desktop_sidecar_app())
        health = client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.json(),
            {
                "service": "lawcase-local-api",
                "mode": "desktop-disabled",
                "persistence": "not-configured",
                "identity": "not-enrolled",
            },
        )
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 404)
        self.assertEqual(client.get("/v1/matters/example/snapshot").status_code, 404)


if __name__ == "__main__":
    unittest.main()
