"""Content-first material identification and capability routing.

This registry is the admission boundary before a parser, converter, OCR model,
or command sandbox sees a case file.  It reads only a bounded prefix and, for
ZIP-family containers, bounded central-directory metadata.  It does not
extract archive members, decode images, execute macros, or parse document
body XML.

The result describes what the current server can *attempt next*.  It is not a
claim that the material has been parsed, rendered, authenticated, or accepted
as evidence.  Downstream skills must still perform their own structural,
malware, encryption, size, and provenance checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import stat
import struct
import unicodedata
import zipfile


class MaterialCanonicalKind(StrEnum):
    PDF = "PDF"
    OFD = "OFD"
    IMAGE = "IMAGE"
    WORD_DOCUMENT = "WORD_DOCUMENT"
    SPREADSHEET = "SPREADSHEET"
    PRESENTATION = "PRESENTATION"
    TEXT = "TEXT"
    EMAIL = "EMAIL"
    ARCHIVE = "ARCHIVE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    EXECUTABLE = "EXECUTABLE"
    DISK_IMAGE = "DISK_IMAGE"
    UNKNOWN = "UNKNOWN"


class MaterialRiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MaterialCapabilityMaturity(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    GATED = "GATED"
    PLANNED = "PLANNED"
    QUARANTINED = "QUARANTINED"


class MaterialRoutingStatus(StrEnum):
    ROUTABLE = "ROUTABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    QUARANTINED = "QUARANTINED"


class ExtensionAlignment(StrEnum):
    MATCH = "MATCH"
    MISSING = "MISSING"
    MISMATCH = "MISMATCH"
    DANGEROUS = "DANGEROUS"


@dataclass(frozen=True)
class MaterialTypeDecision:
    canonical_kind: MaterialCanonicalKind
    media_type: str
    actual_format: str
    risk_level: MaterialRiskLevel
    preferred_skill: str | None
    maturity: MaterialCapabilityMaturity
    routing_status: MaterialRoutingStatus
    extension_alignment: ExtensionAlignment
    reason_code: str
    reason: str
    follow_up_checks: tuple[str, ...]
    legacy_detected_kind: str | None
    inspected_prefix_bytes: int
    zip_entry_count: int | None
    decision_hash: str

    @property
    def routing_allowed(self) -> bool:
        """Whether a registered next skill may receive this file now."""

        return self.routing_status is MaterialRoutingStatus.ROUTABLE


@dataclass(frozen=True)
class _RecognizedFormat:
    canonical_kind: MaterialCanonicalKind
    media_type: str
    actual_format: str
    preferred_skill: str | None
    maturity: MaterialCapabilityMaturity
    allowed_extensions: frozenset[str]
    risk_level: MaterialRiskLevel
    follow_up_checks: tuple[str, ...]
    legacy_detected_kind: str | None


@dataclass(frozen=True)
class _ZipDirectory:
    names: frozenset[str]
    entry_count: int
    unsafe_reason_code: str | None = None
    unsafe_reason: str | None = None


_MAX_PREFIX_BYTES = 64 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
_MAX_ZIP_ENTRY_BYTES = 512 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ZIP_COMPRESSION_RATIO = 200
_MAX_ZIP_NAME_BYTES = 4 * 1024
_ZIP_RATIO_MINIMUM_BYTES = 1024 * 1024

_OLE_COMPOUND_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_MACHO_SIGNATURES = {
    bytes.fromhex("FEEDFACE"),
    bytes.fromhex("FEEDFACF"),
    bytes.fromhex("CEFAEDFE"),
    bytes.fromhex("CFFAEDFE"),
    bytes.fromhex("CAFEBABE"),
    bytes.fromhex("BEBAFECA"),
}
_EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".app",
        ".apk",
        ".bat",
        ".bin",
        ".class",
        ".cmd",
        ".com",
        ".cpl",
        ".dll",
        ".dylib",
        ".elf",
        ".exe",
        ".hta",
        ".ipa",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".msi",
        ".ps1",
        ".py",
        ".scr",
        ".sh",
        ".so",
        ".vbs",
        ".wsf",
    }
)
_DISK_IMAGE_EXTENSIONS = frozenset({".dmg", ".img", ".iso", ".qcow", ".qcow2", ".vhd", ".vhdx"})
_MACRO_OFFICE_EXTENSIONS = frozenset(
    {".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".potm", ".ppam", ".ppsm"}
)
_OOXML_ACTIVE_PARTS = (
    "/vbaproject.bin",
    "/activex/",
    "/embeddings/",
    "customui/",
)
_ZIP_EXECUTABLE_SUFFIXES = _EXECUTABLE_EXTENSIONS | _MACRO_OFFICE_EXTENSIONS

_IMAGE_FOLLOW_UP = (
    "ISOLATED_FULL_IMAGE_DECODE_REQUIRED",
    "DIMENSION_PIXEL_AND_MULTIFRAME_CHECK_REQUIRED",
    "OCR_AND_VISUAL_OUTPUT_MUST_REMAIN_SOURCE_BOUND_CANDIDATES",
)
_PDF_FOLLOW_UP = (
    "FULL_PDF_STRUCTURE_AND_ENCRYPTION_CHECK_REQUIRED",
    "ACTIVE_CONTENT_AND_PAGE_LIMIT_CHECK_REQUIRED",
)
_OFFICE_FOLLOW_UP = (
    "FULL_OFFICE_STRUCTURE_AND_RELATIONSHIP_CHECK_REQUIRED",
    "ISOLATED_RENDER_REQUIRED_BEFORE_VISUAL_REVIEW",
)


def identify_material_type(path: Path) -> MaterialTypeDecision:
    """Identify one immutable case file without parsing its body.

    The caller remains responsible for binding the decision to a full source
    hash and for running malware scanning before any downstream skill.  A
    mismatched or missing extension never reaches a parser without review.
    """

    source = Path(path)
    prefix = _read_bounded_prefix(source)
    suffix = source.suffix.casefold()

    if suffix in _MACRO_OFFICE_EXTENSIONS:
        return _quarantined(
            actual_format="MACRO_ENABLED_OFFICE",
            canonical_kind=_office_kind_for_macro_extension(suffix),
            media_type="application/vnd.ms-office",
            reason_code="MACRO_ENABLED_EXTENSION",
            reason="文件名声明为可执行宏的 Office 格式；不得交给 Office 或命令执行器。",
            prefix_bytes=len(prefix),
            extension_alignment=ExtensionAlignment.DANGEROUS,
        )

    executable_format = _detect_executable_signature(prefix)
    if executable_format is not None:
        return _quarantined(
            actual_format=executable_format,
            canonical_kind=MaterialCanonicalKind.EXECUTABLE,
            media_type="application/x-executable",
            reason_code="EXECUTABLE_CONTENT",
            reason="内容签名表明该文件可执行；案卷文件不得执行。",
            prefix_bytes=len(prefix),
            extension_alignment=_dangerous_or_mismatch(suffix, _EXECUTABLE_EXTENSIONS),
        )

    if _looks_like_iso9660(prefix):
        return _quarantined(
            actual_format="ISO_9660",
            canonical_kind=MaterialCanonicalKind.DISK_IMAGE,
            media_type="application/x-iso9660-image",
            reason_code="DISK_IMAGE_CONTENT",
            reason="内容签名表明该文件是磁盘镜像；不能挂载或执行。",
            prefix_bytes=len(prefix),
            extension_alignment=_dangerous_or_mismatch(suffix, frozenset({".iso"})),
        )

    if prefix.startswith(b"%PDF-") and b"/ENCRYPT" in prefix.upper():
        return _quarantined(
            actual_format="ENCRYPTED_PDF",
            canonical_kind=MaterialCanonicalKind.PDF,
            media_type="application/pdf",
            reason_code="PDF_ENCRYPTED",
            reason="PDF 前缀声明加密；只能请求密码或未加密副本，不能进入解析链。",
            prefix_bytes=len(prefix),
            extension_alignment=_extension_alignment(suffix, frozenset({".pdf"})),
        )

    if prefix.startswith(_OLE_COMPOUND_SIGNATURE):
        return _quarantined(
            actual_format="OLE_COMPOUND",
            canonical_kind=MaterialCanonicalKind.UNKNOWN,
            media_type="application/x-ole-storage",
            reason_code="OPAQUE_OLE_COMPOUND",
            reason="旧式 OLE 容器可能包含宏、嵌入对象或加密 Office 内容，当前不安全路由。",
            prefix_bytes=len(prefix),
            extension_alignment=(
                ExtensionAlignment.DANGEROUS
                if suffix in _EXECUTABLE_EXTENSIONS | _MACRO_OFFICE_EXTENSIONS
                else ExtensionAlignment.MATCH
                if suffix in {".doc", ".xls", ".ppt", ".msg"}
                else ExtensionAlignment.MISMATCH
            ),
        )

    if _is_zip_signature(prefix):
        return _identify_zip_family(source, prefix_bytes=len(prefix), suffix=suffix)

    recognized = _identify_non_zip_signature(prefix, suffix=suffix, file_size=source.stat().st_size)
    if recognized is None:
        if suffix in _DISK_IMAGE_EXTENSIONS:
            return _quarantined(
                actual_format="SUSPECTED_DISK_IMAGE",
                canonical_kind=MaterialCanonicalKind.DISK_IMAGE,
                media_type="application/x-disk-image",
                reason_code="DANGEROUS_DISK_IMAGE_EXTENSION",
                reason="磁盘镜像后缀不允许进入解析或挂载链，且未在有界前缀内确认安全格式。",
                prefix_bytes=len(prefix),
                extension_alignment=ExtensionAlignment.DANGEROUS,
            )
        if suffix in _EXECUTABLE_EXTENSIONS:
            return _quarantined(
                actual_format="SUSPECTED_EXECUTABLE",
                canonical_kind=MaterialCanonicalKind.EXECUTABLE,
                media_type="application/x-executable",
                reason_code="DANGEROUS_EXECUTABLE_EXTENSION",
                reason="可执行或脚本后缀不允许进入案卷解析链。",
                prefix_bytes=len(prefix),
                extension_alignment=ExtensionAlignment.DANGEROUS,
            )
        return _quarantined(
            actual_format="UNKNOWN_BINARY",
            canonical_kind=MaterialCanonicalKind.UNKNOWN,
            media_type="application/octet-stream",
            reason_code="UNKNOWN_HIGH_RISK_FORMAT",
            reason="未能用受支持的内容签名或安全文本规则识别该文件；默认隔离。",
            prefix_bytes=len(prefix),
            extension_alignment=ExtensionAlignment.MISMATCH if suffix else ExtensionAlignment.MISSING,
        )
    return _decision_for_recognized(recognized, suffix=suffix, prefix_bytes=len(prefix), zip_entry_count=None)


# The alias makes the intended use clear to callers that think in registry
# terms while preserving one canonical implementation.
classify_material = identify_material_type


def _identify_zip_family(path: Path, *, prefix_bytes: int, suffix: str) -> MaterialTypeDecision:
    directory = _inspect_zip_directory(path)
    if directory.unsafe_reason_code is not None:
        return _quarantined(
            actual_format="ZIP_CONTAINER",
            canonical_kind=MaterialCanonicalKind.ARCHIVE,
            media_type="application/zip",
            reason_code=directory.unsafe_reason_code,
            reason=directory.unsafe_reason or "ZIP 容器未通过安全目录检查。",
            prefix_bytes=prefix_bytes,
            extension_alignment=_extension_alignment(suffix, frozenset({".zip"})),
            zip_entry_count=directory.entry_count,
        )

    names = directory.names
    if _zip_contains_active_office_content(names):
        return _quarantined(
            actual_format="OOXML_ACTIVE_CONTENT",
            canonical_kind=_ooxml_kind_from_names(names) or MaterialCanonicalKind.UNKNOWN,
            media_type="application/vnd.ms-office",
            reason_code="OOXML_ACTIVE_CONTENT",
            reason="OOXML 中央目录包含宏、ActiveX、嵌入对象或自定义可执行界面内容。",
            prefix_bytes=prefix_bytes,
            extension_alignment=ExtensionAlignment.DANGEROUS,
            zip_entry_count=directory.entry_count,
        )

    if _zip_contains_executable(names):
        return _quarantined(
            actual_format="EXECUTABLE_ARCHIVE",
            canonical_kind=MaterialCanonicalKind.ARCHIVE,
            media_type="application/zip",
            reason_code="ARCHIVE_CONTAINS_EXECUTABLE",
            reason="压缩包目录包含可执行文件或脚本；不得自动解包或执行。",
            prefix_bytes=prefix_bytes,
            extension_alignment=_extension_alignment(suffix, frozenset({".zip"})),
            zip_entry_count=directory.entry_count,
        )

    if "ofd.xml" in names:
        recognized = _format(
            MaterialCanonicalKind.OFD,
            "application/ofd",
            "OFD",
            "ofd_reading",
            MaterialCapabilityMaturity.PLANNED,
            {".ofd"},
            follow_up=("OFD_PACKAGE_STRUCTURE_AND_SIGNATURE_CHECK_REQUIRED",),
            legacy=None,
        )
        return _decision_for_recognized(
            recognized, suffix=suffix, prefix_bytes=prefix_bytes, zip_entry_count=directory.entry_count
        )

    ooxml_families: list[_RecognizedFormat] = []
    if "[content_types].xml" in names and "word/document.xml" in names:
        ooxml_families.append(
            _format(
                MaterialCanonicalKind.WORD_DOCUMENT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "OOXML_DOCX",
                "office_reading",
                MaterialCapabilityMaturity.IMPLEMENTED,
                {".docx"},
                follow_up=_OFFICE_FOLLOW_UP,
                legacy="WORD_DOCUMENT",
            )
        )
    if "[content_types].xml" in names and "xl/workbook.xml" in names:
        ooxml_families.append(
            _format(
                MaterialCanonicalKind.SPREADSHEET,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "OOXML_XLSX",
                "office_reading",
                MaterialCapabilityMaturity.IMPLEMENTED,
                {".xlsx"},
                follow_up=_OFFICE_FOLLOW_UP,
                legacy="SPREADSHEET",
            )
        )
    if "[content_types].xml" in names and "ppt/presentation.xml" in names:
        ooxml_families.append(
            _format(
                MaterialCanonicalKind.PRESENTATION,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "OOXML_PPTX",
                "presentation_reading",
                MaterialCapabilityMaturity.PLANNED,
                {".pptx"},
                follow_up=_OFFICE_FOLLOW_UP,
                legacy=None,
            )
        )
    if len(ooxml_families) > 1:
        return _quarantined(
            actual_format="AMBIGUOUS_OOXML",
            canonical_kind=MaterialCanonicalKind.UNKNOWN,
            media_type="application/zip",
            reason_code="OOXML_MULTIPLE_DOCUMENT_FAMILIES",
            reason="同一 OOXML 容器同时声明多个主文档家族，不能安全路由。",
            prefix_bytes=prefix_bytes,
            extension_alignment=ExtensionAlignment.MISMATCH,
            zip_entry_count=directory.entry_count,
        )
    if len(ooxml_families) == 1:
        return _decision_for_recognized(
            ooxml_families[0], suffix=suffix, prefix_bytes=prefix_bytes, zip_entry_count=directory.entry_count
        )

    recognized = _format(
        MaterialCanonicalKind.ARCHIVE,
        "application/zip",
        "ZIP",
        "archive_intake",
        MaterialCapabilityMaturity.PLANNED,
        {".zip"},
        follow_up=("CONTROLLED_EXPANSION_AND_CHILD_REGISTRATION_REQUIRED",),
        legacy="ARCHIVE",
        risk=MaterialRiskLevel.MODERATE,
    )
    return _decision_for_recognized(
        recognized, suffix=suffix, prefix_bytes=prefix_bytes, zip_entry_count=directory.entry_count
    )


def _inspect_zip_directory(path: Path) -> _ZipDirectory:
    preflight = _preflight_zip_eocd(path)
    if preflight is not None:
        return preflight
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ZIP_ENTRIES:
                return _unsafe_zip(entries, "ZIP_ENTRY_LIMIT", "ZIP 条目数超过安全上限。")
            central_bytes = len(archive.comment)
            total_uncompressed = 0
            identities: set[str] = set()
            normalized_names: set[str] = set()
            for item in entries:
                raw_name_bytes = item.filename.encode("utf-8", errors="surrogatepass")
                central_bytes += 46 + len(raw_name_bytes) + len(item.extra) + len(item.comment)
                if len(raw_name_bytes) > _MAX_ZIP_NAME_BYTES or central_bytes > _MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
                    return _unsafe_zip(entries, "ZIP_CENTRAL_DIRECTORY_LIMIT", "ZIP 中央目录超过安全上限。")
                normalized = _validated_zip_name(item.filename)
                if normalized is None:
                    return _unsafe_zip(entries, "ZIP_UNSAFE_PATH", "ZIP 中央目录包含越界或无效路径。")
                identity = unicodedata.normalize("NFC", normalized).casefold()
                if identity in identities:
                    return _unsafe_zip(entries, "ZIP_DUPLICATE_PATH", "ZIP 中央目录包含规范化后重复路径。")
                identities.add(identity)
                normalized_names.add(identity)
                if item.flag_bits & 0x1:
                    return _unsafe_zip(entries, "ZIP_ENCRYPTED", "加密 ZIP 不得自动解包或路由。")
                file_mode = (item.external_attr >> 16) & 0xFFFF
                if file_mode and stat.S_IFMT(file_mode) == stat.S_IFLNK:
                    return _unsafe_zip(entries, "ZIP_SYMBOLIC_LINK", "ZIP 中央目录包含符号链接。")
                if item.file_size < 0 or item.file_size > _MAX_ZIP_ENTRY_BYTES:
                    return _unsafe_zip(entries, "ZIP_ENTRY_SIZE_LIMIT", "ZIP 单个条目的声明大小超过安全上限。")
                total_uncompressed += item.file_size
                if total_uncompressed > _MAX_ZIP_TOTAL_BYTES:
                    return _unsafe_zip(entries, "ZIP_TOTAL_SIZE_LIMIT", "ZIP 声明的总展开大小超过安全上限。")
                if (
                    item.file_size >= _ZIP_RATIO_MINIMUM_BYTES
                    and item.file_size / max(item.compress_size, 1) > _MAX_ZIP_COMPRESSION_RATIO
                ):
                    return _unsafe_zip(entries, "ZIP_COMPRESSION_RATIO_LIMIT", "ZIP 条目的压缩比疑似压缩炸弹。")
            return _ZipDirectory(frozenset(normalized_names), len(entries))
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return _ZipDirectory(frozenset(), 0, "MALFORMED_ZIP_CONTAINER", "ZIP 容器损坏或目录不可安全读取。")


def _preflight_zip_eocd(path: Path) -> _ZipDirectory | None:
    """Reject unsafe ZIP directory declarations before ``zipfile`` allocates them."""

    try:
        file_size = path.stat().st_size
        tail_size = min(file_size, 65_557)
        with path.open("rb") as source:
            source.seek(file_size - tail_size)
            tail = source.read(tail_size)
    except OSError:
        return _ZipDirectory(frozenset(), 0, "MALFORMED_ZIP_CONTAINER", "ZIP 容器不可读取。")
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or marker + 22 > len(tail):
        return _ZipDirectory(frozenset(), 0, "MALFORMED_ZIP_CONTAINER", "ZIP 中央目录结束记录缺失。")
    try:
        (
            disk_number,
            central_disk,
            disk_entries,
            total_entries,
            central_size,
            central_offset,
            comment_length,
        ) = struct.unpack_from("<HHHHIIH", tail, marker + 4)
    except struct.error:
        return _ZipDirectory(frozenset(), 0, "MALFORMED_ZIP_CONTAINER", "ZIP 中央目录结束记录损坏。")
    if marker + 22 + comment_length != len(tail):
        return _ZipDirectory(frozenset(), total_entries, "MALFORMED_ZIP_CONTAINER", "ZIP 尾部记录长度不一致。")
    if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
        return _ZipDirectory(frozenset(), total_entries, "ZIP_MULTIDISK_UNSUPPORTED", "多卷 ZIP 不得自动处理。")
    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        return _ZipDirectory(frozenset(), total_entries, "ZIP64_REQUIRES_REVIEW", "ZIP64 容器需专用有界检查器。")
    if total_entries > _MAX_ZIP_ENTRIES:
        return _ZipDirectory(frozenset(), total_entries, "ZIP_ENTRY_LIMIT", "ZIP 条目数超过安全上限。")
    if central_size > _MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
        return _ZipDirectory(
            frozenset(), total_entries, "ZIP_CENTRAL_DIRECTORY_LIMIT", "ZIP 中央目录超过安全上限。"
        )
    eocd_offset = file_size - tail_size + marker
    if central_offset + central_size > eocd_offset:
        return _ZipDirectory(frozenset(), total_entries, "MALFORMED_ZIP_CONTAINER", "ZIP 中央目录越界。")
    return None


def _identify_non_zip_signature(prefix: bytes, *, suffix: str, file_size: int) -> _RecognizedFormat | None:
    if prefix.startswith(b"%PDF-"):
        return _format(
            MaterialCanonicalKind.PDF,
            "application/pdf",
            "PDF",
            "pdf_reading",
            MaterialCapabilityMaturity.IMPLEMENTED,
            {".pdf"},
            follow_up=_PDF_FOLLOW_UP,
            legacy="PDF",
        )
    if prefix.startswith(b"\xff\xd8\xff"):
        return _image_format("JPEG", "image/jpeg", {".jpg", ".jpeg", ".jpe"})
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return _image_format("PNG", "image/png", {".png"})
    if prefix.startswith((b"II*\x00", b"MM\x00*")):
        return _image_format("TIFF", "image/tiff", {".tif", ".tiff"}, maturity=MaterialCapabilityMaturity.PLANNED)
    if prefix.startswith(b"BM"):
        return _image_format("BMP", "image/bmp", {".bmp"}, maturity=MaterialCapabilityMaturity.PLANNED)
    if len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return _image_format("WEBP", "image/webp", {".webp"}, maturity=MaterialCapabilityMaturity.PLANNED)
    if len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return _format(
            MaterialCanonicalKind.AUDIO,
            "audio/wav",
            "WAV",
            "audio_transcription",
            MaterialCapabilityMaturity.PLANNED,
            {".wav"},
            follow_up=("AUDIO_METADATA_DURATION_AND_CODEC_CHECK_REQUIRED",),
            legacy=None,
        )
    if prefix.startswith(b"ID3") or _looks_like_mp3_frame(prefix):
        return _format(
            MaterialCanonicalKind.AUDIO,
            "audio/mpeg",
            "MP3",
            "audio_transcription",
            MaterialCapabilityMaturity.PLANNED,
            {".mp3"},
            follow_up=("AUDIO_METADATA_DURATION_AND_CODEC_CHECK_REQUIRED",),
            legacy=None,
        )
    bmff_brand = _iso_bmff_brand(prefix)
    if bmff_brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
        return _image_format("HEIC", "image/heic", {".heic", ".heif"}, maturity=MaterialCapabilityMaturity.PLANNED)
    if bmff_brand in {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}:
        return _format(
            MaterialCanonicalKind.AUDIO,
            "audio/mp4",
            "M4A",
            "audio_transcription",
            MaterialCapabilityMaturity.PLANNED,
            {".m4a", ".m4b"},
            follow_up=("AUDIO_METADATA_DURATION_AND_CODEC_CHECK_REQUIRED",),
            legacy=None,
        )
    if bmff_brand == b"qt  ":
        return _format(
            MaterialCanonicalKind.VIDEO,
            "video/quicktime",
            "MOV",
            "video_analysis",
            MaterialCapabilityMaturity.PLANNED,
            {".mov", ".qt"},
            follow_up=("VIDEO_METADATA_DURATION_CODEC_AND_FRAME_CHECK_REQUIRED",),
            legacy=None,
        )
    if bmff_brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash", b"M4V "}:
        return _format(
            MaterialCanonicalKind.VIDEO,
            "video/mp4",
            "MP4",
            "video_analysis",
            MaterialCapabilityMaturity.PLANNED,
            {".mp4", ".m4v"},
            follow_up=("VIDEO_METADATA_DURATION_CODEC_AND_FRAME_CHECK_REQUIRED",),
            legacy=None,
        )
    text = _decode_plausible_text(prefix)
    if text is None:
        return None
    return _identify_text_format(text, suffix=suffix, complete=file_size <= len(prefix))


def _identify_text_format(text: str, *, suffix: str, complete: bool) -> _RecognizedFormat:
    stripped = text.lstrip("\ufeff\t\r\n ")
    lowered = stripped[:512].casefold()
    if _looks_like_email(text):
        return _text_format(MaterialCanonicalKind.EMAIL, "message/rfc822", "EML", "email_reading", {".eml"}, legacy="EMAIL")
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return _text_format(MaterialCanonicalKind.TEXT, "text/html", "HTML", "structured_text_reading", {".html", ".htm"})
    if lowered.startswith("<?xml"):
        return _text_format(MaterialCanonicalKind.TEXT, "application/xml", "XML", "structured_text_reading", {".xml"})
    if stripped.startswith("<") and ">" in stripped[:512] and not lowered.startswith(("<!doctype", "<!--")):
        return _text_format(MaterialCanonicalKind.TEXT, "application/xml", "XML", "structured_text_reading", {".xml"})
    if stripped.startswith(("{", "[")):
        if complete:
            try:
                json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                return _text_format(MaterialCanonicalKind.TEXT, "application/json", "JSON", "structured_text_reading", {".json"})
        elif suffix == ".json":
            return _text_format(
                MaterialCanonicalKind.TEXT,
                "application/json",
                "JSON",
                "structured_text_reading",
                {".json"},
                follow_up=("FULL_JSON_PARSE_REQUIRED",),
            )
    if suffix == ".tsv" or _has_consistent_delimiter(text, "\t"):
        return _text_format(MaterialCanonicalKind.TEXT, "text/tab-separated-values", "TSV", "structured_text_reading", {".tsv"})
    if suffix == ".csv" or _has_consistent_delimiter(text, ","):
        return _text_format(MaterialCanonicalKind.TEXT, "text/csv", "CSV", "structured_text_reading", {".csv"})
    if suffix in {".md", ".markdown"}:
        return _text_format(MaterialCanonicalKind.TEXT, "text/markdown", "MARKDOWN", "structured_text_reading", {".md", ".markdown"})
    return _text_format(MaterialCanonicalKind.TEXT, "text/plain", "TEXT", "structured_text_reading", {".txt"}, legacy="TEXT")


def _decision_for_recognized(
    recognized: _RecognizedFormat,
    *,
    suffix: str,
    prefix_bytes: int,
    zip_entry_count: int | None,
) -> MaterialTypeDecision:
    alignment = _extension_alignment(suffix, recognized.allowed_extensions)
    if suffix in _EXECUTABLE_EXTENSIONS | _DISK_IMAGE_EXTENSIONS | _MACRO_OFFICE_EXTENSIONS:
        alignment = ExtensionAlignment.DANGEROUS
    if alignment is ExtensionAlignment.DANGEROUS:
        return _quarantined(
            actual_format=recognized.actual_format,
            canonical_kind=recognized.canonical_kind,
            media_type=recognized.media_type,
            reason_code="DANGEROUS_EXTENSION_MISMATCH",
            reason="内容虽可识别，但文件名使用可执行、宏或磁盘镜像后缀；必须隔离核验。",
            prefix_bytes=prefix_bytes,
            extension_alignment=alignment,
            zip_entry_count=zip_entry_count,
        )
    if alignment is not ExtensionAlignment.MATCH:
        return _make_decision(
            recognized,
            risk_level=MaterialRiskLevel.HIGH,
            routing_status=MaterialRoutingStatus.REVIEW_REQUIRED,
            extension_alignment=alignment,
            reason_code="EXTENSION_MISSING" if alignment is ExtensionAlignment.MISSING else "EXTENSION_CONTENT_MISMATCH",
            reason=(
                "内容签名已识别，但文件缺少后缀；确认来源并更正文件名后才可路由。"
                if alignment is ExtensionAlignment.MISSING
                else "文件后缀与实际内容格式不一致；必须人工核验，不能按后缀解析。"
            ),
            prefix_bytes=prefix_bytes,
            zip_entry_count=zip_entry_count,
        )
    if recognized.maturity is MaterialCapabilityMaturity.IMPLEMENTED:
        status = MaterialRoutingStatus.ROUTABLE
        reason_code = "FORMAT_IDENTIFIED"
        reason = "内容签名和容器结构与文件后缀一致，可进入已实现的受控 Skill 前置检查。"
    elif recognized.maturity is MaterialCapabilityMaturity.GATED:
        status = MaterialRoutingStatus.REVIEW_REQUIRED
        reason_code = "CAPABILITY_GATED"
        reason = "格式已识别，但所需 Skill 仍受运行时、授权或供应商配置门控制。"
    else:
        status = MaterialRoutingStatus.REVIEW_REQUIRED
        reason_code = "CAPABILITY_PLANNED"
        reason = "格式已识别，但当前版本尚无可执行的生产适配器。"
    return _make_decision(
        recognized,
        risk_level=recognized.risk_level,
        routing_status=status,
        extension_alignment=alignment,
        reason_code=reason_code,
        reason=reason,
        prefix_bytes=prefix_bytes,
        zip_entry_count=zip_entry_count,
    )


def _make_decision(
    recognized: _RecognizedFormat,
    *,
    risk_level: MaterialRiskLevel,
    routing_status: MaterialRoutingStatus,
    extension_alignment: ExtensionAlignment,
    reason_code: str,
    reason: str,
    prefix_bytes: int,
    zip_entry_count: int | None,
) -> MaterialTypeDecision:
    payload = {
        "schema_version": "material-type-decision-v1",
        "canonical_kind": recognized.canonical_kind.value,
        "media_type": recognized.media_type,
        "actual_format": recognized.actual_format,
        "risk_level": risk_level.value,
        "preferred_skill": recognized.preferred_skill,
        "maturity": recognized.maturity.value,
        "routing_status": routing_status.value,
        "extension_alignment": extension_alignment.value,
        "reason_code": reason_code,
        "follow_up_checks": list(recognized.follow_up_checks),
        "legacy_detected_kind": recognized.legacy_detected_kind,
        "inspected_prefix_bytes": prefix_bytes,
        "zip_entry_count": zip_entry_count,
    }
    decision_hash = sha256(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return MaterialTypeDecision(
        canonical_kind=recognized.canonical_kind,
        media_type=recognized.media_type,
        actual_format=recognized.actual_format,
        risk_level=risk_level,
        preferred_skill=recognized.preferred_skill,
        maturity=recognized.maturity,
        routing_status=routing_status,
        extension_alignment=extension_alignment,
        reason_code=reason_code,
        reason=reason,
        follow_up_checks=recognized.follow_up_checks,
        legacy_detected_kind=recognized.legacy_detected_kind,
        inspected_prefix_bytes=prefix_bytes,
        zip_entry_count=zip_entry_count,
        decision_hash=decision_hash,
    )


def _quarantined(
    *,
    actual_format: str,
    canonical_kind: MaterialCanonicalKind,
    media_type: str,
    reason_code: str,
    reason: str,
    prefix_bytes: int,
    extension_alignment: ExtensionAlignment,
    zip_entry_count: int | None = None,
) -> MaterialTypeDecision:
    recognized = _RecognizedFormat(
        canonical_kind,
        media_type,
        actual_format,
        None,
        MaterialCapabilityMaturity.QUARANTINED,
        frozenset(),
        MaterialRiskLevel.CRITICAL,
        ("ADMINISTRATOR_SECURITY_REVIEW_REQUIRED",),
        None,
    )
    return _make_decision(
        recognized,
        risk_level=MaterialRiskLevel.CRITICAL,
        routing_status=MaterialRoutingStatus.QUARANTINED,
        extension_alignment=extension_alignment,
        reason_code=reason_code,
        reason=reason,
        prefix_bytes=prefix_bytes,
        zip_entry_count=zip_entry_count,
    )


def _format(
    kind: MaterialCanonicalKind,
    media_type: str,
    actual_format: str,
    skill: str | None,
    maturity: MaterialCapabilityMaturity,
    allowed_extensions: set[str],
    *,
    follow_up: tuple[str, ...],
    legacy: str | None,
    risk: MaterialRiskLevel = MaterialRiskLevel.MODERATE,
) -> _RecognizedFormat:
    return _RecognizedFormat(
        kind,
        media_type,
        actual_format,
        skill,
        maturity,
        frozenset(allowed_extensions),
        risk,
        follow_up,
        legacy,
    )


def _image_format(
    actual_format: str,
    media_type: str,
    allowed_extensions: set[str],
    *,
    maturity: MaterialCapabilityMaturity = MaterialCapabilityMaturity.GATED,
) -> _RecognizedFormat:
    return _format(
        MaterialCanonicalKind.IMAGE,
        media_type,
        actual_format,
        "image_visual_ocr",
        maturity,
        allowed_extensions,
        follow_up=_IMAGE_FOLLOW_UP,
        legacy="IMAGE",
    )


def _text_format(
    kind: MaterialCanonicalKind,
    media_type: str,
    actual_format: str,
    skill: str,
    allowed_extensions: set[str],
    *,
    follow_up: tuple[str, ...] = ("FULL_TEXT_ENCODING_AND_SIZE_CHECK_REQUIRED",),
    legacy: str | None = "TEXT",
) -> _RecognizedFormat:
    return _format(
        kind,
        media_type,
        actual_format,
        skill,
        MaterialCapabilityMaturity.PLANNED,
        allowed_extensions,
        follow_up=follow_up,
        legacy=legacy,
        risk=MaterialRiskLevel.LOW,
    )


def _read_bounded_prefix(path: Path) -> bytes:
    with path.open("rb") as source:
        return source.read(_MAX_PREFIX_BYTES)


def _is_zip_signature(prefix: bytes) -> bool:
    return prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))


def _detect_executable_signature(prefix: bytes) -> str | None:
    if prefix.startswith(b"MZ"):
        return "PE_OR_DOS_EXECUTABLE"
    if prefix.startswith(b"\x7fELF"):
        return "ELF_EXECUTABLE"
    if prefix[:4] in _MACHO_SIGNATURES:
        return "MACH_O_OR_FAT_BINARY"
    if prefix.startswith(b"dex\n"):
        return "ANDROID_DEX"
    if prefix.startswith(b"\x00asm"):
        return "WEBASSEMBLY_BINARY"
    if prefix.startswith(b"#!"):
        return "SCRIPT_WITH_SHEBANG"
    return None


def _looks_like_iso9660(prefix: bytes) -> bool:
    return len(prefix) >= 0x8006 and prefix[0x8001:0x8006] in {b"CD001", b"CDROM"}


def _iso_bmff_brand(prefix: bytes) -> bytes | None:
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        return None
    declared_size = int.from_bytes(prefix[:4], "big")
    if declared_size != 0 and declared_size < 12:
        return None
    return prefix[8:12]


def _looks_like_mp3_frame(prefix: bytes) -> bool:
    return len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0 and prefix[1] & 0x06 != 0


def _decode_plausible_text(prefix: bytes) -> str | None:
    if not prefix:
        return None
    encodings = ("utf-16",) if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig",)
    for encoding in encodings:
        try:
            text = prefix.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            return None
        meaningful = [character for character in text if not character.isspace()]
        if not meaningful:
            return text
        printable = sum(1 for character in meaningful if character.isprintable())
        if printable / len(meaningful) >= 0.95:
            return text
    return None


def _looks_like_email(text: str) -> bool:
    header, separator, _ = text.partition("\n\n")
    if not separator:
        header, separator, _ = text.partition("\r\n\r\n")
    if not separator or len(header) > 32 * 1024:
        return False
    fields = set()
    for line in header.replace("\r\n", "\n").split("\n"):
        if ":" not in line or line[:1].isspace():
            continue
        fields.add(line.split(":", 1)[0].strip().casefold())
    identity_fields = {"from", "to", "date", "subject", "message-id", "mime-version"}
    return len(fields & identity_fields) >= 2 and "from" in fields


def _has_consistent_delimiter(text: str, delimiter: str) -> bool:
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if len(lines) < 3:
        return False
    counts = [line.count(delimiter) for line in lines]
    return counts[0] > 0 and len(set(counts)) == 1


def _validated_zip_name(raw_name: str) -> str | None:
    if not raw_name or "\x00" in raw_name:
        return None
    normalized = raw_name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or path.is_absolute() or ".." in path.parts:
        return None
    if path.parts and ":" in path.parts[0]:
        return None
    return normalized.strip("/") or None


def _unsafe_zip(entries: list[zipfile.ZipInfo], reason_code: str, reason: str) -> _ZipDirectory:
    return _ZipDirectory(frozenset(), len(entries), reason_code, reason)


def _zip_contains_active_office_content(names: frozenset[str]) -> bool:
    for name in names:
        padded = f"/{name.strip('/')}"
        if any(marker in padded for marker in _OOXML_ACTIVE_PARTS):
            return True
    return False


def _zip_contains_executable(names: frozenset[str]) -> bool:
    if "meta-inf/manifest.mf" in names and any(name.endswith(".class") for name in names):
        return True
    return any(Path(name).suffix.casefold() in _ZIP_EXECUTABLE_SUFFIXES for name in names)


def _ooxml_kind_from_names(names: frozenset[str]) -> MaterialCanonicalKind | None:
    if "word/document.xml" in names:
        return MaterialCanonicalKind.WORD_DOCUMENT
    if "xl/workbook.xml" in names:
        return MaterialCanonicalKind.SPREADSHEET
    if "ppt/presentation.xml" in names:
        return MaterialCanonicalKind.PRESENTATION
    return None


def _office_kind_for_macro_extension(suffix: str) -> MaterialCanonicalKind:
    if suffix.startswith((".doc", ".dot")):
        return MaterialCanonicalKind.WORD_DOCUMENT
    if suffix.startswith((".xls", ".xlt", ".xla")):
        return MaterialCanonicalKind.SPREADSHEET
    return MaterialCanonicalKind.PRESENTATION


def _extension_alignment(suffix: str, allowed: frozenset[str]) -> ExtensionAlignment:
    if not suffix:
        return ExtensionAlignment.MISSING
    return ExtensionAlignment.MATCH if suffix in allowed else ExtensionAlignment.MISMATCH


def _dangerous_or_mismatch(suffix: str, allowed: frozenset[str]) -> ExtensionAlignment:
    if suffix in _EXECUTABLE_EXTENSIONS | _DISK_IMAGE_EXTENSIONS | _MACRO_OFFICE_EXTENSIONS:
        return ExtensionAlignment.DANGEROUS
    return _extension_alignment(suffix, allowed)
