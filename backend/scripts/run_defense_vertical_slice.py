#!/usr/bin/env python3
"""Run the de-identified defence vertical slice and print measured results."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_kernel.golden_defense_vertical_slice import (  # noqa: E402
    ACCEPT_GOLDEN_RECOMMENDATIONS,
    GoldenVerticalSliceBlocked,
    archive_existing_output,
    run_golden_vertical_slice,
)
from case_kernel.golden_case_calculation import GoldenCaseCalculationBlocked  # noqa: E402
from case_kernel.golden_case_agent_evaluation import (  # noqa: E402
    DEFAULT_TOTAL_BUDGET_CNY,
    run_agent_experiment,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成并实测一个脱敏合成案件的最小完整应诉生产线。"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="本次运行目录；既有目录会被移动到同级 history，不会删除。",
    )
    parser.add_argument(
        "--approve-synthetic-recommendation",
        action="store_true",
        help="明确接受合成复核包中的推荐、备选及后果；缺少时不计算、不锁包。",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="运行成功后在 Finder 中定位锁定 ZIP（仅 macOS）。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--agent",
        action="store_true",
        help="用真实 qwen3-vl-plus 运行至少5次提议/自检实验。",
    )
    mode.add_argument(
        "--agent-offline",
        action="store_true",
        help="CI无模型降级；A1-A5记N/A，仅验证确定性骨架。",
    )
    parser.add_argument(
        "--agent-runs",
        type=int,
        default=5,
        help="真实Agent实验次数，不得少于5（默认5）。",
    )
    parser.add_argument(
        "--agent-budget-cny",
        type=Decimal,
        default=DEFAULT_TOTAL_BUDGET_CNY,
        help="真实Agent总费用硬上限，本实验锁定为2元。",
    )
    parser.add_argument(
        "--agent-prior-cost-reserve-cny",
        type=Decimal,
        default=Decimal("0"),
        help="为已发生但无用量回执的失败请求预留的费用；正常复现保持0。",
    )
    parser.add_argument(
        "--agent-resume-first-proposal-from",
        type=Path,
        default=None,
        help="仅复用与本次run-01脱敏表面字节级一致的已付费提议回执。",
    )
    parser.add_argument(
        "--agent-resume-first-self-check-from",
        type=Path,
        default=None,
        help="仅复用与本次run-01两份候选PDF文本字节级一致的已付费自检回执。",
    )
    parser.add_argument(
        "--agent-resume-exchanges-from",
        type=Path,
        default=None,
        help="从失败实验根目录逐次复用与当前脱敏输入精确绑定的已付费回执。",
    )
    parser.add_argument(
        "--agent-env-file",
        type=Path,
        default=PROJECT_ROOT
        / "deployment"
        / "local-managed-test"
        / "runtime"
        / "local-managed.env",
        help="仅用于导入现有Qwen密钥与业务空间；密钥不进入代码或日志。",
    )
    arguments = parser.parse_args()
    if not arguments.approve_synthetic_recommendation:
        parser.error("必须显式传入 --approve-synthetic-recommendation 才能通过唯一审批门")
    if arguments.output is None:
        output = (
            PROJECT_ROOT
            / "outputs"
            / (
                "defense-agent-experiment"
                if arguments.agent or arguments.agent_offline
                else "defense-vertical-slice"
            )
            / "current"
        )
    else:
        output = arguments.output.expanduser().resolve()
    archived = archive_existing_output(output)
    if arguments.agent or arguments.agent_offline:
        try:
            experiment = run_agent_experiment(
                output,
                project_root=PROJECT_ROOT,
                env_file=arguments.agent_env_file,
                runs=arguments.agent_runs,
                total_budget_cny=arguments.agent_budget_cny,
                prior_cost_reserve_cny=arguments.agent_prior_cost_reserve_cny,
                resume_first_proposal_from=arguments.agent_resume_first_proposal_from,
                resume_first_self_check_from=arguments.agent_resume_first_self_check_from,
                resume_exchanges_from=arguments.agent_resume_exchanges_from,
                offline=arguments.agent_offline,
            )
        except Exception:
            if output.exists():
                failed = output.parent / f"{output.name}-failed"
                suffix = 1
                candidate = failed
                while candidate.exists():
                    suffix += 1
                    candidate = output.parent / f"{failed.name}-{suffix}"
                output.rename(candidate)
            raise
        payload = {
            key: experiment.metrics[key]
            for key in (
                "mode",
                "A1_proposal_accuracy",
                "A2_escalation_discipline",
                "A3_evidence_binding",
                "A4_self_check",
                "A5_redline_discipline",
                "A6_deterministic_recalculation",
                "cost",
                "passed",
                "failed_metrics",
            )
            if key in experiment.metrics
        }
        payload.update(
            {
                "run_report": str(experiment.report_path),
                "metrics_json": str(experiment.metrics_path),
                "archived_previous_run": str(archived) if archived else None,
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        if arguments.open and sys.platform == "darwin":
            target = experiment.report_path
            completed = subprocess.run(["/usr/bin/open", "-R", str(target)], check=False)
            if completed.returncode != 0:
                print("BLOCKED: 报告已生成，但 Finder 未能打开定位。", file=sys.stderr)
                return 2
        return 0 if experiment.passed else 3
    try:
        result = run_golden_vertical_slice(
            output,
            project_root=PROJECT_ROOT,
            synthetic_decision=ACCEPT_GOLDEN_RECOMMENDATIONS,
        )
    except Exception:
        # A failed candidate must never be mistaken for the current complete run.
        if output.exists():
            failed = output.parent / f"{output.name}-failed"
            suffix = 1
            candidate = failed
            while candidate.exists():
                suffix += 1
                candidate = output.parent / f"{failed.name}-{suffix}"
            output.rename(candidate)
        raise

    metrics = result.metrics
    concise = {
        "case_id": metrics["case_id"],
        "synthetic_only": metrics["synthetic_only"],
        "page_deduplication": metrics["page_deduplication"],
        "information_extraction": {
            key: metrics["information_extraction"][key]
            for key in ("gold_field_labels", "tp", "fp", "fn", "precision", "recall")
        },
        "deterministic_calculation": metrics["deterministic_calculation"],
        "cross_document_consistency": metrics["cross_document_consistency"],
        "invalidation_propagation": {
            key: metrics["invalidation_propagation"][key]
            for key in (
                "decision_mutations_tested",
                "expected_downstream_invalidations",
                "actual_downstream_invalidations",
                "old_packages_blocked",
            )
        },
        "provenance": metrics["provenance"],
        "locked_submission": str(result.locked_zip_path),
        "run_report": str(result.report_path),
        "metrics_json": str(result.metrics_path),
        "archived_previous_run": str(archived) if archived else None,
    }
    print(json.dumps(concise, ensure_ascii=False, indent=2, sort_keys=True))
    if arguments.open:
        if sys.platform != "darwin":
            print("BLOCKED: --open 只支持 macOS Finder。", file=sys.stderr)
            return 2
        completed = subprocess.run(
            ["/usr/bin/open", "-R", str(result.locked_zip_path)],
            check=False,
        )
        if completed.returncode != 0:
            print("BLOCKED: 锁定包已生成，但 Finder 未能打开定位。", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GoldenVerticalSliceBlocked, GoldenCaseCalculationBlocked) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(2) from error
