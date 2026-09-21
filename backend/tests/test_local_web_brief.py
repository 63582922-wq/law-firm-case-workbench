"""local_web 答辩状草稿接入测试（选择保存、生成、失效、导出）。

不发起真实模型调用：模型未配置时走确定性骨架，或显式禁用后台线程。
"""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from case_api.local_web import LocalWebStore, create_local_web_app


def _pdf() -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(40, 700, "借款 30000 元 约定月利率 1.5%")
    document.showPage()
    document.save()
    return stream.getvalue()


class LocalWebBriefTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = LocalWebStore(self.root)
        self.client = TestClient(create_local_web_app(self.store))
        self.client.get("/api/local/v1/session")
        self.csrf = self.client.cookies["lawcase_local_csrf"]
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("CASE_WORKBENCH_DISABLE_AGENT", "CASE_WORKBENCH_MODEL_ENV_FILE")
        }
        os.environ["CASE_WORKBENCH_DISABLE_AGENT"] = "1"
        os.environ["CASE_WORKBENCH_MODEL_ENV_FILE"] = str(self.root / "no-such-model.env")
        self.case_id = self._create_case_with_material()

    def tearDown(self) -> None:
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()

    def _headers(self, key: str) -> dict[str, str]:
        return {"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/json",
                "Idempotency-Key": key}

    def _create_case_with_material(self) -> str:
        case = self.client.post("/api/local/v1/cases", json={"title": "（2026）测试民初2号"},
                                headers=self._headers("brief-case-1")).json()["case"]
        data = _pdf()
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads",
            json={"client_filename": "起诉状.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("brief-upload-1"),
        ).json()["upload"]
        self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads/{slot['upload_id']}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        return str(case["case_id"])

    def _selections(self) -> dict:
        return {
            "respondent": "测试乙", "claimant": "测试甲", "court": "合成法院",
            "case_number": "（2026）测试民初2号",
            "grounds": {"cap": True, "lawyer_fee": True},
            "stances": {"principal": "部分认可", "interest": "不认可",
                        "lawyer_fee": "不认可", "costs": "不认可"},
            "authorities": ["《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"],
            "notes": "",
        }

    def test_brief_starts_not_run_with_empty_selections(self) -> None:
        payload = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()
        self.assertEqual(payload["state"]["status"], "NOT_RUN")
        self.assertFalse(payload["state"]["markdown_available"])
        self.assertEqual(payload["selections"]["respondent"], "")

    def test_selections_round_trip_and_unknown_keys_rejected(self) -> None:
        saved = self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                                json={"selections": self._selections()},
                                headers=self._headers("brief-save-1"))
        self.assertEqual(saved.status_code, 200)
        payload = saved.json()
        self.assertTrue(payload["selections"]["grounds"]["cap"])
        self.assertFalse(payload["selections"]["grounds"]["offset"])
        self.assertEqual(payload["selections"]["stances"]["interest"], "不认可")
        again = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()
        self.assertEqual(again["selections"], payload["selections"])

        bad = self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                              json={"selections": self._selections(), "extra": 1},
                              headers=self._headers("brief-save-2"))
        self.assertEqual(bad.status_code, 422)

    def test_generation_without_model_produces_skeleton_and_export(self) -> None:
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": self._selections()},
                        headers=self._headers("brief-save-3"))
        response = self.client.post(f"/api/local/v1/cases/{self.case_id}/brief/generate",
                                    json={}, headers=self._headers("brief-gen-1"))
        self.assertEqual(response.status_code, 200)
        deadline = time.time() + 10
        state = {}
        while time.time() < deadline:
            state = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()["state"]
            if state["status"] not in ("RUNNING", "NOT_RUN"):
                break
            time.sleep(0.05)
        # 后台线程在本环境被禁用 → DISABLED；显式放行后应落到 MODEL_NOT_CONFIGURED
        self.assertIn(state["status"], ("DISABLED", "MODEL_NOT_CONFIGURED"))
        if state["status"] == "MODEL_NOT_CONFIGURED":
            self.assertTrue(state["markdown_available"])
            exported = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief/export?format=md")
            self.assertEqual(exported.status_code, 200)
            self.assertIn("民事答辩状", exported.text)
            docx = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief/export?format=docx")
            self.assertEqual(docx.status_code, 200)
            self.assertTrue(docx.content.startswith(b"PK"))

    def test_first_generation_binds_version_and_run_id(self) -> None:
        """直接生成（没有先保存选择）时，草稿也必须绑定版本/参数/分析运行。"""
        os.environ.pop("CASE_WORKBENCH_DISABLE_AGENT", None)
        response = self.client.post(f"/api/local/v1/cases/{self.case_id}/brief/generate",
                                    json={}, headers=self._headers("brief-gen-fresh"))
        self.assertEqual(response.status_code, 200)
        deadline = time.time() + 10
        state = {}
        while time.time() < deadline:
            state = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()["state"]
            if state["status"] not in ("RUNNING", "NOT_RUN"):
                break
            time.sleep(0.05)
        # 本环境没有模型环境文件 → 确定性骨架；关键是**不能是 STALE**
        self.assertEqual(state["status"], "MODEL_NOT_CONFIGURED")
        self.assertFalse(state["stale"])
        self.assertTrue(state["markdown_available"])
        os.environ["CASE_WORKBENCH_DISABLE_AGENT"] = "1"

    def test_changing_selections_invalidates_existing_draft(self) -> None:
        run_dir = self.store._analysis_dir(self.case_id)
        draft = run_dir / "答辩状草稿.md"
        draft.write_text("# 民事答辩状（草稿）\n\n> 草稿\n", encoding="utf-8")
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": self._selections()},
                        headers=self._headers("brief-save-4"))
        self.store._set_brief_state(
            self.case_id, status="COMPLETED", markdown_path=str(draft),
            gate_level="MARK_FOR_REVIEW",
            source_version=int(self.store._case(self.case_id)["version"]),
            config_hash=self.store._config_hash(self.case_id),
            analysis_run_id=self.store._analysis_run_id(self.case_id),
        )
        self.assertEqual(
            self.client.get(f"/api/local/v1/cases/{self.case_id}/brief/export?format=md").status_code,
            200)

        changed = self._selections()
        changed["stances"] = {**changed["stances"], "interest": "认可"}
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": changed}, headers=self._headers("brief-save-5"))
        state = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()["state"]
        self.assertEqual(state["status"], "STALE")
        self.assertFalse(state["markdown_available"])
        blocked = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief/export?format=md")
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("已失效", blocked.json()["message"])

    def test_material_change_makes_draft_stale(self) -> None:
        run_dir = self.store._analysis_dir(self.case_id)
        draft = run_dir / "答辩状草稿.md"
        draft.write_text("# 民事答辩状（草稿）\n", encoding="utf-8")
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": self._selections()},
                        headers=self._headers("brief-save-6"))
        self.store._set_brief_state(self.case_id, status="COMPLETED",
                                    markdown_path=str(draft),
                                    source_version=int(self.store._case(self.case_id)["version"]))

        data = _pdf()
        case = self.store._case(self.case_id)
        slot = self.client.post(
            f"/api/local/v1/cases/{self.case_id}/material-uploads",
            json={"client_filename": "补充材料.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": int(case["version"])},
            headers=self._headers("brief-upload-2"),
        ).json()["upload"]
        self.client.put(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{slot['upload_id']}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        state = self.client.get(f"/api/local/v1/cases/{self.case_id}/brief").json()["state"]
        self.assertEqual(state["status"], "STALE")
        self.assertTrue(state["stale"])

    def test_budget_out_of_range_is_rejected(self) -> None:
        rejected = self.client.post(f"/api/local/v1/cases/{self.case_id}/brief/generate",
                                    json={"budget_cny": 999},
                                    headers=self._headers("brief-gen-2"))
        self.assertEqual(rejected.status_code, 422)


if __name__ == "__main__":
    unittest.main()
