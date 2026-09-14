"""扫描版 PDF 渲染与 OCR 目标筛选的回归测试。

覆盖本次修复的两个真实缺陷：
1. 渲染页（file_name 仍是 PDF 名）必须进入 OCR 目标，而不是被 PDF 过滤器排除；
2. 图片路径解析必须用文件名（而非 PageText 对象）构造覆盖键，否则触发 unhashable。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from case_kernel.pdf_render import render_pdf_pages, renderer_backend
from case_kernel.shadow_live_transport import _image_path
from case_kernel.shadow_mode import PageText


class ImagePathTests(unittest.TestCase):
    def test_override_resolves_for_pdf_page_key(self) -> None:
        override_path = Path("/tmp/rendered/p0003.png")
        overrides = {("法院材料.pdf", 3): override_path}
        resolved = _image_path(Path("/materials"), "法院材料.pdf", overrides, 3)
        self.assertEqual(resolved, override_path)

    def test_without_override_falls_back_to_material_relative_path(self) -> None:
        resolved = _image_path(Path("/materials"), "银行截图.jpg", {}, 1)
        self.assertEqual(resolved, Path("/materials/银行截图.jpg"))

    def test_string_key_override_is_supported(self) -> None:
        override_path = Path("/tmp/rendered/x.png")
        resolved = _image_path(Path("/materials"), ("图.jpg", 1), {("图.jpg", 1): override_path}, 1)
        self.assertEqual(resolved, override_path)


class RendererTests(unittest.TestCase):
    def test_backend_is_reported(self) -> None:
        self.assertIn(renderer_backend(), {"pymupdf", "pdftoppm", ""})

    def test_renders_pdf_pages_when_backend_available(self) -> None:
        if not renderer_backend():
            self.skipTest("本机无 PDF 栅格化后端")
        from reportlab.pdfgen import canvas

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "扫描件.pdf"
            stream = BytesIO()
            document = canvas.Canvas(stream)
            document.drawString(60, 760, "扫描页内容")
            document.showPage()
            document.save()
            source.write_bytes(stream.getvalue())

            pages, note = render_pdf_pages(source, root / "rendered", pages=[1])
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].page_number, 1)
            self.assertTrue(pages[0].path.is_file())
            self.assertIn("渲染", note)


class OcrTargetTests(unittest.TestCase):
    """渲染页必须被 OCR，未授权页必须被跳过。"""

    def _transport(self):
        from case_kernel.shadow_live_transport import QwenShadowTransport

        return QwenShadowTransport.__new__(QwenShadowTransport)

    def test_rendered_pdf_pages_are_included_and_unauthorized_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rendered = root / "p1.png"
            from PIL import Image

            Image.new("RGB", (300, 400), "white").save(rendered, format="PNG")
            standalone = root / "银行截图.jpg"
            Image.new("RGB", (300, 400), "white").save(standalone, format="JPEG")

            transport = self._transport()
            transport.materials_root = root
            captured: list[list[tuple[str, int]]] = []

            def fake_call(*, instruction, images, max_output_tokens, purpose, ledger,
                          expected_schema, strict_schema=True, image_paths=None):
                captured.append(list(images))
                return {"schema": "shadow-ocr-v1",
                        "pages": [{"file_name": name, "page_number": page, "text": "OCR"}
                                  for name, page in images]}

            transport._call = fake_call  # type: ignore[assignment]

            pages = [
                PageText("法院材料.pdf", "", 3, ""),      # 渲染页
                PageText("银行截图.jpg", "", 1, ""),      # 独立图片
                PageText("未授权.jpg", "", 1, ""),        # 未授权
            ]
            authorized = {("法院材料.pdf", 3), "银行截图.jpg"}
            overrides = {("法院材料.pdf", 3): rendered}

            result = transport._ocr_batches(pages, authorized, ledger=_Ledger(), path_overrides=overrides)

            sent = {key for batch in captured for key in batch}
            self.assertIn(("法院材料.pdf", 3), sent)
            self.assertIn(("银行截图.jpg", 1), sent)
            self.assertNotIn(("未授权.jpg", 1), sent)
            self.assertEqual(len(result), 2)


class _Ledger:
    rows: list[dict] = []

    def append(self, **kwargs) -> None:  # noqa: D102 - 测试替身
        self.rows.append(kwargs)


if __name__ == "__main__":
    unittest.main()
