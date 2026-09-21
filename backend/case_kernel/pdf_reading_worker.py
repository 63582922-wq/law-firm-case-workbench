"""Read bounded text from an authorized, static PDF evidence original.

This worker is deliberately narrower than a general PDF parser. It never
writes beside the original, does not render or execute embedded actions, and
requires the same short-lived authorized file handle used by other case tools.
The returned page text remains in process; callers must separately decide
whether a reviewed derivative may be persisted or sent externally.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from pypdf import PdfReader

from .evidence_intake_worker import _contains_active_pdf_content
from .local_access_grants import AuthorizedOriginalFile


class PdfReadingBlocked(ValueError):
    """The authorized PDF cannot be safely read as evidence text."""


@dataclass(frozen=True)
class PdfPageText:
    page_number: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class PdfReadResult:
    source_sha256: str
    pages: tuple[PdfPageText, ...]
    extracted_character_count: int


_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_PAGES = 10_000
_MAX_PAGE_TEXT_CHARS = 250_000
_MAX_TOTAL_TEXT_CHARS = 10_000_000


def read_authorized_pdf_document(source: AuthorizedOriginalFile) -> PdfReadResult:
    """Return static per-page text after repeat source and active-content checks."""

    _verify_source(source)
    if source.byte_size < 5 or source.byte_size > _MAX_SOURCE_BYTES:
        raise PdfReadingBlocked("PDF source exceeds the reader byte limit")
    try:
        with source.path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise PdfReadingBlocked("authorized source is not a PDF")
        reader = PdfReader(str(source.path), strict=True)
        if reader.is_encrypted:
            raise PdfReadingBlocked("encrypted PDF cannot be read without a separate lawyer-approved path")
        if _contains_active_pdf_content(reader):
            raise PdfReadingBlocked("PDF with active content cannot be read")
        if not 1 <= len(reader.pages) <= _MAX_PAGES:
            raise PdfReadingBlocked("PDF page count exceeds the reader limit")
        pages: list[PdfPageText] = []
        total_characters = 0
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if len(text) > _MAX_PAGE_TEXT_CHARS:
                raise PdfReadingBlocked("PDF page text exceeds the reader limit")
            total_characters += len(text)
            if total_characters > _MAX_TOTAL_TEXT_CHARS:
                raise PdfReadingBlocked("PDF text exceeds the reader limit")
            pages.append(PdfPageText(page_number, text, sha256(text.encode("utf-8")).hexdigest()))
    except PdfReadingBlocked:
        raise
    except Exception as error:
        raise PdfReadingBlocked("PDF text cannot be safely extracted") from error
    _verify_source(source)
    return PdfReadResult(source.sha256, tuple(pages), total_characters)


def _verify_source(source: AuthorizedOriginalFile) -> None:
    if source.path.is_symlink() or not source.path.is_file() or source.path.stat().st_size != source.byte_size:
        raise PdfReadingBlocked("authorized PDF source is missing, symbolic, or changed")
    digest = sha256()
    with source.path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != source.sha256:
        raise PdfReadingBlocked("authorized PDF source hash changed during reading")
