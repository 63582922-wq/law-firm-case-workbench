from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from pypdf import PdfReader

from case_kernel.evidence_normalization_worker import (
    EvidenceNormalizationBlocked,
    normalize_authorized_material,
)
from case_kernel.local_access_grants import AuthorizedOriginalFile


def authorized(path: Path) -> AuthorizedOriginalFile:
    return AuthorizedOriginalFile(
        relative_path=path.name,
        path=path,
        byte_size=path.stat().st_size,
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


class EvidenceNormalizationWorkerTests(unittest.TestCase):
    def test_static_png_becomes_a_verified_single_page_pdf_without_touching_original(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "转账截图.png"
            Image.new("RGB", (120, 80), color="white").save(path)
            source = authorized(path)
            original_bytes = path.read_bytes()
            result = normalize_authorized_material(source, detected_kind="IMAGE")
            self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(result.detected_kind, "IMAGE")
        self.assertEqual(result.source_media_type, "image/png")
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.pdf_sha256, sha256(result.pdf_content).hexdigest())
        self.assertEqual(len(result.transform_hash), 64)
        self.assertEqual(len(PdfReader(BytesIO(result.pdf_content), strict=True).pages), 1)
        self.assertTrue(original_bytes.startswith(b"\x89PNG"))

    def test_utf8_chinese_text_becomes_paginated_pdf_with_stable_transform_provenance(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "付款说明.txt"
            path.write_text("寒雪青松付款说明\n" * 120, encoding="utf-8")
            source = authorized(path)
            first = normalize_authorized_material(source, detected_kind="TEXT")
            second = normalize_authorized_material(source, detected_kind="TEXT")
        self.assertEqual(first.source_media_type, "text/plain")
        self.assertGreaterEqual(first.page_count, 1)
        self.assertEqual(first.transform_hash, second.transform_hash)
        self.assertEqual(first.pdf_sha256, sha256(first.pdf_content).hexdigest())

    def test_other_reviewable_formats_stay_in_their_dedicated_conversion_queue(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "流水.xlsx"
            path.write_bytes(b"not a spreadsheet")
            with self.assertRaisesRegex(EvidenceNormalizationBlocked, "not eligible|available"):
                normalize_authorized_material(authorized(path), detected_kind="SPREADSHEET")


if __name__ == "__main__":
    unittest.main()
