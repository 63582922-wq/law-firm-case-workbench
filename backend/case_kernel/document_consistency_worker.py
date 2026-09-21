"""Bounded SYSTEM_WORKER execution of the deterministic document review.

This worker consumes only already-approved structured draft snapshots supplied
by an internal coordinator.  It deliberately has no HTTP, filesystem, model
or browser input: raw text never crosses the persistence boundary.
"""

from __future__ import annotations

from typing import Protocol

from .case_ledger_postgres import CaseLedgerCommandReceipt
from .document_consistency_postgres import (
    ReviewedWorkProduct,
    build_document_consistency_persistence_record,
)
from .document_consistency_reviewer import (
    ApprovedDocumentSnapshot,
    CanonicalDocumentField,
    review_document_consistency,
)
from .models import Actor, Role


class DocumentConsistencyWorkerBlocked(PermissionError):
    """An untrusted caller attempted to execute or persist a review."""


class DocumentConsistencyReviewPersistence(Protocol):
    def record_review(self, **kwargs) -> CaseLedgerCommandReceipt: ...


def run_document_consistency_review(
    *,
    matter_id: str,
    expected_version: int,
    worker: Actor,
    idempotency_key: str,
    documents: tuple[ApprovedDocumentSnapshot, ...],
    canonical_fields: tuple[CanonicalDocumentField, ...],
    work_product_by_document_id: dict[str, ReviewedWorkProduct],
    persistence: DocumentConsistencyReviewPersistence,
) -> CaseLedgerCommandReceipt:
    """Run, project and append exactly one deterministic review result.

    The persistence store independently verifies that every target is a current
    approved PDF work product with its exact review-input hash.  This worker
    returns only the normal command receipt; callers that need the detailed
    report read the role-gated hash-only snapshot afterward.
    """

    if worker.roles != frozenset({Role.SYSTEM_WORKER}):
        raise DocumentConsistencyWorkerBlocked(
            "document consistency execution requires a dedicated SYSTEM_WORKER"
        )
    if expected_version < 1 or not 16 <= len(idempotency_key) <= 200 or not idempotency_key.isascii():
        raise DocumentConsistencyWorkerBlocked(
            "document consistency execution request is invalid"
        )
    report = review_document_consistency(documents=documents, canonical_fields=canonical_fields)
    record = build_document_consistency_persistence_record(
        report=report,
        documents=documents,
        canonical_fields=canonical_fields,
        work_product_by_document_id=work_product_by_document_id,
    )
    return persistence.record_review(
        matter_id=matter_id,
        actor=worker,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        canonical_fields_hash=record.canonical_fields_hash,
        input_hash=record.input_hash,
        output_hash=record.output_hash,
        documents=record.documents,
        findings=record.findings,
    )
