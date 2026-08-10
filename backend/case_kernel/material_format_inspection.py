"""Bounded structural inspection for non-PDF case materials.

The inspector is deliberately conversion-free.  It decides whether a local,
anti-malware-clean source is eligible for a later isolated conversion job or
must be blocked first.  It never extracts an archive or writes beside the
lawyer's source material.
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import stat
import unicodedata
import warnings
import zipfile
from xml.etree import ElementTree

from PIL import Image


@dataclass(frozen=True)
class MaterialFormatInspection:
    outcome: str
    reason_code: str
    details_hash: str


_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_ARCHIVE_ENTRY_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_MAX_RELATIONSHIP_BYTES = 5 * 1024 * 1024
_MAX_TEXT_BYTES = 25 * 1024 * 1024
_MAX_EMAIL_BYTES = 100 * 1024 * 1024
_MAX_EMAIL_PARTS = 2_000
_MAX_IMAGE_PIXELS = 100_000_000
_OLE_COMPOUND_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def inspect_non_pdf_material(path: Path, *, detected_kind: str) -> MaterialFormatInspection:
    """Inspect one immutable non-PDF source without extracting or rendering it."""
    inspectors = {
        "IMAGE": _inspect_image,
        "WORD_DOCUMENT": lambda value: _inspect_office_container(value, office_kind="WORD_DOCUMENT"),
        "SPREADSHEET": _inspect_spreadsheet,
        "TEXT": _inspect_text,
        "EMAIL": _inspect_email,
        "ARCHIVE": _inspect_generic_archive,
    }
    inspector = inspectors.get(detected_kind)
    if inspector is None:
        return _inspection(
            outcome="REVIEW_REQUIRED",
            reason_code="UNSUPPORTED_FILE_TYPE",
            details={"detected_kind": detected_kind, "inspection": "unsupported"},
        )
    return inspector(path)


def _inspect_image(path: Path) -> MaterialFormatInspection:
    suffix = path.suffix.casefold()
    if suffix == ".heic":
        header = _read_prefix(path, 32)
        if len(header) < 12 or header[4:8] != b"ftyp" or not any(
            brand in header[8:] for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
        ):
            return _blocked("FILE_SIGNATURE_MISMATCH", {"format": "HEIC"})
        return _review("HEIC_CONVERSION_REQUIRED", {"format": "HEIC", "decoder_verified": False})

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                frames = int(getattr(image, "n_frames", 1))
                if image_format not in {"JPEG", "PNG"}:
                    return _blocked("IMAGE_FORMAT_UNSUPPORTED", {"format": image_format or "UNKNOWN"})
                if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
                    return _blocked(
                        "IMAGE_PIXEL_LIMIT",
                        {"format": image_format, "width": width, "height": height},
                    )
                if frames != 1:
                    return _blocked("IMAGE_MULTIFRAME_UNSUPPORTED", {"format": image_format, "frames": frames})
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return _blocked("IMAGE_PIXEL_LIMIT", {"format": "UNKNOWN"})
    except Exception:
        return _blocked("IMAGE_MALFORMED", {"format": "UNKNOWN"})

    expected_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(suffix)
    if expected_format is None or image_format != expected_format:
        return _blocked(
            "FILE_SIGNATURE_MISMATCH",
            {"format": image_format, "extension": suffix},
        )
    return _review(
        "IMAGE_CONVERSION_REQUIRED",
        {"format": image_format, "width": width, "height": height, "frames": frames},
    )


def _inspect_spreadsheet(path: Path) -> MaterialFormatInspection:
    if path.suffix.casefold() == ".xls":
        if _read_prefix(path, len(_OLE_COMPOUND_SIGNATURE)) != _OLE_COMPOUND_SIGNATURE:
            return _blocked("FILE_SIGNATURE_MISMATCH", {"format": "XLS"})
        return _review(
            "LEGACY_SPREADSHEET_CONVERSION_REQUIRED",
            {"format": "OLE_COMPOUND_XLS", "container_verified": False},
        )
    return _inspect_office_container(path, office_kind="SPREADSHEET")


def _inspect_office_container(path: Path, *, office_kind: str) -> MaterialFormatInspection:
    checked = _inspect_zip_structure(path)
    if checked.outcome == "BLOCKED":
        return checked
    try:
        with zipfile.ZipFile(path) as archive:
            normalized_names = {_normalized_archive_name(item.filename) for item in archive.infolist()}
            required = (
                {"[content_types].xml", "word/document.xml"}
                if office_kind == "WORD_DOCUMENT"
                else {"[content_types].xml", "xl/workbook.xml"}
            )
            if not required.issubset(normalized_names):
                return _blocked("OFFICE_CONTAINER_MALFORMED", {"office_kind": office_kind})
            dangerous_prefixes = (
                "word/activex/",
                "word/embeddings/",
                "word/vbaproject.bin",
                "xl/activex/",
                "xl/embeddings/",
                "xl/vbaproject.bin",
                "customui/",
            )
            if any(name.startswith(dangerous_prefixes) for name in normalized_names):
                return _blocked("OFFICE_ACTIVE_CONTENT", {"office_kind": office_kind})
            content_types = archive.read("[Content_Types].xml")
            if len(content_types) > _MAX_RELATIONSHIP_BYTES:
                return _blocked("OFFICE_XML_LIMIT", {"office_kind": office_kind})
            lowered_content_types = content_types.lower()
            if b"<!doctype" in lowered_content_types or b"<!entity" in lowered_content_types:
                return _blocked("OFFICE_XML_ACTIVE_CONTENT", {"office_kind": office_kind})
            if b"macroenabled" in lowered_content_types or b"activex" in lowered_content_types:
                return _blocked("OFFICE_ACTIVE_CONTENT", {"office_kind": office_kind})
            relationship_result = _inspect_office_relationships(archive)
            if relationship_result is not None:
                return relationship_result
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return _blocked("OFFICE_CONTAINER_MALFORMED", {"office_kind": office_kind})
    reason = "WORD_CONVERSION_REQUIRED" if office_kind == "WORD_DOCUMENT" else "SPREADSHEET_CONVERSION_REQUIRED"
    return _review(reason, {"office_kind": office_kind, "container": "OOXML", "structure_hash": checked.details_hash})


def _inspect_office_relationships(archive: zipfile.ZipFile) -> MaterialFormatInspection | None:
    for item in archive.infolist():
        name = _normalized_archive_name(item.filename)
        if not name.endswith(".rels"):
            continue
        if item.file_size > _MAX_RELATIONSHIP_BYTES:
            return _blocked("OFFICE_RELATIONSHIP_LIMIT", {"relationship": name})
        try:
            raw = archive.read(item)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                return _blocked("OFFICE_XML_ACTIVE_CONTENT", {"relationship": name})
            root = ElementTree.fromstring(raw)
        except Exception:
            return _blocked("OFFICE_RELATIONSHIP_MALFORMED", {"relationship": name})
        for relationship in root.iter():
            if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            mode = str(relationship.attrib.get("TargetMode", "")).casefold()
            target = str(relationship.attrib.get("Target", "")).strip()
            if mode == "external" or _target_has_external_scheme(target):
                return _blocked("OFFICE_EXTERNAL_RELATIONSHIP", {"relationship": name})
            if _relationship_target_escapes_package(name, target):
                return _blocked("OFFICE_UNSAFE_RELATIONSHIP", {"relationship": name})
    return None


def _inspect_generic_archive(path: Path) -> MaterialFormatInspection:
    result = _inspect_zip_structure(path)
    if result.outcome == "BLOCKED":
        return result
    return _review("ARCHIVE_EXPANSION_REQUIRES_APPROVAL", {"structure_hash": result.details_hash})


def _inspect_zip_structure(path: Path) -> MaterialFormatInspection:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ARCHIVE_ENTRIES:
                return _blocked("ARCHIVE_ENTRY_LIMIT", {"entries": len(entries)})
            total_size = 0
            seen_names: set[str] = set()
            for item in entries:
                name = _validated_archive_name(item.filename)
                if name is None:
                    return _blocked("ARCHIVE_UNSAFE_PATH", {"entries": len(entries)})
                identity = unicodedata.normalize("NFC", name).casefold()
                if identity in seen_names:
                    return _blocked("ARCHIVE_DUPLICATE_PATH", {"entries": len(entries)})
                seen_names.add(identity)
                if item.flag_bits & 0x1:
                    return _blocked("ARCHIVE_ENCRYPTED", {"entry": identity})
                file_mode = (item.external_attr >> 16) & 0xFFFF
                if file_mode and stat.S_IFMT(file_mode) == stat.S_IFLNK:
                    return _blocked("ARCHIVE_SYMBOLIC_LINK", {"entry": identity})
                if item.file_size < 0 or item.file_size > _MAX_ARCHIVE_ENTRY_BYTES:
                    return _blocked("ARCHIVE_ENTRY_SIZE_LIMIT", {"entry": identity})
                total_size += item.file_size
                if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                    return _blocked("ARCHIVE_TOTAL_SIZE_LIMIT", {"entries": len(entries)})
                if (
                    item.file_size > 1024 * 1024
                    and item.file_size / max(item.compress_size, 1) > _MAX_COMPRESSION_RATIO
                ):
                    return _blocked("ARCHIVE_COMPRESSION_RATIO_LIMIT", {"entry": identity})
            return _inspection(
                outcome="REVIEW_REQUIRED",
                reason_code="ARCHIVE_STRUCTURE_SAFE",
                details={"entries": len(entries), "uncompressed_bytes": total_size},
            )
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return _blocked("ARCHIVE_MALFORMED", {"format": "ZIP"})


def _inspect_text(path: Path) -> MaterialFormatInspection:
    if path.stat().st_size > _MAX_TEXT_BYTES:
        return _blocked("TEXT_SIZE_LIMIT", {"byte_size": path.stat().st_size})
    raw = path.read_bytes()
    if b"\x00" in raw and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _blocked("TEXT_BINARY_CONTENT", {"encoding": "UNKNOWN"})
    try:
        if raw.startswith(b"\xff\xfe"):
            content = raw.decode("utf-16-le")
            encoding = "UTF-16LE"
        elif raw.startswith(b"\xfe\xff"):
            content = raw.decode("utf-16-be")
            encoding = "UTF-16BE"
        else:
            content = raw.decode("utf-8-sig")
            encoding = "UTF-8"
    except UnicodeDecodeError:
        return _blocked("TEXT_ENCODING_UNSUPPORTED", {"encoding": "UNKNOWN"})
    disallowed_controls = sum(1 for character in content if ord(character) < 32 and character not in "\n\r\t\f")
    if disallowed_controls:
        return _blocked("TEXT_CONTROL_CONTENT", {"controls": disallowed_controls})
    return _review("TEXT_CONVERSION_REQUIRED", {"encoding": encoding, "characters": len(content)})


def _inspect_email(path: Path) -> MaterialFormatInspection:
    byte_size = path.stat().st_size
    if byte_size > _MAX_EMAIL_BYTES:
        return _blocked("EMAIL_SIZE_LIMIT", {"byte_size": byte_size})
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception:
        return _blocked("EMAIL_MALFORMED", {"format": "EML"})
    if message.defects:
        return _blocked("EMAIL_MALFORMED", {"defects": len(message.defects)})
    part_count = 0
    attachment_count = 0
    for part in message.walk():
        part_count += 1
        if part_count > _MAX_EMAIL_PARTS:
            return _blocked("EMAIL_PART_LIMIT", {"parts": part_count})
        filename = part.get_filename()
        if filename is None:
            continue
        attachment_count += 1
        if _validated_archive_name(filename) is None or "/" in filename or "\\" in filename:
            return _blocked("EMAIL_UNSAFE_ATTACHMENT_NAME", {"parts": part_count})
    return _review(
        "EMAIL_CONVERSION_REQUIRED",
        {"format": "EML", "parts": part_count, "attachments": attachment_count},
    )


def _validated_archive_name(raw_name: str) -> str | None:
    if not raw_name or "\x00" in raw_name or len(raw_name) > 1_024:
        return None
    normalized = raw_name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or path.is_absolute() or ".." in path.parts:
        return None
    if path.parts and ":" in path.parts[0]:
        return None
    return normalized.rstrip("/") or None


def _normalized_archive_name(raw_name: str) -> str:
    return unicodedata.normalize("NFC", raw_name.replace("\\", "/").strip("/")).casefold()


def _target_has_external_scheme(target: str) -> bool:
    lowered = target.casefold()
    return any(lowered.startswith(prefix) for prefix in ("http:", "https:", "file:", "ftp:", "mailto:", "data:"))


def _relationship_target_escapes_package(relationship_name: str, target: str) -> bool:
    normalized_target = target.replace("\\", "/")
    if not normalized_target or normalized_target.startswith("/"):
        return bool(normalized_target.startswith("/"))
    relationship_path = PurePosixPath(relationship_name)
    if relationship_path == PurePosixPath("_rels/.rels"):
        base_parts: list[str] = []
    else:
        rels_parent = relationship_path.parent
        base_parts = list(rels_parent.parent.parts)
    for part in PurePosixPath(normalized_target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not base_parts:
                return True
            base_parts.pop()
        else:
            base_parts.append(part)
    return False


def _read_prefix(path: Path, length: int) -> bytes:
    with path.open("rb") as source:
        return source.read(length)


def _blocked(reason_code: str, details: dict[str, object]) -> MaterialFormatInspection:
    return _inspection(outcome="BLOCKED", reason_code=reason_code, details=details)


def _review(reason_code: str, details: dict[str, object]) -> MaterialFormatInspection:
    return _inspection(outcome="REVIEW_REQUIRED", reason_code=reason_code, details=details)


def _inspection(*, outcome: str, reason_code: str, details: dict[str, object]) -> MaterialFormatInspection:
    payload = {
        "schema_version": "material-format-inspection-v1",
        "outcome": outcome,
        "reason_code": reason_code,
        "details": details,
    }
    details_hash = sha256(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MaterialFormatInspection(outcome=outcome, reason_code=reason_code, details_hash=details_hash)
