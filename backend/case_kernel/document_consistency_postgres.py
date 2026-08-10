"""Append-only persistence for deterministic document-consistency findings.

The database never stores approved document text, a canonical fact value, or
an Agent prompt here.  It stores only the approved PDF work-product bindings,
hashes, finding codes and hash-only field/source references needed to prove
that a PASS review covered the exact submitted work-product set.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .case_ledger_postgres import (
    CaseLedgerCommandReceipt,
    CaseLedgerPersistenceBlocked,
    _advisory_lock,
    _authorize_and_lock_matter,
    _authorize_matter_read,
    _finish_command,
    _payload_hash,
    _prior_receipt,
    _require_positive_version,
    _require_roles,
    _validate_command_identity,
    _validate_read_identity,
    _validate_sha256,
)
from .models import Actor, Role
from .document_consistency_reviewer import (
    ApprovedDocumentSnapshot,
    CanonicalDocumentField,
    DocumentConsistencyReport,
)


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,119}$")
_MAX_DOCUMENTS = 100
_MAX_FINDINGS = 500


@dataclass(frozen=True)
class ReviewedWorkProduct:
    work_product_id: str
    review_input_hash: str


@dataclass(frozen=True)
class PersistedDocumentConsistencyFinding:
    finding_id: str
    work_product_id: str
    severity: str
    code: str
    field_id_hash: str | None
    source_refs_hash: str


@dataclass(frozen=True)
class PersistentDocumentConsistencySnapshot:
    matter_id: str
    matter_version: int
    reviews: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    snapshot_hash: str


@dataclass(frozen=True)
class DocumentConsistencyPersistenceRecord:
    """Safe persistence projection derived from a deterministic review report."""

    canonical_fields_hash: str
    input_hash: str
    output_hash: str
    documents: tuple[ReviewedWorkProduct, ...]
    findings: tuple[PersistedDocumentConsistencyFinding, ...]


def document_consistency_output_hash(
    *, input_hash: str, findings: tuple[PersistedDocumentConsistencyFinding, ...]
) -> str:
    """Hash the safe, ordered report projection the persistent store accepts."""

    payload = {
        "input_hash": input_hash,
        "findings": [
            {
                "finding_id": item.finding_id,
                "work_product_id": item.work_product_id,
                "severity": item.severity,
                "code": item.code,
                "field_id_hash": item.field_id_hash,
                "source_refs_hash": item.source_refs_hash,
            }
            for item in sorted(findings, key=lambda value: value.finding_id)
        ],
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_document_consistency_persistence_record(
    *,
    report: DocumentConsistencyReport,
    documents: tuple[ApprovedDocumentSnapshot, ...],
    canonical_fields: tuple[CanonicalDocumentField, ...],
    work_product_by_document_id: dict[str, ReviewedWorkProduct],
) -> DocumentConsistencyPersistenceRecord:
    """Convert one deterministic review into the only safe DB projection.

    The caller supplies the already-approved document snapshots that produced
    ``report`` and their current approved PDF work-product bindings.  No
    document text or canonical value is carried into the returned object.
    """

    if not isinstance(report, DocumentConsistencyReport):
        raise CaseLedgerPersistenceBlocked("document consistency report is invalid")
    _validate_sha256("document consistency input_hash", report.input_hash)
    document_ids = {item.document_id for item in documents}
    if document_ids != set(work_product_by_document_id):
        raise CaseLedgerPersistenceBlocked(
            "document consistency work-product bindings must exactly match reviewed documents"
        )
    bindings = _validate_documents(tuple(work_product_by_document_id.values()))
    binding_by_document_id = work_product_by_document_id
    findings = tuple(
        PersistedDocumentConsistencyFinding(
            finding_id=item.finding_id,
            work_product_id=binding_by_document_id[item.document_id].work_product_id,
            severity=item.severity,
            code=item.code,
            field_id_hash=(
                sha256(item.field_id.encode("utf-8")).hexdigest()
                if item.field_id is not None else None
            ),
            source_refs_hash=sha256(
                json.dumps(item.source_refs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        for item in report.findings
    )
    validated_findings = _validate_findings(findings, documents=bindings)
    if sum(item.severity == "BLOCKING" for item in validated_findings) != report.blocking_count:
        raise CaseLedgerPersistenceBlocked("document consistency report blocking count is inconsistent")
    if sum(item.severity == "WARNING" for item in validated_findings) != report.warning_count:
        raise CaseLedgerPersistenceBlocked("document consistency report warning count is inconsistent")
    canonical_payload = [
        {
            "field_id": item.field_id,
            "label": item.label,
            "expected_value": item.expected_value,
            "required_document_kinds": item.required_document_kinds,
            "forbidden_variants": item.forbidden_variants,
        }
        for item in canonical_fields
    ]
    canonical_fields_hash = sha256(
        json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DocumentConsistencyPersistenceRecord(
        canonical_fields_hash=canonical_fields_hash,
        input_hash=report.input_hash,
        output_hash=document_consistency_output_hash(
            input_hash=report.input_hash, findings=validated_findings
        ),
        documents=bindings,
        findings=validated_findings,
    )


class PostgresDocumentConsistencyReviewStore:
    """Writes review outputs only through the dedicated SYSTEM_WORKER role."""

    _RECORD_ROLES = frozenset({Role.SYSTEM_WORKER})
    _READ_ROLES = frozenset(
        {Role.ASSISTANT, Role.COLLABORATING_LAWYER, Role.LEAD_LAWYER, Role.REVIEWER}
    )

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self._dsn = dsn

    def record_review(
        self,
        *,
        matter_id: str,
        actor: Actor,
        expected_version: int,
        idempotency_key: str,
        canonical_fields_hash: str,
        input_hash: str,
        output_hash: str,
        documents: tuple[ReviewedWorkProduct, ...],
        findings: tuple[PersistedDocumentConsistencyFinding, ...],
    ) -> CaseLedgerCommandReceipt:
        _validate_command_identity(matter_id=matter_id, actor=actor, idempotency_key=idempotency_key)
        _require_roles(actor, self._RECORD_ROLES)
        _require_positive_version(expected_version)
        _validate_sha256("canonical_fields_hash", canonical_fields_hash)
        _validate_sha256("input_hash", input_hash)
        _validate_sha256("output_hash", output_hash)
        validated_documents = _validate_documents(documents)
        validated_findings = _validate_findings(findings, documents=validated_documents)
        derived_output_hash = document_consistency_output_hash(
            input_hash=input_hash, findings=validated_findings
        )
        if output_hash != derived_output_hash:
            raise CaseLedgerPersistenceBlocked(
                "document consistency output hash differs from the safe report projection"
            )
        blocking_count = sum(item.severity == "BLOCKING" for item in validated_findings)
        warning_count = sum(item.severity == "WARNING" for item in validated_findings)
        status = "PASS" if blocking_count == 0 else "BLOCKED"
        command_name = "RECORD_DOCUMENT_CONSISTENCY_REVIEW"
        request_payload = {
            "matter_id": matter_id,
            "expected_version": expected_version,
            "canonical_fields_hash": canonical_fields_hash,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "documents": [item.__dict__ for item in validated_documents],
            "findings": [item.__dict__ for item in validated_findings],
        }
        payload_hash = _payload_hash(request_payload)
        review_id = str(uuid4())
        with self._transaction(actor.firm_id) as connection:
            prior = self._begin(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                idempotency_key=idempotency_key, command_name=command_name, payload_hash=payload_hash,
            )
            if prior is not None:
                return prior
            self._assert_documents_are_current_approved_products(
                connection=connection, actor=actor, matter_id=matter_id, documents=validated_documents
            )
            connection.execute(
                """
                INSERT INTO document_consistency_reviews (
                    review_id, firm_id, matter_id, reviewed_matter_version,
                    canonical_fields_hash, input_hash, output_hash, blocking_count,
                    warning_count, status, recorded_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                # The review command itself advances the matter ledger once.
                # Bind the saved review to that resulting version, so the next
                # QA command can use it only if no intervening case change
                # occurred after this exact review completed.
                (review_id, actor.firm_id, matter_id, expected_version + 1, canonical_fields_hash,
                 input_hash, output_hash, blocking_count, warning_count, status, actor.actor_id),
            )
            for document in validated_documents:
                connection.execute(
                    """
                    INSERT INTO document_consistency_review_documents (
                        review_id, work_product_id, firm_id, matter_id, review_input_hash
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (review_id, document.work_product_id, actor.firm_id, matter_id,
                     document.review_input_hash),
                )
            for finding in validated_findings:
                connection.execute(
                    """
                    INSERT INTO document_consistency_review_findings (
                        review_id, finding_id, firm_id, matter_id, work_product_id,
                        severity, code, field_id_hash, source_refs_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (review_id, finding.finding_id, actor.firm_id, matter_id,
                     finding.work_product_id, finding.severity, finding.code,
                     finding.field_id_hash, finding.source_refs_hash),
                )
            return _finish_command(
                connection, actor=actor, matter_id=matter_id, expected_version=expected_version,
                command_name=command_name, idempotency_key=idempotency_key, payload_hash=payload_hash,
                event_type="DOCUMENT_CONSISTENCY_REVIEW_RECORDED",
                object_type="DOCUMENT_CONSISTENCY_REVIEW", object_id=review_id,
                audit_payload={
                    "review_id": review_id, "input_hash": input_hash,
                    "output_hash": output_hash, "canonical_fields_hash": canonical_fields_hash,
                    "status": status, "blocking_count": blocking_count,
                    "warning_count": warning_count,
                    "document_count": len(validated_documents),
                },
                stale_submission=False, stale_calculations=False,
            )

    def get_snapshot(
        self, *, matter_id: str, actor: Actor
    ) -> PersistentDocumentConsistencySnapshot:
        _validate_read_identity(matter_id=matter_id, actor=actor)
        _require_roles(actor, self._READ_ROLES)
        with self._read_transaction(actor.firm_id) as connection:
            _authorize_matter_read(
                connection, actor=actor, matter_id=matter_id, allowed_roles=self._READ_ROLES
            )
            matter = connection.execute(
                "SELECT version FROM matters WHERE matter_id = %s AND firm_id = %s",
                (matter_id, actor.firm_id),
            ).fetchone()
            if matter is None:
                raise KeyError(matter_id)
            reviews = _rows(connection.execute(
                """
                SELECT review_id, reviewed_matter_version, canonical_fields_hash, input_hash,
                       output_hash, blocking_count, warning_count, status, recorded_at
                FROM document_consistency_reviews
                WHERE matter_id = %s AND firm_id = %s
                ORDER BY recorded_at DESC, review_id DESC
                """, (matter_id, actor.firm_id)
            ).fetchall())
            findings = _rows(connection.execute(
                """
                SELECT finding.review_id, finding.finding_id, finding.work_product_id,
                       finding.severity, finding.code, finding.field_id_hash,
                       finding.source_refs_hash
                FROM document_consistency_review_findings finding
                WHERE finding.matter_id = %s AND finding.firm_id = %s
                ORDER BY finding.review_id DESC, finding.severity ASC, finding.code ASC,
                         finding.finding_id ASC
                """, (matter_id, actor.firm_id)
            ).fetchall())
        payload = {
            "matter_id": matter_id, "matter_version": matter["version"],
            "reviews": reviews, "findings": findings,
        }
        return PersistentDocumentConsistencySnapshot(
            matter_id=matter_id, matter_version=matter["version"], reviews=tuple(reviews),
            findings=tuple(findings), snapshot_hash=_payload_hash(payload)
        )

    def _assert_documents_are_current_approved_products(
        self, *, connection, actor: Actor, matter_id: str,
        documents: tuple[ReviewedWorkProduct, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT work_product_id, review_input_hash, status
            FROM submission_work_products
            WHERE matter_id = %s AND firm_id = %s
              AND work_product_id = ANY(%s::uuid[])
            FOR KEY SHARE
            """, (matter_id, actor.firm_id, [item.work_product_id for item in documents]),
        ).fetchall()
        by_id = {str(row["work_product_id"]): row for row in rows}
        if len(by_id) != len(documents):
            raise CaseLedgerPersistenceBlocked(
                "document consistency review references an unknown work product"
            )
        for document in documents:
            product = by_id[document.work_product_id]
            if product["status"] != "APPROVED" or product["review_input_hash"] != document.review_input_hash:
                raise CaseLedgerPersistenceBlocked(
                    "document consistency review must bind current approved work-product inputs"
                )

    def _begin(self, connection, *, actor: Actor, matter_id: str, expected_version: int,
               idempotency_key: str, command_name: str, payload_hash: str):
        _advisory_lock(connection, actor=actor, matter_id=matter_id, command_name=command_name,
                       idempotency_key=idempotency_key)
        prior = _prior_receipt(connection, actor=actor, matter_id=matter_id,
                               command_name=command_name, idempotency_key=idempotency_key,
                               payload_hash=payload_hash)
        if prior is not None:
            return prior
        _authorize_and_lock_matter(connection, actor=actor, matter_id=matter_id,
                                    expected_version=expected_version,
                                    allowed_roles=self._RECORD_ROLES)
        return None

    @contextmanager
    def _transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection

    @contextmanager
    def _read_transaction(self, firm_id: str) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SELECT set_config('app.firm_id', %s, true)", (firm_id,))
            yield connection


def _validate_documents(documents: tuple[ReviewedWorkProduct, ...]) -> tuple[ReviewedWorkProduct, ...]:
    if not 1 <= len(documents) <= _MAX_DOCUMENTS:
        raise CaseLedgerPersistenceBlocked("document consistency review must bind 1 to 100 work products")
    seen: set[str] = set()
    validated: list[ReviewedWorkProduct] = []
    for document in documents:
        if not isinstance(document, ReviewedWorkProduct):
            raise CaseLedgerPersistenceBlocked("document consistency work-product binding is invalid")
        _validate_uuid("work_product_id", document.work_product_id)
        _validate_sha256("review_input_hash", document.review_input_hash)
        if document.work_product_id in seen:
            raise CaseLedgerPersistenceBlocked("document consistency work-product bindings must be unique")
        seen.add(document.work_product_id)
        validated.append(document)
    return tuple(sorted(validated, key=lambda value: value.work_product_id))


def _validate_findings(
    findings: tuple[PersistedDocumentConsistencyFinding, ...], *,
    documents: tuple[ReviewedWorkProduct, ...],
) -> tuple[PersistedDocumentConsistencyFinding, ...]:
    if len(findings) > _MAX_FINDINGS:
        raise CaseLedgerPersistenceBlocked("document consistency review has too many findings")
    allowed_document_ids = {item.work_product_id for item in documents}
    seen: set[str] = set()
    validated: list[PersistedDocumentConsistencyFinding] = []
    for finding in findings:
        if not isinstance(finding, PersistedDocumentConsistencyFinding):
            raise CaseLedgerPersistenceBlocked("document consistency finding is invalid")
        _validate_sha256("finding_id", finding.finding_id)
        _validate_uuid("finding work_product_id", finding.work_product_id)
        if finding.work_product_id not in allowed_document_ids:
            raise CaseLedgerPersistenceBlocked("document consistency finding is outside the reviewed work products")
        if finding.severity not in {"BLOCKING", "WARNING"} or not _CODE.fullmatch(finding.code):
            raise CaseLedgerPersistenceBlocked("document consistency finding classification is invalid")
        if finding.field_id_hash is not None:
            _validate_sha256("finding field_id_hash", finding.field_id_hash)
        _validate_sha256("finding source_refs_hash", finding.source_refs_hash)
        if finding.finding_id in seen:
            raise CaseLedgerPersistenceBlocked("document consistency finding identifiers must be unique")
        seen.add(finding.finding_id)
        validated.append(finding)
    return tuple(sorted(validated, key=lambda value: value.finding_id))


def _validate_uuid(label: str, value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise CaseLedgerPersistenceBlocked(f"{label} must be a UUID") from error


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.isoformat() if hasattr(value, "isoformat") else str(value)
            if key.endswith("_id") and value is not None else value
            for key, value in row.items()
        }
        for row in rows
    ]
