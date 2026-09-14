"""local_web 的 Agent 分析接入测试（触发、状态、报告、导出、降级）。

不发起真实模型调用：模型未配置时走降级路径，或显式禁用后台线程。
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
from case_kernel.lawyer_practical_mode import normalize_and_gate


def _pdf(text: str = "借款与还款约定 月利率 1.5%") -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(40, 700, text)
    document.showPage()
    document.save()
    return stream.getvalue()


class LocalWebAgentAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = LocalWebStore(self.root)
        self.client = TestClient(create_local_web_app(self.store))
        self.client.get("/api/local/v1/session")
        self.csrf = self.client.cookies["lawcase_local_csrf"]
        # 默认禁用到真实模型环境：本文件只验证接入契约与降级行为。
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("CASE_WORKBENCH_DISABLE_AGENT", "CASE_WORKBENCH_MODEL_ENV_FILE")
        }
        os.environ["CASE_WORKBENCH_DISABLE_AGENT"] = "1"
        # 关键：显式指向不存在的 env 文件，避免回退到仓库内真实 env 而发起真实调用。
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
        case = self.client.post("/api/local/v1/cases", json={"title": "（2026）测试民初1号"},
                                headers=self._headers("agent-case-1")).json()["case"]
        data = _pdf()
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads",
            json={"client_filename": "起诉状.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("agent-upload-1"),
        ).json()["upload"]
        self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads/{slot['upload_id']}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        return str(case["case_id"])

    def test_amendment_contract_keeps_deterministic_payload(self) -> None:
        response = self.client.post(f"/api/local/v1/cases/{self.case_id}/analysis",
                                    json={}, headers=self._headers("agent-run-1"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # 既有契约（确定性预处理）保持不变
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertIn("analysis", payload)
        # 新增 Agent 契约
        self.assertIn("agent", payload)
        self.assertIn(payload["agent"]["status"], {"DISABLED", "RUNNING"})

    def test_analysis_status_exposes_agent_fields(self) -> None:
        self.client.post(f"/api/local/v1/cases/{self.case_id}/analysis", json={},
                         headers=self._headers("agent-run-2"))
        payload = self.client.get(f"/api/local/v1/cases/{self.case_id}/analysis").json()
        agent = payload["agent"]
        for key in ("status", "progress", "stage", "gate_level", "cost_cny", "calls",
                    "error", "engine_numbers", "report_available"):
            self.assertIn(key, agent)
        self.assertIn("case_id", json.dumps({"case_id": self.case_id}))

    def test_report_and_export_require_existing_run(self) -> None:
        # 本代码库约定：LocalWebNotFound 继承 LocalWebBlocked → 统一映射 422。
        report = self.client.get(f"/api/local/v1/cases/{self.case_id}/analysis/report")
        self.assertEqual(report.status_code, 422)
        self.assertIn("尚无分析报告", report.text)
        export = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/analysis/export?format=md")
        self.assertEqual(export.status_code, 422)
        self.assertIn("无法导出", export.text)

    def test_export_rejects_unknown_format(self) -> None:
        self.client.post(f"/api/local/v1/cases/{self.case_id}/analysis", json={},
                         headers=self._headers("agent-run-3"))
        response = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/analysis/export?format=pdf")
        self.assertIn(response.status_code, (400, 422))

    def test_budget_out_of_range_is_rejected(self) -> None:
        response = self.client.post(f"/api/local/v1/cases/{self.case_id}/analysis",
                                    json={"budget_cny": 999}, headers=self._headers("agent-run-4"))
        self.assertEqual(response.status_code, 422)

    def test_export_md_and_docx_when_report_exists(self) -> None:
        # 直接放置一份报告文件并把路径写入运行记录，验证导出通道。
        run_dir = self.store._analysis_dir(self.case_id)
        report_path = run_dir / "决策包.md"
        report_path.write_text("# 决策包\n\n> 律师复核候选\n\n## 一、案情\n\n- 事实一\n",
                               encoding="utf-8")
        self.client.post(f"/api/local/v1/cases/{self.case_id}/analysis", json={},
                         headers=self._headers("agent-run-5"))
        self.store._set_agent_state(self.case_id, agent_status="COMPLETED",
                                    agent_report_path=str(report_path))

        md = self.client.get(f"/api/local/v1/cases/{self.case_id}/analysis/export?format=md")
        self.assertEqual(md.status_code, 200)
        self.assertIn("决策包", md.text)
        docx = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/analysis/export?format=docx")
        self.assertEqual(docx.status_code, 200)
        self.assertTrue(docx.content.startswith(b"PK"))  # DOCX 为 zip 容器
        report = self.client.get(f"/api/local/v1/cases/{self.case_id}/analysis/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("律师复核候选", report.text)

    def test_agent_runs_and_reports_model_not_configured(self) -> None:
        """无模型环境文件时：后台线程走降级路径并落到 MODEL_NOT_CONFIGURED。"""
        self.store2 = LocalWebStore(self.root / "second")
        client = TestClient(create_local_web_app(self.store2))
        client.get("/api/local/v1/session")
        csrf = client.cookies["lawcase_local_csrf"]
        case = client.post("/api/local/v1/cases", json={"title": "（2026）降级案"},
                           headers={"X-Lawcase-CSRF": csrf,
                                    "Content-Type": "application/json",
                                    "Idempotency-Key": "degrade-case-1"}).json()["case"]
        case_id = str(case["case_id"])
        data = _pdf()
        slot = client.post(
            f"/api/local/v1/cases/{case_id}/material-uploads",
            json={"client_filename": "起诉状.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 1},
            headers={"X-Lawcase-CSRF": csrf, "Content-Type": "application/json",
                     "Idempotency-Key": "degrade-upload-1"},
        ).json()["upload"]
        client.put(f"/api/local/v1/cases/{case_id}/material-uploads/{slot['upload_id']}/content",
                   content=data,
                   headers={"X-Lawcase-CSRF": csrf, "Content-Type": "application/pdf"})

        os.environ.pop("CASE_WORKBENCH_DISABLE_AGENT", None)  # 允许后台线程运行
        # env 文件仍指向不存在路径 → 线程应落到降级状态，不发起真实调用
        os.environ["CASE_WORKBENCH_MODEL_ENV_FILE"] = str(self.root / "no-such-model.env")
        client.post(f"/api/local/v1/cases/{case_id}/analysis", json={},
                    headers={"X-Lawcase-CSRF": csrf, "Content-Type": "application/json",
                             "Idempotency-Key": "degrade-run-1"})
        deadline = time.time() + 10
        status = ""
        while time.time() < deadline:
            status = client.get(f"/api/local/v1/cases/{case_id}/analysis").json()["agent"]["status"]
            if status not in ("RUNNING", "NOT_RUN"):
                break
            time.sleep(0.05)
        self.assertEqual(status, "MODEL_NOT_CONFIGURED")
        report = client.get(f"/api/local/v1/cases/{case_id}/analysis/report")
        self.assertEqual(report.status_code, 200)  # 降级报告仍可用


if __name__ == "__main__":
    unittest.main()

class StaleAnalysisTests(unittest.TestCase):
    """材料变化后，旧分析结果不得继续读取或导出（上游变化必须使下游失效）。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = LocalWebStore(self.root)
        self.client = TestClient(create_local_web_app(self.store))
        self.client.get("/api/local/v1/session")
        self.csrf = self.client.cookies["lawcase_local_csrf"]
        self._env = os.environ.get("CASE_WORKBENCH_DISABLE_AGENT")
        os.environ["CASE_WORKBENCH_DISABLE_AGENT"] = "1"

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("CASE_WORKBENCH_DISABLE_AGENT", None)
        else:
            os.environ["CASE_WORKBENCH_DISABLE_AGENT"] = self._env
        self.directory.cleanup()

    def _headers(self, key: str) -> dict[str, str]:
        return {"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/json",
                "Idempotency-Key": key}

    def test_stale_report_is_not_readable_or_exportable(self) -> None:
        case = self.client.post("/api/local/v1/cases", json={"title": "失效案"},
                                headers=self._headers("stale-case-1")).json()["case"]
        case_id = str(case["case_id"])
        data = _pdf()
        slot = self.client.post(
            f"/api/local/v1/cases/{case_id}/material-uploads",
            json={"client_filename": "起诉状.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("stale-up-1")).json()["upload"]
        self.client.put(f"/api/local/v1/cases/{case_id}/material-uploads/{slot['upload_id']}/content",
                        content=data, headers={"X-Lawcase-CSRF": self.csrf,
                                               "Content-Type": "application/pdf"})
        self.client.post(f"/api/local/v1/cases/{case_id}/analysis", json={},
                         headers=self._headers("stale-run-1"))
        run_dir = self.store._analysis_dir(case_id)
        report = run_dir / "决策包.md"
        report.write_text("# 旧报告\n", encoding="utf-8")
        self.store._set_agent_state(case_id, agent_status="COMPLETED",
                                    agent_report_path=str(report))

        # 材料变化 → 下游失效
        slot2 = self.client.post(
            f"/api/local/v1/cases/{case_id}/material-uploads",
            json={"client_filename": "补充材料.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 2},
            headers=self._headers("stale-up-2")).json()["upload"]
        self.client.put(f"/api/local/v1/cases/{case_id}/material-uploads/{slot2['upload_id']}/content",
                        content=data, headers={"X-Lawcase-CSRF": self.csrf,
                                               "Content-Type": "application/pdf"})

        state = self.client.get(f"/api/local/v1/cases/{case_id}/analysis").json()
        self.assertEqual(state["agent"]["status"], "STALE")
        self.assertFalse(state["agent"]["report_available"])
        self.assertEqual(self.client.get(f"/api/local/v1/cases/{case_id}/analysis/report").status_code, 422)
        self.assertEqual(
            self.client.get(f"/api/local/v1/cases/{case_id}/analysis/export?format=md").status_code, 422)
        self.assertIn("已失效",
                      self.client.get(f"/api/local/v1/cases/{case_id}/analysis/export?format=md").text)


class RedlineScopeTests(unittest.TestCase):
    """硬红线只拦"我方自称已完成"，不得误伤第三方语境。"""

    def test_third_party_submission_wording_does_not_block(self) -> None:
        raw = {"case_posture": {"summary": "原告已提交法院的证据材料存在矛盾。"}}
        analysis, gate = normalize_and_gate(raw, engine_amounts={})
        self.assertNotEqual(gate.level, "HARD_BLOCKED")
        self.assertIn("原告已提交法院", analysis["case_posture"]["summary"])
        self.assertTrue(any("已提交法院" in item for item in gate.review_items))

    def test_first_person_claim_still_blocks(self) -> None:
        for phrase in ("我已批准该方案", "我已提交法院", "本人已批准"):
            _, gate = normalize_and_gate({"case_posture": {"summary": phrase}},
                                         engine_amounts={})
            self.assertEqual(gate.level, "HARD_BLOCKED", phrase)

