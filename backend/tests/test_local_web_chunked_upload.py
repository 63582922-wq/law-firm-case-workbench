"""分片上传：大文件经反向代理不被超时截断，且偏移不符绝不拼接。

背景（真实缺陷）：27 MB / 43 页的法院证据 PDF 经 `next dev` 的 rewrite 代理上传时
被 30 秒超时截断（HTTP 500），而直连 API 只需 0.15 秒。批量上传 23 份里这一份
永远失败。分片上传让每片都在超时窗口内完成，并支持断点续传。
"""

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


def _pdf_bytes(pages: int = 3, filler: bytes = b"") -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    for index in range(pages):
        document.drawString(40, 700, f"第 {index + 1} 页 买卖合同 货款")
        document.showPage()
    document.save()
    return stream.getvalue() + filler


class ChunkedUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = LocalWebStore(self.root)
        self.client = TestClient(create_local_web_app(self.store))
        self.client.get("/api/local/v1/session")
        self.csrf = self.client.cookies["lawcase_local_csrf"]
        self._slot_counter = 0
        case = self.client.post("/api/local/v1/cases", json={"title": "（2026）分片测试案"},
                                headers=self._headers("chunk-case-1")).json()["case"]
        self.case_id = str(case["case_id"])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _headers(self, key: str) -> dict[str, str]:
        return {"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/json",
                "Idempotency-Key": key}

    def _next_slot_key(self) -> str:
        self._slot_counter += 1
        return f"chunk-slot-{self._slot_counter}"

    def _slot(self, name: str = "证据.pdf", version: int = 1) -> str:
        response = self.client.post(
            f"/api/local/v1/cases/{self.case_id}/material-uploads",
            json={"client_filename": name, "content_length": 0,
                  "content_type": "application/pdf", "expected_version": version},
            headers=self._headers(self._next_slot_key()),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["upload"]["upload_id"])

    def _put_chunk(self, upload_id: str, offset: int, data: bytes):
        return self.client.put(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/chunks",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf",
                     "X-Chunk-Offset": str(offset)},
        )

    def test_chunked_upload_then_finalize_produces_receipt(self) -> None:
        upload_id = self._slot()
        payload = _pdf_bytes(pages=5)
        chunk_size = max(1, len(payload) // 4)
        offset = 0
        while offset < len(payload):
            piece = payload[offset:offset + chunk_size]
            response = self._put_chunk(upload_id, offset, piece)
            self.assertEqual(response.status_code, 200, response.text)
            offset = int(response.json()["offset"])
        self.assertEqual(self.store.staged_size(self.case_id, upload_id), len(payload))

        finalized = self.client.post(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/finalize",
            headers={"X-Lawcase-CSRF": self.csrf},
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        receipt = finalized.json()["receipt"]
        self.assertEqual(receipt["page_count"], 5)
        self.assertEqual(receipt["display_name"], "证据.pdf")

        summary = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/evidence-summary").json()["summary"]
        self.assertEqual(len(summary["original_files"]), 1)
        self.assertEqual(summary["total_pages"], 5)

    def test_offset_mismatch_is_rejected_instead_of_concatenating(self) -> None:
        upload_id = self._slot()
        first = self._put_chunk(upload_id, 0, b"%PDF-1.4 first")
        self.assertEqual(first.status_code, 200)
        wrong = self._put_chunk(upload_id, 999, b"second")
        self.assertIn(wrong.status_code, (409, 422))
        self.assertIn("偏移", wrong.json()["message"])
        # 服务端内容未被污染，仍可从中断处继续
        offset = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/offset").json()["offset"]
        self.assertEqual(offset, len(b"%PDF-1.4 first"))

    def test_resume_from_reported_offset(self) -> None:
        upload_id = self._slot()
        payload = _pdf_bytes(pages=2)
        half = len(payload) // 2
        self._put_chunk(upload_id, 0, payload[:half])
        reported = self.client.get(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/offset").json()["offset"]
        self.assertEqual(reported, half)
        self.assertEqual(self._put_chunk(upload_id, reported, payload[half:]).status_code, 200)
        finalized = self.client.post(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/finalize",
            headers={"X-Lawcase-CSRF": self.csrf},
        )
        self.assertEqual(finalized.json()["receipt"]["page_count"], 2)

    def test_finalize_without_content_is_rejected(self) -> None:
        upload_id = self._slot()
        response = self.client.post(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/finalize",
            headers={"X-Lawcase-CSRF": self.csrf},
        )
        self.assertIn(response.status_code, (409, 422))
        self.assertIn("尚未收到", response.json()["message"])

    def test_chunk_requires_csrf_and_offset(self) -> None:
        upload_id = self._slot()
        self.assertEqual(self._put_chunk(upload_id, 0, b"data").status_code, 200)
        version = int(self.store._case(self.case_id)["version"])   # 上一个分片不改变版本
        upload_id2 = self._slot(name="证据2.pdf", version=version)
        missing_offset = self.client.put(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id2}/chunks",
            content=b"data",
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        self.assertEqual(missing_offset.status_code, 422)
        no_csrf = self.client.put(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id2}/chunks",
            content=b"data",
            headers={"Content-Type": "application/pdf", "X-Chunk-Offset": "0"},
        )
        self.assertIn(no_csrf.status_code, (403, 422))

    def test_single_put_still_works_for_small_files(self) -> None:
        """整份 PUT 通道保留（脚本与测试仍在用），两条通道产出同一回执。"""
        upload_id = self._slot(name="整份.pdf")
        payload = _pdf_bytes(pages=1)
        response = self.client.put(
            f"/api/local/v1/cases/{self.case_id}/material-uploads/{upload_id}/content",
            content=payload,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["receipt"]["page_count"], 1)
        self.assertEqual(response.json()["receipt"]["display_name"], "整份.pdf")


if __name__ == "__main__":
    unittest.main()
