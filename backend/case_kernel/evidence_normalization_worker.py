"""Deterministic, local-only PDF normalization for safe non-PDF evidence.

Only material that has already passed the bounded format inspection reaches
this worker.  The worker writes its PDF solely into a caller-owned temporary
directory; callers must put a verified result into encrypted managed storage
and never beside the lawyer-selected original.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .local_access_grants import AuthorizedOriginalFile
from .material_format_inspection import inspect_non_pdf_material


class EvidenceNormalizationBlocked(ValueError):
    """A source or normalization output cannot safely be used as evidence."""


@dataclass(frozen=True)
class NormalizedEvidencePdf:
    source_sha256: str
    detected_kind: str
    source_media_type: str
    normalizer_id: str
    normalizer_version: str
    transform_hash: str
    pdf_sha256: str
    pdf_bytes: int
    page_count: int
    pdf_content: bytes = b""


_NORMALIZER_ID = "lawcase-local-normalizer"
_NORMALIZER_VERSION = "1"
_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_NORMALIZED_BYTES = 100 * 1024 * 1024
_MAX_TEXT_CHARACTERS = 250_000
_MAX_NORMALIZED_PAGES = 1_000
_TEXT_FONT = "STSong-Light"


def normalize_authorized_material(
    source: AuthorizedOriginalFile,
    *,
    detected_kind: str,
) -> NormalizedEvidencePdf:
    """Return a safe PDF representation for supported material classes.

    The original is re-hashed before and after rendering.  A result keeps no
    local source path, only hashes and normalizer provenance suitable for the
    later immutable evidence ledger.
    """
    _verify_source(source)
    if source.byte_size > _MAX_SOURCE_BYTES:
        raise EvidenceNormalizationBlocked("source exceeds the normalization byte limit")
    inspection = inspect_non_pdf_material(source.path, detected_kind=detected_kind)
    if inspection.outcome != "REVIEW_REQUIRED":
        raise EvidenceNormalizationBlocked(f"source is not eligible for normalization: {inspection.reason_code}")
    if detected_kind == "IMAGE":
        content, source_media_type, config = _normalize_image(source.path)
    elif detected_kind == "TEXT":
        content, source_media_type, config = _normalize_text(source.path)
    else:
        raise EvidenceNormalizationBlocked(
            f"no deterministic local normalizer is available for {detected_kind}; retain the review queue"
        )
    _verify_source(source)
    if not content or len(content) > _MAX_NORMALIZED_BYTES:
        raise EvidenceNormalizationBlocked("normalized PDF is empty or exceeds the byte limit")
    page_count = _verify_normalized_pdf(content)
    transform_hash = _transform_hash(
        source_sha256=source.sha256,
        detected_kind=detected_kind,
        source_media_type=source_media_type,
        config=config,
    )
    return NormalizedEvidencePdf(
        source_sha256=source.sha256,
        detected_kind=detected_kind,
        source_media_type=source_media_type,
        normalizer_id=_NORMALIZER_ID,
        normalizer_version=_NORMALIZER_VERSION,
        transform_hash=transform_hash,
        pdf_sha256=sha256(content).hexdigest(),
        pdf_bytes=len(content),
        page_count=page_count,
        pdf_content=content,
    )


def _normalize_image(path: Path) -> tuple[bytes, str, dict[str, object]]:
    try:
        with Image.open(path) as opened:
            image_format = str(opened.format or "").upper()
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            if image_format not in {"JPEG", "PNG"} or width < 1 or height < 1:
                raise EvidenceNormalizationBlocked("image format is not eligible for deterministic normalization")
            with TemporaryDirectory(prefix="evidence-image-normalize-") as temporary:
                output = Path(temporary) / "normalized.pdf"
                image.save(output, "PDF", resolution=144.0)
                content = output.read_bytes()
    except EvidenceNormalizationBlocked:
        raise
    except Exception as error:
        raise EvidenceNormalizationBlocked("image normalization failed safely") from error
    media_type = "image/jpeg" if image_format == "JPEG" else "image/png"
    return content, media_type, {"format": image_format, "orientation_normalized": True, "width": width, "height": height}


def _normalize_text(path: Path) -> tuple[bytes, str, dict[str, object]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16")
        encoding = "UTF-16"
    elif raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
        encoding = "UTF-16"
    else:
        text = raw.decode("utf-8-sig")
        encoding = "UTF-8"
    if len(text) > _MAX_TEXT_CHARACTERS:
        raise EvidenceNormalizationBlocked("text exceeds the normalization character limit")
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_TEXT_FONT))
        with TemporaryDirectory(prefix="evidence-text-normalize-") as temporary:
            output = Path(temporary) / "normalized.pdf"
            document = canvas.Canvas(str(output), pagesize=A4, pageCompression=1, invariant=1)
            document.setTitle("Evidence text normalization")
            document.setAuthor("Lawcase local normalizer")
            _draw_text(document, text)
            document.save()
            content = output.read_bytes()
    except EvidenceNormalizationBlocked:
        raise
    except Exception as error:
        raise EvidenceNormalizationBlocked("text normalization failed safely") from error
    return content, "text/plain", {"encoding": encoding, "font": _TEXT_FONT, "layout": "A4-10pt-v1"}


def _draw_text(document: canvas.Canvas, text: str) -> None:
    width, height = A4
    margin_x = 54
    margin_top = height - 56
    margin_bottom = 54
    font_size = 10
    leading = 15
    available_width = width - margin_x * 2
    document.setFont(_TEXT_FONT, font_size)
    y = margin_top
    page_count = 1
    for logical_line in text.splitlines() or [""]:
        for line in _wrap_text_line(logical_line.expandtabs(4), available_width, font_size):
            if y < margin_bottom:
                document.showPage()
                page_count += 1
                if page_count > _MAX_NORMALIZED_PAGES:
                    raise EvidenceNormalizationBlocked("text requires too many normalized pages")
                document.setFont(_TEXT_FONT, font_size)
                y = margin_top
            document.drawString(margin_x, y, line)
            y -= leading


def _wrap_text_line(value: str, width: float, font_size: int) -> tuple[str, ...]:
    if not value:
        return ("",)
    lines: list[str] = []
    current = ""
    for character in value:
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, _TEXT_FONT, font_size) > width:
            lines.append(current)
            current = character
        else:
            current = candidate
    lines.append(current)
    return tuple(lines)


def _verify_normalized_pdf(content: bytes) -> int:
    if content[:5] != b"%PDF-":
        raise EvidenceNormalizationBlocked("normalizer did not produce a PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise EvidenceNormalizationBlocked("normalizer produced an encrypted PDF")
        page_count = len(reader.pages)
        if not 1 <= page_count <= _MAX_NORMALIZED_PAGES:
            raise EvidenceNormalizationBlocked("normalized PDF page count is outside the limit")
        for page in reader.pages:
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise EvidenceNormalizationBlocked("normalized PDF has invalid page geometry")
    except EvidenceNormalizationBlocked:
        raise
    except Exception as error:
        raise EvidenceNormalizationBlocked("normalized PDF verification failed") from error
    return page_count


def _verify_source(source: AuthorizedOriginalFile) -> None:
    if source.path.is_symlink() or not source.path.is_file() or source.path.stat().st_size != source.byte_size:
        raise EvidenceNormalizationBlocked("authorized source is missing, symbolic, or changed")
    if _sha256_file(source.path) != source.sha256:
        raise EvidenceNormalizationBlocked("authorized source hash changed during normalization")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _transform_hash(
    *,
    source_sha256: str,
    detected_kind: str,
    source_media_type: str,
    config: dict[str, object],
) -> str:
    payload = {
        "schema_version": "evidence-normalization-transform-v1",
        "source_sha256": source_sha256,
        "detected_kind": detected_kind,
        "source_media_type": source_media_type,
        "normalizer_id": _NORMALIZER_ID,
        "normalizer_version": _NORMALIZER_VERSION,
        "config": config,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
