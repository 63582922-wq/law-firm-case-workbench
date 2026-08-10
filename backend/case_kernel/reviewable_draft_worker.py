"""Create editable Office drafts together with hash-bound rendered review PDFs.

This worker is intentionally one step short of court submission: it prepares a
lawyer-review pair (editable DOCX/XLSX plus isolated PDF preview), but it does
not approve, export, or write alongside original case materials.  The next
coordinator must store both outputs in the managed artifact area and bind the
returned review hash to an explicit lawyer decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable

from .approved_draft_worker import ApprovedDraft, DraftArtifact, create_docx_draft, create_xlsx_ledger
from .office_pdf_conversion_worker import ConvertedOfficePdf, SandboxedOfficePdfConverter


class ReviewableDraftBlocked(ValueError):
    """A draft cannot safely become a lawyer-reviewable Office pair."""


@dataclass(frozen=True)
class ReviewableOfficeDraft:
    editable_artifact: DraftArtifact
    review_pdf: ConvertedOfficePdf
    approval_hash: str
    review_input_hash: str


def create_reviewable_docx_draft(
    draft: ApprovedDraft, *, converter: SandboxedOfficePdfConverter
) -> ReviewableOfficeDraft:
    editable = create_docx_draft(draft)
    return _render_editable_draft(
        editable,
        approval_hash=draft.approval_hash,
        source_name="approved-draft.docx",
        detected_kind="WORD_DOCUMENT",
        converter=converter,
    )


def create_reviewable_xlsx_ledger(
    *,
    approval_hash: str,
    sheet_name: str,
    columns: tuple[str, ...],
    rows: Iterable[tuple[str | int | float | None, ...]],
    converter: SandboxedOfficePdfConverter,
) -> ReviewableOfficeDraft:
    editable = create_xlsx_ledger(
        approval_hash=approval_hash,
        sheet_name=sheet_name,
        columns=columns,
        rows=rows,
    )
    return _render_editable_draft(
        editable,
        approval_hash=approval_hash,
        source_name="approved-ledger.xlsx",
        detected_kind="SPREADSHEET",
        converter=converter,
    )


def _render_editable_draft(
    editable: DraftArtifact,
    *,
    approval_hash: str,
    source_name: str,
    detected_kind: str,
    converter: SandboxedOfficePdfConverter,
) -> ReviewableOfficeDraft:
    if not isinstance(converter, SandboxedOfficePdfConverter):
        raise ReviewableDraftBlocked("reviewable Office drafts require the isolated desktop converter")
    review_pdf = converter.convert_generated_document(
        editable.content,
        content_sha256=editable.content_sha256,
        source_name=source_name,
        detected_kind=detected_kind,
    )
    if review_pdf.source_sha256 != editable.content_sha256:
        raise ReviewableDraftBlocked("rendered review PDF is not bound to its editable Office source")
    review_input_hash = _review_input_hash(
        editable=editable,
        review_pdf=review_pdf,
        approval_hash=approval_hash,
        detected_kind=detected_kind,
    )
    return ReviewableOfficeDraft(
        editable_artifact=editable,
        review_pdf=review_pdf,
        approval_hash=approval_hash,
        review_input_hash=review_input_hash,
    )


def _review_input_hash(
    *, editable: DraftArtifact,
    review_pdf: ConvertedOfficePdf,
    approval_hash: str,
    detected_kind: str,
) -> str:
    payload = {
        "schema_version": "reviewable-office-draft-v1",
        "approval_hash": approval_hash,
        "detected_kind": detected_kind,
        "editable_media_type": editable.media_type,
        "editable_sha256": editable.content_sha256,
        "editable_bytes": len(editable.content),
        "review_pdf_sha256": review_pdf.pdf_sha256,
        "review_pdf_bytes": review_pdf.pdf_bytes,
        "review_pdf_page_count": review_pdf.page_count,
        "render_verification_hash": review_pdf.render_verification_hash,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
