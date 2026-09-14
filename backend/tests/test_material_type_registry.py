from __future__ import annotations

from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
import zipfile

from case_kernel.material_type_registry import (
    ExtensionAlignment,
    MaterialCanonicalKind,
    MaterialCapabilityMaturity,
    MaterialRoutingStatus,
    identify_material_type,
)


class MaterialTypeRegistryTests(unittest.TestCase):
    def _file(self, root: Path, name: str, content: bytes) -> Path:
        path = root / name
        path.write_bytes(content)
        return path

    def _zip(self, root: Path, name: str, entries: dict[str, bytes | str]) -> Path:
        path = root / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, content in entries.items():
                archive.writestr(entry, content)
        return path

    def assert_kind(self, path: Path, kind: MaterialCanonicalKind, actual_format: str) -> None:
        decision = identify_material_type(path)
        self.assertEqual(decision.canonical_kind, kind)
        self.assertEqual(decision.actual_format, actual_format)
        self.assertEqual(len(decision.decision_hash), 64)

    def test_pdf_images_and_multiframe_follow_up_are_content_identified(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("evidence.pdf", b"%PDF-1.7\n1 0 obj\n", MaterialCanonicalKind.PDF, "PDF"),
                ("photo.jpg", b"\xff\xd8\xff\xe0synthetic", MaterialCanonicalKind.IMAGE, "JPEG"),
                ("scan.png", b"\x89PNG\r\n\x1a\nsynthetic", MaterialCanonicalKind.IMAGE, "PNG"),
                ("pages.tiff", b"II*\x00synthetic", MaterialCanonicalKind.IMAGE, "TIFF"),
                ("bitmap.bmp", b"BMsynthetic", MaterialCanonicalKind.IMAGE, "BMP"),
                ("page.webp", b"RIFF\x10\x00\x00\x00WEBPsynthetic", MaterialCanonicalKind.IMAGE, "WEBP"),
                ("iphone.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", MaterialCanonicalKind.IMAGE, "HEIC"),
            )
            for name, content, kind, actual_format in fixtures:
                with self.subTest(name=name):
                    decision = identify_material_type(self._file(root, name, content))
                    self.assertEqual((decision.canonical_kind, decision.actual_format), (kind, actual_format))
                    self.assertEqual(decision.extension_alignment, ExtensionAlignment.MATCH)
                    if kind is MaterialCanonicalKind.IMAGE:
                        self.assertIn("DIMENSION_PIXEL_AND_MULTIFRAME_CHECK_REQUIRED", decision.follow_up_checks)
            jpeg = identify_material_type(root / "photo.jpg")
            self.assertEqual(jpeg.preferred_skill, "image_visual_ocr")
            self.assertEqual(jpeg.maturity, MaterialCapabilityMaturity.GATED)
            self.assertEqual(jpeg.routing_status, MaterialRoutingStatus.REVIEW_REQUIRED)

    def test_ooxml_families_require_key_container_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("pleading.docx", "word/document.xml", MaterialCanonicalKind.WORD_DOCUMENT, "OOXML_DOCX"),
                ("ledger.xlsx", "xl/workbook.xml", MaterialCanonicalKind.SPREADSHEET, "OOXML_XLSX"),
                ("hearing.pptx", "ppt/presentation.xml", MaterialCanonicalKind.PRESENTATION, "OOXML_PPTX"),
            )
            for name, main_part, kind, actual_format in fixtures:
                with self.subTest(name=name):
                    path = self._zip(
                        root,
                        name,
                        {"[Content_Types].xml": "<Types/>", main_part: "<document/>"},
                    )
                    decision = identify_material_type(path)
                    self.assertEqual((decision.canonical_kind, decision.actual_format), (kind, actual_format))
                    self.assertEqual(decision.extension_alignment, ExtensionAlignment.MATCH)
            self.assertEqual(identify_material_type(root / "pleading.docx").legacy_detected_kind, "WORD_DOCUMENT")
            self.assertEqual(identify_material_type(root / "ledger.xlsx").legacy_detected_kind, "SPREADSHEET")

    def test_ofd_and_plain_zip_are_not_confused_with_ooxml(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ofd = self._zip(root, "court.ofd", {"OFD.xml": "<ofd/>", "Doc_0/Document.xml": "<document/>"})
            archive = self._zip(root, "bundle.zip", {"readme.txt": "synthetic"})
            self.assert_kind(ofd, MaterialCanonicalKind.OFD, "OFD")
            decision = identify_material_type(archive)
            self.assertEqual(decision.canonical_kind, MaterialCanonicalKind.ARCHIVE)
            self.assertEqual(decision.actual_format, "ZIP")
            self.assertEqual(decision.maturity, MaterialCapabilityMaturity.PLANNED)

    def test_text_data_email_and_markup_formats_are_bounded_and_explicit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("notes.txt", "ordinary synthetic note\n", "TEXT", "text/plain"),
                ("notes.md", "# Synthetic\n\nText\n", "MARKDOWN", "text/markdown"),
                ("rows.csv", "date,amount\n2026-01-01,10\n2026-01-02,20\n", "CSV", "text/csv"),
                ("rows.tsv", "date\tamount\n2026-01-01\t10\n2026-01-02\t20\n", "TSV", "text/tab-separated-values"),
                ("data.json", '{"synthetic": true}\n', "JSON", "application/json"),
                ("data.xml", "<?xml version='1.0'?><root/>", "XML", "application/xml"),
                ("page.html", "<!doctype html><html><body>x</body></html>", "HTML", "text/html"),
                (
                    "message.eml",
                    "From: sender@example.invalid\nTo: receiver@example.invalid\nSubject: Synthetic\n\nBody\n",
                    "EML",
                    "message/rfc822",
                ),
            )
            for name, content, actual_format, media_type in fixtures:
                with self.subTest(name=name):
                    decision = identify_material_type(self._file(root, name, content.encode("utf-8")))
                    self.assertEqual((decision.actual_format, decision.media_type), (actual_format, media_type))
                    self.assertEqual(decision.extension_alignment, ExtensionAlignment.MATCH)
                    self.assertEqual(decision.maturity, MaterialCapabilityMaturity.PLANNED)

    def test_audio_and_video_signatures_are_not_claimed_as_parsed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("voice.mp3", b"ID3\x04\x00\x00synthetic", MaterialCanonicalKind.AUDIO, "MP3"),
                ("voice.wav", b"RIFF\x10\x00\x00\x00WAVEsynthetic", MaterialCanonicalKind.AUDIO, "WAV"),
                ("voice.m4a", b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00", MaterialCanonicalKind.AUDIO, "M4A"),
                ("video.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", MaterialCanonicalKind.VIDEO, "MP4"),
                ("video.mov", b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00", MaterialCanonicalKind.VIDEO, "MOV"),
            )
            for name, content, kind, actual_format in fixtures:
                with self.subTest(name=name):
                    decision = identify_material_type(self._file(root, name, content))
                    self.assertEqual((decision.canonical_kind, decision.actual_format), (kind, actual_format))
                    self.assertEqual(decision.maturity, MaterialCapabilityMaturity.PLANNED)
                    self.assertFalse(decision.routing_allowed)

    def test_extension_mismatch_blocks_routing_even_when_signature_is_known(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            disguised = self._file(root, "invoice.pdf", b"\x89PNG\r\n\x1a\nsynthetic")
            decision = identify_material_type(disguised)
            self.assertEqual(decision.actual_format, "PNG")
            self.assertEqual(decision.extension_alignment, ExtensionAlignment.MISMATCH)
            self.assertEqual(decision.routing_status, MaterialRoutingStatus.REVIEW_REQUIRED)
            self.assertEqual(decision.reason_code, "EXTENSION_CONTENT_MISMATCH")
            self.assertFalse(decision.routing_allowed)

            dangerous_name = self._file(root, "evidence.exe", b"%PDF-1.7\n")
            dangerous = identify_material_type(dangerous_name)
            self.assertEqual(dangerous.actual_format, "PDF")
            self.assertEqual(dangerous.extension_alignment, ExtensionAlignment.DANGEROUS)
            self.assertEqual(dangerous.routing_status, MaterialRoutingStatus.QUARANTINED)

            missing_name = self._file(root, "evidence", b"%PDF-1.7\n")
            missing = identify_material_type(missing_name)
            self.assertEqual(missing.actual_format, "PDF")
            self.assertEqual(missing.extension_alignment, ExtensionAlignment.MISSING)
            self.assertEqual(missing.reason_code, "EXTENSION_MISSING")
            self.assertFalse(missing.routing_allowed)

    def test_executable_disk_macro_encrypted_and_unknown_files_are_quarantined(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("malware.bin", b"MZ" + b"\x00" * 32, "EXECUTABLE_CONTENT"),
                ("script.sh", b"#!/bin/sh\necho unsafe\n", "EXECUTABLE_CONTENT"),
                ("disk.iso", b"not an image", "DANGEROUS_EXTENSION_MISMATCH"),
                ("macro.docm", b"not opened", "MACRO_ENABLED_EXTENSION"),
                ("locked.pdf", b"%PDF-1.7\n/Encrypt 2 0 R\n", "PDF_ENCRYPTED"),
                ("unknown.dat", b"\x00\x01\x02\x03random", "UNKNOWN_HIGH_RISK_FORMAT"),
            )
            for name, content, reason_code in fixtures:
                with self.subTest(name=name):
                    decision = identify_material_type(self._file(root, name, content))
                    self.assertEqual(decision.reason_code, reason_code)
                    self.assertEqual(decision.maturity, MaterialCapabilityMaturity.QUARANTINED)
                    self.assertEqual(decision.routing_status, MaterialRoutingStatus.QUARANTINED)
                    self.assertIsNone(decision.preferred_skill)

    def test_ooxml_macro_embedding_and_multiple_families_are_quarantined_from_directory_only(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            macro = self._zip(
                root,
                "macro.docx",
                {
                    "[Content_Types].xml": "<Types/>",
                    "word/document.xml": "<document/>",
                    "word/vbaProject.bin": b"never read",
                },
            )
            embedded = self._zip(
                root,
                "embedded.xlsx",
                {
                    "[Content_Types].xml": "<Types/>",
                    "xl/workbook.xml": "<workbook/>",
                    "xl/embeddings/object1.bin": b"never read",
                },
            )
            ambiguous = self._zip(
                root,
                "ambiguous.zip",
                {
                    "[Content_Types].xml": "<Types/>",
                    "word/document.xml": "<document/>",
                    "xl/workbook.xml": "<workbook/>",
                },
            )
            self.assertEqual(identify_material_type(macro).reason_code, "OOXML_ACTIVE_CONTENT")
            self.assertEqual(identify_material_type(embedded).reason_code, "OOXML_ACTIVE_CONTENT")
            self.assertEqual(identify_material_type(ambiguous).reason_code, "OOXML_MULTIPLE_DOCUMENT_FAMILIES")

    def test_archive_path_encryption_executable_and_compression_bomb_are_quarantined(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            traversal = self._zip(root, "traversal.zip", {"../escape.txt": "x"})
            executable = self._zip(root, "executable.zip", {"run.sh": "#!/bin/sh\n"})
            bomb_path = root / "bomb.zip"
            with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("large.txt", b"0" * (2 * 1024 * 1024))
            encrypted_path = self._zip(root, "encrypted.zip", {"secret.txt": "x"})
            raw = bytearray(encrypted_path.read_bytes())
            local = raw.find(b"PK\x03\x04")
            central = raw.find(b"PK\x01\x02")
            struct.pack_into("<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | 1)
            struct.pack_into("<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 1)
            encrypted_path.write_bytes(raw)

            self.assertEqual(identify_material_type(traversal).reason_code, "ZIP_UNSAFE_PATH")
            self.assertEqual(identify_material_type(executable).reason_code, "ARCHIVE_CONTAINS_EXECUTABLE")
            self.assertEqual(identify_material_type(bomb_path).reason_code, "ZIP_COMPRESSION_RATIO_LIMIT")
            self.assertEqual(identify_material_type(encrypted_path).reason_code, "ZIP_ENCRYPTED")

    def test_zip_entry_count_preflight_rejects_declared_limit_before_directory_iteration(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "declared-many.zip"
            # Empty EOCD with a malicious declared entry count.  The registry
            # rejects the central-directory declaration before zipfile builds
            # thousands of metadata objects.
            path.write_bytes(b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 10_001, 10_001, 0, 0, 0))
            decision = identify_material_type(path)
            self.assertEqual(decision.reason_code, "ZIP_ENTRY_LIMIT")
            self.assertEqual(decision.zip_entry_count, 10_001)
            self.assertEqual(decision.routing_status, MaterialRoutingStatus.QUARANTINED)


if __name__ == "__main__":
    unittest.main()
