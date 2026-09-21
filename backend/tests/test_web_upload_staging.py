from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen import canvas

from case_kernel.evidence_intake_worker import FileSafetyScanReceipt
from case_kernel.web_upload_staging import (
    WebUploadLimits,
    WebUploadRejected,
    WebUploadStagingArea,
    WebUploadStagingBlocked,
)


class CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        return FileSafetyScanReceipt("test scanner", "definitions-1", expected_sha256, "CLEAN")


class MutatingScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        path.write_bytes(b"changed while scanning")
        return FileSafetyScanReceipt("test scanner", "definitions-1", expected_sha256, "CLEAN")


def _pdf_bytes(root: Path, *, pages: int = 2) -> bytes:
    path = root / "source.pdf"
    document = canvas.Canvas(str(path))
    for number in range(pages):
        document.drawString(36, 720, f"synthetic page {number + 1}")
        document.showPage()
    document.save()
    return path.read_bytes()


class WebUploadStagingTests(unittest.TestCase):
    def test_stages_and_admits_static_pdf_without_retaining_a_browser_path(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = WebUploadStagingArea(root / "private-staging")
            staged = staging.stage_stream(
                BytesIO(_pdf_bytes(root)),
                client_filename=r"C:\Users\lawyer\案件资料\微信转账记录.pdf",
            )
            admitted = staging.inspect_pdf(staged, scanner=CleanScanner())

            self.assertEqual(staged.display_name, "微信转账记录.pdf")
            self.assertEqual(admitted.page_count, 2)
            self.assertEqual(admitted.media_type, "application/pdf")
            self.assertNotIn("C:\\Users", repr(staged))
            self.assertNotIn(str(staging.staging_root), repr(admitted))
            self.assertTrue(admitted.path.exists())
            staging.discard(admitted)
            self.assertFalse(admitted.path.exists())

    def test_size_boundary_and_failed_write_leave_no_private_partial_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = WebUploadStagingArea(
                root / "private-staging",
                limits=WebUploadLimits(max_file_bytes=70_000, read_chunk_bytes=64 * 1024),
            )
            with self.assertRaisesRegex(WebUploadStagingBlocked, "file-size"):
                staging.stage_stream(BytesIO(b"x" * 70_001), client_filename="too-large.pdf")
            self.assertEqual(list(staging.staging_root.iterdir()), [])

    def test_malicious_or_malformed_pdf_is_not_admitted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = WebUploadStagingArea(root / "private-staging")
            staged = staging.stage_stream(BytesIO(b"not-a-pdf"), client_filename="材料.pdf")
            with self.assertRaisesRegex(WebUploadRejected, "FILE_SIGNATURE_MISMATCH"):
                staging.inspect_pdf(staged, scanner=CleanScanner())
            staging.discard(staged)

    def test_mutation_during_mandatory_scan_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = WebUploadStagingArea(root / "private-staging")
            staged = staging.stage_stream(BytesIO(_pdf_bytes(root)), client_filename="材料.pdf")
            with self.assertRaisesRegex(WebUploadStagingBlocked, "changed before inspection"):
                staging.inspect_pdf(staged, scanner=MutatingScanner())
            staging.discard(staged)

    def test_async_body_stream_uses_the_same_private_hash_and_inspection_boundary(self) -> None:
        async def chunks(content: bytes):
            yield content[:17]
            yield b""
            yield content[17:]

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = WebUploadStagingArea(root / "private-staging")
            staged = asyncio.run(
                staging.stage_async_chunks(chunks(_pdf_bytes(root)), client_filename="法院送达材料.pdf")
            )
            admitted = staging.inspect_pdf(staged, scanner=CleanScanner())
            self.assertEqual(admitted.page_count, 2)
            self.assertEqual(admitted.display_name, "法院送达材料.pdf")
            staging.discard(admitted)


if __name__ == "__main__":
    unittest.main()
