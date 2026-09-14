from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from case_api.local_web import LocalWebStore, create_local_web_app


def _pdf(text: str = "local web test") -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(40, 700, text)
    document.showPage()
    document.save()
    return stream.getvalue()


class LocalWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.client = TestClient(create_local_web_app(LocalWebStore(Path(self.directory.name))))
        self.client.get("/api/local/v1/session")
        self.csrf = self.client.cookies["lawcase_local_csrf"]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _headers(self, key: str) -> dict[str, str]:
        return {"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/json", "Idempotency-Key": key}

    def test_local_path_never_needed_for_case_and_pdf_flow(self) -> None:
        case_response = self.client.post("/api/local/v1/cases", json={"title": "本地案件"}, headers=self._headers("case-local-1"))
        self.assertEqual(case_response.status_code, 201)
        case = case_response.json()["case"]
        data = _pdf()
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads",
            json={"client_filename": "材料.pdf", "content_length": len(data), "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("upload-local-1"),
        )
        self.assertEqual(slot.status_code, 201)
        upload_id = slot.json()["upload"]["upload_id"]
        receipt = self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads/{upload_id}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()["receipt"]["page_count"], 1)
        self.assertEqual(self.client.get(f"/api/local/v1/cases/{case['case_id']}/evidence-pages").json()["total_count"], 1)

    def test_writes_require_local_csrf_and_idempotency_replay_is_stable(self) -> None:
        response = self.client.post("/api/local/v1/cases", json={"title": "不应写入"}, headers={"Content-Type": "application/json", "Idempotency-Key": "case-local-2"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/local/v1/cases", json={"title": "可重放案件"}, headers=self._headers("case-local-3"))
        self.assertEqual(response.status_code, 201)
        replay = self.client.post("/api/local/v1/cases", json={"title": "可重放案件"}, headers=self._headers("case-local-3"))
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json(), response.json())

    def test_zip_is_stored_pending_processing_after_safe_checks(self) -> None:
        case = self.client.post("/api/local/v1/cases", json={"title": "ZIP 案件"}, headers=self._headers("case-local-4")).json()["case"]
        archive = BytesIO()
        with ZipFile(archive, "w") as container:
            container.writestr("材料说明.txt", "只读材料")
        content = archive.getvalue()
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-archives",
            json={"client_filename": "材料.zip", "content_length": len(content), "content_type": "application/zip", "expected_version": 1},
            headers=self._headers("archive-local-1"),
        )
        self.assertEqual(slot.status_code, 201)
        archive_id = slot.json()["upload"]["archive_id"]
        receipt = self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-archives/{archive_id}/content",
            content=content,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/zip"},
        )
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()["receipt"]["processing_status"], "STORED_PENDING_PROCESSING")

    def test_material_preprocessing_candidates_are_source_page_bound_and_stale_after_write(self) -> None:
        case = self.client.post(
            "/api/local/v1/cases",
            json={"title": "预处理案件"},
            headers=self._headers("case-local-analysis"),
        ).json()["case"]
        data = _pdf("repayment 2020-08-20 CNY 1200")
        slot = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads",
            json={"client_filename": "repayment.pdf", "content_length": len(data), "content_type": "application/pdf", "expected_version": 1},
            headers=self._headers("upload-analysis-1"),
        ).json()["upload"]
        self.client.put(
            f"/api/local/v1/cases/{case['case_id']}/material-uploads/{slot['upload_id']}/content",
            content=data,
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/pdf"},
        )

        result = self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/analysis",
            json={},
            headers=self._headers("analysis-local-1"),
        )
        self.assertEqual(result.status_code, 200)
        candidate = result.json()["analysis"]["candidates"][0]
        page = self.client.get(f"/api/local/v1/cases/{case['case_id']}/evidence-pages").json()["items"][0]
        self.assertEqual(candidate["evidence_page_id"], page["evidence_page_id"])
        self.assertEqual(len(candidate["source_sha256"]), 64)

        self.client.post(
            f"/api/local/v1/cases/{case['case_id']}/evidence-pages/{page['evidence_page_id']}/decisions",
            json={"expected_version": 2, "disposition": "INCLUDE", "reason": "律师核对"},
            headers={"X-Lawcase-CSRF": self.csrf, "Content-Type": "application/json"},
        )
        stale = self.client.get(f"/api/local/v1/cases/{case['case_id']}/analysis")
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.json(), {"status": "STALE", "analysis": None})


if __name__ == "__main__":
    unittest.main()
