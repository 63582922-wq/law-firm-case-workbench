from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import zipfile

from reportlab.pdfgen import canvas

from case_kernel.local_access_grants import AuthorizedOriginalFile
from case_kernel.office_pdf_conversion_worker import (
    OfficePdfConversionBlocked,
    SandboxedOfficePdfConverter,
)


class OfficePdfConversionWorkerTests(unittest.TestCase):
    def _source(self, path: Path) -> AuthorizedOriginalFile:
        return AuthorizedOriginalFile(path.name, path, path.stat().st_size, sha256(path.read_bytes()).hexdigest())

    def _converter(self) -> SandboxedOfficePdfConverter:
        with (
            patch("case_kernel.office_pdf_conversion_worker._safe_executable", side_effect=lambda value, label: Path(value)),
            patch("case_kernel.office_pdf_conversion_worker._read_converter_version", return_value="LibreOffice test 1"),
        ):
            return SandboxedOfficePdfConverter(
                soffice_executable="/opt/test/soffice",
                sandbox_executable="/usr/bin/sandbox-exec",
                pdf_renderer_executable="/opt/test/pdftoppm",
            )

    def test_converter_stages_source_and_denies_network_before_accepting_pdf(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "答辩材料.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
            commands: list[tuple[str, ...]] = []

            def fake_run(command, **kwargs):
                commands.append(tuple(command))
                if "/opt/test/pdftoppm" in command:
                    rendered = Path(command[-1]).parent / "page-1.png"
                    from PIL import Image

                    Image.new("RGB", (300, 400), color="white").save(rendered)
                    return CompletedProcess(command, 0, b"", b"")
                output = Path(command[command.index("--outdir") + 1]) / "authorized-source.pdf"
                document = canvas.Canvas(str(output))
                document.drawString(30, 700, "synthetic converted PDF")
                document.save()
                return CompletedProcess(command, 0, b"", b"")

            with patch("case_kernel.office_pdf_conversion_worker.subprocess.run", side_effect=fake_run):
                result = self._converter().convert(self._source(path), detected_kind="WORD_DOCUMENT")
        command = commands[0]
        self.assertIn("(deny network*)", command[2])
        self.assertNotIn(str(path), command)
        self.assertIn("authorized-source.docx", " ".join(command))
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))
        self.assertEqual(result.page_count, 1)
        self.assertEqual(len(result.render_verification_hash), 64)
        self.assertIn("/opt/test/pdftoppm", commands[1])
        self.assertIn("(deny network*)", commands[1][2])

    def test_macro_source_is_blocked_without_starting_converter(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "宏材料.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/vbaProject.bin", b"macro")
            with patch("case_kernel.office_pdf_conversion_worker.subprocess.run") as run:
                with self.assertRaisesRegex(OfficePdfConversionBlocked, "structural inspection"):
                    self._converter().convert(self._source(path), detected_kind="WORD_DOCUMENT")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
