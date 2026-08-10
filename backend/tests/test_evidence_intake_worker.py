from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from PIL import Image
from openpyxl import Workbook
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
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
                )
                archive.writestr("xl/workbook.xml", "<workbook/>")
            result = inspect_authorized_original(self.authorized(path, path.name), detected_kind="SPREADSHEET", scanner=CleanScanner())
        self.assertEqual(result.outcome, "REVIEW_REQUIRED")
        self.assertEqual(result.reason_code, "SPREADSHEET_CONVERSION_REQUIRED")
        self.assertIsNone(result.page_count)

    def test_image_signature_and_dimensions_are_verified_before_conversion(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "转账截图.png"
            Image.new("RGB", (64, 48), color="white").save(path)
            result = inspect_authorized_original(
                self.authorized(path, path.name), detected_kind="IMAGE", scanner=CleanScanner()
            )
        self.assertEqual(result.outcome, "REVIEW_REQUIRED")
        self.assertEqual(result.reason_code, "IMAGE_CONVERSION_REQUIRED")

    def test_office_macro_and_external_relationship_are_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            macro_path = root / "宏材料.docx"
            with zipfile.ZipFile(macro_path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/vbaProject.bin", b"synthetic macro")
            macro_result = inspect_authorized_original(
                self.authorized(macro_path, macro_path.name), detected_kind="WORD_DOCUMENT", scanner=CleanScanner()
            )
            external_path = root / "外链材料.docx"
            with zipfile.ZipFile(external_path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr(
                    "word/_rels/document.xml.rels",
                    """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                    <Relationship Id="rId1" TargetMode="External" Target="https://example.invalid/payload"/>
                    </Relationships>""",
                )
            external_result = inspect_authorized_original(
                self.authorized(external_path, external_path.name),
                detected_kind="WORD_DOCUMENT",
                scanner=CleanScanner(),
            )
        self.assertEqual(macro_result.outcome, "BLOCKED")
        self.assertEqual(macro_result.reason_code, "OFFICE_ACTIVE_CONTENT")
        self.assertEqual(external_result.outcome, "BLOCKED")
        self.assertEqual(external_result.reason_code, "OFFICE_EXTERNAL_RELATIONSHIP")

    def test_standard_xlsx_package_root_worksheet_relationship_is_not_mistaken_for_an_escape(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "标准台账.xlsx"
            workbook = Workbook()
            workbook.active["A1"] = "付款"
            workbook.save(path)
            result = inspect_authorized_original(
                self.authorized(path, path.name), detected_kind="SPREADSHEET", scanner=CleanScanner()
            )
        self.assertEqual(result.outcome, "REVIEW_REQUIRED")
        self.assertEqual(result.reason_code, "SPREADSHEET_CONVERSION_REQUIRED")

    def test_archive_path_traversal_is_blocked_without_extraction(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "材料包.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../逃逸.txt", "synthetic")
            result = inspect_authorized_original(
                self.authorized(path, path.name), detected_kind="ARCHIVE", scanner=CleanScanner()
            )
        self.assertEqual(result.outcome, "BLOCKED")
        self.assertEqual(result.reason_code, "ARCHIVE_UNSAFE_PATH")

    def test_binary_text_and_unsafe_email_attachment_are_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_path = root / "伪文本.txt"
            text_path.write_bytes(b"text\x00binary")
            text_result = inspect_authorized_original(
                self.authorized(text_path, text_path.name), detected_kind="TEXT", scanner=CleanScanner()
            )
            email_path = root / "邮件.eml"
            email_path.write_bytes(
                b"From: sender@example.invalid\r\nTo: receiver@example.invalid\r\n"
                b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
                b"--x\r\nContent-Type: text/plain\r\n\r\nSynthetic\r\n"
                b"--x\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Disposition: attachment; filename=../escape.bin\r\n\r\nX\r\n--x--\r\n"
            )
            email_result = inspect_authorized_original(
                self.authorized(email_path, email_path.name), detected_kind="EMAIL", scanner=CleanScanner()
            )
        self.assertEqual(text_result.reason_code, "TEXT_BINARY_CONTENT")
        self.assertEqual(email_result.reason_code, "EMAIL_UNSAFE_ATTACHMENT_NAME")

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
