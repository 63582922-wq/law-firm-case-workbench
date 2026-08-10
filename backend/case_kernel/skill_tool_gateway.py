"""Concrete local Tool Gateway for the Agent's enabled document skills.

The gateway accepts already-authorized in-memory evidence handles and approved
draft snapshots only.  It deliberately has no path-string, shell or generic
HTTP execution API, so an Agent cannot turn a tool request into arbitrary
desktop access.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date
from hashlib import sha256
import json
from typing import Any

from .approved_draft_worker import ApprovedDraft, create_pdf_draft
from .evidence_normalization_worker import normalize_authorized_material
from .local_access_grants import AuthorizedOriginalFile
from .office_reading_worker import read_authorized_office_document
from .office_pdf_conversion_worker import SandboxedOfficePdfConverter
from .private_lending_transition_planner import (
    HistoricalInterestPayment,
    plan_private_lending_interest_transition,
)
from .reviewable_draft_worker import create_reviewable_docx_draft, create_reviewable_xlsx_ledger
from .research_gateway import PublicResearchGateway, ResearchBlocked
from .document_consistency_reviewer import (
    ApprovedDocumentSnapshot,
    CanonicalDocumentField,
    review_document_consistency,
)
from .skill_registry import CapabilityScope, CaseSkillRegistry, SkillRegistryBlocked


class SkillToolGatewayBlocked(PermissionError):
    """The requested tool has no safe, registered execution path."""


class CaseSkillToolGateway:
    def __init__(
        self, *, registry: CaseSkillRegistry, office_pdf_converter: SandboxedOfficePdfConverter | None = None
    ) -> None:
        self._registry = registry
        self._office_pdf_converter = office_pdf_converter

    def execute(
        self,
        *,
        skill_id: str,
        tool_id: str,
        payload: dict[str, Any],
        granted_scopes: frozenset[CapabilityScope],
        lawyer_approved: bool,
        release_locked: bool,
    ) -> tuple[Any, str]:
        """Run one whitelisted local tool and return result plus output hash.

        Payloads are intentionally object-typed at this boundary; the dispatch
        below validates each exact shape before it reaches a worker.
        """
        try:
            self._registry.authorize_tool(
                skill_id=skill_id,
                tool_id=tool_id,
                granted_scopes=granted_scopes,
                lawyer_approved=lawyer_approved,
                release_locked=release_locked,
            )
        except SkillRegistryBlocked as error:
            raise SkillToolGatewayBlocked(str(error)) from error
        if tool_id == "parse_office_document":
            source = _authorized_source(payload)
            result = read_authorized_office_document(source, detected_kind=_text(payload, "detected_kind"))
        elif tool_id == "normalize_image_or_text_pdf":
            source = _authorized_source(payload)
            result = normalize_authorized_material(source, detected_kind=_text(payload, "detected_kind"))
        elif tool_id == "render_office_to_pdf":
            source = _authorized_source(payload)
            if self._office_pdf_converter is None:
                raise SkillToolGatewayBlocked("the isolated Office PDF converter is not configured for this desktop")
            result = self._office_pdf_converter.convert(source, detected_kind=_text(payload, "detected_kind"))
        elif tool_id == "create_reviewable_docx_draft":
            result = create_reviewable_docx_draft(
                _approved_draft(payload), converter=_required_office_converter(self._office_pdf_converter)
            )
        elif tool_id == "create_pdf_derivative":
            result = create_pdf_draft(_approved_draft(payload))
        elif tool_id == "create_reviewable_xlsx_ledger":
            result = create_reviewable_xlsx_ledger(
                approval_hash=_text(payload, "approval_hash"),
                sheet_name=_text(payload, "sheet_name"),
                columns=_text_tuple(payload, "columns"),
                rows=_ledger_rows(payload),
                converter=_required_office_converter(self._office_pdf_converter),
            )
        elif tool_id == "review_document_consistency":
            result = review_document_consistency(
                documents=_document_snapshots(payload),
                canonical_fields=_canonical_fields(payload),
            )
        elif tool_id == "search_authoritative_rules":
            try:
                result = PublicResearchGateway().prepare_plan(
                    issue=_text(payload, "issue"),
                    proposed_query=_text(payload, "proposed_query"),
                )
            except ResearchBlocked as error:
                raise SkillToolGatewayBlocked("official research candidate planning was blocked") from error
        elif tool_id == "plan_private_lending_transition":
            result = plan_private_lending_interest_transition(
                contract_formed_on=_optional_date(payload, "contract_formed_on"),
                claim_filed_on=_optional_date(payload, "claim_filed_on"),
                first_instance_accepted_on=_optional_date(payload, "first_instance_accepted_on"),
                calculation_start=_required_date(payload, "calculation_start"),
                calculation_end=_required_date(payload, "calculation_end"),
                historical_interest_payments=_historical_interest_payments(payload),
            )
        else:
            raise SkillToolGatewayBlocked("registered tool has no local execution adapter")
        return result, _result_hash(result)


def _authorized_source(payload: dict[str, Any]) -> AuthorizedOriginalFile:
    source = payload.get("source")
    if not isinstance(source, AuthorizedOriginalFile):
        raise SkillToolGatewayBlocked("tool requires an authorized original-file handle")
    return source


def _approved_draft(payload: dict[str, Any]) -> ApprovedDraft:
    draft = payload.get("draft")
    if not isinstance(draft, ApprovedDraft):
        raise SkillToolGatewayBlocked("tool requires an approved structured draft snapshot")
    return draft


def _required_office_converter(
    converter: SandboxedOfficePdfConverter | None,
) -> SandboxedOfficePdfConverter:
    if converter is None:
        raise SkillToolGatewayBlocked("the isolated Office PDF converter is not configured for this desktop")
    return converter


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillToolGatewayBlocked(f"tool requires non-empty {key}")
    return value


def _optional_date(payload: dict[str, Any], key: str) -> date | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, date):
        raise SkillToolGatewayBlocked(f"tool requires {key} to be a date or null")
    return value


def _required_date(payload: dict[str, Any], key: str) -> date:
    value = _optional_date(payload, key)
    if value is None:
        raise SkillToolGatewayBlocked(f"tool requires {key} to be a date")
    return value


def _historical_interest_payments(payload: dict[str, Any]) -> tuple[HistoricalInterestPayment, ...]:
    value = payload.get("historical_interest_payments", ())
    if not isinstance(value, tuple) or not all(isinstance(item, HistoricalInterestPayment) for item in value):
        raise SkillToolGatewayBlocked("tool requires tuple[HistoricalInterestPayment] historical_interest_payments")
    return value


def _text_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise SkillToolGatewayBlocked(f"tool requires tuple[str, ...] {key}")
    return value


def _ledger_rows(payload: dict[str, Any]) -> tuple[tuple[str | int | float | None, ...], ...]:
    value = payload.get("rows")
    if not isinstance(value, tuple):
        raise SkillToolGatewayBlocked("tool requires tuple ledger rows")
    rows: list[tuple[str | int | float | None, ...]] = []
    for row in value:
        if not isinstance(row, tuple) or any(not isinstance(item, (str, int, float, type(None))) for item in row):
            raise SkillToolGatewayBlocked("ledger rows have an unsupported value type")
        rows.append(row)
    return tuple(rows)


def _document_snapshots(payload: dict[str, Any]) -> tuple[ApprovedDocumentSnapshot, ...]:
    value = payload.get("documents")
    if not isinstance(value, tuple) or not all(isinstance(item, ApprovedDocumentSnapshot) for item in value):
        raise SkillToolGatewayBlocked("tool requires tuple[ApprovedDocumentSnapshot] documents")
    return value


def _canonical_fields(payload: dict[str, Any]) -> tuple[CanonicalDocumentField, ...]:
    value = payload.get("canonical_fields")
    if not isinstance(value, tuple) or not all(isinstance(item, CanonicalDocumentField) for item in value):
        raise SkillToolGatewayBlocked("tool requires tuple[CanonicalDocumentField] canonical_fields")
    return value


def _result_hash(result: Any) -> str:
    if hasattr(result, "content_sha256"):
        value = getattr(result, "content_sha256")
        if isinstance(value, str):
            return value
    if hasattr(result, "pdf_sha256"):
        value = getattr(result, "pdf_sha256")
        if isinstance(value, str):
            return value
    if is_dataclass(result):
        payload = asdict(result)
        payload.pop("pdf_content", None)
        editable = payload.get("editable_artifact")
        if isinstance(editable, dict):
            editable.pop("content", None)
        review_pdf = payload.get("review_pdf")
        if isinstance(review_pdf, dict):
            review_pdf.pop("pdf_content", None)
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
    raise SkillToolGatewayBlocked("tool result does not have a stable audit hash")
