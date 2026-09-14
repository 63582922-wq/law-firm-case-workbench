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
    """按已确认参数计算毛额（不含未确认的付款冲抵）。

    返回 (数字字典, 口径说明)。缺参数时返回空字典与原因说明。
    """
    if case_config_path is None or not Path(case_config_path).is_file():
        return {}, "未提供案件计算参数（case_config），正式数字待补充参数后计算。"
    try:
        cfg = load_case_config(case_config_path)
    except Exception as error:  # noqa: BLE001 - 参数问题不阻断分析
        return {}, f"案件计算参数无法解析：{type(error).__name__}；正式数字待修正参数后计算。"

    debts: dict[str, ShadowDebt] = cfg["debts"]
    active = {k: v for k, v in debts.items() if not v.evidence_pending}
    pending = [k for k, v in debts.items() if v.evidence_pending]
    if not active:
        return {}, "全部债务均缺少出借凭证（evidence_pending），正式数字暂不可计算。"

    rows = [
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
        result = run_engine(rows, active, config)
    except ShadowEngineBlocked as error:
        return {}, f"参数不完整，正式数字待补充：{error}"

    numbers: dict[str, str] = {}
    total_principal = Decimal("0")
    total_interest = Decimal("0")
    for debt_id in sorted(result.loans):
        loan = result.loan(debt_id)
        numbers[f"{debt_id} 未偿本金"] = canonical_amount(loan.principal)
        numbers[f"{debt_id} 未付利息挂账"] = canonical_amount(loan.interest_arrears)
        total_principal += loan.principal
        total_interest += loan.interest_arrears
    numbers["合计本金"] = canonical_amount(total_principal)
    numbers["合计未付利息挂账"] = canonical_amount(total_interest)
    numbers["利息暂计截止日"] = str(cfg["final_date"])

    note = (
        "以上数字由确定性引擎按律师确认的债务参数与司法保护上限计算至截止日，"
        "为不含付款冲抵的毛额；模型未参与任何计算。"
        "付款冲抵后的净额须待律师确认各笔付款性质后另行计算。"
    )
    if pending:
        note += f" 债务 {', '.join(sorted(pending))} 因缺少出借凭证已挂起，未计入合计。"
    return numbers, note


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
    try:
        from case_kernel.shadow_live_transport import QwenShadowTransport
        transport = request.transport or QwenShadowTransport(
            materials_root=request.materials_dir,
            env_file=request.env_file,
            budget_cny=request.budget_cny,
            run_root=out_root,
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
            pages=sorted(page_numbers)[:MAX_RENDERED_PAGES_DEFAULT],
        )
        notes.append(f"{Path(file_name).name}: {note}")
        for item in rendered_file_pages:
            key = (file_name, item.page_number)
            overrides[key] = item.path
            rendered.append(PageText(file_name, "", item.page_number, ""))
    return rendered, "；".join(notes), overrides


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
