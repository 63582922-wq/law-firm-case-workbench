"""Server-owned generation of lawyer-reviewable Office/PDF draft pairs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol

from case_kernel.approved_draft_worker import ApprovedDraft, ApprovedSection
from case_kernel.case_ledger_postgres import CaseLedgerPersistenceBlocked
from case_kernel.models import Actor, Role
from case_kernel.reviewable_draft_worker import (
    ReviewOfficeConversionBlocked,
    ReviewOfficeConversionUnknown,
    ReviewableDraftBlocked,
    ReviewableOfficeDraft,
    create_reviewable_docx_draft,
    create_reviewable_xlsx_ledger,
)


class WebDocumentDraftBlocked(ValueError):
    """The case does not have sufficient confirmed inputs for a draft."""


class WebDocumentDraftRendererBlocked(RuntimeError):
    """The fixed isolated renderer returned a known safe rejection."""


class WebDocumentDraftRendererUnknown(TimeoutError):
    """The renderer result may be unknown and must not be automatically retried."""


class ReviewableDraftStore(Protocol):
    def get_reviewable_office_draft_snapshot(self, *, matter_id: str, actor: Actor) -> object: ...
    def register_reviewable_office_draft_pair(self, **kwargs: Any) -> object: ...
    def approve_reviewable_office_draft_pair(self, **kwargs: Any) -> object: ...


class WebReviewDocumentStore(Protocol):
    def put_verified_office_artifact(self, content: bytes, *, content_sha256: str, media_type: str) -> object: ...
    def put_verified_review_pdf(self, content: bytes, *, content_sha256: str) -> object: ...


@dataclass(frozen=True)
class WebDocumentDraftReceipt:
    pair_id: str
    matter_version: int
    document_kind: str
    review_input_hash: str


class WebDocumentDraftService:
    """Build candidates only from confirmed server snapshots.

    No browser text, prompt, URL, path, or uploaded filename enters this
    service.  A generated pair remains an internal review candidate until a
    lawyer approves the exact server-derived review hash.
    """

    _KINDS = frozenset({"CASE_REVIEW_MEMO", "PAYMENT_LEDGER"})

    def __init__(self, *, case_ledger_store: object, reviewable_store: ReviewableDraftStore, object_store: WebReviewDocumentStore, system_worker_for_firm, converter) -> None:
        self._case = case_ledger_store
        self._reviewable = reviewable_store
        self._objects = object_store
        self._worker_for_firm = system_worker_for_firm
        self._converter = converter

    def snapshot(self, *, matter_id: str, actor: Actor) -> object:
        return self._reviewable.get_reviewable_office_draft_snapshot(matter_id=matter_id, actor=actor)

    def generate(self, *, matter_id: str, actor: Actor, expected_version: int, idempotency_key: str, document_kind: str) -> WebDocumentDraftReceipt:
        if Role.LEAD_LAWYER not in actor.roles:
            raise WebDocumentDraftBlocked("文书候选生成需要主办律师权限")
        if document_kind not in self._KINDS:
            raise WebDocumentDraftBlocked("文书类型必须由系统固定选择")
        snapshot = self._case.get_case_snapshot(matter_id=matter_id, actor=actor)
        if getattr(snapshot, "version", None) != expected_version:
            raise WebDocumentDraftBlocked("案件版本已变化，请刷新后重新生成")
        draft, detected_kind = _approved_draft_from_snapshot(snapshot, document_kind)
        try:
            if detected_kind == "WORD_DOCUMENT":
                pair: ReviewableOfficeDraft = create_reviewable_docx_draft(
                    draft, converter=self._converter
                )
            else:
                rows = _ledger_rows(snapshot)
                pair = create_reviewable_xlsx_ledger(
                    approval_hash=draft.approval_hash,
                    sheet_name="已确认收付款",
                    columns=("日期", "收付方向", "金额（元）", "币种", "付款方", "收款方", "核对状态", "交易编号"),
                    rows=rows,
                    converter=self._converter,
                )
        except ReviewOfficeConversionUnknown as error:
            raise WebDocumentDraftRendererUnknown(
                "isolated document renderer result is unknown"
            ) from error
        except (ReviewOfficeConversionBlocked, ReviewableDraftBlocked) as error:
            raise WebDocumentDraftRendererBlocked(
                "isolated document renderer rejected the candidate"
            ) from error
        worker = self._worker_for_firm(actor.firm_id)
        editable = self._objects.put_verified_office_artifact(
            pair.editable_artifact.content,
            content_sha256=pair.editable_artifact.content_sha256,
            media_type=pair.editable_artifact.media_type,
        )
        rendered = self._objects.put_verified_review_pdf(pair.review_pdf.pdf_content, content_sha256=pair.review_pdf.pdf_sha256)
        receipt = self._reviewable.register_reviewable_office_draft_pair(
            matter_id=matter_id, actor=worker, expected_version=expected_version,
            idempotency_key=idempotency_key, document_kind=document_kind,
            editable_media_type=pair.editable_artifact.media_type,
            editable_object_key=editable.object_key, editable_sha256=editable.content_sha256,
            editable_bytes=editable.byte_size, review_pdf_object_key=rendered.object_key,
            review_pdf_sha256=rendered.content_sha256, review_pdf_bytes=rendered.byte_size,
            review_pdf_page_count=pair.review_pdf.page_count, approval_input_hash=pair.approval_hash,
            render_verification_hash=pair.review_pdf.render_verification_hash,
            review_input_hash=pair.review_input_hash,
        )
        return WebDocumentDraftReceipt(str(receipt.object_id), int(receipt.matter_version), document_kind, pair.review_input_hash)


def _approved_draft_from_snapshot(snapshot: object, document_kind: str) -> tuple[ApprovedDraft, str]:
    facts = tuple(row for row in getattr(snapshot, "facts", ()) if isinstance(row, Mapping) and row.get("status") == "CONFIRMED")
    claims = tuple(row for row in getattr(snapshot, "claims", ()) if isinstance(row, Mapping) and row.get("status") == "CONFIRMED_SCOPE")
    transactions = tuple(row for row in getattr(snapshot, "transactions", ()) if isinstance(row, Mapping) and row.get("status") == "CONFIRMED")
    if document_kind == "CASE_REVIEW_MEMO":
        if not facts and not claims:
            raise WebDocumentDraftBlocked("本案没有已确认事实或诉请，不能生成文书候选")
        sections: list[ApprovedSection] = []
        if facts:
            sections.append(ApprovedSection("已确认事实", tuple(str(row["original_text"]).strip() for row in facts), tuple(f"事实 {row['fact_id']}" for row in facts)))
        if claims:
            sections.append(ApprovedSection("已确认诉请", tuple(_claim_text(row) for row in claims), tuple(f"诉请 {row['claim_id']}" for row in claims)))
        if transactions:
            sections.append(ApprovedSection("已确认收付款", (f"共 {len(transactions)} 笔已确认交易，详细明细见同案收付款台账。",), tuple(f"交易 {row['transaction_id']}" for row in transactions)))
        refs = [getattr(snapshot, "snapshot_hash", "")]
        return ApprovedDraft("案件核对摘要（律师审阅候选）", tuple(sections), _approval_hash(snapshot, document_kind, refs)), "WORD_DOCUMENT"
    if not transactions:
        raise WebDocumentDraftBlocked("本案没有已确认收付款，不能生成收付款核对表")
    refs = [str(row["transaction_id"]) for row in transactions]
    section = ApprovedSection("已确认收付款", (f"本表仅列入 {len(transactions)} 笔已确认交易；未确认记录未写入。",), tuple(f"交易 {ref}" for ref in refs))
    return ApprovedDraft("已确认收付款核对表（律师审阅候选）", (section,), _approval_hash(snapshot, document_kind, refs)), "SPREADSHEET"


_DIRECTION_DISPLAY = {
    "INBOUND": "收款",
    "INCOMING": "收款",
    "OUTBOUND": "付款",
    "OUTGOING": "付款",
}
_CURRENCY_DISPLAY = {
    "CNY": "人民币",
    "EUR": "欧元",
    "HKD": "港币",
    "JPY": "日元",
    "USD": "美元",
}


def _ledger_rows(
    snapshot: object,
) -> tuple[tuple[str | Decimal | date | None, ...], ...]:
    """Project confirmed transaction facts into lawyer-readable ledger cells.

    This is a deterministic presentation projection only.  The confirmed
    transaction, its raw enum values and fixed-decimal source amount remain in
    the immutable case snapshot and the candidate input hash; a browser or a
    model never supplies these display values.
    """

    rows = []
    for row in getattr(snapshot, "transactions", ()):
        if not isinstance(row, Mapping) or row.get("status") != "CONFIRMED":
            continue
        rows.append(
            (
                _ledger_date(row.get("local_date")),
                _direction_display(row.get("direction")),
                _ledger_amount(row.get("amount")),
                _currency_display(row.get("currency")),
                _lawyer_label(row.get("payer_label")),
                _lawyer_label(row.get("payee_label")),
                "已确认",
                _lawyer_label(row.get("transaction_id")),
            )
        )
    return tuple(rows)


def _ledger_date(value: object) -> date | str:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if candidate:
            try:
                return date.fromisoformat(candidate)
            except ValueError:
                pass
    return "待律师核对"


def _ledger_amount(value: object) -> Decimal | str:
    if isinstance(value, bool) or value is None:
        return "待律师核对"
    candidate = str(value).strip()
    if not candidate:
        return "待律师核对"
    try:
        parsed = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return "待律师核对"
    if not parsed.is_finite():
        return "待律师核对"
    return parsed


def _direction_display(value: object) -> str:
    if not isinstance(value, str):
        return "待律师核对"
    return _DIRECTION_DISPLAY.get(value.strip().upper(), "待律师核对")


def _currency_display(value: object) -> str:
    if not isinstance(value, str):
        return "待律师核对"
    candidate = value.strip().upper()
    return _CURRENCY_DISPLAY.get(candidate, candidate if len(candidate) == 3 else "待律师核对")


def _lawyer_label(value: object) -> str:
    if not isinstance(value, str):
        return "待律师核对"
    candidate = value.strip()
    return candidate or "待律师核对"


def _claim_text(row: Mapping[str, Any]) -> str:
    amount = row.get("claimed_amount") or "未记录金额"
    currency = row.get("currency") or ""
    return f"{str(row.get('original_claim_text') or '').strip()}（金额：{amount} {currency}）"


def _approval_hash(snapshot: object, document_kind: str, refs: list[str]) -> str:
    payload = {"schema": "web-document-candidate-v1", "snapshot_hash": getattr(snapshot, "snapshot_hash", None), "document_kind": document_kind, "refs": refs}
    if not isinstance(payload["snapshot_hash"], str) or len(payload["snapshot_hash"]) != 64:
        raise WebDocumentDraftBlocked("案件台账没有可核验快照")
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
