"""Persist an approved-input PDF draft as a reviewable court work-product.

The model-facing layer may suggest wording, but this coordinator receives only
an already-approved structured snapshot.  It produces a PDF candidate in
memory, verifies it, encrypts it outside the case folder, and records a review
hash that the lawyer must approve exactly before the PDF can enter a bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from .approved_draft_worker import ApprovedDraft, DraftArtifact, create_pdf_draft
from .case_ledger_postgres import CaseLedgerCommandReceipt
from .managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked, StoredArtifactObject
from .models import Actor, Role


class SubmissionWorkProductCoordinationBlocked(ValueError):
    """The draft cannot become a reviewable submission work product."""


class SubmissionWorkProductPersistencePort(Protocol):
    def register_work_product_candidate(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class CoordinatedSubmissionWorkProduct:
    matter_id: str
    work_product_id: str
    document_kind: str
    audience: str
    artifact: StoredArtifactObject
    page_count: int
    semantic_text_sha256: str
    approved_input_hash: str
    review_input_hash: str
    registration_receipt: CaseLedgerCommandReceipt


def coordinate_pdf_draft_work_product(
    *,
    matter_id: str,
    expected_version: int,
    idempotency_key: str,
    document_kind: str,
    audience: str,
    draft: ApprovedDraft,
    case_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore,
    persistence: SubmissionWorkProductPersistencePort,
    system_actor: Actor,
) -> CoordinatedSubmissionWorkProduct:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise SubmissionWorkProductCoordinationBlocked("work-product generation requires a dedicated SYSTEM_WORKER")
    if not matter_id.strip() or expected_version < 1 or not idempotency_key.strip() or len(idempotency_key) > 160:
        raise SubmissionWorkProductCoordinationBlocked("work-product command identity is invalid")
    if not document_kind.strip() or len(document_kind) > 120:
        raise SubmissionWorkProductCoordinationBlocked("work-product document kind is invalid")
    if audience not in {"COURT_SUBMISSION", "INTERNAL_ONLY"}:
        raise SubmissionWorkProductCoordinationBlocked("work-product audience is invalid")
    original_root = Path(case_root).expanduser().resolve(strict=True)
    if not original_root.is_dir():
        raise SubmissionWorkProductCoordinationBlocked("case root must be an existing directory")

    generated = create_pdf_draft(draft)
    page_count = _verify_generated_pdf(generated)
    semantic_text_sha256 = _semantic_text_hash(draft)
    review_input_hash = _review_input_hash(
        matter_id=matter_id,
        document_kind=document_kind.strip(),
        audience=audience,
        draft=draft,
        artifact=generated,
        page_count=page_count,
        semantic_text_sha256=semantic_text_sha256,
    )
    try:
        encrypted = artifact_store.put_bytes(
            generated.content,
            expected_sha256=generated.content_sha256,
            case_root=original_root,
        )
    except ManagedArtifactBlocked as error:
        raise SubmissionWorkProductCoordinationBlocked("generated draft could not enter managed encrypted storage") from error
    receipt = persistence.register_work_product_candidate(
        matter_id=matter_id,
        actor=system_actor,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        document_kind=document_kind.strip(),
        audience=audience,
        media_type=generated.media_type,
        storage_object_key=encrypted.object_key,
        artifact_sha256=encrypted.plaintext_sha256,
        byte_size=encrypted.plaintext_bytes,
        page_count=page_count,
        semantic_text_sha256=semantic_text_sha256,
        review_input_hash=review_input_hash,
    )
    return CoordinatedSubmissionWorkProduct(
        matter_id=matter_id,
        work_product_id=receipt.object_id,
        document_kind=document_kind.strip(),
        audience=audience,
        artifact=encrypted,
        page_count=page_count,
        semantic_text_sha256=semantic_text_sha256,
        approved_input_hash=draft.approval_hash,
        review_input_hash=review_input_hash,
        registration_receipt=receipt,
    )


def _verify_generated_pdf(artifact: DraftArtifact) -> int:
    if artifact.media_type != "application/pdf" or sha256(artifact.content).hexdigest() != artifact.content_sha256:
        raise SubmissionWorkProductCoordinationBlocked("draft worker returned an invalid PDF artifact binding")
    try:
        reader = PdfReader(BytesIO(artifact.content), strict=True)
        if reader.is_encrypted:
            raise SubmissionWorkProductCoordinationBlocked("generated draft PDF must not be encrypted before managed storage")
        count = len(reader.pages)
        if not 1 <= count <= 20_000:
            raise SubmissionWorkProductCoordinationBlocked("generated draft PDF page count is outside the supported boundary")
        for page in reader.pages:
            if float(page.mediabox.width) <= 0 or float(page.mediabox.height) <= 0:
                raise SubmissionWorkProductCoordinationBlocked("generated draft PDF has invalid page geometry")
    except SubmissionWorkProductCoordinationBlocked:
        raise
    except Exception as error:
        raise SubmissionWorkProductCoordinationBlocked("generated draft PDF cannot be safely reopened") from error
    return count


def _semantic_text_hash(draft: ApprovedDraft) -> str:
    payload = {
        "title": draft.title,
        "sections": [
            {"heading": section.heading, "paragraphs": list(section.paragraphs), "source_refs": list(section.source_refs)}
            for section in draft.sections
        ],
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _review_input_hash(
    *,
    matter_id: str,
    document_kind: str,
    audience: str,
    draft: ApprovedDraft,
    artifact: DraftArtifact,
    page_count: int,
    semantic_text_sha256: str,
) -> str:
    payload = {
        "schema_version": "submission-work-product-review-v1",
        "matter_id": matter_id,
        "document_kind": document_kind,
        "audience": audience,
        "approved_input_hash": draft.approval_hash,
        "artifact_sha256": artifact.content_sha256,
        "artifact_bytes": len(artifact.content),
        "page_count": page_count,
        "semantic_text_sha256": semantic_text_sha256,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
