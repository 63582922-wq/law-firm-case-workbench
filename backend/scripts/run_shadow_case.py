#!/usr/bin/env python3
"""Shadow test mode CLI: analyze a de-identified real-case copy without any
formal submission artifact.  See docs/SHADOW_MODE_ACCEPTANCE.md (S1-S7)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from case_kernel.shadow_engine import ShadowEngineBlocked  # noqa: E402
from case_kernel.shadow_mode import (  # noqa: E402
    ShadowBlocked,
    ShadowGateFailed,
    attempt_formal_lock,
    run_shadow_case,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="脱敏真实案件影子测试模式：只分析、无正式输出、不外发、不锁包。"
    )
    parser.add_argument("--materials", type=Path, default=None,
                        help="彻底脱敏的案件材料目录（PDF 含文本层 / 图片）")
    parser.add_argument("--expected", type=Path, default=None,
                        help="自带答案 CSV（S6 对账）；列：row_id,date,channel,amount,currency,direction,classification,debt_id,note")
    parser.add_argument("--case-config", type=Path, default=None,
                        help="案件计算配置 JSON；缺省时读取 <materials>/case_config.json")
    parser.add_argument("--proposal-file", type=Path, default=None,
                        help="干跑提议文件（无模型调用）；缺省且无 --confirm-data-path 时降级为本地模式")
    parser.add_argument("--confirm-data-path", type=Path, default=None,
                        help="数据路径 preflight 确认文件；缺省时不允许任何模型调用")
    parser.add_argument("--budget-cny", type=Decimal, default=Decimal("2"),
                        help="单次运行成本硬上限（元），默认 2 元")
    parser.add_argument("--open", action="store_true",
                        help="运行成功后打开影子报告所在目录（仅 macOS）")
    parser.add_argument(
        "--agent-env-file",
        type=Path,
        default=PROJECT_ROOT / "deployment" / "local-managed-test" / "runtime" / "local-managed.env",
        help="Qwen 密钥环境文件（仅运行时读取，密钥不进代码/日志/Git）",
    )
    parser.add_argument("--attempt-lock", action="store_true",
                        help=argparse.SUPPRESS)  # S2 对抗用例钩子
    arguments = parser.parse_args()

    if arguments.attempt_lock:
        try:
            attempt_formal_lock(Path("."))
        except ShadowBlocked as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            return 2
        return 1
    if arguments.materials is None:
        parser.error("--materials 为必填参数")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = PROJECT_ROOT / "outputs" / "shadow-case" / f"run-{stamp}"
    suffix = 1
    while output.exists() and any(output.iterdir()):
        output = PROJECT_ROOT / "outputs" / "shadow-case" / f"run-{stamp}-{suffix}"
        suffix += 1
    transport = None
    if arguments.confirm_data_path is not None:
        from case_kernel.shadow_live_transport import QwenShadowTransport
        transport = QwenShadowTransport(
            materials_root=arguments.materials,
            env_file=arguments.agent_env_file,
            budget_cny=arguments.budget_cny,
            run_root=output,
        )
    try:
        outcome = run_shadow_case(
            materials=arguments.materials,
            output_root=output,
            case_config=arguments.case_config,
            expected_csv=arguments.expected,
            proposal_file=arguments.proposal_file,
            confirm_data_path=arguments.confirm_data_path,
            budget_cny=arguments.budget_cny,
            transport=transport,
        )
    except (ShadowBlocked, ShadowGateFailed, ShadowEngineBlocked) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(json.dumps(
        {
            "run_root": str(outcome.run_root),
            "report": str(outcome.run_root / "shadow_report.md"),
            "ledger": str(outcome.run_root / "request_ledger.json"),
            "blocking_log": str(outcome.run_root / "blocking_log.json"),
            "exit_code": outcome.exit_code,
            "gates": [gate.to_dict() for gate in outcome.gate_statuses],
        },
        ensure_ascii=False, indent=1, sort_keys=True,
    ))
    if arguments.open:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-R", str(outcome.run_root / "shadow_report.md")],
                           check=False)
        else:
            print("BLOCKED: --open 仅支持 macOS Finder。", file=sys.stderr)
            return 2
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
