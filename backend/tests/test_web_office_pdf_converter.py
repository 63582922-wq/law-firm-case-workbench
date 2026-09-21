from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from case_kernel.web_office_pdf_converter import (
    WebOfficePdfConversionBlocked,
    WebOfficePdfConverter,
)


def _pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(output)
    return output.getvalue()


class WebOfficePdfConverterTests(unittest.TestCase):
    def test_startup_removes_only_interrupted_direct_child_workspaces(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            soffice = root / "soffice"
            renderer = root / "pdftoppm"
            for executable in (soffice, renderer):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o700)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            interrupted = worker_root / "web-office-abcdef12"
            interrupted.mkdir(mode=0o700)
            (interrupted / "candidate.pdf").write_bytes(b"private-derived-output")
            retained = worker_root / "operator-note"
            retained.mkdir(mode=0o700)

            with patch(
                "case_kernel.web_office_pdf_converter.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=b"LibreOffice 25.2.0",
                    stderr=b"",
                ),
            ):
                WebOfficePdfConverter(
                    soffice_executable=soffice,
                    pdf_renderer_executable=renderer,
                    worker_root=worker_root,
                )

            self.assertFalse(interrupted.exists())
            self.assertTrue(retained.is_dir())
            self.assertTrue((worker_root / "runtime-home").is_dir())
            self.assertTrue((worker_root / "runtime-cache").is_dir())

    def test_startup_rejects_a_workspace_symlink_without_touching_target(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            soffice = root / "soffice"
            renderer = root / "pdftoppm"
            for executable in (soffice, renderer):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o700)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            target = root / "must-remain"
            target.mkdir(mode=0o700)
            (target / "proof").write_text("retained", encoding="utf-8")
            (worker_root / "web-office-abcdef12").symlink_to(
                target, target_is_directory=True
            )

            with (
                patch(
                    "case_kernel.web_office_pdf_converter.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=b"LibreOffice 25.2.0",
                        stderr=b"",
                    ),
                ),
                self.assertRaisesRegex(
                    WebOfficePdfConversionBlocked, "workspace is unsafe"
                ),
            ):
                WebOfficePdfConverter(
                    soffice_executable=soffice,
                    pdf_renderer_executable=renderer,
                    worker_root=worker_root,
                )

            self.assertEqual((target / "proof").read_text(encoding="utf-8"), "retained")

    def test_runtime_font_cache_is_reused_but_document_profile_is_not(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            soffice = root / "soffice"
            renderer = root / "pdftoppm"
            for executable in (soffice, renderer):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o700)
            worker_root = root / "worker"
            worker_root.mkdir(mode=0o700)
            profiles: list[str] = []
            environments: list[dict[str, str]] = []

            def run(command, **kwargs):
                if command == [str(soffice.resolve()), "--version"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=b"LibreOffice 25.2.0",
                        stderr=b"",
                    )
                if "--convert-to" in command:
                    profiles.append(
                        next(
                            item.removeprefix("-env:UserInstallation=")
                            for item in command
                            if item.startswith("-env:UserInstallation=")
                        )
                    )
                    environments.append(dict(kwargs["env"]))
                    output = Path(command[command.index("--outdir") + 1])
                    (output / "candidate.pdf").write_bytes(_pdf())
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                prefix = Path(command[-1])
                prefix.with_suffix(".png").write_bytes(b"png" * 64)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                patch(
                    "case_kernel.web_office_pdf_converter.subprocess.run",
                    side_effect=run,
                ),
                patch(
                    "case_kernel.web_office_pdf_converter.inspect_non_pdf_material",
                    return_value=SimpleNamespace(outcome="REVIEW_REQUIRED"),
                ),
            ):
                converter = WebOfficePdfConverter(
                    soffice_executable=soffice,
                    pdf_renderer_executable=renderer,
                    worker_root=worker_root,
                )
                content = b"generated-office-content"
                for _ in range(2):
                    result = converter.convert_generated_document(
                        content,
                        content_sha256=sha256(content).hexdigest(),
                        source_name="candidate.docx",
                        detected_kind="WORD_DOCUMENT",
                    )
                    self.assertEqual(result.page_count, 1)

            runtime_home = worker_root / "runtime-home"
            runtime_cache = worker_root / "runtime-cache"
            self.assertEqual(len(profiles), 2)
            self.assertNotEqual(profiles[0], profiles[1])
            self.assertEqual(
                {item["HOME"] for item in environments},
                {str(runtime_home.resolve())},
            )
            self.assertEqual(
                {item["XDG_CACHE_HOME"] for item in environments},
                {str(runtime_cache.resolve())},
            )
            self.assertEqual(os.stat(runtime_home).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(runtime_cache).st_mode & 0o777, 0o700)
            self.assertEqual(list(runtime_home.rglob("candidate*")), [])
            self.assertEqual(list(runtime_cache.rglob("candidate*")), [])


if __name__ == "__main__":
    unittest.main()
