#!/usr/bin/env python3
"""Prove domestic-format Office and direct-PDF production delivery.

The probe deliberately calls the same bounded DOCX/XLSX generators used by
the document Agent. It then sends those immutable bytes through the
authenticated renderer client and independently verifies the returned PDF's
text, A4 geometry, embedded CJK fonts, page count and real raster output.  It
also calls the production direct-PDF generator so a non-portable CID font can
never hide behind a successful LibreOffice conversion.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
import json
from math import hypot
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid5

from PIL import Image, ImageChops
from pypdf import PdfReader

from case_kernel.approved_draft_worker import ApprovedDraftBlocked, create_pdf_draft
from case_kernel.isolated_document_renderer import (
    IsolatedDocumentRendererClient,
    IsolatedDocumentRendererClientSettings,
)
from case_kernel.case_agent_document_delivery import (
    AuthoritativeDocumentSource,
    DocumentSourceKind,
    DynamicDocumentTaskBinding,
    ReviewableDocumentCandidate,
    build_deterministic_payment_ledger_candidate,
    first_release_reviewable_document_templates,
    parse_reviewable_document_candidate,
    visible_document_source_labels,
)
from case_kernel.case_work_plan import (
    CaseWorkPlanItem,
    DeliveryTarget,
    ReviewGate,
    WorkPlanItemKind,
    WorkPlanReadiness,
)
from case_kernel.reviewable_draft_worker import (
    ReviewableOfficeDraft,
    create_reviewable_docx_draft,
    create_reviewable_xlsx_ledger,
)


_A4_POINTS = (595.28, 841.89)
_A4_TOLERANCE_POINTS = 3.0
_PDF_FONT_LINE = re.compile(
    r"^(?P<name>\S+)\s+.+?\s+\S+\s+"
    r"(?P<embedded>yes|no)\s+(?P<subset>yes|no)\s+"
    r"(?P<unicode>yes|no)\s+\d+\s+\d+$"
)
_DISALLOWED_FONT_FRAGMENTS = (
    "dejavu",
    "liberation",
    "carlito",
    "caladea",
)
_OFFICE_CJK_FONT_NAME = re.compile(r"noto(?:sans|serif)cjk(?:sc|jp)", re.IGNORECASE)
_DIRECT_PDF_FONT_FRAGMENTS = ("umingcn", "ukaicn", "wenquanyimicrohei")
_REVIEW_MARKERS = ("待律师终审", "非正式文书")
_PROBE_NAMESPACE = UUID("3a317485-ebf2-5e24-832d-62f991b60b52")
_MIN_LEDGER_HEADER_FONT_POINTS = 9.0
_MIN_LEDGER_BODY_FONT_POINTS = 9.0
_MAX_LEDGER_BODY_FONT_POINTS = 11.0
_MIN_SOURCE_LINE_GAP_POINTS = 8.0
_MAX_SOURCE_LINE_GAP_POINTS = 18.0
_MAX_SOURCE_LINE_X_DRIFT_POINTS = 1.0
_MIN_SOURCE_COLUMN_X_RATIO = 0.80
_MAX_SOURCE_HEADER_ANCHOR_DELTA_POINTS = 45.0
_MIN_SOURCE_LEFT_NEIGHBOR_GAP_POINTS = 18.0
_PAYMENT_LEDGER_TEMPLATE_VERSION = "1.2.4"
_PAYMENT_LEDGER_TEMPLATE_HASH = (
    "64485435e38de144189d27252f3d1459b8cc9e2bafb5976e8930a5e0571cf53a"
)
_UNBROKEN_LEDGER_CODES = (
    "精确到日",
    "付款",
    "收款",
    "款项交付",
)


@dataclass(frozen=True)
class _SpreadsheetTypography:
    title_font_points: float
    header_min_font_points: float
    body_min_font_points: float


@dataclass(frozen=True)
class _PdfTextRun:
    page_number: int
    text: str
    font_points: float
    x: float
    y: float


@dataclass(frozen=True)
class _SourceColumnLine:
    """One rendered line from the payment-ledger source column.

    LibreOffice is allowed to emit one visible source label as several PDF text
    runs (for example ``来源`` + ``02`` + ``｜已确认交易`` + ``2``).  Keep the
    original runs so the probe can both reconstruct the label and verify its
    physical placement rather than treating a PDF extractor implementation
    detail as a lost source reference.
    """

    page_number: int
    y: float
    text: str
    x: float
    font_points: float
    runs: tuple[_PdfTextRun, ...]


@dataclass(frozen=True)
class _LedgerProbe:
    pair: ReviewableOfficeDraft
    title: str
    headers: tuple[str, ...]
    body_values: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class _PdfInspection:
    pages: int
    font_names: tuple[str, ...]
    text: str
    spreadsheet_typography: _SpreadsheetTypography | None = None


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _probe_uuid(deliverable: str, label: str) -> str:
    return str(uuid5(_PROBE_NAMESPACE, f"{deliverable}:{label}"))


def _transaction_text(
    *,
    local_date: str,
    amount: str,
    direction: str,
    payer: str,
    payee: str,
    reference: str,
    nature: str,
    sequence: int,
) -> str:
    return json.dumps(
        {
            "local_date": local_date,
            "date_precision": "EXACT_DATE",
            "amount": amount,
            "currency": "CNY",
            "direction": direction,
            "payer_label": payer,
            "payee_label": payee,
            "channel": "BANK",
            "transaction_reference": reference,
            "nature": nature,
            "same_day_sequence": sequence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _binding(deliverable: str) -> DynamicDocumentTaskBinding:
    template = first_release_reviewable_document_templates().get(deliverable)
    if deliverable == "PAYMENT_LEDGER" and (
        template.template_version != _PAYMENT_LEDGER_TEMPLATE_VERSION
        or template.template_hash != _PAYMENT_LEDGER_TEMPLATE_HASH
    ):
        raise RuntimeError("payment-ledger typography template is not the frozen release")
    item = CaseWorkPlanItem(
        item_id=_probe_uuid(deliverable, "work-plan-item"),
        sequence=1,
        kind=WorkPlanItemKind.DOCUMENT_CANDIDATE,
        readiness=WorkPlanReadiness.ACTIONABLE,
        title="形成与当前已确认材料对应的律师复核候选",
        purpose="验证受管中文文书候选可以生成可编辑文件和PDF审阅稿。",
        rationale="当前代理情境、工作计划与已确认来源满足探针的受管构建边界。",
        prerequisites=(),
        trigger_refs=(),
        source_refs=(),
        risk_if_omitted="无法证明生产文书链在固定Linux镜像中保持中文排版。",
        confidence=0.99,
        review_gate=ReviewGate.LEAD_LAWYER_CONFIRMATION,
        delivery_target=DeliveryTarget.INTERNAL_WORK_PRODUCT,
        deliverable_kind=deliverable,
        required_for_delivery=True,
        is_primary_document=True,
    )
    sources: list[AuthoritativeDocumentSource] = []
    for index, source_kind in enumerate(
        sorted(template.required_source_kinds, key=lambda value: value.value),
        start=1,
    ):
        if source_kind is DocumentSourceKind.CONFIRMED_TRANSACTION:
            transactions = (
                (
                    "2026-08-15",
                    "30000.00",
                    "INCOMING",
                    "华东某设备有限公司",
                    "某某律师事务所客户资金账户",
                    "CN202608150001",
                    "DISBURSEMENT",
                    1,
                ),
                (
                    "2026-08-15",
                    "10000.00",
                    "OUTGOING",
                    "某某律师事务所客户资金账户",
                    "华东某设备有限公司",
                    "CN202608150002",
                    "REFUND",
                    2,
                ),
            )
            for transaction_index, values in enumerate(transactions, start=1):
                text = _transaction_text(
                    local_date=values[0],
                    amount=values[1],
                    direction=values[2],
                    payer=values[3],
                    payee=values[4],
                    reference=values[5],
                    nature=values[6],
                    sequence=values[7],
                )
                transaction_id = _probe_uuid(
                    deliverable, f"transaction-{transaction_index}"
                )
                sources.append(
                    AuthoritativeDocumentSource(
                        input_ref=f"transaction:{transaction_id}",
                        source_kind=source_kind,
                        source_version="v1",
                        source_hash=_digest(text),
                        label=f"已确认银行交易 {transaction_index}",
                        text=text,
                    )
                )
            continue
        text = (
            "本来源仅用于固定的本地受管渲染验收；已确认材料显示设备采购款、"
            "履行节点与证据对应关系仍须由承办律师复核。"
        )
        sources.append(
            AuthoritativeDocumentSource(
                input_ref=f"source:{index:02d}",
                source_kind=source_kind,
                source_version="v1",
                source_hash=_digest(f"{source_kind.value}:{text}"),
                label=f"已确认来源 {index}",
                text=text,
            )
        )
    binding = DynamicDocumentTaskBinding(
        firm_id=_probe_uuid(deliverable, "firm"),
        matter_id=_probe_uuid(deliverable, "matter"),
        run_id=_probe_uuid(deliverable, "run"),
        graph_id=_probe_uuid(deliverable, "graph"),
        task_id=_probe_uuid(deliverable, "task"),
        task_input_hash=_digest(f"{deliverable}:task-input"),
        case_snapshot_hash=_digest(f"{deliverable}:case-snapshot"),
        work_plan_id=_probe_uuid(deliverable, "work-plan"),
        work_plan_hash=_digest(f"{deliverable}:work-plan"),
        work_plan_status="ACTIVE",
        work_plan_item=item,
        posture_profile_id=_probe_uuid(deliverable, "posture-profile"),
        posture_profile_hash=_digest(f"{deliverable}:posture-profile"),
        template=template,
        sources=tuple(sources),
    )
    binding.validate()
    return binding


def _candidate_binding(binding: DynamicDocumentTaskBinding) -> dict[str, str]:
    return {
        "binding_hash": binding.binding_hash,
        "source_set_hash": binding.source_set_hash,
        "task_input_hash": binding.task_input_hash,
        "work_plan_item_id": binding.work_plan_item.item_id,
        "template_id": binding.template.template_id,
        "template_version": binding.template.template_version,
        "template_hash": binding.template.template_hash,
        "deliverable_kind": binding.template.deliverable_kind,
        "output_format": binding.template.output_format.value,
    }


def _case_review_candidate() -> ReviewableDocumentCandidate:
    binding = _binding("CASE_REVIEW_MEMO")
    refs = tuple(source.input_ref for source in binding.sources)
    raw = json.dumps(
        {
            "schema_version": "case-agent-reviewable-docx-candidate-v1",
            "binding": _candidate_binding(binding),
            "title": binding.template.title_label,
            "review_status": "NEEDS_LAWYER_REVIEW",
            "formal_fact": False,
            "formal_legal_conclusion": False,
            "court_ready": False,
            "sections": [
                {
                    "heading": "一、案件基本情况",
                    "paragraphs": [
                        {
                            "text": "已确认材料显示设备采购款为人民币30,000.00元，具体履行节点及证据对应关系仍须由承办律师终审确认。",
                            "source_refs": list(refs[:2]),
                        },
                        {
                            "text": "本候选意见只整理当前授权来源，不替代律师判断，也不得直接用于法院提交。",
                            "source_refs": [refs[0]],
                        },
                    ],
                },
                {
                    "heading": "（一）证据对应关系",
                    "paragraphs": [
                        {
                            "text": "现有材料之间存在需要复核的时间和金额关系，应先完成证据原件核验。",
                            "source_refs": [refs[-1]],
                        }
                    ],
                },
                {
                    "heading": "1. 待核金额与履行节点",
                    "paragraphs": [
                        {
                            "text": "付款主体、收款主体、交易参考号与合同履行节点须逐项核对。",
                            "source_refs": [refs[0]],
                        }
                    ],
                },
                {
                    "heading": "（1）律师复核事项",
                    "paragraphs": [
                        {
                            "text": "是否采取后续法律行动，应由承办律师在核验原件后决定。",
                            "source_refs": [refs[-1]],
                        }
                    ],
                },
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return parse_reviewable_document_candidate(raw, binding=binding)


def _docx_pair(
    client: IsolatedDocumentRendererClient,
    *,
    candidate: ReviewableDocumentCandidate,
) -> ReviewableOfficeDraft:
    return create_reviewable_docx_draft(candidate.to_docx_input(), converter=client)


def _xlsx_pair(client: IsolatedDocumentRendererClient) -> _LedgerProbe:
    binding = _binding("PAYMENT_LEDGER")
    candidate = build_deterministic_payment_ledger_candidate(binding)
    sheet_name, columns, rows = candidate.to_xlsx_input(
        visible_document_source_labels(binding)
    )
    pair = create_reviewable_xlsx_ledger(
        approval_hash=candidate.candidate_hash,
        sheet_name=sheet_name,
        columns=columns,
        rows=rows,
        converter=client,
    )
    body_values = tuple(
        str(value).strip()
        for row in rows
        for value in row
        if value is not None and str(value).strip()
    )
    source_column = columns.index("来源")
    source_refs = tuple(str(row[source_column]).strip() for row in rows)
    if len(source_refs) != 2:
        raise RuntimeError("payment-ledger probe did not produce two visible sources")
    if any("transaction:" in value for value in source_refs):
        raise RuntimeError("payment-ledger probe exposed an internal source identifier")
    return _LedgerProbe(
        pair=pair,
        title=sheet_name,
        headers=tuple(columns),
        body_values=body_values,
        source_refs=source_refs,
    )


def _run_checked(command: list[str], *, label: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{label} executable is unavailable") from error
    if result.returncode != 0:
        raise RuntimeError(f"{label} rejected the rendered PDF")
    return result.stdout


def _assert_a4(reader: PdfReader, *, landscape: bool) -> None:
    expected = (_A4_POINTS[1], _A4_POINTS[0]) if landscape else _A4_POINTS
    for index, page in enumerate(reader.pages, start=1):
        width = abs(float(page.mediabox.width))
        height = abs(float(page.mediabox.height))
        if (
            abs(width - expected[0]) > _A4_TOLERANCE_POINTS
            or abs(height - expected[1]) > _A4_TOLERANCE_POINTS
        ):
            raise RuntimeError(
                f"rendered page {index} is not A4 "
                f"{'landscape' if landscape else 'portrait'}: {width:.2f}x{height:.2f}pt"
            )


def _font_names(pdf_path: Path, *, font_profile: str) -> tuple[str, ...]:
    output = _run_checked(["/usr/bin/pdffonts", str(pdf_path)], label="pdffonts")
    rows: list[tuple[str, str, str, str]] = []
    for line in output.splitlines():
        match = _PDF_FONT_LINE.fullmatch(line.strip())
        if match is not None:
            rows.append(
                (
                    match.group("name"),
                    match.group("embedded"),
                    match.group("subset"),
                    match.group("unicode"),
                )
            )
    if not rows:
        raise RuntimeError("rendered PDF exposes no inspectable font records")
    if any(
        embedded != "yes" or subset != "yes" or unicode_map != "yes"
        for _, embedded, subset, unicode_map in rows
    ):
        raise RuntimeError("rendered PDF contains a non-subset, unembedded or non-Unicode font")
    names = tuple(name for name, _, _, _ in rows)
    normalized = " ".join(names).casefold()
    if any(fragment in normalized for fragment in _DISALLOWED_FONT_FRAGMENTS):
        raise RuntimeError("rendered PDF drifted to an unapproved fallback font")
    if font_profile == "office-noto-cjk":
        # LibreOffice 7.4 labels its PDF subset ``Noto*CJKjp-*-VKana`` even
        # when only the standalone SC face extracted from TTC index 2 is
        # installed.  The image build separately fails unless fontconfig maps
        # every domestic family to the Noto CJK SC PostScript face at index 2.  Treat
        # this BaseFont value as an exporter label, while still rejecting any
        # non-Noto font and any CJK region other than SC/the observed JP label.
        if any(_OFFICE_CJK_FONT_NAME.search(name) is None for name in names):
            raise RuntimeError(
                "LibreOffice PDF contains a non-approved CJK font subset: "
                + ",".join(names)
            )
    elif font_profile == "embedded-direct-pdf":
        if any(fragment not in normalized for fragment in _DIRECT_PDF_FONT_FRAGMENTS):
            raise RuntimeError(
                "direct PDF is missing an approved embedded Chinese font: "
                + ",".join(names)
            )
        if any(
            all(fragment not in name.casefold() for fragment in _DIRECT_PDF_FONT_FRAGMENTS)
            for name in names
        ):
            raise RuntimeError(
                "direct PDF contains an undeclared font resource: " + ",".join(names)
            )
    else:
        raise RuntimeError("PDF font inspection profile is invalid")
    return names


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value))


def _spreadsheet_text_runs(reader: PdfReader) -> tuple[_PdfTextRun, ...]:
    runs: list[_PdfTextRun] = []
    for page_number, page in enumerate(reader.pages, start=1):

        def visitor_text(
            text: str,
            current_transformation_matrix: list[float],
            text_matrix: list[float],
            _font_dictionary: object,
            font_size: float,
        ) -> None:
            normalized = _normalized_text(text)
            if not normalized:
                return
            cm = current_transformation_matrix
            tm = text_matrix
            vertical_x = float(tm[2]) * float(cm[0]) + float(tm[3]) * float(cm[2])
            vertical_y = float(tm[2]) * float(cm[1]) + float(tm[3]) * float(cm[3])
            effective_font_points = abs(float(font_size)) * hypot(
                vertical_x, vertical_y
            )
            x = float(tm[4]) * float(cm[0]) + float(tm[5]) * float(cm[2]) + float(cm[4])
            y = float(tm[4]) * float(cm[1]) + float(tm[5]) * float(cm[3]) + float(cm[5])
            runs.append(
                _PdfTextRun(
                    page_number=page_number,
                    text=normalized,
                    font_points=effective_font_points,
                    x=x,
                    y=y,
                )
            )

        page.extract_text(visitor_text=visitor_text)
    return tuple(runs)


def _assert_spreadsheet_typography(
    reader: PdfReader,
    *,
    title: str,
    headers: tuple[str, ...],
    body_values: tuple[str, ...],
    source_refs: tuple[str, ...],
) -> _SpreadsheetTypography:
    runs = _spreadsheet_text_runs(reader)
    normalized_title = _normalized_text(title)
    normalized_headers = tuple(_normalized_text(value) for value in headers)
    normalized_body = tuple(
        _normalized_text(value)
        for value in body_values
        if len(_normalized_text(value)) >= 2
    )
    extracted = "".join(_normalized_text(page.extract_text() or "") for page in reader.pages)

    title_sizes = tuple(
        run.font_points for run in runs if run.text == normalized_title
    )
    if not title_sizes:
        raise RuntimeError("spreadsheet PDF has no measurable managed title")

    missing_headers = tuple(
        header
        for header in normalized_headers
        if not any(run.text == header for run in runs)
    )
    if missing_headers:
        raise RuntimeError(
            "spreadsheet PDF lost measurable header cells: " + ",".join(missing_headers)
        )
    header_sizes = tuple(
        run.font_points for run in runs if run.text in normalized_headers
    )

    body_runs = tuple(
        run
        for run in runs
        if len(run.text) >= 2
        # LibreOffice appends the review state to the visible sheet title.
        # The short ledger enum “付款” occurs inside that title, so exact title
        # equality would wrongly count an 8.7pt title-decoration as a table
        # body cell.  Measure only table content, never title/footer chrome.
        and normalized_title not in run.text
        and run.text not in normalized_headers
        and not any(marker in run.text for marker in _REVIEW_MARKERS)
        and any(run.text in value or value in run.text for value in normalized_body)
    )
    if not body_runs:
        raise RuntimeError("spreadsheet PDF has no measurable ledger body text")

    missing_codes = tuple(
        value
        for value in _UNBROKEN_LEDGER_CODES
        if not any(run.text == value for run in body_runs)
    )
    if missing_codes:
        raise RuntimeError(
            "spreadsheet PDF split a managed ledger code: " + ",".join(missing_codes)
        )

    page_sizes = {
        index: (
            abs(float(page.mediabox.width)),
            abs(float(page.mediabox.height)),
        )
        for index, page in enumerate(reader.pages, start=1)
    }
    if "来源" not in normalized_headers:
        raise RuntimeError("spreadsheet source column is not declared in the header")

    # A body source value can itself start with “来源”.  The actual table header
    # is the only such run that shares a baseline with every declared header.
    # This avoids mistaking a body label for the column anchor on multi-page
    # ledgers where a header is repeated on each printed page.
    source_header_anchors: dict[int, _PdfTextRun] = {}
    for page_number in page_sizes:
        anchors = tuple(
            run
            for run in runs
            if run.page_number == page_number
            and run.text == "来源"
            and all(
                any(
                    candidate.page_number == page_number
                    and candidate.text == header
                    and abs(candidate.y - run.y) <= 0.5
                    for candidate in runs
                )
                for header in normalized_headers
            )
        )
        if not anchors:
            continue
        if len(anchors) != 1:
            raise RuntimeError(
                "spreadsheet source column has no unique managed header anchor"
            )
        source_header_anchors[page_number] = anchors[0]

    if not source_header_anchors:
        raise RuntimeError("spreadsheet source column has no managed header anchor")

    # Reconstruct labels only from the physical source column.  Do not scan
    # arbitrary short PDF runs globally: the same-day sequence value “2”, for
    # example, must never be allowed to complete 来源02｜已确认交易2.
    source_column_lines: dict[int, tuple[_SourceColumnLine, ...]] = {}
    source_column_floor: dict[int, float] = {}
    for page_number, header_anchor in source_header_anchors.items():
        page_width, _ = page_sizes[page_number]
        column_floor = max(
            page_width * _MIN_SOURCE_COLUMN_X_RATIO,
            header_anchor.x - _MAX_SOURCE_HEADER_ANCHOR_DELTA_POINTS,
        )
        source_column_floor[page_number] = column_floor
        grouped_lines: dict[float, list[_PdfTextRun]] = {}
        for run in runs:
            if run.page_number == page_number and run.x >= column_floor:
                grouped_lines.setdefault(round(run.y, 3), []).append(run)
        source_column_lines[page_number] = tuple(
            _SourceColumnLine(
                page_number=page_number,
                y=y,
                text="".join(
                    run.text for run in sorted(line_runs, key=lambda item: item.x)
                ),
                x=min(run.x for run in line_runs),
                font_points=max(run.font_points for run in line_runs),
                runs=tuple(sorted(line_runs, key=lambda item: item.x)),
            )
            for y, line_runs in sorted(grouped_lines.items(), reverse=True)
        )

    source_line_blocks: dict[int, list[tuple[str, float, float, float]]] = {}
    for source_ref in source_refs:
        normalized_source = _normalized_text(source_ref)
        if normalized_source not in extracted:
            raise RuntimeError(
                "spreadsheet PDF did not preserve a complete source reference"
            )

        matching_blocks: list[tuple[_SourceColumnLine, ...]] = []
        for lines in source_column_lines.values():
            for start in range(len(lines)):
                reconstructed_source = ""
                for end in range(start, min(start + 4, len(lines))):
                    reconstructed_source += lines[end].text
                    if reconstructed_source == normalized_source:
                        matching_blocks.append(tuple(lines[start : end + 1]))
                        break
                    if not normalized_source.startswith(reconstructed_source):
                        break
        if len(matching_blocks) != 1:
            raise RuntimeError(
                "spreadsheet source reference did not preserve complete managed lines"
            )
        ordered_lines = matching_blocks[0]
        page_number = ordered_lines[0].page_number
        if (
            len(ordered_lines) > 4
            or any(line.page_number != page_number for line in ordered_lines)
        ):
            raise RuntimeError(
                "spreadsheet source reference did not preserve complete managed lines"
            )
        source_runs = tuple(
            run for line in ordered_lines for run in line.runs
        )
        for run in source_runs:
            page_width, page_height = page_sizes[run.page_number]
            if not (0 < run.x < page_width and 0 < run.y < page_height):
                raise RuntimeError("spreadsheet source reference left the page bounds")
        ordered_y = tuple(line.y for line in ordered_lines)
        line_gaps = tuple(
            (
                abs(right.y - left.y),
                max(
                    _MIN_SOURCE_LINE_GAP_POINTS,
                    left.font_points,
                    right.font_points,
                ),
            )
            for left, right in zip(ordered_lines, ordered_lines[1:])
        )
        if any(
            gap < required_gap
            or gap > _MAX_SOURCE_LINE_GAP_POINTS
            for gap, required_gap in line_gaps
        ):
            raise RuntimeError(
                "spreadsheet source lines overlap or are vertically clipped"
            )
        line_x = tuple(line.x for line in ordered_lines)
        if max(line_x) - min(line_x) > _MAX_SOURCE_LINE_X_DRIFT_POINTS:
            raise RuntimeError(
                "spreadsheet source reference escaped its managed column"
            )
        page_width, _ = page_sizes[page_number]
        if page_number not in source_header_anchors:
            raise RuntimeError(
                "spreadsheet source column has no unique managed header anchor"
            )
        if any(
            x < source_column_floor[page_number] or x >= page_width
            for x in line_x
        ):
            raise RuntimeError(
                "spreadsheet source reference escaped its managed column"
            )
        source_run_ids = {id(run) for run in source_runs}
        for line in ordered_lines:
            left_neighbors = tuple(
                run.x
                for run in runs
                if id(run) not in source_run_ids
                and run.page_number == page_number
                and abs(run.y - line.y) <= 0.5
                and run.x < line.x
            )
            if (
                left_neighbors
                and line.x - max(left_neighbors)
                < _MIN_SOURCE_LEFT_NEIGHBOR_GAP_POINTS
            ):
                raise RuntimeError(
                    "spreadsheet source reference collided with its left field"
                )
        source_line_blocks.setdefault(page_number, []).append(
            (
                normalized_source,
                min(ordered_y),
                max(ordered_y),
                max(line.font_points for line in ordered_lines),
            )
        )

    for positioned_blocks in source_line_blocks.values():
        for index, (_, left_min_y, left_max_y, left_font_points) in enumerate(
            positioned_blocks
        ):
            for _, right_min_y, right_max_y, right_font_points in positioned_blocks[
                index + 1 :
            ]:
                if left_max_y < right_min_y:
                    gap = right_min_y - left_max_y
                elif right_max_y < left_min_y:
                    gap = left_min_y - right_max_y
                else:
                    gap = 0.0
                if gap < max(
                    _MIN_SOURCE_LINE_GAP_POINTS,
                    left_font_points,
                    right_font_points,
                ):
                    raise RuntimeError(
                        "spreadsheet source rows overlap or are vertically clipped"
                    )

    title_font_points = min(title_sizes)
    header_min_font_points = min(header_sizes)
    body_min_font_points = min(run.font_points for run in body_runs)
    if header_min_font_points < _MIN_LEDGER_HEADER_FONT_POINTS:
        raise RuntimeError(
            "spreadsheet PDF header text is below 9pt: "
            f"{header_min_font_points:.3f}pt"
        )
    if body_min_font_points < _MIN_LEDGER_BODY_FONT_POINTS:
        raise RuntimeError(
            "spreadsheet PDF body text is below 9pt: "
            f"{body_min_font_points:.3f}pt"
        )
    body_max_font_points = max(run.font_points for run in body_runs)
    if body_max_font_points > _MAX_LEDGER_BODY_FONT_POINTS:
        raise RuntimeError(
            "spreadsheet PDF body text exceeds the managed print size: "
            f"{body_max_font_points:.3f}pt"
        )
    return _SpreadsheetTypography(
        title_font_points=title_font_points,
        header_min_font_points=header_min_font_points,
        body_min_font_points=body_min_font_points,
    )


def _assert_raster(
    pdf_path: Path, *, expected_pages: int, landscape: bool, output_name: str
) -> None:
    with TemporaryDirectory(prefix="document-render-probe-raster-") as temporary:
        prefix = Path(temporary) / "page"
        _run_checked(
            [
                "/usr/bin/pdftoppm",
                "-r",
                "120",
                "-png",
                str(pdf_path),
                str(prefix),
            ],
            label="pdftoppm",
        )
        images = sorted(Path(temporary).glob("page-*.png"))
        if len(images) != expected_pages:
            raise RuntimeError("raster page count differs from the parsed PDF")
        for index, image_path in enumerate(images, start=1):
            with Image.open(image_path) as image:
                image.load()
                width, height = image.size
                if (width > height) is not landscape:
                    raise RuntimeError(f"raster page {index} orientation is wrong")
                ratio = max(width, height) / min(width, height)
                if not 1.39 <= ratio <= 1.43:
                    raise RuntimeError(f"raster page {index} does not have an A4 aspect ratio")
                gray = image.convert("L")
                white = Image.new("L", gray.size, 255)
                difference = ImageChops.difference(gray, white)
                bounds = difference.point(lambda value: 255 if value > 18 else 0).getbbox()
                if bounds is None:
                    raise RuntimeError(f"raster page {index} is blank")
                ink = sum(1 for value in gray.get_flattened_data() if value < 235)
                ink_ratio = ink / (width * height)
                if ink_ratio < 0.001:
                    raise RuntimeError(f"raster page {index} contains implausibly little visible content")
                if bounds[0] <= 1 or bounds[1] <= 1 or bounds[2] >= width - 1 or bounds[3] >= height - 1:
                    raise RuntimeError(f"raster page {index} has clipped edge content")
            _retain_probe_file(
                image_path,
                output_name=f"{Path(output_name).stem}-page-{index:03d}.png",
            )


def _inspect_pdf(
    pdf_content: bytes,
    *,
    kind: str,
    landscape: bool,
    required_text: tuple[str, ...],
    output_name: str,
    font_profile: str,
    spreadsheet: _LedgerProbe | None = None,
) -> _PdfInspection:
    try:
        reader = PdfReader(BytesIO(pdf_content), strict=True)
    except Exception as error:
        raise RuntimeError(f"{kind} PDF cannot be parsed") from error
    if reader.is_encrypted or not reader.pages:
        raise RuntimeError(f"{kind} PDF is encrypted or empty")
    _assert_a4(reader, landscape=landscape)
    page_text = tuple(
        re.sub(r"\s+", "", page.extract_text() or "") for page in reader.pages
    )
    text = "\n".join(page_text)
    for value in required_text:
        if re.sub(r"\s+", "", value) not in text:
            raise RuntimeError(f"{kind} PDF lost required Chinese text: {value}")
    for index, value in enumerate(page_text, start=1):
        if not value.strip():
            raise RuntimeError(f"{kind} PDF page {index} has no extractable text")
        if any(marker not in value for marker in _REVIEW_MARKERS):
            raise RuntimeError(f"{kind} PDF page {index} lost its lawyer-review footer")

    spreadsheet_typography = None
    if spreadsheet is not None:
        spreadsheet_typography = _assert_spreadsheet_typography(
            reader,
            title=spreadsheet.title,
            headers=spreadsheet.headers,
            body_values=spreadsheet.body_values,
            source_refs=spreadsheet.source_refs,
        )

    with TemporaryDirectory(prefix="document-render-probe-pdf-") as temporary:
        pdf_path = Path(temporary) / output_name
        pdf_path.write_bytes(pdf_content)
        info = _run_checked(["/usr/bin/pdfinfo", str(pdf_path)], label="pdfinfo")
        page_line = re.search(r"^Pages:\s+(\d+)\s*$", info, flags=re.MULTILINE)
        if page_line is None or int(page_line.group(1)) != len(reader.pages) or "(A4)" not in info:
            raise RuntimeError(f"{kind} PDF metadata does not confirm its A4 page count")
        fonts = _font_names(pdf_path, font_profile=font_profile)
        _assert_raster(
            pdf_path,
            expected_pages=len(reader.pages),
            landscape=landscape,
            output_name=output_name,
        )
        _retain_probe_file(pdf_path, output_name=output_name)
    return _PdfInspection(
        pages=len(reader.pages),
        font_names=fonts,
        text=text,
        spreadsheet_typography=spreadsheet_typography,
    )


def _retain_probe_file(source: Path, *, output_name: str) -> None:
    _retain_probe_bytes(source.read_bytes(), output_name=output_name)


def _retain_probe_bytes(content: bytes, *, output_name: str) -> None:
    raw = os.environ.get("LAWCASE_DOCUMENT_RENDERER_PROBE_OUTPUT_ROOT", "").strip()
    if not raw:
        return
    if not content or Path(output_name).name != output_name:
        raise RuntimeError("probe output is invalid")
    root = Path(raw)
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError("probe output root must be an absolute non-symbolic path")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / output_name
    destination.write_bytes(content)
    destination.chmod(0o600)


def _validate_pair(
    pair: ReviewableOfficeDraft,
    *,
    kind: str,
    landscape: bool,
    required_text: tuple[str, ...],
    output_name: str,
    elapsed: float,
    spreadsheet: _LedgerProbe | None = None,
) -> None:
    editable = pair.editable_artifact
    converted = pair.review_pdf
    if (
        converted.converter_id != "libreoffice-web-worker"
        or converted.detected_kind != kind
        or converted.source_sha256 != editable.content_sha256
        or converted.pdf_bytes != len(converted.pdf_content)
        or not converted.pdf_content.startswith(b"%PDF-")
        or len(pair.review_input_hash) != 64
    ):
        raise RuntimeError(f"{kind} renderer result failed its hash-bound production contract")
    editable_name = (
        "domestic-case-review-memo.docx"
        if kind == "WORD_DOCUMENT"
        else "domestic-payment-ledger.xlsx"
    )
    _retain_probe_bytes(editable.content, output_name=editable_name)
    inspected = _inspect_pdf(
        converted.pdf_content,
        kind=kind,
        landscape=landscape,
        required_text=required_text,
        output_name=output_name,
        font_profile="office-noto-cjk",
        spreadsheet=spreadsheet,
    )
    typography = inspected.spreadsheet_typography
    typography_summary = (
        ""
        if typography is None
        else (
            f" title_font_points={typography.title_font_points:.3f}"
            f" header_min_font_points={typography.header_min_font_points:.3f}"
            f" body_min_font_points={typography.body_min_font_points:.3f}"
        )
    )
    print(
        "document-renderer domestic-format PASS "
        f"kind={kind} source_bytes={len(editable.content)} pdf_bytes={converted.pdf_bytes} "
        f"pages={inspected.pages} fonts={','.join(inspected.font_names)} "
        f"elapsed_seconds={elapsed:.3f}{typography_summary}"
    )


def _validate_direct_pdf(candidate: ReviewableDocumentCandidate) -> None:
    started = monotonic()
    draft = candidate.to_docx_input()
    direct_sections = tuple(
        replace(
            section,
            heading=(
                f"{section.heading}｜异体字㖞核对"
                if section.heading.startswith("（一）")
                else section.heading
            ),
            source_refs=(
                section.source_refs + ("字体来源㖞",)
                if section.heading.startswith("（一）")
                else section.source_refs
            ),
        )
        for section in draft.sections
    )
    direct_draft = replace(
        draft,
        title=f"{draft.title}｜温度20℃与姓名ǎ",
        sections=direct_sections,
    )
    artifact = create_pdf_draft(direct_draft)
    if (
        artifact.media_type != "application/pdf"
        or artifact.content_sha256 != sha256(artifact.content).hexdigest()
        or not artifact.content.startswith(b"%PDF-")
    ):
        raise RuntimeError("direct PDF failed its production artifact contract")
    inspected = _inspect_pdf(
        artifact.content,
        kind="DIRECT_PDF",
        landscape=False,
        required_text=(
            "案件审阅意见候选",
            "温度20℃与姓名ǎ",
            "一、案件基本情况",
            "异体字㖞核对",
            "字体来源㖞",
            "人民币30,000.00元",
        ),
        output_name="domestic-case-review-memo-direct.pdf",
        font_profile="embedded-direct-pdf",
    )
    print(
        "document-renderer domestic-format PASS "
        f"kind=DIRECT_PDF pdf_bytes={len(artifact.content)} pages={inspected.pages} "
        f"fonts={','.join(inspected.font_names)} "
        f"elapsed_seconds={monotonic() - started:.3f}"
    )


def _assert_unsafe_direct_pdf_unicode_is_blocked(
    candidate: ReviewableDocumentCandidate,
) -> None:
    draft = candidate.to_docx_input()
    unsafe_samples = (
        ("\U00020021", "U+20021"),
        ("e\u0301", "U+0301"),
        ("\u200d", "U+200D"),
        ("\u00ad", "U+00AD"),
        ("\ufffc", "U+FFFC"),
        ("\ufffd", "U+FFFD"),
    )
    for unsafe_text, expected_codepoint in unsafe_samples:
        unsupported = replace(draft, title=f"{draft.title}{unsafe_text}")
        try:
            create_pdf_draft(unsupported)
        except ApprovedDraftBlocked as error:
            if expected_codepoint not in str(error):
                raise RuntimeError(
                    "direct PDF blocked unsafe Unicode without its codepoint"
                ) from error
        else:
            raise RuntimeError(
                f"direct PDF silently accepted unsafe {expected_codepoint}"
            )
    print("document-renderer direct-PDF unsafe-Unicode fail-closed: PASS")


def main() -> None:
    settings = IsolatedDocumentRendererClientSettings.from_worker_environment(os.environ)
    client = IsolatedDocumentRendererClient(settings=settings)
    client.preflight()

    memo_candidate = _case_review_candidate()
    started = monotonic()
    docx = _docx_pair(client, candidate=memo_candidate)
    _validate_pair(
        docx,
        kind="WORD_DOCUMENT",
        landscape=False,
        required_text=("案件审阅意见候选", "一、案件基本情况", "人民币30,000.00元"),
        output_name="domestic-case-review-memo.pdf",
        elapsed=monotonic() - started,
    )

    started = monotonic()
    xlsx = _xlsx_pair(client)
    _validate_pair(
        xlsx.pair,
        kind="SPREADSHEET",
        landscape=True,
        required_text=(
            xlsx.title,
            "精确到日",
            "30,000.00",
            "收款",
            *xlsx.source_refs,
        ),
        output_name="domestic-payment-ledger.pdf",
        elapsed=monotonic() - started,
        spreadsheet=xlsx,
    )

    _validate_direct_pdf(memo_candidate)
    _assert_unsafe_direct_pdf_unicode_is_blocked(memo_candidate)
    print("document-renderer production DOCX/XLSX/direct-PDF domestic-format conversion: PASS")


if __name__ == "__main__":
    main()
