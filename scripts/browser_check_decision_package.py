#!/usr/bin/env python3
"""浏览器自检：打开「决策包」页面，核对状态、正式数字与导出按钮是否真的可用。

背景：只看构建产物或只用 API 调用无法发现「页面根本点不开」「POST 响应解析失败」
这类缺陷。本脚本用真实浏览器（Playwright + 本机 Chrome）走一遍本机模式的关键路径。

依赖：`python3 -m pip install --user playwright`（浏览器复用本机已安装的 Chrome，
不额外下载浏览器内核）。

用法：

    # 只读自检：打开页面并核对渲染结果，不发起任何模型调用
    python3 scripts/browser_check_decision_package.py --case-id <案件ID>

    # 触发一次真实分析（会产生模型费用）并在完成后核对导出
    python3 scripts/browser_check_decision_package.py --case-id <案件ID> --run

    # 附带填写案件计算参数（JSON 文件，结构见 docs/LOCAL_WEB.md）
    python3 scripts/browser_check_decision_package.py --case-id <案件ID> \
        --parameters /path/to/parameters.json --run

前置条件：先启动本机工作台（`python3 scripts/start_local_web.py`），并把
`--web-origin` 指向终端打印的地址（默认 http://127.0.0.1:33000）。
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="决策包页面浏览器自检（本机模式）")
    parser.add_argument("--case-id", required=True, help="案件 ID（工作台 URL 中的 case 参数）")
    parser.add_argument("--web-origin", default="http://127.0.0.1:33000",
                        help="本机工作台地址，必须与启动器打印的地址一致")
    parser.add_argument("--run", action="store_true",
                        help="点击「开始分析」并等待完成（会产生真实模型调用与费用）")
    parser.add_argument("--authorize-image-identifiers", action="store_true",
                        help="勾选「扫描件图像原样发送」（仅在 --run 时生效）")
    parser.add_argument("--parameters", type=Path, default=None,
                        help="案件计算参数 JSON（含 lpr_4x_monthly_rate/interest_cutoff/debts）")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="等待分析完成的上限")
    parser.add_argument("--expect-status", default="COMPLETED",
                        help="允许的终态，逗号分隔；默认 COMPLETED。"
                             "未配置模型时用 MODEL_NOT_CONFIGURED")
    parser.add_argument("--screenshot-dir", type=Path, default=None,
                        help="截图输出目录（默认不截图）")
    parser.add_argument("--json-out", type=Path, default=None, help="把自检结果写入 JSON 文件")
    parser.add_argument("--brief", action="store_true",
                        help="同时检查「答辩状」页面：状态、草稿与导出按钮")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright：python3 -m pip install --user playwright", file=sys.stderr)
        return 2

    shots = args.screenshot_dir
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"case_id": args.case_id, "steps": [], "console_errors": []}

    def step(name: str, **extra: object) -> None:
        report["steps"].append({"step": name, **extra})  # type: ignore[attr-defined]
        print(json.dumps({"step": name, **extra}, ensure_ascii=False), flush=True)

    def snapshot(page, name: str) -> None:
        if shots is not None:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100},
                                      accept_downloads=True)
        page = context.new_page()
        page.on("console", lambda m: report["console_errors"].append(m.text)  # type: ignore[attr-defined]
                if m.type == "error" else None)
        page.goto(f"{args.web_origin}/analysis?case={args.case_id}",
                  wait_until="domcontentloaded", timeout=120_000)
        page.get_by_role("button", name="开始分析").wait_for(timeout=180_000)
        status = " | ".join(t.strip() for t in page.get_by_role("status").all_inner_texts() if t.strip())
        step("页面已打开", status=status)
        snapshot(page, "01-open")

        if args.parameters is not None:
            try:
                payload = json.loads(args.parameters.read_text(encoding="utf-8"))
                _ = Decimal(str(payload["lpr_4x_monthly_rate"]))
                _ = str(payload["interest_cutoff"])
            except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as error:
                print(f"案件计算参数文件不可用：{error}", file=sys.stderr)
                browser.close()
                return 2
            section = page.locator("section[aria-label='案件计算参数']")
            section.get_by_label("司法保护上限月利率（%，例：1 表示月利率 1%）").fill(
                str(Decimal(str(payload["lpr_4x_monthly_rate"])) * 100))
            section.get_by_label("利息暂计截止日").fill(str(payload["interest_cutoff"]))
            debts = payload.get("debts", [])
            # 页面会先回填服务器上已保存的参数；这里按目标条数对齐行数，
            # 避免既有多出的空行导致「本金额请填数字」这类校验失败。
            while section.locator("tbody tr").count() > len(debts) and len(debts) >= 1:
                last = section.locator("tbody tr").last
                if last.get_by_role("button", name="删除").is_disabled():
                    break
                last.get_by_role("button", name="删除").click()
            for index, debt in enumerate(debts):
                if index >= section.locator("tbody tr").count():
                    section.get_by_role("button", name="新增一笔借款").click()
                inputs = section.locator("tbody tr").nth(index).locator("input")
                inputs.nth(0).fill(str(debt["debt_id"]))
                inputs.nth(1).fill(str(debt["principal"]))
                inputs.nth(2).fill(str(debt["disbursed_on"]))
                if debt.get("due_on"):
                    inputs.nth(3).fill(str(debt["due_on"]))
                inputs.nth(4).fill(str(Decimal(str(debt["agreed_monthly_rate"])) * 100))
                if debt.get("evidence_pending"):
                    inputs.nth(5).check()
            step("已填写案件计算参数", debts=len(payload.get("debts", [])))
            snapshot(page, "02-parameters")

        if not args.run:
            page.wait_for_timeout(1500)
            passed = finish(page, args, report, step)
            if args.brief:
                passed = check_brief_page(page, args, report, step) and passed
            if args.json_out is not None:
                args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
            browser.close()
            return passed

        if args.authorize_image_identifiers:
            page.get_by_role("checkbox").first.check()
            step("已授权扫描件图像原样发送")

        before = read_run_summary(page, args.case_id)
        step("运行前状态", 状态=before)
        page.get_by_role("button", name="开始分析").click()

        # 先确认这次真的跑起来了：必须观察到运行态，否则不能把上一次的「分析完成」
        # 当成这次的结果。页面状态与同源 API 都作为依据，两者取更可靠者。
        # 先确认这次点击真的登记了新一轮运行：run_id 必须变化，否则不能把上一次的
        # 终态当成这次的结果（未配置模型时一轮只需几百毫秒，界面可能来不及显示「进行中」）。
        deadline = time.time() + args.timeout_seconds
        observed_at = time.time()
        started = False
        terminal = ""
        page_blob = ""
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            page_blob = " | ".join(t.strip() for t in page.get_by_role("status").all_inner_texts() if t.strip())
            server = read_run_summary(page, args.case_id)
            server_status = str(server.get("status", ""))
            if str(server.get("run_id", "")) and str(server.get("run_id", "")) != str(before.get("run_id", "")):
                started = True
            if started and server_status not in ("RUNNING", "NOT_RUN", ""):
                terminal = server_status
                break
            if not started and time.time() - observed_at > 90:
                break  # 90 秒内没有登记新一轮：判定没有真正启动，立即报告
        after = read_run_summary(page, args.case_id)
        step("分析终态", 页面=page_blob, 服务端=after)
        report["run_started"] = started
        report["run_before"] = before
        report["run_after"] = after
        report["terminal_status"] = terminal or after.get("status", "")
        if not started:
            report["alerts"] = [a.strip() for a in page.get_by_role("alert").all_inner_texts() if a.strip()]
            report["outcome"] = "REVIEW"
            step("检查未通过", reason="未观察到本次分析进入运行态，无法确认结果属于本次运行",
                 alerts=report["alerts"])
            snapshot(page, "03-final")
            browser.close()
            return 1
        expected = {item.strip() for item in str(args.expect_status).split(",") if item.strip()}
        if report["terminal_status"] not in expected:
            report["outcome"] = "REVIEW"
            step("检查未通过",
                 reason=f"本次分析终态为 {report['terminal_status']}，不在允许的终态 {sorted(expected)} 内")
            snapshot(page, "03-final")
            browser.close()
            return 1
        snapshot(page, "03-final")
        passed = finish(page, args, report, step, final=page_blob)
        if args.brief:
            passed = check_brief_page(page, args, report, step) and passed
        if args.json_out is not None:
            args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        browser.close()
        return passed


def check_brief_page(page, args, report, step) -> bool:
    """只读检查答辩状页面：状态、正文与导出按钮是否真的可用。"""
    page.goto(f"{args.web_origin}/brief?case={args.case_id}",
              wait_until="domcontentloaded", timeout=120_000)
    page.get_by_role("button", name="保存并生成草稿").wait_for(timeout=180_000)
    page.wait_for_timeout(2_000)
    status = " | ".join(t.strip() for t in page.get_by_role("status").all_inner_texts() if t.strip())
    state = page.evaluate("""async (caseId) => {
      const payload = await (await fetch(`/api/local/v1/cases/${caseId}/brief`)).json();
      return payload.state;
    }""", args.case_id)
    report["brief_state"] = state
    report["brief_page_status"] = status
    exports = [b.strip() for b in page.get_by_role("button").all_inner_texts() if "导出" in b]
    report["brief_export_buttons"] = exports
    step("答辩状页面", 状态=status, 服务端=state, 导出按钮=exports)
    if state.get("markdown_available") and not exports:
        step("检查未通过", reason="服务端有草稿但页面没有导出按钮")
        return False
    if state.get("stale") and exports:
        step("检查未通过", reason="草稿已失效但页面仍显示导出按钮")
        return False
    return True


def read_run_summary(page, case_id: str) -> dict:
    """通过同源 API 读取运行记录，用于确认本次分析真的产生了新的模型调用。"""
    script = """async (caseId) => {
      const response = await fetch(`/api/local/v1/cases/${caseId}/analysis`);
      if (!response.ok) return { error: `HTTP ${response.status}` };
      const payload = await response.json();
      const agent = payload.agent ?? {};
      return { status: String(agent.status ?? ""), calls: Number(agent.calls ?? 0),
               cost: String(agent.cost_cny ?? "0"), progress: Number(agent.progress ?? 0),
               run_id: String(agent.run_id ?? ""), started_at: String(agent.started_at ?? "") };
    }"""
    try:
        return page.evaluate(script, case_id)
    except Exception as error:  # noqa: BLE001 - 自检脚本报告而不中断
        return {"error": str(error)}


def finish(page, args, report, step, final: str = "") -> int:
    """核对正式数字、状态与导出，并给出退出码。"""
    ok = True
    numbers = page.locator("section[aria-label='正式数字']")
    engine_text = numbers.inner_text() if numbers.count() else ""
    report["engine_numbers"] = engine_text
    step("正式数字", text=engine_text or "（未填写案件计算参数，本次不产出正式数字）")
    if not engine_text and args.run and args.parameters is not None:
        ok = False
        step("检查未通过", reason="已提供计算参数，但页面没有正式数字")

    exports = [b.strip() for b in page.get_by_role("button").all_inner_texts() if "导出" in b]
    report["export_buttons"] = exports
    step("导出按钮", buttons=exports)

    downloads = []
    for label, suffix in (("导出 Markdown", "md"), ("导出 Word", "docx")):
        button = page.get_by_role("button", name=label)
        if button.count() == 0:
            downloads.append({"label": label, "ok": False, "reason": "按钮不存在"})
            # 只读自检时没有报告是可接受的（尚未运行分析）；运行后缺失才算失败。
            if args.run:
                ok = False
            continue
        try:
            with page.expect_download(timeout=90_000) as info:
                button.click()
            download = info.value
            entry: dict[str, object] = {"label": label, "ok": True,
                                       "suggested_name": download.suggested_filename}
            if args.screenshot_dir is not None:
                target = args.screenshot_dir / f"export.{suffix}"
                download.save_as(str(target))
                entry["bytes"] = target.stat().st_size
            downloads.append(entry)
        except Exception as error:  # noqa: BLE001 - 自检脚本要报告而不是中断
            downloads.append({"label": label, "ok": False, "reason": str(error)})
            ok = False
    report["downloads"] = downloads
    step("导出结果", results=downloads)
    report["outcome"] = "OK" if ok else "REVIEW"
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
