from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject
from reportlab.pdfgen import canvas

from case_kernel.evidence_intake_worker import (
    EvidenceIntakeBlocked,
    FileSafetyScanReceipt,
    inspect_authorized_original,
)
from case_kernel.local_access_grants import AuthorizedOriginalFile


class CleanScanner:
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        return FileSafetyScanReceipt("Synthetic local scanner", "test-definitions-1", expected_sha256, "CLEAN")


class EvidenceIntakeWorkerTests(unittest.TestCase):
    def authorized(self, path: Path, relative_path: str) -> AuthorizedOriginalFile:
        content_hash = sha256(path.read_bytes()).hexdigest()
        return AuthorizedOriginalFile(relative_path, path, path.stat().st_size, content_hash)

    def test_clean_static_pdf_is_registerable_with_real_page_count(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "法院送达材料.pdf"
            document = canvas.Canvas(str(path))
            document.drawString(30, 700, "synthetic court material")
            document.showPage()
            document.drawString(30, 700, "page two")
            document.save()
            result = inspect_authorized_original(self.authorized(path, path.name), detected_kind="PDF", scanner=CleanScanner())
        self.assertEqual(result.outcome, "REGISTERABLE")
        self.assertEqual(result.media_type, "application/pdf")
        self.assertEqual(result.page_count, 2)
        self.assertEqual(len(result.inspection_hash), 64)

    def test_non_pdf_requires_conversion_instead_of_inventing_pages(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "微信流水.xlsx"
            path.write_bytes(b"synthetic spreadsheet bytes")
            result = inspect_authorized_original(self.authorized(path, path.name), detected_kind="SPREADSHEET", scanner=CleanScanner())
        self.assertEqual(result.outcome, "REVIEW_REQUIRED")
        self.assertEqual(result.reason_code, "SPREADSHEET_RENDER_REQUIRED")
        self.assertIsNone(result.page_count)

    def test_pdf_active_content_is_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary) / "base.pdf"
            document = canvas.Canvas(str(base))
            document.drawString(30, 700, "synthetic")
            document.save()
            reader = PdfReader(str(base))
            writer = PdfWriter()
            writer.append_pages_from_reader(reader)
            writer._root_object[NameObject("/OpenAction")] = DictionaryObject({
                NameObject("/S"): NameObject("/JavaScript"),
                NameObject("/JS"): TextStringObject("app.alert('x')"),
            })
            active = Path(temporary) / "active.pdf"
            with active.open("wb") as stream:
                writer.write(stream)
            result = inspect_authorized_original(self.authorized(active, active.name), detected_kind="PDF", scanner=CleanScanner())
        self.assertEqual(result.outcome, "BLOCKED")
        self.assertEqual(result.reason_code, "PDF_ACTIVE_CONTENT")

    def test_scanner_receipt_must_match_authorized_hash(self) -> None:
        class MismatchedScanner:
            def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
                return FileSafetyScanReceipt("scanner", "defs", "0" * 64, "CLEAN")

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "material.txt"
            path.write_text("synthetic", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceIntakeBlocked, "does not match"):
                inspect_authorized_original(self.authorized(path, path.name), detected_kind="TEXT", scanner=MismatchedScanner())

    def test_infected_file_is_blocked_before_format_processing(self) -> None:
        class InfectedScanner:
            def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
                return FileSafetyScanReceipt("scanner", "defs", expected_sha256, "INFECTED")

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "材料.pdf"
            path.write_bytes(b"not inspected after malware result")
            result = inspect_authorized_original(
                self.authorized(path, path.name), detected_kind="PDF", scanner=InfectedScanner()
            )
        self.assertEqual(result.outcome, "BLOCKED")
        self.assertEqual(result.reason_code, "MALWARE_DETECTED")

    def test_empty_file_is_explicitly_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "空白材料.pdf"
            path.write_bytes(b"")
            result = inspect_authorized_original(
                self.authorized(path, path.name), detected_kind="PDF", scanner=CleanScanner()
            )
        self.assertEqual(result.outcome, "BLOCKED")
        self.assertEqual(result.reason_code, "EMPTY_FILE")


if __name__ == "__main__":
    unittest.main()
