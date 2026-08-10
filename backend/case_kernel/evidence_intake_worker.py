"""Deterministic, local-only inspection before an approved file becomes evidence.

The worker never edits the source.  A clean anti-malware receipt is mandatory;
PDF structure is then parsed with active content rejected.  Other file kinds
remain review/conversion work instead of being assigned invented page counts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Protocol

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from .local_access_grants import AuthorizedOriginalFile
from .material_format_inspection import inspect_non_pdf_material


class EvidenceIntakeBlocked(ValueError):
    """The source cannot safely enter the evidence ledger."""


@dataclass(frozen=True)
class FileSafetyScanReceipt:
    scanner_name: str
    definitions_version: str
    content_sha256: str
    result: str


class FileSafetyScanner(Protocol):
    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt: ...


class ClamAvCommandScanner:
    """Explicit local ClamAV adapter; no network fallback and no shell execution."""

    def __init__(self, executable: str | Path, *, timeout_seconds: int = 120) -> None:
        raw = Path(executable).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise EvidenceIntakeBlocked("ClamAV executable must be an explicit non-symbolic absolute path")
        resolved = raw.resolve(strict=True)
        if not resolved.is_file() or timeout_seconds < 10 or timeout_seconds > 600:
            raise EvidenceIntakeBlocked("ClamAV executable or timeout is invalid")
        self._executable = resolved
        self._timeout_seconds = timeout_seconds

    def scan(self, path: Path, *, expected_sha256: str) -> FileSafetyScanReceipt:
        try:
            version = subprocess.run(
                [str(self._executable), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if version.returncode != 0 or not version.stdout.strip():
                raise EvidenceIntakeBlocked("local ClamAV version check failed")
            with path.open("rb") as source:
                result = subprocess.run(
                    [str(self._executable), "--no-summary", "--stdout", f"/dev/fd/{source.fileno()}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    pass_fds=(source.fileno(),),
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EvidenceIntakeBlocked("local ClamAV scanner is unavailable") from error
        if _path_sha256(path) != expected_sha256:
            raise EvidenceIntakeBlocked("source changed while local ClamAV was scanning")
        scan_result = {0: "CLEAN", 1: "INFECTED"}.get(result.returncode, "INDETERMINATE")
        return FileSafetyScanReceipt(
            scanner_name="ClamAV",
            definitions_version=version.stdout.strip().splitlines()[0][:160],
            content_sha256=expected_sha256,
            result=scan_result,
        )


@dataclass(frozen=True)
class EvidenceIntakeInspection:
    outcome: str
    reason_code: str | None
    media_type: str | None
    page_count: int | None
    inspection_hash: str
    scanner_name: str
    scanner_definitions_version: str


_DANGEROUS_PDF_NAMES = {
    "/AA",
    "/EmbeddedFile",
    "/EmbeddedFiles",
    "/ImportData",
    "/JavaScript",
    "/JS",
    "/Launch",
    "/Movie",
    "/OpenAction",
    "/RichMedia",
    "/Sound",
    "/SubmitForm",
}


def inspect_authorized_original(
    source: AuthorizedOriginalFile,
    *,
    detected_kind: str,
    scanner: FileSafetyScanner,
    max_pdf_pages: int = 10_000,
) -> EvidenceIntakeInspection:
    if not detected_kind.strip():
        raise EvidenceIntakeBlocked("detected file kind is required")
    if max_pdf_pages < 1 or max_pdf_pages > 10_000:
        raise EvidenceIntakeBlocked("PDF page limit must be between 1 and 10000")
    _verify_immutable_source(source)
    receipt = scanner.scan(source.path, expected_sha256=source.sha256)
    _validate_scan_receipt(receipt, expected_sha256=source.sha256)
    if receipt.result != "CLEAN":
        return _result(
            source,
            detected_kind=detected_kind,
            receipt=receipt,
            outcome="BLOCKED",
            reason_code="MALWARE_DETECTED" if receipt.result == "INFECTED" else "MALWARE_SCAN_INDETERMINATE",
            media_type=None,
            page_count=None,
        )

    if source.byte_size == 0:
        return _result(
            source,
            detected_kind=detected_kind,
            receipt=receipt,
            outcome="BLOCKED",
            reason_code="EMPTY_FILE",
            media_type=None,
            page_count=None,
        )

    if detected_kind != "PDF":
        format_inspection = inspect_non_pdf_material(source.path, detected_kind=detected_kind)
        result = _result(
            source,
            detected_kind=detected_kind,
            receipt=receipt,
            outcome=format_inspection.outcome,
            reason_code=format_inspection.reason_code,
            media_type=None,
            page_count=None,
            format_inspection_hash=format_inspection.details_hash,
        )
        _verify_immutable_source(source)
        return result

    result = _inspect_pdf(
        source,
        detected_kind=detected_kind,
        receipt=receipt,
        max_pdf_pages=max_pdf_pages,
    )
    _verify_immutable_source(source)
    return result


def _inspect_pdf(
    source: AuthorizedOriginalFile,
    *,
    detected_kind: str,
    receipt: FileSafetyScanReceipt,
    max_pdf_pages: int,
) -> EvidenceIntakeInspection:
    with source.path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="FILE_SIGNATURE_MISMATCH", media_type=None, page_count=None)
    try:
        reader = PdfReader(str(source.path), strict=True)
        if reader.is_encrypted:
            return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="PDF_ENCRYPTED", media_type=None, page_count=None)
        page_count = len(reader.pages)
        if page_count < 1 or page_count > max_pdf_pages:
            return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="PDF_PAGE_LIMIT", media_type=None, page_count=None)
        if _contains_active_pdf_content(reader):
            return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="PDF_ACTIVE_CONTENT", media_type=None, page_count=None)
        for page in reader.pages:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            if width <= 0 or height <= 0 or width > 20_000 or height > 20_000:
                return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="PDF_INVALID_GEOMETRY", media_type=None, page_count=None)
    except Exception:
        return _result(source, detected_kind=detected_kind, receipt=receipt, outcome="BLOCKED", reason_code="PDF_MALFORMED", media_type=None, page_count=None)
    return _result(
        source,
        detected_kind=detected_kind,
        receipt=receipt,
        outcome="REGISTERABLE",
        reason_code=None,
        media_type="application/pdf",
        page_count=page_count,
    )


def _contains_active_pdf_content(reader: PdfReader) -> bool:
    pending: list[object] = [reader.trailer]
    seen_indirect: set[tuple[int, int]] = set()
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 200_000:
            raise EvidenceIntakeBlocked("PDF object graph exceeds the inspection limit")
        if isinstance(current, IndirectObject):
            identity = (current.idnum, current.generation)
            if identity in seen_indirect:
                continue
            seen_indirect.add(identity)
            pending.append(current.get_object())
            continue
        if isinstance(current, DictionaryObject):
            for key, value in current.items():
                if str(key) in _DANGEROUS_PDF_NAMES or str(value) in _DANGEROUS_PDF_NAMES:
                    return True
                pending.append(value)
            continue
        if isinstance(current, ArrayObject):
            pending.extend(current)
    return False


def _validate_scan_receipt(receipt: FileSafetyScanReceipt, *, expected_sha256: str) -> None:
    if not receipt.scanner_name.strip() or not receipt.definitions_version.strip():
        raise EvidenceIntakeBlocked("anti-malware scanner identity and definitions version are required")
    if receipt.content_sha256 != expected_sha256:
        raise EvidenceIntakeBlocked("anti-malware receipt does not match the authorized source")
    if receipt.result not in {"CLEAN", "INFECTED", "INDETERMINATE"}:
        raise EvidenceIntakeBlocked("anti-malware result is invalid")


def _verify_immutable_source(source: AuthorizedOriginalFile) -> None:
    path = source.path
    if path.is_symlink() or not path.is_file() or path.stat().st_size != source.byte_size:
        raise EvidenceIntakeBlocked("authorized source is missing, symbolic, or changed")
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != source.sha256:
        raise EvidenceIntakeBlocked("authorized source hash changed during intake")


def _path_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _result(
    source: AuthorizedOriginalFile,
    *,
    detected_kind: str,
    receipt: FileSafetyScanReceipt,
    outcome: str,
    reason_code: str | None,
    media_type: str | None,
    page_count: int | None,
    format_inspection_hash: str | None = None,
) -> EvidenceIntakeInspection:
    payload = {
        "schema_version": "evidence-intake-inspection-v2",
        "relative_path": source.relative_path,
        "byte_size": source.byte_size,
        "content_sha256": source.sha256,
        "detected_kind": detected_kind,
        "scanner": asdict(receipt),
        "outcome": outcome,
        "reason_code": reason_code,
        "media_type": media_type,
        "page_count": page_count,
        "format_inspection_hash": format_inspection_hash,
    }
    inspection_hash = sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return EvidenceIntakeInspection(
        outcome=outcome,
        reason_code=reason_code,
        media_type=media_type,
        page_count=page_count,
        inspection_hash=inspection_hash,
        scanner_name=receipt.scanner_name,
        scanner_definitions_version=receipt.definitions_version,
    )
