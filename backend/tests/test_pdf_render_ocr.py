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

        from case_kernel.model_providers import ALIYUN

        transport = QwenShadowTransport.__new__(QwenShadowTransport)
        transport.provider = ALIYUN
        transport.model = ALIYUN.model
        return transport

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


class OcrCacheTests(unittest.TestCase):
    """第二次运行必须能读回缓存：JSON 会把授权键的元组还原成列表。"""

    def test_second_run_reads_cache_without_type_error(self) -> None:
        from case_kernel.shadow_live_transport import QwenShadowTransport
        from case_kernel.shadow_mode import PageText

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = root / "materials"
            materials.mkdir()
            from PIL import Image

            Image.new("RGB", (300, 400), "white").save(materials / "银行流水.jpg", format="JPEG")

            from case_kernel.model_providers import ALIYUN

            transport = QwenShadowTransport.__new__(QwenShadowTransport)
            transport.provider = ALIYUN
            transport.model = ALIYUN.model
            transport.materials_root = materials
            calls: list[int] = []

            def fake_call(*, instruction, images, max_output_tokens, purpose, ledger,
                          expected_schema, strict_schema=True, image_paths=None):
                calls.append(len(images))
                return {"schema": "shadow-ocr-v1",
                        "pages": [{"file_name": name, "page_number": page, "text": "第一页文字"}
                                  for name, page in images]}

            transport._call = fake_call  # type: ignore[assignment]
            pages = [PageText("银行流水.jpg", "", 1, "")]
            authorized = {"银行流水.jpg", ("法院材料.pdf", 3)}

            first = transport._ocr_batches(pages, authorized, ledger=_Ledger())
            self.assertEqual(len(first), 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue((root / "materials.ocr_cache.json").is_file())

            # 第二遍：命中缓存，不再调用模型，且不得抛 TypeError
            second = transport._ocr_batches(pages, authorized, ledger=_Ledger())
            self.assertEqual([page.text for page in second], ["第一页文字"])
            self.assertEqual(len(calls), 1)

    def test_cache_is_scoped_to_provider_and_model(self) -> None:
        """换了模型必须重新 OCR：不能把上一家模型的识别结果当成这一家的输出。"""
        from case_kernel.model_providers import ALIYUN, DEEPSEEK
        from case_kernel.shadow_live_transport import QwenShadowTransport
        from case_kernel.shadow_mode import PageText
        from PIL import Image
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = root / "materials"
            materials.mkdir()
            Image.new("RGB", (300, 400), "white").save(materials / "a.jpg", format="JPEG")
            calls: list[str] = []

            def make(provider):
                transport = QwenShadowTransport.__new__(QwenShadowTransport)
                transport.provider = provider
                transport.model = provider.model
                transport.materials_root = materials

                def fake_call(*, instruction, images, max_output_tokens, purpose, ledger,
                              expected_schema, strict_schema=True, image_paths=None):
                    calls.append(provider.key)
                    return {"schema": "shadow-ocr-v1",
                            "pages": [{"file_name": name, "page_number": page,
                                       "text": provider.key} for name, page in images]}

                transport._call = fake_call  # type: ignore[assignment]
                return transport

            pages = [PageText("a.jpg", "", 1, "")]
            first = make(ALIYUN)._ocr_batches(pages, {"a.jpg"}, ledger=_Ledger())
            self.assertEqual(first[0].text, "aliyun-maas")
            # 同一供应商：命中缓存，不再调用
            again = make(ALIYUN)._ocr_batches(pages, {"a.jpg"}, ledger=_Ledger())
            self.assertEqual(again[0].text, "aliyun-maas")
            self.assertEqual(calls, ["aliyun-maas"])
            # 换供应商：缓存失效，重新识别
            switched = make(DEEPSEEK)._ocr_batches(pages, {"a.jpg"}, ledger=_Ledger())
            self.assertEqual(switched[0].text, "deepseek")
            self.assertEqual(calls, ["aliyun-maas", "deepseek"])

    def test_corrupt_cache_is_ignored(self) -> None:
        from case_kernel.shadow_live_transport import QwenShadowTransport
        from case_kernel.shadow_mode import PageText

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            materials = root / "materials"
            materials.mkdir()
            from PIL import Image

            Image.new("RGB", (300, 400), "white").save(materials / "a.jpg", format="JPEG")
            (root / "materials.ocr_cache.json").write_text("{ 不是 JSON", encoding="utf-8")

            from case_kernel.model_providers import ALIYUN

            transport = QwenShadowTransport.__new__(QwenShadowTransport)
            transport.provider = ALIYUN
            transport.model = ALIYUN.model
            transport.materials_root = materials
            transport._call = lambda **kwargs: {  # type: ignore[assignment]
                "schema": "shadow-ocr-v1",
                "pages": [{"file_name": "a.jpg", "page_number": 1, "text": "重新识别"}],
            }
            pages = transport._ocr_batches([PageText("a.jpg", "", 1, "")], {"a.jpg"},
                                           ledger=_Ledger())
            self.assertEqual(pages[0].text, "重新识别")


class ImageIdentifierGateTests(unittest.TestCase):
    """扫描件图像无法脱敏：默认 fail closed；律师授权后掩码文本并记录检出。"""

    _CARD = "6228000000000003"  # 合成号码（Luhn 有效），非真实账户

    def _transport(self, *, allow: bool):
        from case_kernel.shadow_live_transport import QwenShadowTransport

        from case_kernel.model_providers import ALIYUN

        transport = QwenShadowTransport.__new__(QwenShadowTransport)
        transport.provider = ALIYUN
        transport.model = ALIYUN.model
        transport.allow_image_identifiers = allow
        transport.image_identifier_findings = []
        return transport

    def _batch(self, transport, text: str):
        def fake_call(*, instruction, images, max_output_tokens, purpose, ledger,
                      expected_schema, strict_schema=True, image_paths=None):
            return {"schema": "shadow-ocr-v1",
                    "pages": [{"file_name": name, "page_number": page, "text": text}
                              for name, page in images]}

        transport._call = fake_call  # type: ignore[assignment]
        from case_kernel.shadow_mode import PageText

        return transport._ocr_batch([PageText("银行流水.jpg", "", 1, "")], _Ledger())

    def test_default_blocks_with_actionable_message(self) -> None:
        from case_kernel.shadow_mode import ShadowBlocked

        transport = self._transport(allow=False)
        with self.assertRaises(ShadowBlocked) as caught:
            self._batch(transport, f"账号 {self._CARD} 转入 100000 元")
        self.assertIn("BANK_CARD", str(caught.exception))
        self.assertIn("扫描件", str(caught.exception))
        self.assertIn("勾选", str(caught.exception))

    def test_authorized_masks_text_and_records_findings(self) -> None:
        transport = self._transport(allow=True)
        pages = self._batch(transport, f"账号 {self._CARD} 转入 100000 元")
        self.assertEqual(len(pages), 1)
        self.assertNotIn(self._CARD, pages[0].text)
        self.assertIn("6228 **** **** 0003", pages[0].text)
        self.assertEqual(len(transport.image_identifier_findings), 1)
        finding = transport.image_identifier_findings[0]
        self.assertEqual(finding["pattern"], "BANK_CARD")
        self.assertEqual(finding["file_name"], "银行流水.jpg")
        self.assertEqual(finding["page_number"], 1)

    def test_luhn_invalid_long_number_is_not_an_identifier(self) -> None:
        transport = self._transport(allow=False)
        text = "交易单号 1000050001202601310127877735780 金额 2250.00 元"
        pages = self._batch(transport, text)
        self.assertEqual(pages[0].text, text)
        self.assertEqual(transport.image_identifier_findings, [])


if __name__ == "__main__":
    unittest.main()
