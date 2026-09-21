"""Deterministic, non-mutating consistency review for approved draft snapshots.

This is the bounded foundation for an adversarial document-review Agent.  It
does not decide a legal position or rewrite a document; it only compares the
lawyer-approved canonical values and explicit forbidden variants against the
approved draft text, then returns review findings with source locations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

from .approved_draft_worker import ApprovedDraft


class DocumentConsistencyBlocked(ValueError):
    """The review input does not describe a bounded approved-document set."""


@dataclass(frozen=True)
class CanonicalDocumentField:
    field_id: str
    label: str
    expected_value: str
    required_document_kinds: tuple[str, ...]
    forbidden_variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovedDocumentSnapshot:
    document_id: str
    document_kind: str
    draft: ApprovedDraft


@dataclass(frozen=True)
class DocumentConsistencyFinding:
    finding_id: str
    severity: str
    code: str
    document_id: str
    document_kind: str
    section_index: int | None
    field_id: str | None
    message: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class DocumentConsistencyReport:
    input_hash: str
    findings: tuple[DocumentConsistencyFinding, ...]
    blocking_count: int
    warning_count: int


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_DOCUMENTS = 100
_MAX_FIELDS = 100
_MAX_VARIANTS = 30


def review_document_consistency(
    *,
    documents: tuple[ApprovedDocumentSnapshot, ...],
    canonical_fields: tuple[CanonicalDocumentField, ...],
) -> DocumentConsistencyReport:
    """Return findings only; immutable input documents are never changed."""

    _validate_input(documents, canonical_fields)
    findings: list[DocumentConsistencyFinding] = []
    for document in documents:
        sections = ((None, document.draft.title, ()),) + tuple(
            (index, "\n".join(section.paragraphs), section.source_refs)
            for index, section in enumerate(document.draft.sections, start=1)
        )
        full_text = "\n".join(text for _, text, _ in sections)
        for field in canonical_fields:
            if document.document_kind not in field.required_document_kinds:
                continue
            if field.expected_value not in full_text:
                findings.append(_finding(
                    severity="BLOCKING",
                    code="MISSING_CANONICAL_VALUE",
                    document=document,
                    section_index=None,
                    field=field,
                    message=f"{field.label}未出现为律师确认的值“{field.expected_value}”。",
                    source_refs=(),
                ))
            for section_index, text, source_refs in sections:
                for variant in field.forbidden_variants:
                    if variant in text:
                        findings.append(_finding(
                            severity="BLOCKING",
                            code="CONFLICTING_VALUE",
                            document=document,
                            section_index=section_index,
                            field=field,
                            message=f"{field.label}出现与确认值冲突的文本“{variant}”；应由律师复核。",
                            source_refs=source_refs,
                        ))
        for section_index, text, source_refs in sections:
            if section_index is not None and _contains_material_statement(text) and not source_refs:
                findings.append(_finding(
                    severity="WARNING",
                    code="MISSING_SOURCE_REFS",
                    document=document,
                    section_index=section_index,
                    field=None,
                    message="该文书段落含实质文字但没有来源引用，不能作为已完成溯源处理。",
                    source_refs=(),
                ))
    ordered = tuple(sorted(findings, key=lambda item: (item.document_id, item.section_index or 0, item.code, item.field_id or "")))
    return DocumentConsistencyReport(
        input_hash=_input_hash(documents, canonical_fields),
        findings=ordered,
        blocking_count=sum(item.severity == "BLOCKING" for item in ordered),
        warning_count=sum(item.severity == "WARNING" for item in ordered),
    )


def _validate_input(
    documents: tuple[ApprovedDocumentSnapshot, ...],
    fields: tuple[CanonicalDocumentField, ...],
) -> None:
    if not 1 <= len(documents) <= _MAX_DOCUMENTS or not 1 <= len(fields) <= _MAX_FIELDS:
        raise DocumentConsistencyBlocked("document consistency review requires bounded documents and canonical fields")
    seen_documents: set[str] = set()
    for document in documents:
        if not document.document_id.strip() or document.document_id in seen_documents or not document.document_kind.strip():
            raise DocumentConsistencyBlocked("document snapshot identifier or kind is invalid")
        seen_documents.add(document.document_id)
        if not isinstance(document.draft, ApprovedDraft) or not _SHA256.fullmatch(document.draft.approval_hash):
            raise DocumentConsistencyBlocked("document snapshot requires an approved structured draft")
    seen_fields: set[str] = set()
    document_kinds = {item.document_kind for item in documents}
    for field in fields:
        if not field.field_id.strip() or field.field_id in seen_fields or not field.label.strip():
            raise DocumentConsistencyBlocked("canonical field identifier is invalid")
        seen_fields.add(field.field_id)
        if not field.expected_value.strip() or len(field.expected_value) > 500:
            raise DocumentConsistencyBlocked("canonical field expected value is invalid")
        if not field.required_document_kinds or not set(field.required_document_kinds).issubset(document_kinds):
            raise DocumentConsistencyBlocked("canonical field has an unknown required document kind")
        if len(field.forbidden_variants) > _MAX_VARIANTS or any(
            not value.strip() or value == field.expected_value or len(value) > 500
            for value in field.forbidden_variants
        ):
            raise DocumentConsistencyBlocked("canonical field forbidden variants are invalid")


def _finding(
    *,
    severity: str,
    code: str,
    document: ApprovedDocumentSnapshot,
    section_index: int | None,
    field: CanonicalDocumentField | None,
    message: str,
    source_refs: tuple[str, ...],
) -> DocumentConsistencyFinding:
    payload = {
        "severity": severity, "code": code, "document_id": document.document_id,
        "section_index": section_index, "field_id": field.field_id if field else None,
        "message": message, "source_refs": source_refs,
    }
    finding_id = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return DocumentConsistencyFinding(
        finding_id=finding_id, severity=severity, code=code,
        document_id=document.document_id, document_kind=document.document_kind,
        section_index=section_index, field_id=field.field_id if field else None,
        message=message, source_refs=source_refs,
    )


def _input_hash(documents: tuple[ApprovedDocumentSnapshot, ...], fields: tuple[CanonicalDocumentField, ...]) -> str:
    payload = {
        "documents": [
            {"document_id": item.document_id, "document_kind": item.document_kind, "draft": asdict(item.draft)}
            for item in documents
        ],
        "canonical_fields": [asdict(item) for item in fields],
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _contains_material_statement(text: str) -> bool:
    return len(text.strip()) >= 2
