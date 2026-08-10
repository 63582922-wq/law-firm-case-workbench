from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen import canvas

from case_kernel.local_access_grants import AuthorizedOriginalFile
from case_kernel.pdf_reading_worker import PdfReadingBlocked, read_authorized_pdf_document


class PdfReadingWorkerTests(unittest.TestCase):
    def _source(self, path: Path) -> AuthorizedOriginalFile:
        return AuthorizedOriginalFile(path.name, path, path.stat().st_size, sha256(path.read_bytes()).hexdigest())

    def _pdf(self, path: Path, text: str) -> None:
        document = canvas.Canvas(str(path))
        document.drawString(72, 720, text)
        document.save()

    def test_reads_page_text_without_changing_authorized_pdf(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "微信流水.pdf"
            self._pdf(path, "已付利息 300 元")
            before = path.read_bytes()
            result = read_authorized_pdf_document(self._source(path))
            self.assertEqual(result.source_sha256, sha256(before).hexdigest())
            self.assertEqual(len(result.pages), 1)
            self.assertIn("300", result.pages[0].text)
            self.assertEqual(len(result.pages[0].text_sha256), 64)
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_non_pdf_even_when_presented_as_an_authorized_handle(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-a-pdf.pdf"
            path.write_text("not pdf", encoding="utf-8")
            with self.assertRaisesRegex(PdfReadingBlocked, "not a PDF"):
                read_authorized_pdf_document(self._source(path))


if __name__ == "__main__":
    unittest.main()
