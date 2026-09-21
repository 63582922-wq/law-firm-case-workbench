"""Source-bound visual/OCR candidates for evidence images and scanned pages.

This module is the shared contract between deterministic image admission, a
future local or external vision adapter, and lawyer review.  It deliberately
contains no HTTP transport and no persistence: a model receives only a
normalized single-page raster projection and can return review candidates.
It cannot authenticate an image, decide whether an alteration occurred,
create a case fact, classify a payment, or make a legal conclusion.

Native evidence images and pages rendered from PDF/OFD/Office enter the same
``VisualPageProjection`` after their actual image content has been decoded,
EXIF orientation normalized, flattened onto an explicit background, and
re-encoded to a single-frame PNG.  Extensions and browser media types are not
trusted at this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from io import BytesIO
import json
import math
import re
import warnings
from typing import Protocol
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError


VISUAL_PAGE_SCHEMA_VERSION = "visual-page-understanding-v1"
VISUAL_PAGE_PARSER_VERSION = "1.0.0"
VISUAL_PAGE_SKILL_ID = "image_visual_ocr"
MAX_SOURCE_IMAGE_BYTES = 64 * 1024 * 1024
MAX_NORMALIZED_PNG_BYTES = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 20_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_TEXT_BLOCKS = 2_000
MAX_TABLES = 200
MAX_FIELDS = 2_000
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLUMNS = 100
MAX_TOTAL_TEXT_CHARACTERS = 500_000


class VisualPageBlocked(ValueError):
    """A source page or untrusted vision result violates the visual contract."""


class VisualSourceKind(StrEnum):
    NATIVE_IMAGE = "NATIVE_IMAGE"
    RENDERED_PDF_PAGE = "RENDERED_PDF_PAGE"
    RENDERED_OFD_PAGE = "RENDERED_OFD_PAGE"
    RENDERED_OFFICE_PAGE = "RENDERED_OFFICE_PAGE"


class VisualCandidateStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"


class VisualBlockKind(StrEnum):
    TEXT = "TEXT"
    HEADING = "HEADING"
    HANDWRITING = "HANDWRITING"
    STAMP_TEXT = "STAMP_TEXT"
    SIGNATURE_TEXT = "SIGNATURE_TEXT"
    PAGE_NUMBER = "PAGE_NUMBER"
    UNKNOWN = "UNKNOWN"


class VisualFieldKind(StrEnum):
    PERSON_NAME = "PERSON_NAME"
    ORGANIZATION = "ORGANIZATION"
    DATE = "DATE"
    TIME = "TIME"
    AMOUNT = "AMOUNT"
    CURRENCY = "CURRENCY"
    ACCOUNT_IDENTIFIER = "ACCOUNT_IDENTIFIER"
    TRANSACTION_IDENTIFIER = "TRANSACTION_IDENTIFIER"
    CASE_IDENTIFIER = "CASE_IDENTIFIER"
    COURT_NAME = "COURT_NAME"
    PHONE_NUMBER = "PHONE_NUMBER"
    OTHER = "OTHER"


class VisualQualityRisk(StrEnum):
    LOW_RESOLUTION = "LOW_RESOLUTION"
    BLUR = "BLUR"
    GLARE = "GLARE"
    SHADOW = "SHADOW"
    SKEW = "SKEW"
    ROTATION_UNCERTAIN = "ROTATION_UNCERTAIN"
    OCCLUSION = "OCCLUSION"
    CROPPED_CONTENT = "CROPPED_CONTENT"
    COMPRESSION_ARTIFACTS = "COMPRESSION_ARTIFACTS"
    LOW_CONTRAST = "LOW_CONTRAST"
    SCREENSHOT_STITCH_RISK = "SCREENSHOT_STITCH_RISK"
    POSSIBLE_EDITING_RISK = "POSSIBLE_EDITING_RISK"
    COLOR_PROFILE_NORMALIZED = "COLOR_PROFILE_NORMALIZED"
    TRANSPARENCY_FLATTENED = "TRANSPARENCY_FLATTENED"


@dataclass(frozen=True)
class VisualRegion:
    """Normalized coordinates in [0, 1], independent of raster resolution."""

    x: float
    y: float
    width: float
    height: float

    def validate(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ):
            raise VisualPageBlocked("visual region coordinates must be finite numbers")
        if (
            self.x < 0
            or self.y < 0
            or self.width <= 0
            or self.height <= 0
            or self.x > 1
            or self.y > 1
            or self.width > 1
            or self.height > 1
            or self.x + self.width > 1.000000001
            or self.y + self.height > 1.000000001
        ):
            raise VisualPageBlocked("visual region exceeds the bound evidence page")


@dataclass(frozen=True)
class VisualPageProjection:
    matter_id: str
    evidence_page_id: str
    page_number: int
    source_kind: VisualSourceKind
    source_file_sha256: str
    source_page_sha256: str
    rendered_page_sha256: str
    width: int
    height: int
    media_type: str
    parser_id: str
    parser_version: str
    orientation_applied: int
    source_format: str
    had_transparency: bool
    projection_hash: str
    raster_content: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class VisualTextBlockCandidate:
    block_id: str
    kind: VisualBlockKind
    text: str
    region: VisualRegion
    confidence: float


@dataclass(frozen=True)
class VisualTableCandidate:
    table_id: str
    region: VisualRegion
    row_count: int
    column_count: int
    cells: tuple[tuple[str, ...], ...]
    confidence: float


@dataclass(frozen=True)
class VisualFieldCandidate:
    field_id: str
    kind: VisualFieldKind
    value: str
    region: VisualRegion
    confidence: float
    currency: str | None = None


@dataclass(frozen=True)
class VisualQualityRiskCandidate:
    code: VisualQualityRisk
    severity: str
    region: VisualRegion | None
    confidence: float
    note: str


@dataclass(frozen=True)
class VisualPageCandidate:
    matter_id: str
    evidence_page_id: str
    source_file_sha256: str
    source_page_sha256: str
    rendered_page_sha256: str
    projection_hash: str
    provider_id: str
    model_id: str
    provider_request_ref_hash: str
    text_blocks: tuple[VisualTextBlockCandidate, ...]
    tables: tuple[VisualTableCandidate, ...]
    fields: tuple[VisualFieldCandidate, ...]
    quality_risks: tuple[VisualQualityRiskCandidate, ...]
    status: VisualCandidateStatus
    candidate_hash: str


class VisualPageProvider(Protocol):
    """Server-side adapter contract; implementation must own authorization.

    A production provider must bind one already-authorized external request,
    preserve started/succeeded/failed/unknown receipts, and never auto-retry an
    unknown submission.  The protocol intentionally has no URL, API key,
    prompt, path or model parameter supplied by a browser or model.
    """

    provider_id: str
    model_id: str

    def analyze_page(
        self,
        *,
        projection: VisualPageProjection,
        external_request_id: str,
    ) -> str | bytes: ...


def build_visual_page_projection(
    *,
    matter_id: str,
    evidence_page_id: str,
    page_number: int,
    source_kind: VisualSourceKind,
    source_file_sha256: str,
    source_page_sha256: str,
    source_media_type: str,
    source_bytes: bytes,
) -> VisualPageProjection:
    """Decode one source-bound image and produce a canonical single-page PNG.

    ``source_media_type`` is an asserted registration value and is checked
    against decoded content.  It never selects a decoder by extension.
    """

    _require_uuid(matter_id, "matter_id")
    _require_uuid(evidence_page_id, "evidence_page_id")
    _require_positive(page_number, "page_number")
    if not isinstance(source_kind, VisualSourceKind):
        raise VisualPageBlocked("visual source kind is invalid")
    _require_sha256(source_file_sha256, "source_file_sha256")
    _require_sha256(source_page_sha256, "source_page_sha256")
    if not isinstance(source_bytes, bytes) or not 1 <= len(source_bytes) <= MAX_SOURCE_IMAGE_BYTES:
        raise VisualPageBlocked("visual source bytes are empty or exceed the page limit")
    if source_page_sha256 != sha256(source_bytes).hexdigest():
        raise VisualPageBlocked("visual source page hash differs from the registered bytes")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source_bytes)) as opened:
                source_format = str(opened.format or "").upper()
                if source_format not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP", "HEIF", "HEIC"}:
                    raise VisualPageBlocked("visual source format is unsupported")
                expected_media_type = _media_type_for_format(source_format)
                if source_media_type != expected_media_type:
                    raise VisualPageBlocked("registered media type differs from decoded image content")
                width, height = opened.size
                frames = int(getattr(opened, "n_frames", 1))
                if width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise VisualPageBlocked("visual source dimensions exceed the page limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise VisualPageBlocked("visual source exceeds the decompressed pixel limit")
                if frames != 1 or bool(getattr(opened, "is_animated", False)):
                    raise VisualPageBlocked("visual source must contain exactly one non-animated frame")
                orientation = int(opened.getexif().get(274, 1) or 1)
                if orientation not in range(1, 9):
                    raise VisualPageBlocked("visual source EXIF orientation is invalid")
                had_transparency = _has_transparency(opened)
                normalized = ImageOps.exif_transpose(opened)
                normalized.load()
                normalized = _flatten_to_rgb(normalized)
                normalized_width, normalized_height = normalized.size
                if normalized_width * normalized_height > MAX_IMAGE_PIXELS:
                    raise VisualPageBlocked("normalized visual page exceeds the pixel limit")
                output = BytesIO()
                normalized.save(
                    output,
                    format="PNG",
                    optimize=False,
                    compress_level=6,
                    icc_profile=None,
                    exif=b"",
                )
                png = output.getvalue()
    except VisualPageBlocked:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise VisualPageBlocked("visual source exceeds the decompressed pixel limit") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise VisualPageBlocked("visual source could not be safely decoded") from None

    if not 1 <= len(png) <= MAX_NORMALIZED_PNG_BYTES:
        raise VisualPageBlocked("normalized visual page exceeds the byte limit")
    _verify_canonical_png(png, normalized_width, normalized_height)
    rendered_hash = sha256(png).hexdigest()
    projection_payload = {
        "schema_version": "visual-page-projection-v1",
        "matter_id": matter_id,
        "evidence_page_id": evidence_page_id,
        "page_number": page_number,
        "source_kind": source_kind.value,
        "source_file_sha256": source_file_sha256,
        "source_page_sha256": source_page_sha256,
        "rendered_page_sha256": rendered_hash,
        "width": normalized_width,
        "height": normalized_height,
        "media_type": "image/png",
        "parser_id": "deterministic_visual_page_normalizer",
        "parser_version": VISUAL_PAGE_PARSER_VERSION,
        "orientation_applied": orientation,
        "source_format": source_format,
        "had_transparency": had_transparency,
    }
    return VisualPageProjection(
        matter_id=matter_id,
        evidence_page_id=evidence_page_id,
        page_number=page_number,
        source_kind=source_kind,
        source_file_sha256=source_file_sha256,
        source_page_sha256=source_page_sha256,
        rendered_page_sha256=rendered_hash,
        width=normalized_width,
        height=normalized_height,
        media_type="image/png",
        parser_id="deterministic_visual_page_normalizer",
        parser_version=VISUAL_PAGE_PARSER_VERSION,
        orientation_applied=orientation,
        source_format=source_format,
        had_transparency=had_transparency,
        projection_hash=_canonical_hash(projection_payload),
        raster_content=png,
    )


def visual_page_request_hash(projection: VisualPageProjection) -> str:
    _validate_projection(projection)
    return _canonical_hash(
        {
            "schema_version": "visual-page-model-input-v1",
            "projection_hash": projection.projection_hash,
            "rendered_page_sha256": projection.rendered_page_sha256,
            "width": projection.width,
            "height": projection.height,
        }
    )


def parse_visual_page_candidate(
    raw: str | bytes,
    *,
    projection: VisualPageProjection,
    expected_provider_id: str,
    expected_model_id: str,
    provider_request_ref_hash: str,
) -> VisualPageCandidate:
    """Parse one strict, source-bound model result into review-only candidates."""

    _validate_projection(projection)
    _require_code(expected_provider_id, "provider_id")
    _require_code(expected_model_id, "model_id")
    _require_sha256(provider_request_ref_hash, "provider_request_ref_hash")
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or not 2 <= len(encoded) <= 2 * 1024 * 1024:
        raise VisualPageBlocked("visual provider response size is invalid")
    try:
        json_payload = _exact_json_payload(encoded)
        value = json.loads(
            json_payload,
            parse_constant=lambda _: (_ for _ in ()).throw(
                VisualPageBlocked("visual provider response contains a non-finite number")
            ),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VisualPageBlocked("visual provider response must be one JSON object") from error
    required = {
        "schema_version",
        "request_hash",
        "matter_id",
        "evidence_page_id",
        "source_file_sha256",
        "source_page_sha256",
        "rendered_page_sha256",
        "projection_hash",
        "provider_id",
        "model_id",
        "text_blocks",
        "tables",
        "fields",
        "quality_risks",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise VisualPageBlocked("visual provider response schema is invalid")
    exact_bindings = {
        "schema_version": VISUAL_PAGE_SCHEMA_VERSION,
        "request_hash": visual_page_request_hash(projection),
        "matter_id": projection.matter_id,
        "evidence_page_id": projection.evidence_page_id,
        "source_file_sha256": projection.source_file_sha256,
        "source_page_sha256": projection.source_page_sha256,
        "rendered_page_sha256": projection.rendered_page_sha256,
        "projection_hash": projection.projection_hash,
        "provider_id": expected_provider_id,
        "model_id": expected_model_id,
    }
    if any(value.get(key) != expected for key, expected in exact_bindings.items()):
        raise VisualPageBlocked("visual provider response differs from the bound evidence page")
    blocks = _parse_text_blocks(value["text_blocks"])
    tables = _parse_tables(value["tables"])
    fields = _parse_fields(value["fields"])
    risks = _parse_quality_risks(value["quality_risks"])
    total_characters = sum(len(item.text) for item in blocks)
    total_characters += sum(len(cell) for table in tables for row in table.cells for cell in row)
    total_characters += sum(len(item.value) for item in fields)
    total_characters += sum(len(item.note) for item in risks)
    if total_characters > MAX_TOTAL_TEXT_CHARACTERS:
        raise VisualPageBlocked("visual provider response text exceeds the page limit")
    candidate_payload = {
        "schema_version": "visual-page-candidate-v1",
        "matter_id": projection.matter_id,
        "evidence_page_id": projection.evidence_page_id,
        "source_file_sha256": projection.source_file_sha256,
        "source_page_sha256": projection.source_page_sha256,
        "rendered_page_sha256": projection.rendered_page_sha256,
        "projection_hash": projection.projection_hash,
        "provider_id": expected_provider_id,
        "model_id": expected_model_id,
        "provider_request_ref_hash": provider_request_ref_hash,
        "text_blocks": [_text_block_payload(item) for item in blocks],
        "tables": [_table_payload(item) for item in tables],
        "fields": [_field_payload(item) for item in fields],
        "quality_risks": [_risk_payload(item) for item in risks],
        "status": VisualCandidateStatus.NEEDS_REVIEW.value,
    }
    return VisualPageCandidate(
        matter_id=projection.matter_id,
        evidence_page_id=projection.evidence_page_id,
        source_file_sha256=projection.source_file_sha256,
        source_page_sha256=projection.source_page_sha256,
        rendered_page_sha256=projection.rendered_page_sha256,
        projection_hash=projection.projection_hash,
        provider_id=expected_provider_id,
        model_id=expected_model_id,
        provider_request_ref_hash=provider_request_ref_hash,
        text_blocks=blocks,
        tables=tables,
        fields=fields,
        quality_risks=risks,
        status=VisualCandidateStatus.NEEDS_REVIEW,
        candidate_hash=_canonical_hash(candidate_payload),
    )


def build_server_bound_ocr_text_candidate(
    *,
    projection: VisualPageProjection,
    provider_id: str,
    model_id: str,
    provider_request_ref_hash: str,
    ocr_text: str,
) -> VisualPageCandidate:
    """Bind provider OCR text to server-owned provenance.

    Hosted OCR models are allowed to return text, but they are not trusted to
    echo evidence identifiers, source hashes, page geometry or a business
    schema.  This builder supplies those fields from the already-authorized
    projection and marks the entire page as one review-only text block.
    """

    _validate_projection(projection)
    _require_code(provider_id, "provider_id")
    _require_code(model_id, "model_id")
    _require_sha256(provider_request_ref_hash, "provider_request_ref_hash")
    text = _bounded_text(
        ocr_text,
        "OCR provider text",
        MAX_TOTAL_TEXT_CHARACTERS,
        allow_empty=False,
    ).strip()
    block = VisualTextBlockCandidate(
        block_id="ocr-full-page",
        kind=VisualBlockKind.TEXT,
        text=text,
        region=VisualRegion(x=0.0, y=0.0, width=1.0, height=1.0),
        # Qwen's plain OCR response does not expose a calibrated confidence.
        # A neutral midpoint prevents this transport field from becoming a
        # fabricated legal confidence score.
        confidence=0.5,
    )
    candidate_payload = {
        "schema_version": "visual-page-candidate-v1",
        "matter_id": projection.matter_id,
        "evidence_page_id": projection.evidence_page_id,
        "source_file_sha256": projection.source_file_sha256,
        "source_page_sha256": projection.source_page_sha256,
        "rendered_page_sha256": projection.rendered_page_sha256,
        "projection_hash": projection.projection_hash,
        "provider_id": provider_id,
        "model_id": model_id,
        "provider_request_ref_hash": provider_request_ref_hash,
        "text_blocks": [_text_block_payload(block)],
        "tables": [],
        "fields": [],
        "quality_risks": [],
        "status": VisualCandidateStatus.NEEDS_REVIEW.value,
    }
    return VisualPageCandidate(
        matter_id=projection.matter_id,
        evidence_page_id=projection.evidence_page_id,
        source_file_sha256=projection.source_file_sha256,
        source_page_sha256=projection.source_page_sha256,
        rendered_page_sha256=projection.rendered_page_sha256,
        projection_hash=projection.projection_hash,
        provider_id=provider_id,
        model_id=model_id,
        provider_request_ref_hash=provider_request_ref_hash,
        text_blocks=(block,),
        tables=(),
        fields=(),
        quality_risks=(),
        status=VisualCandidateStatus.NEEDS_REVIEW,
        candidate_hash=_canonical_hash(candidate_payload),
    )


def _exact_json_payload(encoded: bytes) -> bytes:
    """Accept raw JSON or Qwen's exact, content-free JSON code fence.

    Qwen OCR currently wraps an otherwise valid JSON object in one ``json``
    fence even when instructed not to use Markdown.  We remove only that
    exact wrapper.  Any prose, multiple fences or another fence language still
    reaches the strict JSON parser and fails closed.
    """

    text = encoded.decode("utf-8")
    stripped = text.strip()
    if not stripped.startswith("```"):
        return encoded
    lines = stripped.splitlines()
    if (
        len(lines) < 3
        or lines[0] != "```json"
        or lines[-1] != "```"
        or "```" in "\n".join(lines[1:-1])
    ):
        return encoded
    return "\n".join(lines[1:-1]).encode("utf-8")


def _parse_text_blocks(value: object) -> tuple[VisualTextBlockCandidate, ...]:
    if not isinstance(value, list) or len(value) > MAX_TEXT_BLOCKS:
        raise VisualPageBlocked("visual text block collection is invalid")
    result: list[VisualTextBlockCandidate] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"block_id", "kind", "text", "region", "confidence"}:
            raise VisualPageBlocked("visual text block schema is invalid")
        block_id = _unique_item_id(item["block_id"], ids, "block_id")
        try:
            kind = VisualBlockKind(item["kind"])
        except (TypeError, ValueError) as error:
            raise VisualPageBlocked("visual text block kind is invalid") from error
        text = _bounded_text(item["text"], "visual text block", 20_000, allow_empty=False)
        result.append(
            VisualTextBlockCandidate(
                block_id,
                kind,
                text,
                _parse_region(item["region"]),
                _confidence(item["confidence"]),
            )
        )
    return tuple(result)


def _parse_tables(value: object) -> tuple[VisualTableCandidate, ...]:
    if not isinstance(value, list) or len(value) > MAX_TABLES:
        raise VisualPageBlocked("visual table collection is invalid")
    result: list[VisualTableCandidate] = []
    ids: set[str] = set()
    for item in value:
        required = {"table_id", "region", "row_count", "column_count", "cells", "confidence"}
        if not isinstance(item, dict) or set(item) != required:
            raise VisualPageBlocked("visual table schema is invalid")
        table_id = _unique_item_id(item["table_id"], ids, "table_id")
        rows = item["row_count"]
        columns = item["column_count"]
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or not 1 <= rows <= MAX_TABLE_ROWS
            or isinstance(columns, bool)
            or not isinstance(columns, int)
            or not 1 <= columns <= MAX_TABLE_COLUMNS
        ):
            raise VisualPageBlocked("visual table dimensions are invalid")
        raw_cells = item["cells"]
        if not isinstance(raw_cells, list) or len(raw_cells) != rows:
            raise VisualPageBlocked("visual table rows do not match row_count")
        cells: list[tuple[str, ...]] = []
        for row in raw_cells:
            if not isinstance(row, list) or len(row) != columns:
                raise VisualPageBlocked("visual table columns do not match column_count")
            cells.append(
                tuple(_bounded_text(cell, "visual table cell", 4_000, allow_empty=True) for cell in row)
            )
        result.append(
            VisualTableCandidate(
                table_id,
                _parse_region(item["region"]),
                rows,
                columns,
                tuple(cells),
                _confidence(item["confidence"]),
            )
        )
    return tuple(result)


def _parse_fields(value: object) -> tuple[VisualFieldCandidate, ...]:
    if not isinstance(value, list) or len(value) > MAX_FIELDS:
        raise VisualPageBlocked("visual field collection is invalid")
    result: list[VisualFieldCandidate] = []
    ids: set[str] = set()
    for item in value:
        required = {"field_id", "kind", "value", "region", "confidence", "currency"}
        if not isinstance(item, dict) or set(item) != required:
            raise VisualPageBlocked("visual field schema is invalid")
        field_id = _unique_item_id(item["field_id"], ids, "field_id")
        try:
            kind = VisualFieldKind(item["kind"])
        except (TypeError, ValueError) as error:
            raise VisualPageBlocked("visual field kind is invalid") from error
        currency = item["currency"]
        if currency is not None and (not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None):
            raise VisualPageBlocked("visual field currency must be an ISO-style uppercase code")
        if kind is VisualFieldKind.AMOUNT and currency is None:
            # Amounts without a visible currency remain valid candidates, but
            # the model cannot silently assume CNY.  ``currency=None`` makes
            # the unresolved state explicit for lawyer review.
            pass
        elif kind is not VisualFieldKind.AMOUNT and currency is not None:
            raise VisualPageBlocked("only an amount field may carry currency")
        result.append(
            VisualFieldCandidate(
                field_id,
                kind,
                _bounded_text(item["value"], "visual field value", 4_000, allow_empty=False),
                _parse_region(item["region"]),
                _confidence(item["confidence"]),
                currency,
            )
        )
    return tuple(result)


def _parse_quality_risks(value: object) -> tuple[VisualQualityRiskCandidate, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise VisualPageBlocked("visual quality-risk collection is invalid")
    result: list[VisualQualityRiskCandidate] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        required = {"code", "severity", "region", "confidence", "note"}
        if not isinstance(item, dict) or set(item) != required:
            raise VisualPageBlocked("visual quality-risk schema is invalid")
        try:
            code = VisualQualityRisk(item["code"])
        except (TypeError, ValueError) as error:
            raise VisualPageBlocked("visual quality-risk code is invalid") from error
        severity = item["severity"]
        if severity not in {"LOW", "MEDIUM", "HIGH"}:
            raise VisualPageBlocked("visual quality-risk severity is invalid")
        note = _bounded_text(item["note"], "visual quality-risk note", 1_000, allow_empty=False)
        dedupe = (code.value, note.casefold())
        if dedupe in seen:
            raise VisualPageBlocked("visual quality-risk candidates contain duplicates")
        seen.add(dedupe)
        result.append(
            VisualQualityRiskCandidate(
                code,
                severity,
                None if item["region"] is None else _parse_region(item["region"]),
                _confidence(item["confidence"]),
                note,
            )
        )
    return tuple(result)


def _validate_projection(value: VisualPageProjection) -> None:
    if not isinstance(value, VisualPageProjection):
        raise VisualPageBlocked("visual page projection type is invalid")
    _require_uuid(value.matter_id, "projection matter_id")
    _require_uuid(value.evidence_page_id, "projection evidence_page_id")
    _require_positive(value.page_number, "projection page_number")
    if not isinstance(value.source_kind, VisualSourceKind):
        raise VisualPageBlocked("visual page projection source kind is invalid")
    _require_sha256(value.source_file_sha256, "projection source_file_sha256")
    _require_sha256(value.source_page_sha256, "projection source_page_sha256")
    _require_sha256(value.rendered_page_sha256, "projection rendered_page_sha256")
    _require_sha256(value.projection_hash, "projection_hash")
    if (
        not isinstance(value.raster_content, bytes)
        or not 1 <= len(value.raster_content) <= MAX_NORMALIZED_PNG_BYTES
        or sha256(value.raster_content).hexdigest() != value.rendered_page_sha256
    ):
        raise VisualPageBlocked("visual page raster differs from its source binding")
    if (
        value.media_type != "image/png"
        or value.parser_id != "deterministic_visual_page_normalizer"
        or value.parser_version != VISUAL_PAGE_PARSER_VERSION
        or value.source_format
        not in {"PNG", "JPEG", "TIFF", "WEBP", "BMP", "HEIF", "HEIC"}
        or isinstance(value.orientation_applied, bool)
        or not isinstance(value.orientation_applied, int)
        or value.orientation_applied not in range(1, 9)
        or not isinstance(value.had_transparency, bool)
    ):
        raise VisualPageBlocked("visual page projection parser contract is invalid")
    _verify_canonical_png(value.raster_content, value.width, value.height)
    expected = _canonical_hash(
        {
            "schema_version": "visual-page-projection-v1",
            "matter_id": value.matter_id,
            "evidence_page_id": value.evidence_page_id,
            "page_number": value.page_number,
            "source_kind": value.source_kind.value,
            "source_file_sha256": value.source_file_sha256,
            "source_page_sha256": value.source_page_sha256,
            "rendered_page_sha256": value.rendered_page_sha256,
            "width": value.width,
            "height": value.height,
            "media_type": value.media_type,
            "parser_id": value.parser_id,
            "parser_version": value.parser_version,
            "orientation_applied": value.orientation_applied,
            "source_format": value.source_format,
            "had_transparency": value.had_transparency,
        }
    )
    if value.projection_hash != expected:
        raise VisualPageBlocked("visual page projection hash differs from its exact provenance")


def _parse_region(value: object) -> VisualRegion:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise VisualPageBlocked("visual region schema is invalid")
    region = VisualRegion(value["x"], value["y"], value["width"], value["height"])
    region.validate()
    return region


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualPageBlocked("visual confidence must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise VisualPageBlocked("visual confidence must be between zero and one")
    return result


def _unique_item_id(value: object, seen: set[str], label: str) -> str:
    if not isinstance(value, str) or _ITEM_ID_RE.fullmatch(value) is None:
        raise VisualPageBlocked(f"visual {label} is invalid")
    if value in seen:
        raise VisualPageBlocked(f"visual {label} must be unique")
    seen.add(value)
    return value


def _bounded_text(value: object, label: str, maximum: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise VisualPageBlocked(f"{label} is invalid")
    if not allow_empty and not value.strip():
        raise VisualPageBlocked(f"{label} is empty")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise VisualPageBlocked(f"{label} contains control characters")
    return value


def _has_transparency(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if _has_transparency(image):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def _verify_canonical_png(content: bytes, width: int, height: int) -> None:
    if not isinstance(content, bytes) or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VisualPageBlocked("normalized visual page is not PNG")
    if width < 1 or height < 1 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise VisualPageBlocked("normalized visual page dimensions are invalid")
    if width * height > MAX_IMAGE_PIXELS:
        raise VisualPageBlocked("normalized visual page exceeds the pixel limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != "PNG" or image.size != (width, height):
                    raise VisualPageBlocked("normalized visual page metadata is inconsistent")
                if image.mode != "RGB":
                    raise VisualPageBlocked("normalized visual page must use the canonical RGB mode")
                if getattr(image, "is_animated", False) or int(getattr(image, "n_frames", 1)) != 1:
                    raise VisualPageBlocked("normalized visual page contains multiple frames")
                if image.info.get("transparency") is not None or image.mode in {"RGBA", "LA"}:
                    raise VisualPageBlocked("normalized visual page retains an ambiguous transparent layer")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except VisualPageBlocked:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise VisualPageBlocked("normalized visual page exceeds the decompressed pixel limit") from None
    except Exception:
        raise VisualPageBlocked("normalized visual page could not be verified") from None


def _media_type_for_format(value: str) -> str:
    mapping = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "TIFF": "image/tiff",
        "WEBP": "image/webp",
        "BMP": "image/bmp",
        "HEIF": "image/heif",
        "HEIC": "image/heic",
    }
    try:
        return mapping[value]
    except KeyError as error:
        raise VisualPageBlocked("visual source media type is unsupported") from error


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VisualPageBlocked("visual provider response contains duplicate JSON keys")
        result[key] = value
    return result


def _text_block_payload(value: VisualTextBlockCandidate) -> dict[str, object]:
    return {
        "block_id": value.block_id,
        "kind": value.kind.value,
        "text": value.text,
        "region": _region_payload(value.region),
        "confidence": format(value.confidence, ".8f"),
    }


def _table_payload(value: VisualTableCandidate) -> dict[str, object]:
    return {
        "table_id": value.table_id,
        "region": _region_payload(value.region),
        "row_count": value.row_count,
        "column_count": value.column_count,
        "cells": value.cells,
        "confidence": format(value.confidence, ".8f"),
    }


def _field_payload(value: VisualFieldCandidate) -> dict[str, object]:
    return {
        "field_id": value.field_id,
        "kind": value.kind.value,
        "value": value.value,
        "region": _region_payload(value.region),
        "confidence": format(value.confidence, ".8f"),
        "currency": value.currency,
    }


def _risk_payload(value: VisualQualityRiskCandidate) -> dict[str, object]:
    return {
        "code": value.code.value,
        "severity": value.severity,
        "region": _region_payload(value.region) if value.region else None,
        "confidence": format(value.confidence, ".8f"),
        "note": value.note,
    }


def _region_payload(value: VisualRegion) -> dict[str, str]:
    return {
        "x": format(value.x, ".8f"),
        "y": format(value.y, ".8f"),
        "width": format(value.width, ".8f"),
        "height": format(value.height, ".8f"),
    }


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise VisualPageBlocked(f"{label} must be a UUID") from error


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VisualPageBlocked(f"{label} must be a lowercase SHA-256")


def _require_positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VisualPageBlocked(f"{label} must be positive")


def _require_code(value: str, label: str) -> None:
    if not isinstance(value, str) or _CODE_RE.fullmatch(value) is None:
        raise VisualPageBlocked(f"{label} must be a stable code")


def _canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


__all__ = (
    "MAX_IMAGE_PIXELS",
    "VISUAL_PAGE_SCHEMA_VERSION",
    "VISUAL_PAGE_SKILL_ID",
    "VisualBlockKind",
    "VisualCandidateStatus",
    "VisualFieldCandidate",
    "VisualFieldKind",
    "VisualPageBlocked",
    "VisualPageCandidate",
    "VisualPageProjection",
    "VisualPageProvider",
    "VisualQualityRisk",
    "VisualQualityRiskCandidate",
    "VisualRegion",
    "VisualSourceKind",
    "VisualTableCandidate",
    "VisualTextBlockCandidate",
    "build_visual_page_projection",
    "build_server_bound_ocr_text_candidate",
    "parse_visual_page_candidate",
    "visual_page_request_hash",
)
