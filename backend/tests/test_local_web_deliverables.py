"""local_web 交付清单与应诉材料包接入测试。"""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from case_api.local_web import LocalWebStore, create_local_web_app
from case_kernel.matter_deliverables import CATALOGUE_BY_ID


def _pdf(name: str) -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(40, 700, name)
    document.showPage()
    document.save()
    return stream.getvalue()


class LocalWebDeliverablesTest(unittest.TestCase):
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
        case = self.client.post("/api/local/v1/cases", json={"title": "（2026）测试民初3号"},
                                headers=self._headers("dev-case-1")).json()["case"]
        data = _pdf("买卖合同纠纷 送货单")
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads",
            json={"client_filename": "起诉状.pdf", "content_length": len(data),
                  "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("dev-upload-1"),
        ).json()["upload"]
        self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads/{slot['upload_id']}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        return str(case["case_id"])

    def _parties(self) -> dict:
        return {
            "respondent": "测试乙", "respondent_address": "合成市测试路1号",
            "respondent_phone": "13800000000", "claimant": "测试合成木业有限公司",
            "court": "合成市测试区人民法院", "case_number": "（2026）测试民初3号",
            "cause": "买卖合同纠纷", "lawyer": "测试律师", "law_firm": "测试律师事务所",
        }

    def test_catalogue_covers_signature_items(self) -> None:
        payload = self.client.get(f"/api/local/v1/cases/{self.case_id}/deliverables").json()
        ids = {item["item_id"] for item in payload["catalogue"]}
        self.assertIn("authorisation", ids)
        self.assertIn("answer", ids)
        signature_items = {item["item_id"] for item in payload["catalogue"]
                           if item["needs_client_signature"]}
        self.assertIn("authorisation", signature_items)
        self.assertIn("answer", signature_items)
        self.assertEqual(payload["states"], {})

    def test_save_and_read_back_parties_and_states(self) -> None:
        saved = self.client.put(
            f"/api/local/v1/cases/{self.case_id}/deliverables",
            json={"parties": self._parties(),
                  "states": {"answer": "待当事人签字", "evidence_list": "起草中",
                             "no_such_item": "已提交", "authorisation": "乱写"}},
            headers=self._headers("dev-put-1"),
        )
        self.assertEqual(saved.status_code, 200)
        payload = saved.json()
        self.assertEqual(payload["parties"]["respondent"], "测试乙")
        self.assertEqual(payload["states"], {"answer": "待当事人签字", "evidence_list": "起草中",
                                            "authorisation": "未开始"})
        again = self.client.get(f"/api/local/v1/cases/{self.case_id}/deliverables").json()
        self.assertEqual(again["parties"], payload["parties"])

        rejected = self.client.put(
            f"/api/local/v1/cases/{self.case_id}/deliverables",
            json={"parties": self._parties(), "states": {}, "extra": 1},
            headers=self._headers("dev-put-2"),
        )
        self.assertEqual(rejected.status_code, 422)

    def test_template_route_renders_only_known_items(self) -> None:
        self.client.put(f"/api/local/v1/cases/{self.case_id}/deliverables",
                        json={"parties": self._parties(), "states": {}},
                        headers=self._headers("dev-put-3"))
        for item_id in ("authorisation", "service_address", "statement", "mediation",
                        "evidence_source"):
            response = self.client.get(
                f"/api/local/v1/cases/{self.case_id}/deliverables/template/{item_id}")
            self.assertEqual(response.status_code, 200, item_id)
            self.assertIn("签名", response.json()["markdown"], item_id)   # 需当事人签字
        evidence_list = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/deliverables/template/evidence_list")
        self.assertEqual(evidence_list.status_code, 200)
        self.assertIn("证明内容", evidence_list.json()["markdown"])        # 律师署名文件
        # 本机模式统一用 422 表达"该操作/资源不可用"（与报告、导出等路径一致）
        missing = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/deliverables/template/answer")
        self.assertEqual(missing.status_code, 422)
        self.assertIn("没有可用模板", missing.json()["message"])

    def test_export_contains_checklist_signature_docs_and_evidence_list(self) -> None:
        self.client.put(f"/api/local/v1/cases/{self.case_id}/deliverables",
                        json={"parties": self._parties(),
                              "states": {"answer": "未开始"}},
                        headers=self._headers("dev-put-4"))
        exported = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/deliverables/export?format=md")
        self.assertEqual(exported.status_code, 200)
        text = exported.text
        for marker in ("交付清单", "授权委托书", "送达地址确认书", "当事人陈述",
                       "调解意见确认", "证据目录", "证据来源说明", "提交前检查清单"):
            self.assertIn(marker, text)
        self.assertIn("起诉状.pdf", text)          # 证据目录自动列出本案材料
        self.assertIn("尚未生成答辩状草稿", text)
        docx = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/deliverables/export?format=docx")
        self.assertEqual(docx.status_code, 200)
        self.assertTrue(docx.content.startswith(b"PK"))

    def test_export_includes_answer_draft_when_present(self) -> None:
        draft = self.store._analysis_dir(self.case_id) / "答辩状草稿.md"
        draft.write_text("# 民事答辩状（草稿）\n\n答辩请求：驳回原告全部诉请。\n", encoding="utf-8")
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": {"respondent": "测试乙", "grounds": {}, "stances": {},
                                             "authorities": []}},
                        headers=self._headers("dev-brief-1"))
        self.store._set_brief_state(
            self.case_id, status="COMPLETED", markdown_path=str(draft),
            source_version=int(self.store._case(self.case_id)["version"]),
            config_hash=self.store._config_hash(self.case_id),
            analysis_run_id=self.store._analysis_run_id(self.case_id),
        )
        exported = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/deliverables/export?format=md")
        self.assertIn("附：民事答辩状（草稿）", exported.text)
        self.assertIn("驳回原告全部诉请", exported.text)

    def test_parties_prefilled_from_brief_selections(self) -> None:
        self.client.put(f"/api/local/v1/cases/{self.case_id}/brief",
                        json={"selections": {"respondent": "来自答辩状", "claimant": "对方",
                                             "court": "某法院", "case_number": "（2026）某号",
                                             "grounds": {}, "stances": {}, "authorities": []}},
                        headers=self._headers("dev-brief-2"))
        payload = self.client.get(f"/api/local/v1/cases/{self.case_id}/deliverables").json()
        self.assertEqual(payload["parties"]["respondent"], "来自答辩状")
        self.assertEqual(payload["parties"]["court"], "某法院")
        self.assertIn("answer", CATALOGUE_BY_ID)


if __name__ == "__main__":
    unittest.main()
