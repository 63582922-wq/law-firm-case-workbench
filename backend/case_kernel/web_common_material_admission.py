"""Fail-closed admission for non-PDF browser case materials.

The browser is allowed to provide only a display filename, a declared byte
length, a declared media type, and an asynchronous byte stream.  Every other
property is derived by the server.  Bytes are first streamed into a private
0600 staging file, hashed, scanned by the configured anti-malware scanner,
identified by content, and then passed through the existing bounded format
inspectors/readers.

Admission is deliberately narrower than the phrase "any file".  The first
production slice accepts DOCX, XLSX, PPTX, RTF, TXT, CSV, HTML, EML, JPEG and
PNG.  PDF stays on the existing page/evidence intake path.  Legacy OLE Office
files (DOC/XLS/PPT/MSG), OFD, macros, executables, unknown formats and active
content are rejected rather than being mislabeled as supported.

An admitted object is still only a ``NEEDS_LAWYER_REVIEW`` source candidate.
This module cannot create facts, transactions, legal conclusions, evidence
decisions or court-ready work products.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Protocol
from unicodedata import category
from uuid import UUID, uuid4

from .common_document_reader import (
    CommonDocumentFormat,
    CommonDocumentReadingBlocked,
    DocumentReadBudget,
    MaterializedDocumentSource,
    read_materialized_common_document,
)
from .evidence_intake_worker import FileSafetyScanReceipt, FileSafetyScanner
from .material_format_inspection import inspect_non_pdf_material
from .material_type_registry import (
    ExtensionAlignment,
    MaterialCanonicalKind,
    MaterialTypeDecision,
    identify_material_type,
)


class CommonMaterialAdmissionBlocked(ValueError):
    """An upload cannot enter the managed case-material object ledger."""


class CommonMaterialContentRejected(CommonMaterialAdmissionBlocked):
    """The bytes have a deterministic, safe-to-report admission rejection."""

    def __init__(self, reason_code: str) -> None:
        _stable_code(reason_code, "common material rejection reason")
        self.reason_code = reason_code
        super().__init__("common material content was rejected")


class CommonMaterialFormat(StrEnum):
    DOCX = "DOCX"
    XLSX = "XLSX"
    PPTX = "PPTX"
    RTF = "RTF"
    TXT = "TXT"
    CSV = "CSV"
    HTML = "HTML"
    EML = "EML"
    JPEG = "JPEG"
    PNG = "PNG"


class CommonMaterialRoute(StrEnum):
    COMMON_DOCUMENT_READER = "COMMON_DOCUMENT_READER"
    VISUAL_OCR = "VISUAL_OCR"


class CommonMaterialReviewStatus(StrEnum):
    NEEDS_LAWYER_REVIEW = "NEEDS_LAWYER_REVIEW"


class CommonMaterialAgentStatus(StrEnum):
    AGENT_READY = "AGENT_READY"
    INGESTED_PENDING_ADAPTER = "INGESTED_PENDING_ADAPTER"


@dataclass(frozen=True)
class CommonMaterialAdmissionLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    stream_chunk_bytes: int = 1024 * 1024
    document_budget: DocumentReadBudget = DocumentReadBudget()

    def __post_init__(self) -> None:
        if type(self.max_file_bytes) is not int or not 1 <= self.max_file_bytes <= 1024**3:
            raise ValueError("common material byte limit is invalid")
        if type(self.stream_chunk_bytes) is not int or not 64 * 1024 <= self.stream_chunk_bytes <= 4 * 1024 * 1024:
            raise ValueError("common material stream chunk size is invalid")
        if not isinstance(self.document_budget, DocumentReadBudget):
            raise ValueError("common material document budget is invalid")
        self.document_budget.validate()
        if self.document_budget.max_source_bytes < self.max_file_bytes:
            raise ValueError("document reader source budget is smaller than the admission byte limit")


@dataclass(frozen=True)
class StagedCommonMaterial:
    upload_id: str
    display_name: str
    declared_media_type: str
    declared_byte_size: int
    byte_size: int
    content_sha256: str
    path: Path = field(repr=False, compare=False)


@dataclass(frozen=True)
class AdmittedCommonMaterial:
    """Server-only hand-off to immutable private object storage."""

    upload_id: str
    material_object_id: str
    display_name: str
    admitted_format: CommonMaterialFormat
    canonical_kind: MaterialCanonicalKind
    media_type: str
    route: CommonMaterialRoute
    byte_size: int
    content_sha256: str
    inspection_hash: str
    scanner_name: str
    scanner_definitions_version: str
    review_flags: tuple[str, ...]
    review_status: CommonMaterialReviewStatus = CommonMaterialReviewStatus.NEEDS_LAWYER_REVIEW
    formal_fact: bool = False
    formal_transaction: bool = False
    legal_conclusion: bool = False
    evidence_decision: bool = False
    court_ready: bool = False
    path: Path = field(repr=False, compare=False, default=Path("/private/unavailable"))

    def validate(self) -> None:
        _uuid(self.upload_id, "common material upload")
        _uuid(self.material_object_id, "common material object")
        _safe_display_name(self.display_name)
        if not isinstance(self.admitted_format, CommonMaterialFormat):
            raise CommonMaterialAdmissionBlocked("admitted common material format is invalid")
        if not isinstance(self.canonical_kind, MaterialCanonicalKind):
            raise CommonMaterialAdmissionBlocked("admitted common material kind is invalid")
        expected = _FORMAT_POLICIES[self.admitted_format]
        if (
            self.canonical_kind is not expected.canonical_kind
            or self.media_type != expected.media_type
            or self.route is not expected.route
        ):
            raise CommonMaterialAdmissionBlocked("admitted common material routing differs")
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= 1024**3:
            raise CommonMaterialAdmissionBlocked("admitted common material size is invalid")
        _sha256_value(self.content_sha256, "common material content hash")
        _sha256_value(self.inspection_hash, "common material inspection hash")
        _bounded_text(self.scanner_name, "common material scanner", 160)
        _bounded_text(self.scanner_definitions_version, "common material scanner definitions", 160)
        if tuple(sorted(set(self.review_flags))) != self.review_flags or len(self.review_flags) > 100:
            raise CommonMaterialAdmissionBlocked("common material review flags are invalid")
        for value in self.review_flags:
            _stable_code(value, "common material review flag")
        if self.review_status is not CommonMaterialReviewStatus.NEEDS_LAWYER_REVIEW:
            raise CommonMaterialAdmissionBlocked("common material cannot bypass lawyer review")
        if any(
            (
                self.formal_fact,
                self.formal_transaction,
                self.legal_conclusion,
                self.evidence_decision,
                self.court_ready,
            )
        ):
            raise CommonMaterialAdmissionBlocked("common material admission cannot create a formal conclusion")


@dataclass(frozen=True)
class _FormatPolicy:
    suffixes: frozenset[str]
    media_type: str
    declared_media_types: frozenset[str]
    canonical_kind: MaterialCanonicalKind
    route: CommonMaterialRoute
    reader_format: CommonDocumentFormat | None
    registry_actual_format: str | None
    legacy_inspection_kind: str


_FORMAT_POLICIES: dict[CommonMaterialFormat, _FormatPolicy] = {
    CommonMaterialFormat.DOCX: _FormatPolicy(
        frozenset({".docx"}),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
        MaterialCanonicalKind.WORD_DOCUMENT,
        CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.DOCX,
        "OOXML_DOCX",
        "WORD_DOCUMENT",
    ),
    CommonMaterialFormat.XLSX: _FormatPolicy(
        frozenset({".xlsx"}),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
        MaterialCanonicalKind.SPREADSHEET,
        CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.XLSX,
        "OOXML_XLSX",
        "SPREADSHEET",
    ),
    CommonMaterialFormat.PPTX: _FormatPolicy(
        frozenset({".pptx"}),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
        MaterialCanonicalKind.PRESENTATION,
        CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.PPTX,
        "OOXML_PPTX",
        "PRESENTATION",
    ),
    CommonMaterialFormat.RTF: _FormatPolicy(
        frozenset({".rtf"}), "application/rtf",
        frozenset({"application/rtf", "text/rtf"}), MaterialCanonicalKind.TEXT,
        CommonMaterialRoute.COMMON_DOCUMENT_READER, CommonDocumentFormat.RTF,
        None, "TEXT",
    ),
    CommonMaterialFormat.TXT: _FormatPolicy(
        frozenset({".txt"}), "text/plain", frozenset({"text/plain"}),
        MaterialCanonicalKind.TEXT, CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.TXT, "TEXT", "TEXT",
    ),
    CommonMaterialFormat.CSV: _FormatPolicy(
        frozenset({".csv"}), "text/csv", frozenset({"text/csv", "application/csv"}),
        MaterialCanonicalKind.TEXT, CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.CSV, "CSV", "TEXT",
    ),
    CommonMaterialFormat.HTML: _FormatPolicy(
        frozenset({".html", ".htm"}), "text/html", frozenset({"text/html"}),
        MaterialCanonicalKind.TEXT, CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.HTML, "HTML", "TEXT",
    ),
    CommonMaterialFormat.EML: _FormatPolicy(
        frozenset({".eml"}), "message/rfc822", frozenset({"message/rfc822"}),
        MaterialCanonicalKind.EMAIL, CommonMaterialRoute.COMMON_DOCUMENT_READER,
        CommonDocumentFormat.EML, "EML", "EMAIL",
    ),
    CommonMaterialFormat.JPEG: _FormatPolicy(
        frozenset({".jpg", ".jpeg", ".jpe"}), "image/jpeg", frozenset({"image/jpeg"}),
        MaterialCanonicalKind.IMAGE, CommonMaterialRoute.VISUAL_OCR,
        None, "JPEG", "IMAGE",
    ),
    CommonMaterialFormat.PNG: _FormatPolicy(
        frozenset({".png"}), "image/png", frozenset({"image/png"}),
        MaterialCanonicalKind.IMAGE, CommonMaterialRoute.VISUAL_OCR,
        None, "PNG", "IMAGE",
    ),
}

_SUFFIX_TO_FORMAT = {
    suffix: material_format
    for material_format, policy in _FORMAT_POLICIES.items()
    for suffix in policy.suffixes
}
_LEGACY_OR_SEPARATE_SUFFIXES = {
    ".doc": "LEGACY_DOC_NOT_SUPPORTED",
    ".xls": "LEGACY_XLS_NOT_SUPPORTED",
    ".ppt": "LEGACY_PPT_NOT_SUPPORTED",
    ".msg": "LEGACY_MSG_NOT_SUPPORTED",
    ".ofd": "OFD_SAFE_READER_NOT_AVAILABLE",
    ".pdf": "PDF_USES_EXISTING_EVIDENCE_INTAKE",
}
_GENERIC_DECLARED_TYPES = frozenset({"application/octet-stream"})


class CommonMaterialStagingArea:
    """Private streamed staging and deterministic common-material admission."""

    def __init__(
        self,
        staging_root: str | Path,
        *,
        limits: CommonMaterialAdmissionLimits = CommonMaterialAdmissionLimits(),
    ) -> None:
        raw = Path(staging_root).expanduser()
        if not raw.is_absolute() or (raw.exists() and raw.is_symlink()):
            raise CommonMaterialAdmissionBlocked("common material staging root is unsafe")
        try:
            raw.mkdir(parents=True, mode=0o700, exist_ok=True)
            root = raw.resolve(strict=True)
            if not root.is_dir() or root.is_symlink():
                raise CommonMaterialAdmissionBlocked("common material staging root is unavailable")
            root.chmod(0o700)
        except OSError as error:
            raise CommonMaterialAdmissionBlocked("common material staging root is unavailable") from error
        if not isinstance(limits, CommonMaterialAdmissionLimits):
            raise ValueError("common material admission limits are invalid")
        self._root = root
        self._limits = limits

    @property
    def staging_root(self) -> Path:
        """Server-only path; it must never enter an API response or log."""

        return self._root

    async def stage_async_chunks(
        self,
        chunks: AsyncIterable[bytes],
        *,
        client_filename: str,
        declared_byte_size: int,
        declared_media_type: str,
    ) -> StagedCommonMaterial:
        if not hasattr(chunks, "__aiter__"):
            raise CommonMaterialAdmissionBlocked("common material byte stream is invalid")
        display_name = _safe_display_name(client_filename)
        material_format = _format_for_filename(display_name)
        normalized_declared_type = _validate_declared_media_type(
            declared_media_type, policy=_FORMAT_POLICIES[material_format]
        )
        format_byte_limit = min(
            self._limits.max_file_bytes,
            64 * 1024 * 1024
            if material_format in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}
            else self._limits.max_file_bytes,
        )
        if type(declared_byte_size) is not int or not 1 <= declared_byte_size <= format_byte_limit:
            raise CommonMaterialContentRejected("DECLARED_SIZE_OUT_OF_RANGE")
        upload_id = str(uuid4())
        suffix = Path(display_name).suffix.casefold()
        destination = self._root / f"upload-{upload_id}{suffix}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        digest = sha256()
        byte_size = 0
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                async for block in chunks:
                    if not isinstance(block, (bytes, bytearray)):
                        raise CommonMaterialAdmissionBlocked("common material stream yielded non-bytes")
                    if not block:
                        continue
                    byte_size += len(block)
                    if byte_size > format_byte_limit or byte_size > declared_byte_size:
                        raise CommonMaterialContentRejected("DECLARED_SIZE_MISMATCH")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o600)
            if byte_size != declared_byte_size:
                raise CommonMaterialContentRejected("DECLARED_SIZE_MISMATCH")
            _assert_staged(
                destination,
                root=self._root,
                upload_id=upload_id,
                suffix=suffix,
                expected_size=byte_size,
                expected_sha256=digest.hexdigest(),
            )
        except BaseException:
            _unlink_private(destination, root=self._root)
            raise
        return StagedCommonMaterial(
            upload_id=upload_id,
            display_name=display_name,
            declared_media_type=normalized_declared_type,
            declared_byte_size=declared_byte_size,
            byte_size=byte_size,
            content_sha256=digest.hexdigest(),
            path=destination,
        )

    def admit(
        self,
        staged: StagedCommonMaterial,
        *,
        material_object_id: str,
        scanner: FileSafetyScanner,
    ) -> AdmittedCommonMaterial:
        _uuid(material_object_id, "common material object")
        if not callable(getattr(scanner, "scan", None)):
            raise CommonMaterialAdmissionBlocked("common material scanner is unavailable")
        self._assert_current(staged)
        try:
            scan = scanner.scan(staged.path, expected_sha256=staged.content_sha256)
        finally:
            self._assert_current(staged)
        _validate_scan(scan, expected_sha256=staged.content_sha256)
        if scan.result != "CLEAN":
            raise CommonMaterialContentRejected(
                "MALWARE_DETECTED" if scan.result == "INFECTED" else "MALWARE_SCAN_INDETERMINATE"
            )

        admitted_format = _format_for_filename(staged.display_name)
        policy = _FORMAT_POLICIES[admitted_format]
        decision = _identify_exact_format(staged.path, admitted_format=admitted_format)
        format_inspection_hash: str | None = None
        reader_result_hash: str | None = None
        review_flags: tuple[str, ...] = ()

        inspected = inspect_non_pdf_material(
            staged.path,
            detected_kind=policy.legacy_inspection_kind,
        )
        if inspected.outcome == "BLOCKED":
            raise CommonMaterialContentRejected(inspected.reason_code)
        format_inspection_hash = inspected.details_hash

        if policy.reader_format is None:
            if admitted_format not in {CommonMaterialFormat.JPEG, CommonMaterialFormat.PNG}:
                raise CommonMaterialAdmissionBlocked("common material has no safe reader route")
        else:
            if admitted_format is CommonMaterialFormat.HTML:
                _reject_active_html_declaration(staged.path)
            source = MaterializedDocumentSource(
                source_object_id=material_object_id,
                source_object_version=f"staging-sha256:{staged.content_sha256}",
                materialization_root=self._root,
                path=staged.path,
                byte_size=staged.byte_size,
                content_sha256=staged.content_sha256,
                admitted_format=policy.reader_format,
            )
            try:
                result = read_materialized_common_document(
                    source,
                    budget=self._limits.document_budget,
                )
            except CommonDocumentReadingBlocked as error:
                raise CommonMaterialContentRejected("SAFE_READER_REJECTED_CONTENT") from error
            reader_result_hash = result.result_hash
            review_flags = tuple(sorted(set(result.document_risk_flags)))

        self._assert_current(staged)
        inspection_hash = _canonical_hash(
            {
                "schema_version": "web-common-material-admission-v1",
                "upload_id": staged.upload_id,
                "material_object_id": material_object_id,
                "display_name": staged.display_name,
                "declared_media_type": staged.declared_media_type,
                "byte_size": staged.byte_size,
                "content_sha256": staged.content_sha256,
                "admitted_format": admitted_format.value,
                "canonical_kind": policy.canonical_kind.value,
                "media_type": policy.media_type,
                "route": policy.route.value,
                "material_type_decision_hash": decision.decision_hash if decision is not None else None,
                "format_inspection_hash": format_inspection_hash,
                "safe_reader_result_hash": reader_result_hash,
                "scanner_name": scan.scanner_name,
                "scanner_definitions_version": scan.definitions_version,
                "review_flags": list(review_flags),
                "review_status": CommonMaterialReviewStatus.NEEDS_LAWYER_REVIEW.value,
                "formal_fact": False,
                "formal_transaction": False,
                "legal_conclusion": False,
                "evidence_decision": False,
                "court_ready": False,
            }
        )
        admitted = AdmittedCommonMaterial(
            upload_id=staged.upload_id,
            material_object_id=material_object_id,
            display_name=staged.display_name,
            admitted_format=admitted_format,
            canonical_kind=policy.canonical_kind,
            media_type=policy.media_type,
            route=policy.route,
            byte_size=staged.byte_size,
            content_sha256=staged.content_sha256,
            inspection_hash=inspection_hash,
            scanner_name=scan.scanner_name,
            scanner_definitions_version=scan.definitions_version,
            review_flags=review_flags,
            path=staged.path,
        )
        admitted.validate()
        return admitted

    def discard(self, staged: StagedCommonMaterial | AdmittedCommonMaterial) -> None:
        _unlink_private(staged.path, root=self._root)

    def _assert_current(self, staged: StagedCommonMaterial) -> None:
        if not isinstance(staged, StagedCommonMaterial):
            raise CommonMaterialAdmissionBlocked("common material staging handle is invalid")
        suffix = Path(staged.display_name).suffix.casefold()
        _assert_staged(
            staged.path,
            root=self._root,
            upload_id=staged.upload_id,
            suffix=suffix,
            expected_size=staged.byte_size,
            expected_sha256=staged.content_sha256,
        )


def accepted_common_material_extensions() -> tuple[str, ...]:
    """Public product contract for the future Web file picker."""

    return tuple(sorted(_SUFFIX_TO_FORMAT))


def rejected_legacy_material_extensions() -> tuple[str, ...]:
    """Formats that must be shown as unsupported, never silently accepted."""

    return tuple(sorted(suffix for suffix in _LEGACY_OR_SEPARATE_SUFFIXES if suffix != ".pdf"))


def common_material_agent_status(
    admitted_format: CommonMaterialFormat,
) -> CommonMaterialAgentStatus:
    """Truthful execution status after the 0041 registration transaction."""

    if not isinstance(admitted_format, CommonMaterialFormat):
        raise CommonMaterialAdmissionBlocked("common material format is invalid")
    if admitted_format in {
        CommonMaterialFormat.DOCX,
        CommonMaterialFormat.XLSX,
        CommonMaterialFormat.JPEG,
        CommonMaterialFormat.PNG,
    }:
        return CommonMaterialAgentStatus.AGENT_READY
    return CommonMaterialAgentStatus.INGESTED_PENDING_ADAPTER


def _identify_exact_format(path: Path, *, admitted_format: CommonMaterialFormat) -> MaterialTypeDecision | None:
    policy = _FORMAT_POLICIES[admitted_format]
    if admitted_format is CommonMaterialFormat.RTF:
        try:
            prefix = path.read_bytes()[:64]
        except OSError as error:
            raise CommonMaterialAdmissionBlocked("RTF bytes are unavailable") from error
        if not prefix.lstrip().startswith(b"{\\rtf"):
            raise CommonMaterialContentRejected("FILE_SIGNATURE_MISMATCH")
        return None
    decision = identify_material_type(path)
    if (
        decision.extension_alignment is not ExtensionAlignment.MATCH
        or decision.actual_format != policy.registry_actual_format
        or decision.canonical_kind is not policy.canonical_kind
        or decision.media_type != policy.media_type
    ):
        raise CommonMaterialContentRejected("FILE_SIGNATURE_OR_EXTENSION_MISMATCH")
    return decision


def _format_for_filename(display_name: str) -> CommonMaterialFormat:
    suffix = Path(display_name).suffix.casefold()
    explicit_rejection = _LEGACY_OR_SEPARATE_SUFFIXES.get(suffix)
    if explicit_rejection is not None:
        raise CommonMaterialContentRejected(explicit_rejection)
    try:
        return _SUFFIX_TO_FORMAT[suffix]
    except KeyError as error:
        raise CommonMaterialContentRejected("UNSUPPORTED_COMMON_MATERIAL_FORMAT") from error


def _validate_declared_media_type(value: object, *, policy: _FormatPolicy) -> str:
    if not isinstance(value, str):
        raise CommonMaterialContentRejected("DECLARED_MEDIA_TYPE_INVALID")
    normalized = value.split(";", 1)[0].strip().casefold()
    if normalized not in policy.declared_media_types | _GENERIC_DECLARED_TYPES:
        raise CommonMaterialContentRejected("DECLARED_MEDIA_TYPE_MISMATCH")
    return normalized


def _validate_scan(receipt: object, *, expected_sha256: str) -> None:
    if not isinstance(receipt, FileSafetyScanReceipt):
        raise CommonMaterialAdmissionBlocked("anti-malware scanner receipt is invalid")
    _bounded_text(receipt.scanner_name, "anti-malware scanner", 160)
    _bounded_text(receipt.definitions_version, "anti-malware definitions", 160)
    if receipt.content_sha256 != expected_sha256 or receipt.result not in {"CLEAN", "INFECTED", "INDETERMINATE"}:
        raise CommonMaterialAdmissionBlocked("anti-malware scanner receipt differs from the staged source")


def _reject_active_html_declaration(path: Path) -> None:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CommonMaterialAdmissionBlocked("HTML bytes are unavailable") from error
    lowered = raw.lower()
    if b"<!entity" in lowered or re.search(br"<!doctype\s+html\s+(?:public|system)\b", lowered):
        raise CommonMaterialContentRejected("HTML_EXTERNAL_DTD_OR_ENTITY")


def _safe_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise CommonMaterialContentRejected("FILENAME_INVALID")
    candidate = value
    try:
        encoded = candidate.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CommonMaterialContentRejected("FILENAME_INVALID") from error
    if (
        candidate in {"", ".", ".."}
        or candidate != candidate.strip()
        or "/" in candidate
        or "\\" in candidate
        or not 1 <= len(encoded) <= 255
        or any(character == "\x00" or category(character).startswith("C") for character in candidate)
    ):
        raise CommonMaterialContentRejected("FILENAME_INVALID")
    return candidate


def _assert_staged(
    path: Path,
    *,
    root: Path,
    upload_id: str,
    suffix: str,
    expected_size: int,
    expected_sha256: str,
) -> None:
    _uuid(upload_id, "common material upload")
    expected_name = f"upload-{upload_id}{suffix}"
    if path.parent != root or path.name != expected_name or path.is_symlink() or not path.is_file():
        raise CommonMaterialAdmissionBlocked("common material staging file is missing or unsafe")
    try:
        if path.stat().st_size != expected_size:
            raise CommonMaterialAdmissionBlocked("common material staging file size changed")
        digest = sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise CommonMaterialAdmissionBlocked("common material staging file is unavailable") from error
    if digest.hexdigest() != expected_sha256:
        raise CommonMaterialAdmissionBlocked("common material staging file content changed")


def _unlink_private(path: Path, *, root: Path) -> None:
    try:
        if path.parent == root and path.name.startswith("upload-") and path.suffix.casefold() in _SUFFIX_TO_FORMAT:
            path.unlink(missing_ok=True)
    except OSError as error:
        raise CommonMaterialAdmissionBlocked("common material staging cleanup failed") from error


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise CommonMaterialAdmissionBlocked(f"{label} is invalid") from error


def _sha256_value(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CommonMaterialAdmissionBlocked(f"{label} is invalid")
    return value


def _bounded_text(value: object, label: str, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value.encode("utf-8")) <= maximum_bytes
        or any(ord(character) < 32 for character in value)
    ):
        raise CommonMaterialAdmissionBlocked(f"{label} is invalid")
    return value


def _stable_code(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) is None:
        raise CommonMaterialAdmissionBlocked(f"{label} is invalid")
    return value


def _canonical_hash(payload: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class CommonMaterialAdmissionPort(Protocol):
    """Worker-facing admission contract used by a future Web composition."""

    async def stage_async_chunks(
        self,
        chunks: AsyncIterable[bytes],
        *,
        client_filename: str,
        declared_byte_size: int,
        declared_media_type: str,
    ) -> StagedCommonMaterial: ...

    def admit(
        self,
        staged: StagedCommonMaterial,
        *,
        material_object_id: str,
        scanner: FileSafetyScanner,
    ) -> AdmittedCommonMaterial: ...

    def discard(self, staged: StagedCommonMaterial | AdmittedCommonMaterial) -> None: ...


__all__ = (
    "AdmittedCommonMaterial",
    "CommonMaterialAdmissionBlocked",
    "CommonMaterialAdmissionLimits",
    "CommonMaterialAdmissionPort",
    "CommonMaterialAgentStatus",
    "CommonMaterialContentRejected",
    "CommonMaterialFormat",
    "CommonMaterialReviewStatus",
    "CommonMaterialRoute",
    "CommonMaterialStagingArea",
    "StagedCommonMaterial",
    "accepted_common_material_extensions",
    "common_material_agent_status",
    "rejected_legacy_material_extensions",
)
