#!/usr/bin/env python3
"""律师实用模式 CLI：一条命令把案件材料变成可复核的决策包。

用法：
  scripts/run_case_analysis --materials <材料目录> \
      --case-number "(2026)粤1302民初XXXX号" --role 被告 --stage 一审应诉 \
      --confirm-data-path <preflight.json> --budget-cny 2

设计目标（回答"Agent 到底怎么搞"）：
  - Agent 做粗活：读材料、提事实、找争点、给方向
  - 代码做精活：金额/比例由引擎提供，模型输出的数字一律剔除
  - 三档门禁：硬红线拒绝 / 格式自动修复 / 内容标记放行
  - 输出可降级交付：不确定项进"待律师确认"清单
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_kernel.lawyer_practical_mode import (  # noqa: E402
    build_practical_prompt,
    normalize_and_gate,
    render_practical_report,
    PracticalResult,
    GateDecision,
)
from case_kernel.shadow_mode import (  # noqa: E402
    RequestLedger,
    ShadowBlocked,
    build_import_manifest,
    load_case_config,
    validate_preflight,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="律师实用模式：材料 → 决策包（Agent 提议、代码计算、律师决定）")
    parser.add_argument("--materials", type=Path, required=True, help="案件材料目录（PDF/图片）")
    parser.add_argument("--case-number", default="（未填写案号）", help="案号")
    parser.add_argument("--role", default="被告", choices=["被告", "原告"], help="代理方")
    parser.add_argument("--stage", default="一审应诉", help="诉讼阶段")
    parser.add_argument("--case-config", type=Path, default=None, help="案件计算配置（可选）")
    parser.add_argument("--confirm-data-path", type=Path, required=True, help="数据路径 preflight 确认文件")
    parser.add_argument("--agent-env-file", type=Path,
                        default=PROJECT_ROOT / "deployment" / "local-managed-test" / "runtime" / "local-managed.env",
                        help="Qwen 密钥环境文件（仅运行时读取）")
    parser.add_argument("--budget-cny", type=Decimal, default=Decimal("2"), help="单次运行成本上限（元）")
    parser.add_argument("--output", type=Path, default=None, help="输出目录；缺省按时间戳创建")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_root = args.output or (PROJECT_ROOT / "outputs" / "case-analysis" / f"run-{stamp}")
    out_root.mkdir(parents=True, exist_ok=True)
    ledger = RequestLedger(out_root / "request_ledger.json")

    try:
        # 1) 材料导入 + S1 门
        # 实用模式：真实案件材料不阻断，对将发送给模型的文本自动掩码
        entries, pages, masked = build_import_manifest(
            args.materials, mask_identifiers=True)
        # 2) 引擎数字（来自 case_config；无配置时为空字典，模型不会拿到数字）
        engine_amounts: dict[str, str] = {}
        if args.case_config and args.case_config.is_file():
            cfg = load_case_config(args.case_config)
            for debt_id, debt in cfg["debts"].items():
                engine_amounts[f"{debt_id} 本金"] = f"{debt.principal}"
            engine_amounts["利息暂计截止"] = str(cfg["final_date"])
        # 3) preflight + 传输层
        preflight = validate_preflight(json.loads(args.confirm_data_path.read_text(encoding="utf-8")))
        from case_kernel.shadow_live_transport import QwenShadowTransport
        transport = QwenShadowTransport(
            materials_root=args.materials, env_file=args.agent_env_file,
            budget_cny=args.budget_cny, run_root=out_root,
        )
        authorized = set(preflight.get("sent_fields", {}).get("page_files", []))
        ocr_pages = transport.ocr_pages(pages, authorized, ledger) if authorized else []
        ocr_by_key = {(p.file_name, p.page_number): p.text for p in ocr_pages}
        # 4) 材料表面（PDF 文本层 + OCR 文本）
        def surface_for(page) -> str:
            text = ocr_by_key.get((page.file_name, page.page_number)) or page.text
            return f"### FILE={page.file_name} PAGE={page.page_number}\n{text}"
        surface = "\n".join(surface_for(p) for p in pages)
        if len(surface) > 30_000:
            surface = surface[:30_000] + "\n[截断]"
        # 5) 分析调用（宽松契约）
        instruction = build_practical_prompt(
            case_number=args.case_number, role=args.role, stage=args.stage,
            surface=surface, engine_amounts=engine_amounts,
            trusted_authorities=preflight.get("trusted_authorities", []),
        )
        raw = transport.call_analysis(instruction=instruction, ledger=ledger)
        # 6) 三档门禁
        analysis, gate = normalize_and_gate(raw, engine_amounts=engine_amounts)
        result = PracticalResult(gate=gate, analysis=analysis, engine_amounts=engine_amounts,
                                 review_queue=[])
        # 7) 报告
        report = render_practical_report(
            case_number=args.case_number, role=args.role, result=result,
            proposal_source=f"{preflight.get('model', 'qwen')}（真实调用）",
        )
        (out_root / "决策包.md").write_text(report, encoding="utf-8")
        (out_root / "agent_raw_output.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        spent = sum(Decimal(r.get("cost_cny", "0") or "0") for r in ledger.rows)
        summary = {
            "run_root": str(out_root),
            "report": str(out_root / "决策包.md"),
            "gate_level": gate.level,
            "repairs": gate.repairs,
            "review_items": gate.review_items,
            "cost_cny": str(spent),
            "calls": len([r for r in ledger.rows if r.get("status") == "ok"]),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if gate.level in ("PASS", "AUTO_REPAIRED", "MARK_FOR_REVIEW") else 2
    except ShadowBlocked as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
