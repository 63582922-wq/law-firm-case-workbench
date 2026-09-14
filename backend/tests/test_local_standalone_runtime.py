from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from case_api.desktop_sidecar import create_desktop_sidecar_app
from case_api.local_standalone_app import create_local_standalone_app
from case_api.local_standalone_runtime import (
    LocalStandaloneAccessBlocked,
    NativeFolderSelectionRegistry,
    build_local_standalone_runtime,
)
from case_kernel.runtime import RuntimeConfigurationBlocked, RuntimeMode, RuntimeSettings, build_runtime_services


class LocalStandaloneRuntimeTests(unittest.TestCase):
    def _runtime(self, workspace: Path, parent_token: str):
        return build_local_standalone_runtime(
            environ={"CASE_WORKBENCH_LOCAL_WORKSPACE_ROOT": str(workspace)},
            parent_api_token=parent_token,
        )

    def _client(self, workspace: Path, parent_token: str) -> tuple[TestClient, object]:
        runtime = self._runtime(workspace, parent_token)
        return (
            TestClient(
                create_local_standalone_app(runtime, native_parent_api_token=parent_token),
                client=("127.0.0.1", 51001),
            ),
            runtime,
        )

    def _session_headers(self, client: TestClient, parent_token: str) -> dict[str, str]:
        response = client.post(
            "/v1/desktop-sessions/exchange",
            headers={
                "Origin": "tauri://localhost",
                "X-Desktop-Bootstrap": parent_token,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        grant = response.json()
        self.assertEqual(grant["status"], "SESSION_READY")
        return {
            "Origin": "tauri://localhost",
            "Authorization": f"Bearer {grant['access_token']}",
        }

    def test_native_choice_creates_case_and_real_inventory_without_persisting_absolute_path(self) -> None:
        token = "a" * 64
        with TemporaryDirectory(prefix="lawcase-local-standalone-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            materials = root / "周雅丽诉讼"
            materials.mkdir()
            (materials / "法院送达资料.pdf").write_bytes(b"not-a-real-pdf")
            (materials / "微信转账记录.txt").write_text("local fixture", encoding="utf-8")
            client, runtime = self._client(workspace, token)
            self.assertEqual(runtime.settings.mode, RuntimeMode.LOCAL_STANDALONE)
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/v1/native-model/matters/example").status_code, 404)

            parent_headers = {"Authorization": f"Bearer {token}"}
            selection = client.post(
                "/v1/native-local/folder-selections",
                headers=parent_headers,
                json={"selected_root": str(materials)},
            )
            self.assertEqual(selection.status_code, 200, selection.text)
            selected = selection.json()
            self.assertEqual(selected["display_name"], "周雅丽诉讼")
            self.assertNotIn(str(materials), selection.text)

            created = client.post(
                "/v1/native-local/cases",
                headers=parent_headers,
                json={"title": "周雅丽民间借贷应诉", "selection_id": selected["selection_id"]},
            )
            self.assertEqual(created.status_code, 201, created.text)
            case = created.json()
            self.assertEqual(case["stage"], "MATERIALS_PENDING")
            self.assertEqual(case["material_root"]["display_name"], "周雅丽诉讼")
            self.assertNotIn("selected_root", case)
            self.assertNotIn(str(materials), created.text)

            inventory = client.post(
                f"/v1/native-local/cases/{case['case_id']}/folder-inventory",
                headers=parent_headers,
                json={"selection_id": selected["selection_id"]},
            )
            self.assertEqual(inventory.status_code, 200, inventory.text)
            completed = inventory.json()
            self.assertEqual(completed["stage"], "MATERIALS_INVENTORIED")
            self.assertEqual(completed["inventory"]["total_files"], 2)
            self.assertNotIn(str(materials), inventory.text)

            session_headers = self._session_headers(client, token)
            listed = client.get("/v1/local-standalone/cases", headers=session_headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(len(listed.json()["cases"]), 1)
            scan_id = completed["inventory"]["scan_id"]
            page = client.get(
                f"/v1/local-standalone/cases/{case['case_id']}/folder-inventories/{scan_id}/items?limit=100&offset=0",
                headers=session_headers,
            )
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual([item["relative_path"] for item in page.json()["items"]], ["微信转账记录.txt", "法院送达资料.pdf"])

            database_bytes = runtime.store.database_path.read_bytes()
            self.assertNotIn(str(materials).encode("utf-8"), database_bytes)
            self.assertEqual(runtime.store.database_path.stat().st_mode & 0o077, 0)
            self.assertEqual(workspace.stat().st_mode & 0o077, 0)

    def test_local_case_survives_restart_but_folder_path_and_selection_do_not(self) -> None:
        first_token = "b" * 64
        second_token = "c" * 64
        with TemporaryDirectory(prefix="lawcase-local-restart-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            materials = root / "案件资料"
            materials.mkdir()
            first_client, _ = self._client(workspace, first_token)
            parent_headers = {"Authorization": f"Bearer {first_token}"}
            selection = first_client.post(
                "/v1/native-local/folder-selections",
                headers=parent_headers,
                json={"selected_root": str(materials)},
            ).json()
            created = first_client.post(
                "/v1/native-local/cases",
                headers=parent_headers,
                json={"title": "本机重启恢复案件", "selection_id": selection["selection_id"]},
            )
            self.assertEqual(created.status_code, 201, created.text)
            case_id = created.json()["case_id"]

            second_client, _ = self._client(workspace, second_token)
            second_parent_headers = {"Authorization": f"Bearer {second_token}"}
            listed = second_client.get("/v1/native-local/cases", headers=second_parent_headers)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()["cases"][0]["case_id"], case_id)
            self.assertEqual(listed.json()["cases"][0]["material_root"]["display_name"], "案件资料")
            self.assertNotIn(str(materials), listed.text)

            # A remembered case is not a remembered file permission.  The old
            # opaque native selection cannot trigger a read after restart.
            blocked = second_client.post(
                f"/v1/native-local/cases/{case_id}/folder-inventory",
                headers=second_parent_headers,
                json={"selection_id": selection["selection_id"]},
            )
            self.assertEqual(blocked.status_code, 403, blocked.text)

    def test_browser_cannot_submit_paths_or_bypass_native_parent_boundary(self) -> None:
        token = "d" * 64
        with TemporaryDirectory(prefix="lawcase-local-boundary-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            materials = root / "materials"
            materials.mkdir()
            client, _ = self._client(workspace, token)
            session_headers = self._session_headers(client, token)
            denied = client.post(
                "/v1/native-local/folder-selections",
                headers=session_headers,
                json={"selected_root": str(materials)},
            )
            self.assertEqual(denied.status_code, 404)
            denied_create = client.post(
                "/v1/native-local/cases",
                headers=session_headers,
                json={"title": "浏览器伪造案件", "selection_id": "11111111-1111-4111-8111-111111111111"},
            )
            self.assertEqual(denied_create.status_code, 404)
            denied_list = client.get(
                "/v1/native-local/cases",
                headers=session_headers,
            )
            self.assertEqual(denied_list.status_code, 404)
            denied_open = client.get(
                "/v1/native-local/cases/11111111-1111-4111-8111-111111111111",
                headers=session_headers,
            )
            self.assertEqual(denied_open.status_code, 404)
            denied_reconnect = client.post(
                "/v1/native-local/cases/11111111-1111-4111-8111-111111111111/material-root",
                headers=session_headers,
                json={"selection_id": "11111111-1111-4111-8111-111111111111"},
            )
            self.assertEqual(denied_reconnect.status_code, 404)
            denied_inventory = client.post(
                "/v1/native-local/cases/11111111-1111-4111-8111-111111111111/folder-inventory",
                headers=session_headers,
                json={"selection_id": "11111111-1111-4111-8111-111111111111"},
            )
            self.assertEqual(denied_inventory.status_code, 404)
            self.assertEqual(client.post("/v1/local-standalone/cases", headers=session_headers).status_code, 405)
            capabilities = client.get("/v1/local-standalone/capabilities", headers=session_headers)
            self.assertEqual(capabilities.status_code, 200, capabilities.text)
            self.assertEqual(capabilities.json()["external_model_execution"], "DISABLED")
            self.assertEqual(capabilities.json()["formal_facts_and_calculation"], "FIRM_MANAGED_REQUIRED")

    def test_folder_selection_rejects_broad_roots_and_expires_without_persisting_permission(self) -> None:
        with TemporaryDirectory(prefix="lawcase-local-selection-") as temporary:
            root = Path(temporary)
            materials = root / "materials"
            materials.mkdir()
            registry = NativeFolderSelectionRegistry(ttl=timedelta(seconds=1))
            with self.assertRaises(LocalStandaloneAccessBlocked):
                registry.register(Path("/"))
            with self.assertRaises(LocalStandaloneAccessBlocked):
                registry.register(Path.home())
            now = datetime.now(timezone.utc)
            selection = registry.register(materials, now=now)
            with self.assertRaisesRegex(LocalStandaloneAccessBlocked, "已过期"):
                registry.resolve(selection.selection_id, now=now + timedelta(seconds=2))

    def test_runtime_mode_is_separate_from_synthetic_and_firm_managed_composition(self) -> None:
        settings = RuntimeSettings.from_environment(
            {"CASE_WORKBENCH_RUNTIME_MODE": "local-standalone"}
        )
        self.assertEqual(settings.mode, RuntimeMode.LOCAL_STANDALONE)
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "isolated local workspace"):
            build_runtime_services(settings)
        with self.assertRaisesRegex(RuntimeConfigurationBlocked, "cannot be present"):
            RuntimeSettings.from_environment(
                {
                    "CASE_WORKBENCH_RUNTIME_MODE": "local-standalone",
                    "CASE_WORKBENCH_POSTGRES_DSN": "postgresql://localhost/lawcase_production",
                }
            )

    def test_desktop_sidecar_delegates_local_runtime_without_enrollment_or_postgres(self) -> None:
        token = "e" * 64
        with TemporaryDirectory(prefix="lawcase-local-sidecar-") as temporary:
            runtime = self._runtime(Path(temporary) / "workspace", token)
            client = TestClient(
                create_desktop_sidecar_app(
                    parent_api_token=token,
                    local_standalone_runtime=runtime,
                ),
                client=("127.0.0.1", 51002),
            )
            self.assertEqual(
                client.get("/healthz").json(),
                {
                    "service": "lawcase-local-api",
                    "mode": "local-standalone",
                    "persistence": "local-configured",
                    "external_network": "disabled",
                    "external_model_execution": "disabled",
                },
            )


if __name__ == "__main__":
    unittest.main()
