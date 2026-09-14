from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zipfile import ZipFile

from PIL import Image

from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.web_common_material_admission import (
    CommonMaterialContentRejected,
    CommonMaterialFormat,
    CommonMaterialRoute,
    CommonMaterialStagingArea,
    accepted_common_material_extensions,
    rejected_legacy_material_extensions,
)


class _CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        assert sha256(path.read_bytes()).hexdigest() == expected_sha256
        return FileSafetyScanReceipt("ClamAV", "ClamAV 1.4/test-db", expected_sha256, "CLEAN")


def _docx(*, active: bool = False) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>仅为待律师复核材料</w:t></w:r></w:p></w:body></w:document>""",
        )
        if active:
            archive.writestr("word/vbaProject.bin", b"macro")
    return output.getvalue()


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), color=(255, 255, 255)).save(output, format="PNG")
    return output.getvalue()


async def _chunks(content: bytes):
    for offset in range(0, len(content), 17):
        yield content[offset : offset + 17]


class WebCommonMaterialAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_docx_is_scanned_safely_read_and_remains_review_only(self) -> None:
        content = _docx()
        with TemporaryDirectory() as temporary:
            staging = CommonMaterialStagingArea(Path(temporary))
            staged = await staging.stage_async_chunks(
                _chunks(content),
                client_filename="案件说明.docx",
                declared_byte_size=len(content),
                declared_media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            admitted = staging.admit(
                staged,
                material_object_id=str(uuid4()),
                scanner=_CleanScanner(),
            )
            self.assertEqual(admitted.admitted_format, CommonMaterialFormat.DOCX)
            self.assertEqual(admitted.route, CommonMaterialRoute.COMMON_DOCUMENT_READER)
            self.assertFalse(admitted.formal_fact)
            self.assertFalse(admitted.formal_transaction)
            self.assertFalse(admitted.legal_conclusion)
            self.assertFalse(admitted.evidence_decision)
            self.assertFalse(admitted.court_ready)
            self.assertNotIn("仅为待律师复核材料", repr(admitted))
            self.assertTrue(staged.path.exists())
            staging.discard(admitted)
            self.assertFalse(staged.path.exists())

    async def test_png_is_registered_only_for_controlled_visual_ocr(self) -> None:
        content = _png()
        with TemporaryDirectory() as temporary:
            staging = CommonMaterialStagingArea(Path(temporary))
            staged = await staging.stage_async_chunks(
                _chunks(content),
                client_filename="转账截图.png",
                declared_byte_size=len(content),
                declared_media_type="image/png",
            )
            admitted = staging.admit(staged, material_object_id=str(uuid4()), scanner=_CleanScanner())
            self.assertEqual(admitted.admitted_format, CommonMaterialFormat.PNG)
            self.assertEqual(admitted.route, CommonMaterialRoute.VISUAL_OCR)
            self.assertEqual(admitted.media_type, "image/png")
            staging.discard(admitted)

    async def test_macro_html_script_external_dtd_and_extension_spoof_are_rejected(self) -> None:
        cases = (
            ("宏.docx", _docx(active=True), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("脚本.html", b"<!doctype html><html><script>alert(1)</script></html>", "text/html"),
            ("外部DTD.html", b"<!DOCTYPE html SYSTEM 'https://evil.invalid/x'><html></html>", "text/html"),
            ("伪装.docx", b"plain text", "application/octet-stream"),
        )
        for filename, content, media_type in cases:
            with self.subTest(filename=filename), TemporaryDirectory() as temporary:
                staging = CommonMaterialStagingArea(Path(temporary))
                staged = await staging.stage_async_chunks(
                    _chunks(content),
                    client_filename=filename,
                    declared_byte_size=len(content),
                    declared_media_type=media_type,
                )
                with self.assertRaises(CommonMaterialContentRejected):
                    staging.admit(staged, material_object_id=str(uuid4()), scanner=_CleanScanner())
                staging.discard(staged)

    async def test_legacy_formats_pdf_and_declared_media_mismatch_are_explicitly_rejected(self) -> None:
        self.assertEqual(
            rejected_legacy_material_extensions(),
            (".doc", ".msg", ".ofd", ".ppt", ".xls"),
        )
        self.assertIn(".pptx", accepted_common_material_extensions())
        with TemporaryDirectory() as temporary:
            staging = CommonMaterialStagingArea(Path(temporary))
            for filename in ("旧文档.doc", "旧表格.xls", "旧幻灯片.ppt", "邮件.msg", "材料.ofd", "材料.pdf"):
                with self.subTest(filename=filename), self.assertRaises(CommonMaterialContentRejected):
                    await staging.stage_async_chunks(
                        _chunks(b"not consumed"),
                        client_filename=filename,
                        declared_byte_size=12,
                        declared_media_type="application/octet-stream",
                    )
            with self.assertRaises(CommonMaterialContentRejected):
                await staging.stage_async_chunks(
                    _chunks(_png()),
                    client_filename="截图.png",
                    declared_byte_size=len(_png()),
                    declared_media_type="text/plain",
                )


if __name__ == "__main__":
    unittest.main()
