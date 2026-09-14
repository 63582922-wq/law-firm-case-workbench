#!/usr/bin/env python3
"""Run one bounded real-model lawyer decision-package acceptance."""

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

from case_kernel.golden_case_agent_evaluation import (  # noqa: E402
    BudgetLedger,
    LAWYER_PACKAGE_MODEL_ID,
    QwenGoldenAgentProvider,
    load_qwen_environment,
)
from case_kernel.golden_defense_vertical_slice import GoldenVerticalSliceBlocked  # noqa: E402
from case_kernel.golden_case_lawyer_package import (  # noqa: E402
    GoldenLawyerPackageBlocked,
    run_golden_lawyer_package,
)


def _record_blocked_attempt(
    output: Path,
    *,
    run_id: str,
    error: Exception,
    call_reserve_cny: Decimal,
) -> None:
    """Preserve one-call failure evidence without retrying or exposing secrets."""

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    actual_usage: dict[str, object] | None = None
    transcript_path = output / "agent_provider_transcript.json"
    if transcript_path.is_file():
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            usage = transcript.get("usage") if isinstance(transcript, dict) else None
            if isinstance(usage, dict) and isinstance(usage.get("cost_cny"), str):
                actual_usage = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "cost_cny": usage["cost_cny"],
                }
        except (OSError, json.JSONDecodeError):
            actual_usage = None
    receipt = {
        "schema_version": "golden-lawyer-single-call-blocked-v1",
        "run_id": run_id,
        "status": "BLOCKED_SINGLE_CALL_NO_RETRY",
        "model": LAWYER_PACKAGE_MODEL_ID,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "error_type": type(error).__name__,
        "error": str(error)[:500],
        "synthetic_only": True,
        "retry_count": 0,
        "additional_calls_allowed_for_this_acceptance": 0,
        "cost_status": (
            "PROVIDER_USAGE_RECEIPT_CONFIRMED"
            if actual_usage is not None
            else "UNKNOWN_CONSERVATIVE_RESERVE"
        ),
        "actual_usage": actual_usage,
        "conservative_unknown_cost_reserve_cny": (
            "0.000000"
            if actual_usage is not None
            else format(call_reserve_cny, "f")
        ),
    }
    receipt_path = output / "BLOCKED_CALL_RECEIPT.json"
    if not receipt_path.exists():
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    report_path = output / "RUN_BLOCKED.md"
    if not report_path.exists():
        report_path.write_text(
            "\n".join(
                [
                    "# 律师 Agent 单次真实验收阻断记录",
                    "",
                    "- 状态：`BLOCKED_SINGLE_CALL_NO_RETRY`",
                    f"- 模型：`{LAWYER_PACKAGE_MODEL_ID}`",
                    f"- 原因：{type(error).__name__}：{str(error)[:300]}",
                    (
                        f"- 费用处理：供应商回执确认本次费用为 {actual_usage['cost_cny']} 元。"
                        if actual_usage is not None
                        else f"- 费用处理：结果不明时按 {format(call_reserve_cny, 'f')} 元保守预留。"
                    ),
                    "- 后续边界：本次验收不再发起任何外部调用；保留当前材料、输入包和回执供修复。",
                    "- 数据：全部为合成测试材料，不含真实客户数据。",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用88页全合成案件执行一次真实律师Agent决策包验收。"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="lawyer-package-run-01")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT
        / "deployment"
        / "local-managed-test"
        / "runtime"
        / "local-managed.env",
    )
    parser.add_argument(
        "--budget-cny",
        type=Decimal,
        default=Decimal("1.200000"),
        help="本次唯一模型调用的总费用硬上限，默认1.20元。",
    )
    parser.add_argument(
        "--prior-uncertain-cost-reserve-cny",
        type=Decimal,
        default=Decimal("0"),
        help="前次无用量回执调用的保守费用预留；会占用总预算。",
    )
    arguments = parser.parse_args()
    if arguments.budget_cny != Decimal("1.200000"):
        parser.error("严格结构化验收预算必须固定为1.20元")
    if arguments.prior_uncertain_cost_reserve_cny != 0:
        parser.error("严格结构化验收必须是全新的独立运行，不能携带前次不确定费用")
    model_environment = load_qwen_environment(arguments.env_file)
    budget = BudgetLedger(
        total_limit_cny=arguments.budget_cny,
        spent_cny=arguments.prior_uncertain_cost_reserve_cny,
    )
    provider = QwenGoldenAgentProvider(
        project_root=PROJECT_ROOT,
        run_id=arguments.run_id,
        shuffle_seed=20260826,
        model_environment=model_environment,
        budget=budget,
    )
    try:
        result = run_golden_lawyer_package(
            arguments.output,
            project_root=PROJECT_ROOT,
            run_id=arguments.run_id,
            agent_provider=provider.lawyer_package,
        )
    except (GoldenLawyerPackageBlocked, GoldenVerticalSliceBlocked) as error:
        _record_blocked_attempt(
            arguments.output.resolve(),
            run_id=arguments.run_id,
            error=error,
            call_reserve_cny=Decimal("1.200000"),
        )
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "passed": result.passed,
                "docx": str(result.docx_path),
                "pdf": str(result.pdf_path),
                "acceptance": str(result.acceptance_path),
                "report": str(result.report_path),
                "usage": result.metrics.get("usage", {}),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
