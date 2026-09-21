"""案件分析服务层：把实用模式封装成 Web 与 CLI 共用的可复用服务。

职责边界（与产品红线一致）：
- Agent 只提议：读材料、提事实、找争点、给方向；
- 代码算钱：正式数字一律由确定性引擎按**律师确认的参数**计算，模型输出中的
  数字在门禁阶段已被剔除；
- 律师决定：付款性质、诉讼立场、最终采用，全部进入"待律师确认清单"。

正式数字的口径声明：本服务只计算「以已确认债务参数、按司法保护上限计息至
截止日」的毛额，**不包含尚未经律师确认的付款冲抵**。付款冲抵后的净额需在律师
确认付款性质后另行计算——避免把模型提议洗成正式金额。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from case_kernel.lawyer_practical_mode import (
    GateDecision,
    PracticalResult,
    build_practical_prompt,
    extract_source_amounts,
    normalize_and_gate,
    render_practical_report,
)
from case_kernel.pdf_render import (
    MAX_RENDERED_PAGES_DEFAULT,
    render_pdf_pages,
    renderer_unavailable_reason,
)
from case_kernel.shadow_engine import (
    ShadowDebt,
    ShadowEngineBlocked,
    ShadowEngineConfig,
    ShadowRow,
    canonical_amount,
    run_engine,
)
from case_kernel.case_sales_claim import (
    SalesClaimError,
    compute_sales_numbers,
    load_sales_claim,
)
from case_kernel.case_payments import (
    PaymentError,
    load_payments,
    payment_rows,
    payment_summary,
)
from case_kernel.shadow_mode import (
    RequestLedger,
    ShadowBlocked,
    build_import_manifest,
    load_case_config,
    validate_preflight,
)

ProgressCallback = Callable[[str, int], None]

STATUS_COMPLETED = "COMPLETED"
STATUS_MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
STATUS_BLOCKED = "BLOCKED"
STATUS_FAILED = "FAILED"


@dataclass
class AnalysisRequest:
    case_id: str
    materials_dir: Path
    output_root: Path
    case_number: str = "（未填写案号）"
    role: str = "被告"
    stage: str = "一审应诉"
    case_config_path: Path | None = None
    preflight_path: Path | None = None
    env_file: Path | None = None
    budget_cny: Decimal = Decimal("2")
    progress: ProgressCallback | None = None
    transport: object | None = None  # 可注入（测试）；缺省构造真实传输层
    # 扫描件以图像发送，图像内的身份证号/银行卡号无法在本机自动脱敏。
    # 默认 fail closed；律师在界面明确授权后才记为待核并继续。
    allow_image_identifiers: bool = False


@dataclass
class AnalysisResult:
    status: str
    gate_level: str = ""
    report_md: str = ""
    analysis: dict = field(default_factory=dict)
    engine_numbers: dict = field(default_factory=dict)
    engine_note: str = ""
    review_items: list[str] = field(default_factory=list)
    cost_cny: str = "0.000000"
    calls: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "gate_level": self.gate_level,
            "report_md": self.report_md,
            "analysis": self.analysis,
            "engine_numbers": self.engine_numbers,
            "engine_note": self.engine_note,
            "review_items": self.review_items,
            "cost_cny": self.cost_cny,
            "calls": self.calls,
            "error": self.error,
        }


def _emit(request: AnalysisRequest, stage: str, percent: int) -> None:
    if request.progress is not None:
        try:
            request.progress(stage, percent)
        except Exception:  # noqa: BLE001 - 进度回调不得影响分析主流程
            pass


def compute_engine_numbers(
    case_config_path: Path | None,
) -> tuple[dict[str, str], str]:
    """按已确认参数计算正式数字。

    - 无付款记录时给出**毛额**（不含冲抵）；
    - 律师确认了付款性质时，追加**冲抵后净额**（同一引擎、同一冻结规则）；
    - 争议/排除的付款永不进入计算，只在说明里计数。

    返回 (数字字典, 口径说明)。缺参数时返回空字典与原因说明。
    """
    if case_config_path is None or not Path(case_config_path).is_file():
        return {}, "未提供案件计算参数（case_config），正式数字待补充参数后计算。"
    try:
        raw_config = json.loads(Path(case_config_path).read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001 - 参数问题不阻断分析
        return {}, f"案件计算参数无法解析：{type(error).__name__}；正式数字待修正参数后计算。"

    # 买卖合同（货款）口径：与民间借贷是两套规则，按律师配置的口径分流，
    # 绝不把货款硬套进借贷的 LPR×4 与先息后本模型。货款案由不需要借贷的 debts，
    # 因此必须先判断口径，再决定是否要求借贷参数。
    try:
        sales_claim = load_sales_claim(raw_config if isinstance(raw_config, dict) else None)
    except SalesClaimError as error:
        return {}, f"货款口径参数不合法（{error}）；正式数字待修正参数后计算。"
    if sales_claim is not None:
        return compute_sales_numbers(sales_claim)

    try:
        cfg = load_case_config(case_config_path)
    except Exception as error:  # noqa: BLE001 - 参数问题不阻断分析
        return {}, f"案件计算参数无法解析：{type(error).__name__}；正式数字待修正参数后计算。"

    payment_problem = ""
    try:
        payments = load_payments(raw_config if isinstance(raw_config, dict) else None)
    except PaymentError as error:
        # 付款记录坏了不影响毛额（毛额只由债务参数决定），但绝不静默忽略：
        # 报告里明确写出问题，并且不给任何冲抵后净额。
        payments = []
        payment_problem = f"付款记录不合法（{error}），本次未计算冲抵后净额。"

    debts: dict[str, ShadowDebt] = cfg["debts"]
    active = {k: v for k, v in debts.items() if not v.evidence_pending}
    pending = [k for k in debts.keys() if k not in active]
    if not active:
        return {}, "全部债务均缺少出借凭证（evidence_pending），正式数字暂不可计算。"

    borrow_rows = [
        ShadowRow(
            row_id=f"DISBURSE-{debt_id}",
            occurred_on=debt.disbursed_on,
            channel="参数确认",
            amount=debt.principal,
            currency="CNY",
            direction="出借",
            classification="本金出借",
            debt_id=debt_id,
            memo=f"{debt_id} 出借本金（律师确认参数）",
        )
        for debt_id, debt in active.items()
    ]
    config = ShadowEngineConfig(final_date=cfg["final_date"], new_cap=cfg["new_cap"])
    try:
        gross = run_engine(borrow_rows, active, config)
    except ShadowEngineBlocked as error:
        return {}, f"参数不完整，正式数字待补充：{error}"

    numbers = _loan_numbers(gross, prefix="")
    numbers["利息暂计截止日"] = str(cfg["final_date"])

    summary = payment_summary(payments)
    entered = [item for item in payments if item.enters_calculation]
    note = (
        "以上数字由确定性引擎按律师确认的债务参数与司法保护上限计算至截止日，"
        "毛额部分不含付款冲抵；模型未参与任何计算。"
    )
    if payment_problem:
        note += f" {payment_problem}"
    elif not entered:
        note += "付款冲抵后的净额须待律师确认各笔付款性质后另行计算。"
    else:
        try:
            net = run_engine(borrow_rows + payment_rows(payments), active, config)
        except ShadowEngineBlocked as error:
            note += f" 付款冲抵无法计算（{error}），净额待修正付款参数后计算。"
        else:
            numbers.update(_loan_numbers(net, prefix="冲抵后"))
            numbers["已确认付款合计"] = summary["confirmed_total"]
            numbers["已确认付款笔数"] = str(summary["confirmed_count"])
            note += (
                f" 已计入律师确认的 {summary['confirmed_count']} 笔付款（合计 "
                f"{summary['confirmed_total']} 元），按法定顺序先冲利息、后冲本金，"
                "冲抵后净额见「冲抵后」各项；毛额各项同时保留以便逐项核对。"
            )
    if summary["pending_count"]:
        note += (f" 另有 {summary['pending_count']} 笔付款标记为争议/排除，未进入计算。")
    if pending:
        note += f" 债务 {', '.join(sorted(pending))} 因缺少出借凭证已挂起，未计入合计。"
    return numbers, note


def _loan_numbers(result, *, prefix: str) -> dict[str, str]:
    """把引擎结果整理成报告用数字；``prefix`` 非空时给出冲抵后口径。"""
    numbers: dict[str, str] = {}
    total_principal = Decimal("0")
    total_interest = Decimal("0")
    for debt_id in sorted(result.loans):
        loan = result.loan(debt_id)
        numbers[f"{debt_id} {prefix}未偿本金".replace("  ", " ")] = canonical_amount(loan.principal)
        numbers[f"{debt_id} {prefix}未付利息挂账".replace("  ", " ")] = canonical_amount(
            loan.interest_arrears)
        total_principal += loan.principal
        total_interest += loan.interest_arrears
    numbers[f"{prefix}合计本金".replace("  ", " ")] = canonical_amount(total_principal)
    numbers[f"{prefix}合计未付利息挂账".replace("  ", " ")] = canonical_amount(total_interest)
    return numbers


def run_analysis(request: AnalysisRequest) -> AnalysisResult:
    """完整分析流水线：材料导入 → OCR → Agent 分析 → 三档门禁 → 数字注入 → 报告。"""
    out_root = Path(request.output_root)
    out_root.mkdir(parents=True, exist_ok=True)
    ledger = RequestLedger(out_root / "request_ledger.json")

    # 1) 正式数字（引擎，先算，供提示词与报告使用）
    _emit(request, "计算正式数字", 5)
    engine_numbers, engine_note = compute_engine_numbers(request.case_config_path)

    # 2) 材料导入（实用模式：不阻断，发送前自动掩码）
    _emit(request, "导入材料", 10)
    try:
        entries, pages, masked = build_import_manifest(
            request.materials_dir, mask_identifiers=True
        )
    except ShadowBlocked as error:
        return AnalysisResult(status=STATUS_BLOCKED, error=str(error),
                              engine_numbers=engine_numbers, engine_note=engine_note)
    out_root.joinpath("import_manifest.json").write_text(
        json.dumps({"files": [e.to_dict() for e in entries], "masked_identifiers": len(masked)},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 3) 模型未配置 → 降级：确定性结果与正式数字仍可用
    if request.preflight_path is None or not Path(request.preflight_path).is_file():
        return _degraded(request, engine_numbers, engine_note,
                         reason="未配置数据路径确认文件（preflight），本次仅产出确定性结果与正式数字。")

    try:
        preflight = validate_preflight(json.loads(Path(request.preflight_path).read_text(encoding="utf-8")))
    except ShadowBlocked as error:
        return _degraded(request, engine_numbers, engine_note, reason=str(error))

    missing_env = request.env_file is None or not Path(request.env_file).is_file()
    if missing_env:
        return _degraded(request, engine_numbers, engine_note,
                         reason="未找到模型环境配置文件，Agent 分析未运行；正式数字与材料导入已完成。")

    # 4) OCR（仅授权图片）+ 分析调用
    image_findings: list[dict] = []
    try:
        from case_kernel.shadow_live_transport import QwenShadowTransport
        transport = request.transport or QwenShadowTransport(
            materials_root=request.materials_dir,
            env_file=request.env_file,
            budget_cny=request.budget_cny,
            run_root=out_root,
            allow_image_identifiers=request.allow_image_identifiers,
        )
        _emit(request, "识别扫描件（OCR）", 20)
        # 扫描版 PDF：先把页面渲染成图片，才能进入视觉 OCR。
        rendered_pages: list = []
        path_overrides: dict = {}
        render_note = ""
        scanned_pdf_pages = [
            page for page in pages
            if page.file_name.lower().endswith(".pdf") and not page.text.strip()
        ]
        if scanned_pdf_pages:
            rendered_pages, render_note, path_overrides = _render_scanned_pages(
                request, pages, out_root
            )

        authorized = set(preflight.get("sent_fields", {}).get("page_files", []))
        for page in rendered_pages:
            authorized.add((page.file_name, page.page_number))
        ocr_targets = [
            page for page in pages if not page.file_name.lower().endswith(".pdf")
        ] + rendered_pages
        ocr_pages = (
            transport.ocr_pages(ocr_targets, authorized, ledger, path_overrides)
            if ocr_targets else []
        )
        ocr_by_key = {(p.file_name, p.page_number): p.text for p in ocr_pages}
        image_findings = list(getattr(transport, "image_identifier_findings", []) or [])

        def surface_for(page) -> str:
            text = ocr_by_key.get((page.file_name, page.page_number)) or page.text
            return f"### FILE={page.file_name} PAGE={page.page_number}\n{text}"

        surface = "\n".join(surface_for(page) for page in pages)
        if len(surface) > 30_000:
            surface = surface[:30_000] + "\n[截断：材料过长，仅分析前部内容]"
        instruction = build_practical_prompt(
            case_number=request.case_number, role=request.role, stage=request.stage,
            surface=surface, engine_amounts=engine_numbers,
            trusted_authorities=preflight.get("trusted_authorities", []),
        )
        _emit(request, "Agent 分析中", 60)
        raw = transport.call_analysis(instruction=instruction, ledger=ledger)
        _emit(request, "门禁校验", 85)
        # 材料原文数字白名单：事实引用保留，模型自算数字剔除。
        source_amounts = extract_source_amounts(
            [page.text for page in pages] + [page.text for page in ocr_pages]
        )
        analysis, gate = normalize_and_gate(
            raw, engine_amounts=engine_numbers, source_amounts=source_amounts
        )
        out_root.joinpath("agent_raw_output.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except ShadowBlocked as error:
        spent = _spent(ledger)
        return AnalysisResult(status=STATUS_BLOCKED, error=str(error),
                              engine_numbers=engine_numbers, engine_note=engine_note,
                              cost_cny=spent, calls=_ok_calls(ledger))
    except Exception as error:  # noqa: BLE001 - 服务边界
        spent = _spent(ledger)
        return AnalysisResult(status=STATUS_FAILED,
                              error=f"{type(error).__name__}: {error}",
                              engine_numbers=engine_numbers, engine_note=engine_note,
                              cost_cny=spent, calls=_ok_calls(ledger))

    # 5) 硬红线命中：拒绝整份分析，但保留引擎数字
    if gate.level == "HARD_BLOCKED":
        return AnalysisResult(
            status=STATUS_BLOCKED,
            gate_level=gate.level,
            error="模型输出命中安全红线（声称已批准/已提交等），整份分析已拒绝。",
            engine_numbers=engine_numbers,
            engine_note=engine_note,
            review_items=list(gate.review_items),
            cost_cny=_spent(ledger),
            calls=_ok_calls(ledger),
        )

    # 5b) 扫描件图像内的完整标识符：本机无法脱敏，按律师授权记为待核
    if image_findings:
        gate.review_items.extend(_image_identifier_review_items(image_findings))
        if gate.level == "PASS":
            gate.level = "MARK_FOR_REVIEW"

    # 6) 报告（数字由引擎段注入）
    result = PracticalResult(gate=gate, analysis=analysis,
                             engine_amounts=engine_numbers, review_queue=[])
    report = render_practical_report(
        case_number=request.case_number, role=request.role, result=result,
        proposal_source=f"{preflight.get('model', 'qwen')}（真实调用）",
    )
    if render_note or scanned_pdf_pages:
        note_parts = [f"扫描版 PDF 处理：{render_note or renderer_unavailable_reason()}"]
        if scanned_pdf_pages:
            note_parts.append(
                f"本次识别到 {len(scanned_pdf_pages)} 个无文本层页面"
                + (f"，已渲染 {len(rendered_pages)} 页进入 OCR。" if rendered_pages
                   else "，未能进入 OCR（见上）。")
            )
        report = _inject_render_note(report, " ".join(note_parts))
    if image_findings:
        report = _inject_render_note(
            report,
            "扫描件图像原样发送（律师已在决策包页面授权）：图像内的完整标识符无法在本机自动脱敏，"
            f"本次检出 {len(image_findings)} 处，已在下文待核清单逐项列出；下游模型文本已按标识符掩码处理。",
        )
    report = _inject_engine_note(report, engine_note)
    out_root.joinpath("决策包.md").write_text(report, encoding="utf-8")
    _emit(request, "完成", 100)

    return AnalysisResult(
        status=STATUS_COMPLETED,
        gate_level=gate.level,
        report_md=report,
        analysis=analysis,
        engine_numbers=engine_numbers,
        engine_note=engine_note,
        review_items=list(gate.review_items),
        cost_cny=_spent(ledger),
        calls=_ok_calls(ledger),
    )


def _max_rendered_pages() -> int:
    """扫描页渲染上限：大案卷（几十页微信/流水证据）可用环境变量放宽。

    默认 24 页以控制单次成本；``CASE_WORKBENCH_MAX_RENDERED_PAGES`` 可在需要
    完整读取证据时提高上限（例如 60）。非法值回退默认，不静默放大成本。
    """
    raw = os.environ.get("CASE_WORKBENCH_MAX_RENDERED_PAGES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if 1 <= value <= 500:
            return value
    return MAX_RENDERED_PAGES_DEFAULT


def _render_scanned_pages(request: AnalysisRequest, pages, out_root: Path):
    """把无文本层的 PDF 页渲染为图片；返回 (PageText 列表, 说明, 路径覆盖表)。

    来源仍指向原 PDF 的页号与文件名，保证引用可通过导入清单解析。
    """
    from case_kernel.shadow_mode import PageText

    rendered: list = []
    overrides: dict = {}
    notes: list[str] = []
    by_file: dict[str, list[int]] = {}
    for page in pages:
        if page.file_name.lower().endswith(".pdf") and not page.text.strip():
            by_file.setdefault(page.file_name, []).append(page.page_number)
    if not by_file:
        return [], "", {}

    render_dir = Path(out_root) / "rendered"
    for file_name, page_numbers in sorted(by_file.items()):
        source = Path(request.materials_dir) / file_name
        rendered_file_pages, note = render_pdf_pages(
            source, render_dir,
            pages=sorted(page_numbers)[:_max_rendered_pages()],
        )
        notes.append(f"{Path(file_name).name}: {note}")
        for item in rendered_file_pages:
            key = (file_name, item.page_number)
            overrides[key] = item.path
            rendered.append(PageText(file_name, "", item.page_number, ""))
    return rendered, "；".join(notes), overrides


def _image_identifier_review_items(findings: list[dict]) -> list[str]:
    """把扫描件图像内检出的标识符转成待核条目（律师逐项确认）。"""
    items: list[str] = []
    seen: set[tuple] = set()
    for finding in findings:
        key = (finding.get("pattern"), finding.get("value"),
               finding.get("file_name"), finding.get("page_number"))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            f"扫描件图像内含 {finding.get('pattern')}：{finding.get('value')}"
            f"（{finding.get('file_name')} p{finding.get('page_number')}）"
            "，图像未脱敏即已发送；请核对是否为真实证件/账号，必要时人工脱敏后重跑。"
        )
    return items


def _inject_render_note(report: str, note: str) -> str:
    """把扫描件处理说明写入报告（不可静默假装已读取）。"""
    if not note:
        return report
    marker = "## 一、案情与立场"
    block = f"> 材料读取说明：{note}\n\n"
    if marker in report:
        return report.replace(marker, block + marker, 1)
    return report + f"\n\n{block}"


def _inject_engine_note(report: str, note: str) -> str:
    if not note:
        return report
    marker = "## 七、正式数字（引擎输出，模型未参与计算）"
    if marker in report:
        return report.replace(marker, marker + "\n\n> " + note + "\n", 1)
    return report + f"\n\n> {note}\n"


def _degraded(request: AnalysisRequest, engine_numbers: dict, engine_note: str,
              *, reason: str) -> AnalysisResult:
    lines = [
        f"# 案件分析（降级模式）— {request.case_number}",
        "",
        "> 本文件为 **律师复核候选**，不是正式法律意见，不可直接提交法院。",
        f"- 状态：未运行 Agent 分析。原因：{reason}",
        "- 已完成：材料导入与掩码、按已确认参数的正式数字计算。",
        "",
        "## 正式数字（引擎输出，模型未参与计算）",
    ]
    for key, value in engine_numbers.items():
        lines.append(f"- {key}：{value}")
    if engine_note:
        lines += ["", f"> {engine_note}"]
    lines += ["", "## 下一步", "- 配置模型后重新分析，即可获得争点矩阵、对抗分析与决策清单。"]
    report = "\n".join(lines) + "\n"
    Path(request.output_root).joinpath("决策包.md").write_text(report, encoding="utf-8")
    return AnalysisResult(
        status=STATUS_MODEL_NOT_CONFIGURED,
        report_md=report,
        engine_numbers=engine_numbers,
        engine_note=engine_note,
        error=reason,
    )


def _spent(ledger: RequestLedger) -> str:
    total = Decimal("0")
    for row in ledger.rows:
        for key in ("cost_cny", "cost_reserved_cny"):
            value = row.get(key)
            if value:
                try:
                    total += Decimal(str(value))
                except Exception:  # noqa: BLE001
                    pass
    return format(total.quantize(Decimal("0.000001")), "f")


def _ok_calls(ledger: RequestLedger) -> int:
    return len([row for row in ledger.rows if row.get("status") == "ok"])
