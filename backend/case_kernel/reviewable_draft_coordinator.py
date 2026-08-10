"""Persist one immutable editable-Office/PDF review pair as a single command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .managed_artifact_store import LocalEncryptedArtifactStore, ManagedArtifactBlocked, StoredArtifactObject
from .models import Actor, Role
from .reviewable_draft_worker import ReviewableOfficeDraft


class ReviewableDraftCoordinationBlocked(ValueError):
    pass


class ReviewableDraftPersistencePort(Protocol):
    def register_reviewable_office_draft_pair(self, **kwargs) -> CaseLedgerCommandReceipt: ...


@dataclass(frozen=True)
class CoordinatedReviewableOfficeDraft:
    matter_id: str
    pair_id: str
    editable: StoredArtifactObject
    review_pdf: StoredArtifactObject
    review_input_hash: str
    registration_receipt: CaseLedgerCommandReceipt


def coordinate_reviewable_office_draft(
    *, matter_id: str, expected_version: int, idempotency_key: str,
    document_kind: str, draft: ReviewableOfficeDraft, case_root: str | Path,
    artifact_store: LocalEncryptedArtifactStore, persistence: ReviewableDraftPersistencePort,
    system_actor: Actor,
) -> CoordinatedReviewableOfficeDraft:
    if system_actor.roles != frozenset({Role.SYSTEM_WORKER}):
        raise ReviewableDraftCoordinationBlocked("reviewable Office draft generation requires a dedicated SYSTEM_WORKER")
    if not matter_id.strip() or expected_version < 1 or not idempotency_key.strip() or not document_kind.strip():
        raise ReviewableDraftCoordinationBlocked("reviewable Office draft command identity is invalid")
    root = Path(case_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ReviewableDraftCoordinationBlocked("case root must be an existing directory")
    try:
        editable = artifact_store.put_bytes(
            draft.editable_artifact.content,
            expected_sha256=draft.editable_artifact.content_sha256,
            case_root=root,
        )
        review_pdf = artifact_store.put_bytes(
            draft.review_pdf.pdf_content,
            expected_sha256=draft.review_pdf.pdf_sha256,
            case_root=root,
        )
    except ManagedArtifactBlocked as error:
        raise ReviewableDraftCoordinationBlocked("reviewable Office pair could not enter managed encrypted storage") from error
    receipt = persistence.register_reviewable_office_draft_pair(
        matter_id=matter_id, actor=system_actor, expected_version=expected_version,
        idempotency_key=idempotency_key, document_kind=document_kind.strip(),
        editable_media_type=draft.editable_artifact.media_type,
        editable_object_key=editable.object_key, editable_sha256=editable.plaintext_sha256,
        editable_bytes=editable.plaintext_bytes, review_pdf_object_key=review_pdf.object_key,
        review_pdf_sha256=review_pdf.plaintext_sha256, review_pdf_bytes=review_pdf.plaintext_bytes,
        review_pdf_page_count=draft.review_pdf.page_count, approval_input_hash=draft.approval_hash,
        render_verification_hash=draft.review_pdf.render_verification_hash,
        review_input_hash=draft.review_input_hash,
    )
    return CoordinatedReviewableOfficeDraft(matter_id, receipt.object_id, editable, review_pdf, draft.review_input_hash, receipt)
