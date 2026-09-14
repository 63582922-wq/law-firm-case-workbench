"""Linux/server-safe rendering of generated DOCX/XLSX review candidates."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import os
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from pypdf import PdfReader

from .office_pdf_conversion_worker import ConvertedOfficePdf
from .material_format_inspection import inspect_non_pdf_material
from .reviewable_draft_worker import ReviewOfficeConversionBlocked


class WebOfficePdfConversionBlocked(ReviewOfficeConversionBlocked):
    """The isolated server document renderer is unavailable or unsafe."""


class WebOfficePdfConverter:
    """Convert only server-generated Office bytes in a private worker directory.

    Network isolation is a deployment responsibility of the document Worker
    container.  The process invocation itself is still fixed, absolute-path,
    timeout-bound, and never receives a browser path or command fragment.
    """

    def __init__(self, *, soffice_executable: str | Path, pdf_renderer_executable: str | Path, worker_root: str | Path, timeout_seconds: int = 120) -> None:
        self._soffice = _safe_executable(soffice_executable, "LibreOffice executable")
        self._renderer = _safe_executable(pdf_renderer_executable, "PDF renderer executable")
        self._worker_root = _safe_root(worker_root)
        _remove_interrupted_conversion_workspaces(self._worker_root)
        # Font discovery is the dominant cold-start cost in the isolated
        # renderer.  Keep only process/runtime caches across conversions; each
        # document still receives a fresh UserInstallation profile and private
        # input/output directory, so no editable document state is reused.
        self._runtime_home = _private_runtime_directory(
            self._worker_root / "runtime-home"
        )
        self._runtime_cache = _private_runtime_directory(
            self._worker_root / "runtime-cache"
        )
        if not 10 <= timeout_seconds <= 300:
            raise ValueError("Web Office conversion timeout is invalid")
        self._timeout = timeout_seconds
        self.converter_version = _version(self._soffice)

    def convert_generated_document(self, content: bytes, *, content_sha256: str, source_name: str, detected_kind: str) -> ConvertedOfficePdf:
        if detected_kind not in {"WORD_DOCUMENT", "SPREADSHEET"}:
            raise WebOfficePdfConversionBlocked("only DOCX and XLSX candidates can be rendered")
        suffix = ".docx" if detected_kind == "WORD_DOCUMENT" else ".xlsx"
        if not content or len(content) > 100 * 1024 * 1024 or sha256(content).hexdigest() != content_sha256:
            raise WebOfficePdfConversionBlocked("generated Office bytes are not hash-bound")
        if not source_name.casefold().endswith(suffix):
            raise WebOfficePdfConversionBlocked("generated Office extension is invalid")
        with TemporaryDirectory(prefix="web-office-", dir=self._worker_root) as temporary:
            root = Path(temporary)
            incoming, profile, output = (root / name for name in ("incoming", "profile", "output"))
            for directory in (incoming, profile, output):
                directory.mkdir(mode=0o700)
            source = incoming / ("candidate" + suffix)
            source.write_bytes(content)
            source.chmod(0o600)
            inspection = inspect_non_pdf_material(source, detected_kind=detected_kind)
            if inspection.outcome != "REVIEW_REQUIRED":
                raise WebOfficePdfConversionBlocked("generated Office structure was rejected")
            environment = {
                "HOME": str(self._runtime_home),
                "XDG_CACHE_HOME": str(self._runtime_cache),
                "TMPDIR": str(root), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            }
            command = [str(self._soffice), "--headless", "--nologo", "--nodefault", "--nolockcheck", "--norestore", "--nofirststartwizard", f"-env:UserInstallation={profile.as_uri()}", "--convert-to", "pdf", "--outdir", str(output), str(source)]
            try:
                result = subprocess.run(command, capture_output=True, check=False, timeout=self._timeout, env=environment)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WebOfficePdfConversionBlocked("server Office renderer is unavailable or timed out") from error
            rendered = output / "candidate.pdf"
            if result.returncode != 0 or rendered.is_symlink() or not rendered.is_file():
                raise WebOfficePdfConversionBlocked("server Office renderer did not produce a PDF")
            pdf = rendered.read_bytes()
            pages = _verify_pdf(pdf)
            verification_hash = _raster_verification(rendered, self._renderer, root)
        return ConvertedOfficePdf(
            source_sha256=content_sha256, detected_kind=detected_kind,
            converter_id="libreoffice-web-worker", converter_version=self.converter_version,
            transform_hash=sha256((content_sha256 + detected_kind + self.converter_version).encode()).hexdigest(),
            pdf_sha256=sha256(pdf).hexdigest(), pdf_bytes=len(pdf), page_count=pages,
            render_verification_hash=verification_hash, pdf_content=pdf,
        )


def _safe_executable(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise WebOfficePdfConversionBlocked(f"{label} is not an explicit executable")
    return path.resolve(strict=True)


def _safe_root(value: str | Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or root.is_symlink():
        raise WebOfficePdfConversionBlocked("Web document worker root is invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root.resolve(strict=True)


def _private_runtime_directory(path: Path) -> Path:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise WebOfficePdfConversionBlocked(
            "Web document renderer runtime cache is invalid"
        )
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    path.chmod(0o700)
    resolved = path.resolve(strict=True)
    if resolved.parent != path.parent.resolve(strict=True):
        raise WebOfficePdfConversionBlocked(
            "Web document renderer runtime cache escaped its private root"
        )
    return resolved


def _remove_interrupted_conversion_workspaces(worker_root: Path) -> None:
    """Remove only direct child workspaces left by an interrupted conversion.

    This runs before the renderer accepts requests, so no legitimate active
    conversion can exist.  Persistent runtime caches have different fixed
    names and are deliberately preserved.
    """

    for candidate in worker_root.iterdir():
        if not re.fullmatch(r"web-office-[A-Za-z0-9_-]{6,64}", candidate.name):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise WebOfficePdfConversionBlocked(
                "interrupted Web document workspace is unsafe"
            )
        if candidate.parent.resolve(strict=True) != worker_root:
            raise WebOfficePdfConversionBlocked(
                "interrupted Web document workspace escaped its private root"
            )
        shutil.rmtree(candidate)


def _version(executable: Path) -> str:
    try:
        result = subprocess.run([str(executable), "--version"], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WebOfficePdfConversionBlocked("server Office renderer version probe failed") from error
    text = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not text:
        raise WebOfficePdfConversionBlocked("server Office renderer version probe failed")
    return text[:160]


def _verify_pdf(content: bytes) -> int:
    if not content.startswith(b"%PDF-") or len(content) > 128 * 1024 * 1024:
        raise WebOfficePdfConversionBlocked("rendered review output is not a bounded PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise WebOfficePdfConversionBlocked("rendered review PDF is invalid")
        return len(reader.pages)
    except WebOfficePdfConversionBlocked:
        raise
    except Exception as error:
        raise WebOfficePdfConversionBlocked("rendered review PDF cannot be reopened") from error


def _raster_verification(pdf: Path, renderer: Path, root: Path) -> str:
    out = root / "raster"
    out.mkdir(mode=0o700)
    try:
        result = subprocess.run([str(renderer), "-f", "1", "-l", "1", "-singlefile", "-png", str(pdf), str(out / "page")], capture_output=True, check=False, timeout=60, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WebOfficePdfConversionBlocked("review PDF raster verification failed") from error
    image = out / "page.png"
    if result.returncode != 0 or not image.is_file() or image.stat().st_size < 100:
        raise WebOfficePdfConversionBlocked("review PDF raster verification produced no image")
    return sha256(image.read_bytes()).hexdigest()
