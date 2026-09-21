"""答辩状草稿生成服务：确定性骨架 + 受门禁约束的模型文字。

与案件分析服务共用同一套数据路径纪律：

- 正式数字只由 ``compute_engine_numbers`` 产出，模型不得计算；
- 模型调用需要已确认的 preflight、显式启用的模型环境文件与预算上限；
- 没有可用的分析报告时，模型不参与，只输出确定性骨架并在正文标注待补写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Callable, Mapping

from case_kernel.case_analysis_service import compute_engine_numbers
from case_kernel.defence_brief import (
    BriefSelections,
    authority_cause_warnings,
    build_brief_prompt,
    claim_catalogue,
    ground_catalogue,
    normalize_brief_output,
    render_brief_markdown,
)
from case_kernel.shadow_mode import RequestLedger, ShadowBlocked, validate_preflight

STATUS_COMPLETED = "COMPLETED"
STATUS_MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
STATUS_BLOCKED = "BLOCKED"
STATUS_FAILED = "FAILED"

ProgressCallback = Callable[[str, int], None]

def _ground_text(cause: str) -> dict[str, str]:
    return {ground_id: description for ground_id, _title, description in ground_catalogue(cause)}


@dataclass
class BriefRequest:
    case_id: str
    output_root: Path
    selections: BriefSelections
    case_config_path: Path | None = None
    case_number: str = ""
    analysis_report_path: Path | None = None
    preflight_path: Path | None = None
    env_file: Path | None = None
    budget_cny: Decimal = Decimal("2")
    materials: list[dict] = field(default_factory=list)
    evidence_index: str = ""
    progress: ProgressCallback | None = None
    transport: object | None = None
    allow_image_identifiers: bool = False


@dataclass
class BriefResult:
    status: str
    gate_level: str = ""
    markdown: str = ""
    review_items: list[str] = field(default_factory=list)
    engine_amounts: dict = field(default_factory=dict)
    cost_cny: str = "0.000000"
    calls: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "gate_level": self.gate_level,
            "markdown": self.markdown,
            "review_items": self.review_items,
            "engine_amounts": self.engine_amounts,
            "cost_cny": self.cost_cny,
            "calls": self.calls,
            "error": self.error,
        }


def _emit(request: BriefRequest, stage: str, percent: int) -> None:
    if request.progress is not None:
        request.progress(stage, percent)


def _preflight_review(request: BriefRequest, engine_amounts: dict) -> list[str]:
    """进入正文前先如实说明缺什么，避免律师拿到一份"看起来完整"的空壳。"""
    items: list[str] = []
    if not engine_amounts:
        items.append("尚未填写案件计算参数：文书中没有正式数字，请先在决策包页面填写后再生成。")
    if not request.selections.authorities:
        items.append("律师尚未登记法源：正文中的法条位置保留占位，需律师填写。")
    if not any(request.selections.grounds.get(ground) for ground in request.selections.grounds):
        items.append("律师尚未选择任何主张：事实与理由各节为空，请先在页面上勾选。")
    if not request.selections.cause.strip():
        items.append("律师尚未填写案由：术语与分节标题只能按中性表述生成，请填写案由后重新生成。")
    items.extend(authority_cause_warnings(request.selections.authorities, request.selections.cause))
    return items


def _degraded(request: BriefRequest, engine_amounts: dict, reason: str,
              *, out_root: Path | None = None) -> BriefResult:
    """未配置模型：只产出确定性骨架，逐节标注待律师补写。"""
    sections = [
        {
            "ground_id": ground_id,
            "title": title,
            "paragraphs": [
                f"【本节论证待律师补写】{description}",
            ],
        }
        for ground_id, title, description in ground_catalogue(request.selections.cause)
        if request.selections.grounds.get(ground_id)
    ]
    review_items = [
        "未配置模型：本稿只有文书骨架，正文论证需律师补写。",
        f"未参与模型分析的原因：{reason}",
        *_preflight_review(request, engine_amounts),
    ]
    markdown = render_brief_markdown(
        selections=request.selections,
        engine_amounts=engine_amounts,
        sections=sections,
        review_items=review_items,
        gate_level="MODEL_NOT_CONFIGURED",
        materials=request.materials,
        evidence_index=request.evidence_index,
        proposal_source="未调用模型（确定性骨架）",
        generated_at=_now(),
    )
    if out_root is not None:
        # 降级稿同样要落盘：否则界面显示「草稿可用」却无法打开或导出。
        Path(out_root).joinpath("答辩状草稿.md").write_text(markdown, encoding="utf-8")
    return BriefResult(
        status=STATUS_MODEL_NOT_CONFIGURED,
        gate_level="MODEL_NOT_CONFIGURED",
        markdown=markdown,
        review_items=review_items,
        engine_amounts=engine_amounts,
        error=reason,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analysis_context(path: Path | None) -> tuple[str, str]:
    """返回（原告主张概要, 分析报告全文）。报告缺失时返回空串。"""
    if path is None or not Path(path).is_file():
        return "", ""
    text = Path(path).read_text(encoding="utf-8")
    summary = ""
    marker = "## 一、案情与立场"
    if marker in text:
        tail = text.split(marker, 1)[1]
        summary = tail.split("\n## ", 1)[0].strip()[:1200]
    return summary, text.strip()


def run_brief(request: BriefRequest) -> BriefResult:
    """生成答辩状草稿：骨架 → （可选）模型文字 → 门禁 → 渲染。"""
    out_root = Path(request.output_root)
    out_root.mkdir(parents=True, exist_ok=True)
    _emit(request, "计算正式数字", 10)
    engine_amounts, _note = compute_engine_numbers(request.case_config_path)

    summary, context = _analysis_context(request.analysis_report_path)
    if not summary and request.selections.notes:
        summary = request.selections.notes[:600]

    if request.preflight_path is None or not Path(request.preflight_path).is_file():
        return _degraded(request, engine_amounts, "未配置数据路径确认文件（preflight）。",
                         out_root=out_root)
    try:
        preflight = validate_preflight(json.loads(Path(request.preflight_path).read_text(encoding="utf-8")))
    except (ShadowBlocked, ValueError) as error:
        return _degraded(request, engine_amounts, str(error), out_root=out_root)
    if request.env_file is None or not Path(request.env_file).is_file():
        return _degraded(request, engine_amounts, "未找到模型环境配置文件。", out_root=out_root)
    if not context:
        return _degraded(request, engine_amounts, "本案尚无决策包报告，模型不参与文书拟写。",
                         out_root=out_root)

    ledger = RequestLedger(out_root / "brief_ledger.json")
    _emit(request, "起草论证文字", 40)
    try:
        from case_kernel.shadow_live_transport import QwenShadowTransport

        transport = request.transport or QwenShadowTransport(
            materials_root=out_root,
            env_file=request.env_file,
            budget_cny=request.budget_cny,
            run_root=out_root,
            allow_image_identifiers=request.allow_image_identifiers,
        )
        instruction = build_brief_prompt(
            selections=request.selections,
            engine_amounts=engine_amounts,
            claim_summary=summary,
            analysis_context=context,
            evidence_index=request.evidence_index,
        )
        raw = getattr(transport, "call_analysis")(
            instruction=instruction, ledger=ledger, purpose="defence-brief",
        )
        _emit(request, "门禁校验", 80)
        source_amounts = _source_amounts_from_report(context)
        payload, gate = normalize_brief_output(
            raw,
            selections=request.selections,
            engine_amounts=engine_amounts,
            source_amounts=source_amounts,
        )
        out_root.joinpath("答辩状_raw_output.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except ShadowBlocked as error:
        return BriefResult(status=STATUS_BLOCKED, error=str(error), engine_amounts=engine_amounts,
                           cost_cny=_spent(ledger), calls=_calls(ledger))
    except Exception as error:  # noqa: BLE001 - 服务边界
        return BriefResult(status=STATUS_FAILED, error=f"{type(error).__name__}: {error}",
                           engine_amounts=engine_amounts,
                           cost_cny=_spent(ledger), calls=_calls(ledger))

    if gate.level == "HARD_BLOCKED":
        return BriefResult(
            status=STATUS_BLOCKED,
            gate_level=gate.level,
            error="模型文字命中安全红线（声称已批准/已提交等），本稿已拒绝。",
            engine_amounts=engine_amounts,
            review_items=list(gate.review_items),
            cost_cny=_spent(ledger),
            calls=_calls(ledger),
        )

    review_items = [*_preflight_review(request, engine_amounts), *gate.review_items]
    markdown = render_brief_markdown(
        selections=request.selections,
        engine_amounts=engine_amounts,
        sections=payload.get("sections") or [],
        review_items=review_items,
        gate_level=gate.level,
        materials=request.materials,
        evidence_index=request.evidence_index,
        proposal_source=f"{preflight.get('model', 'qwen')}（真实调用）",
        generated_at=_now(),
    )
    out_root.joinpath("答辩状草稿.md").write_text(markdown, encoding="utf-8")
    _emit(request, "完成", 100)
    return BriefResult(
        status=STATUS_COMPLETED,
        gate_level=gate.level,
        markdown=markdown,
        review_items=review_items,
        engine_amounts=engine_amounts,
        cost_cny=_spent(ledger),
        calls=_calls(ledger),
    )


def _source_amounts_from_report(report: str) -> set[str]:
    """报告里出现过的数字视为已核对事实引用（报告本身已过数字门禁）。"""
    from case_kernel.lawyer_practical_mode import extract_source_amounts

    return extract_source_amounts([report])


def _spent(ledger: RequestLedger) -> str:
    total = Decimal("0")
    for row in ledger.rows:
        if str(row.get("status")) == "ok":
            total += Decimal(str(row.get("cost_cny", "0")))
    return f"{total:.6f}"


def _calls(ledger: RequestLedger) -> int:
    return sum(1 for row in ledger.rows if str(row.get("status")) == "ok")
