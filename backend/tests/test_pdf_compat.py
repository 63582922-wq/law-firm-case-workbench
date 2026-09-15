"""PDF 兼容层测试：pypdf 打不开的真实案卷 PDF 也要能收进来。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from case_kernel.pdf_compat import (
    PdfUnreadable,
    find_tool,
    pdf_page_count,
    pdf_page_texts,
    render_single_page_png,
)

REAL_CASE_PDF = Path.home() / "Downloads/合成木业/原告证据1微信聊天记录.pdf"


def _synthetic_pdf(path: Path, pages: int = 2) -> Path:
    from reportlab.pdfgen import canvas

    document = canvas.Canvas(str(path))
    for index in range(pages):
        document.drawString(40, 700, f"第 {index + 1} 页 借款 30000 元")
        document.showPage()
    document.save()
    return path


class PdfCompatTests(unittest.TestCase):
    def test_page_count_and_texts_for_standard_pdf(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            path = _synthetic_pdf(Path(tmp) / "ok.pdf", pages=3)
            count, backend = pdf_page_count(path)
            self.assertEqual(count, 3)
            self.assertEqual(backend, "pypdf")
            texts, backend = pdf_page_texts(path)
            self.assertIsNotNone(texts)
            self.assertEqual(len(texts or []), 3)
            self.assertIn("30000", " ".join(texts or []))

    def test_missing_file_is_reported_not_treated_as_empty(self) -> None:
        with self.assertRaises(PdfUnreadable):
            pdf_page_count("/tmp/definitely-not-a-real-file-9f2a.pdf")

    def test_pypdf_failure_falls_back_to_poppler(self) -> None:
        """pypdf 抛错时改用 pdfinfo 读页数；文本层按无文本层处理（不假装读过）。"""
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        if find_tool("pdfinfo") is None:
            self.skipTest("本机没有 pdfinfo")
        with TemporaryDirectory() as tmp:
            path = _synthetic_pdf(Path(tmp) / "source.pdf", pages=4)
            with patch("pypdf.PdfReader", side_effect=RuntimeError("模拟损坏")):
                count, backend = pdf_page_count(path)
                self.assertEqual(count, 4)
                self.assertEqual(backend, "poppler")
                texts, reason = pdf_page_texts(path)
            self.assertIsNone(texts)
            self.assertIn("无文本层", reason)

    def test_both_backends_unavailable_raises(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            path = _synthetic_pdf(Path(tmp) / "source.pdf", pages=1)
            with patch("pypdf.PdfReader", side_effect=RuntimeError("模拟损坏")), \
                    patch("case_kernel.pdf_compat.find_tool", return_value=None):
                with self.assertRaises(PdfUnreadable):
                    pdf_page_count(path)

    @unittest.skipUnless(REAL_CASE_PDF.is_file(), "本机没有合成木业案卷样本")
    def test_real_case_evidence_pdf_is_accepted(self) -> None:
        """真实案卷 PDF（43 页微信证据）必须能被读出页数，无论用哪个后端。"""
        count, backend = pdf_page_count(REAL_CASE_PDF)
        self.assertGreater(count, 10)
        self.assertIn(backend, ("pypdf", "poppler"))
        texts, _reason = pdf_page_texts(REAL_CASE_PDF)
        if texts is not None:
            self.assertEqual(len(texts), count)


if __name__ == "__main__":
    unittest.main()
