"""Network-isolated Office-to-PDF conversion for structurally safe evidence.

The converter never passes an original case path to LibreOffice.  It re-hashes
the authorized source, copies its bytes into a private temporary directory,
runs a headless LibreOffice process under macOS sandbox-exec with networking
denied, then validates the resulting PDF.  Callers must encrypt the bytes in
managed artifact storage; this worker writes no output beside the original.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import os
import subprocess

from PIL import Image
from pypdf import PdfReader

from .local_access_grants import AuthorizedOriginalFile
from .material_format_inspection import inspect_non_pdf_material


class OfficePdfConversionBlocked(ValueError):
    """An Office source or isolated conversion output is unsafe."""


@dataclass(frozen=True)
class ConvertedOfficePdf:
    source_sha256: str
    detected_kind: str
    converter_id: str
    converter_version: str
    transform_hash: str
    pdf_sha256: str
    pdf_bytes: int
    page_count: int
    render_verification_hash: str
    pdf_content: bytes = b""


_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_PDF_BYTES = 100 * 1024 * 1024
_MAX_PAGES = 10_000
_SOFFICE_ARGS = ("--headless", "--nologo", "--nodefault", "--nolockcheck", "--norestore", "--nofirststartwizard")
_RENDER_DPI = 144


class SandboxedOfficePdfConverter:
    def __init__(
        self,
        *,
        soffice_executable: str | Path,
        sandbox_executable: str | Path = "/usr/bin/sandbox-exec",
        pdf_renderer_executable: str | Path,
        timeout_seconds: int = 120,
    ) -> None:
        self._soffice = _safe_executable(soffice_executable, "LibreOffice executable")
        self._sandbox = _safe_executable(sandbox_executable, "sandbox-exec executable")
        self._pdf_renderer = _safe_executable(pdf_renderer_executable, "PDF renderer executable")
        if timeout_seconds < 10 or timeout_seconds > 300:
            raise ValueError("Office conversion timeout must be between 10 and 300 seconds")
        self._timeout_seconds = timeout_seconds
        self._version = _read_converter_version(self._soffice)

    @property
    def converter_version(self) -> str:
        return self._version

    def convert(self, source: AuthorizedOriginalFile, *, detected_kind: str) -> ConvertedOfficePdf:
        if detected_kind not in {"WORD_DOCUMENT", "SPREADSHEET"}:
            raise OfficePdfConversionBlocked("isolated Office conversion supports only DOCX and XLSX evidence")
        _verify_source(source)
        if source.byte_size > _MAX_SOURCE_BYTES:
            raise OfficePdfConversionBlocked("Office source exceeds the conversion byte limit")
        inspection = inspect_non_pdf_material(source.path, detected_kind=detected_kind)
        if inspection.outcome != "REVIEW_REQUIRED":
            raise OfficePdfConversionBlocked(f"Office source was blocked by structural inspection: {inspection.reason_code}")
        with TemporaryDirectory(prefix="office-pdf-convert-") as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            profile = root / "profile"
            output = root / "output"
            incoming.mkdir(mode=0o700)
            profile.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            staged = incoming / _safe_staged_name(source.path.name, detected_kind)
            _copy_verified_source(source, staged)
            command = (
                str(self._sandbox),
                "-p",
                _network_denied_profile(),
                str(self._soffice),
                *_SOFFICE_ARGS,
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output),
                str(staged),
            )
            environment = {
                "HOME": str(root / "home"),
                "TMPDIR": str(root),
                "LANG": "zh_CN.UTF-8",
                "LC_ALL": "zh_CN.UTF-8",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }
            (root / "home").mkdir(mode=0o700)
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=self._timeout_seconds,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise OfficePdfConversionBlocked("isolated Office converter is unavailable or timed out") from error
            expected = output / f"{staged.stem}.pdf"
            if completed.returncode != 0 or not expected.is_file() or expected.is_symlink():
                raise OfficePdfConversionBlocked("isolated Office conversion did not produce the expected PDF")
            content = expected.read_bytes()
            render_verification_hash = _render_and_verify_pdf(
                expected,
                renderer=self._pdf_renderer,
                sandbox=self._sandbox,
                expected_page_count=_verify_pdf(content),
            )
        _verify_source(source)
        page_count = _verify_pdf(content)
        return ConvertedOfficePdf(
            source_sha256=source.sha256,
            detected_kind=detected_kind,
            converter_id="libreoffice-sandbox-exec",
            converter_version=self._version,
            transform_hash=_transform_hash(source.sha256, detected_kind, self._version),
            pdf_sha256=sha256(content).hexdigest(),
            pdf_bytes=len(content),
            page_count=page_count,
            render_verification_hash=render_verification_hash,
            pdf_content=content,
        )


def _safe_executable(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise OfficePdfConversionBlocked(f"{label} must be an explicit non-symbolic absolute path")
    resolved = raw.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise OfficePdfConversionBlocked(f"{label} is not executable")
    return resolved


def _read_converter_version(executable: Path) -> str:
    try:
        result: CompletedProcess[bytes] = subprocess.run(
            [str(executable), "--version"], check=False, capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OfficePdfConversionBlocked("LibreOffice version probe failed") from error
    output = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not output:
        raise OfficePdfConversionBlocked("LibreOffice version probe failed")
    return output[:160]


def _network_denied_profile() -> str:
    return "(version 1) (allow default) (deny network*)"


def _safe_staged_name(name: str, detected_kind: str) -> str:
    suffix = ".docx" if detected_kind == "WORD_DOCUMENT" else ".xlsx"
    if not name.casefold().endswith(suffix):
        raise OfficePdfConversionBlocked("Office source extension does not match its detected kind")
    return "authorized-source" + suffix


def _copy_verified_source(source: AuthorizedOriginalFile, destination: Path) -> None:
    content = source.path.read_bytes()
    if len(content) != source.byte_size or sha256(content).hexdigest() != source.sha256:
        raise OfficePdfConversionBlocked("Office source changed before isolation copy")
    destination.write_bytes(content)
    destination.chmod(0o600)


def _verify_source(source: AuthorizedOriginalFile) -> None:
    if source.path.is_symlink() or not source.path.is_file() or source.path.stat().st_size != source.byte_size:
        raise OfficePdfConversionBlocked("authorized Office source is missing, symbolic, or changed")
    digest = sha256()
    with source.path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != source.sha256:
        raise OfficePdfConversionBlocked("authorized Office source hash changed during conversion")


def _verify_pdf(content: bytes) -> int:
    if not content or len(content) > _MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
        raise OfficePdfConversionBlocked("isolated Office converter output is not a bounded PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise OfficePdfConversionBlocked("isolated Office converter output is encrypted")
        page_count = len(reader.pages)
        if not 1 <= page_count <= _MAX_PAGES:
            raise OfficePdfConversionBlocked("isolated Office PDF page count is outside the supported boundary")
        for page in reader.pages:
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise OfficePdfConversionBlocked("isolated Office PDF geometry is invalid")
    except OfficePdfConversionBlocked:
        raise
    except Exception as error:
        raise OfficePdfConversionBlocked("isolated Office converter output is malformed") from error
    return page_count


def _render_and_verify_pdf(
    pdf_path: Path, *, renderer: Path, sandbox: Path, expected_page_count: int
) -> str:
    """Require real raster output before an Office conversion becomes evidence.

    The PNGs are intentionally temporary: their hashes prove the exact rendered
    output that was checked, while the managed PDF remains the only retained
    derivative.  This catches conversion results that pass a PDF parser but
    cannot actually render or have invalid pixel geometry.
    """
    with TemporaryDirectory(prefix="office-pdf-render-check-") as temporary:
        prefix = Path(temporary) / "page"
        try:
            completed = subprocess.run(
                [
                    str(sandbox),
                    "-p",
                    _network_denied_profile(),
                    str(renderer),
                    "-r",
                    str(_RENDER_DPI),
                    "-png",
                    str(pdf_path),
                    str(prefix),
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OfficePdfConversionBlocked("Office PDF visual verification is unavailable") from error
        if completed.returncode != 0:
            raise OfficePdfConversionBlocked("Office PDF failed visual rendering verification")
        pages = sorted(
            Path(temporary).glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1])
        )
        if len(pages) != expected_page_count:
            raise OfficePdfConversionBlocked("Office PDF rendered page count does not match its PDF structure")
        rendered_pages: list[dict[str, int | str]] = []
        for index, page in enumerate(pages, start=1):
            try:
                with Image.open(page) as image:
                    image.verify()
                with Image.open(page) as image:
                    width, height = image.size
                    if width < 64 or height < 64:
                        raise OfficePdfConversionBlocked("Office PDF rendered page is implausibly small")
            except OfficePdfConversionBlocked:
                raise
            except Exception as error:
                raise OfficePdfConversionBlocked("Office PDF rendered page is unreadable") from error
            rendered_pages.append({"page": index, "sha256": sha256(page.read_bytes()).hexdigest(), "width": width, "height": height})
    payload = {
        "schema_version": "office-pdf-render-verification-v1",
        "pdf_sha256": sha256(pdf_path.read_bytes()).hexdigest(),
        "renderer": renderer.name,
        "renderer_sandbox": "network-denied-v1",
        "dpi": _RENDER_DPI,
        "pages": rendered_pages,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _transform_hash(source_sha256: str, detected_kind: str, converter_version: str) -> str:
    payload = {
        "schema_version": "office-pdf-conversion-transform-v1",
        "source_sha256": source_sha256,
        "detected_kind": detected_kind,
        "converter_id": "libreoffice-sandbox-exec",
        "converter_version": converter_version,
        "sandbox_profile": "network-denied-v1",
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
