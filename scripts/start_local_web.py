#!/usr/bin/env python3
"""Start the offline development slice of the cross-platform Web workbench.

The local mode intentionally starts only a loopback FastAPI process and the
existing Next.js browser shell.  It does not require Docker, OIDC,
PostgreSQL, object storage, ClamAV, Poppler, or LibreOffice.
"""

from __future__ import annotations

import argparse

import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import IO
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
WEB = ROOT / "web"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="律师办案工作台（本机模式）：一条命令启动 API 与浏览器界面。"
    )
    parser.add_argument(
        "--enable-model",
        action="store_true",
        help="启用模型深度分析（默认关闭；开启后使用项目默认模型环境文件）。",
    )
    parser.add_argument(
        "--model-env-file",
        type=Path,
        default=None,
        help="指定模型密钥环境文件；设置后自动启用模型分析。",
    )
    parser.add_argument(
        "--budget-cny",
        type=float,
        default=2.0,
        help="每案分析成本上限（元），默认 2 元。",
    )
    arguments = parser.parse_args()
    env = os.environ.copy()
    if arguments.model_env_file is not None:
        env["CASE_WORKBENCH_MODEL_ENV_FILE"] = str(arguments.model_env_file.expanduser())
    elif arguments.enable_model:
        env["CASE_WORKBENCH_ENABLE_MODEL"] = "1"
    env["CASE_WORKBENCH_MODEL_BUDGET_CNY"] = str(arguments.budget_cny)
    configured_python = env.get("LAWCASE_PYTHON", "").strip()
    venv_python = (
        BACKEND / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else BACKEND / ".venv" / "bin" / "python"
    )
    python = _resolve_executable(
        configured_python or (str(venv_python) if venv_python.exists() else sys.executable)
    )
    if python is None:
        print("找不到本地 Web 使用的 Python。", file=sys.stderr)
        return 2
    pnpm = _resolve_executable("pnpm")
    if pnpm is None:
        print("找不到 pnpm；请先安装项目所需的 Node.js 和 pnpm。", file=sys.stderr)
        return 2
    env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env.setdefault("CASE_WORKBENCH_LOCAL_WEB_ROOT", str(_default_root()))
    try:
        api_port = _resolve_port(env, "LAWCASE_LOCAL_API_PORT", preferred=38787)
        web_port = _resolve_port(env, "LAWCASE_LOCAL_WEB_PORT", preferred=33000, excluded={api_port})
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    api_log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    frontend_log = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    processes: list[tuple[str, subprocess.Popen[bytes], IO[str]]] = []
    try:
        api = subprocess.Popen(
            [python, "-m", "uvicorn", "case_api.local_web:create_local_web_app", "--factory", "--host", "127.0.0.1", "--port", str(api_port), "--no-access-log"],
            cwd=ROOT,
            env=env,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(("本地 API", api, api_log))
        frontend_env = env.copy()
        frontend_env["LAWCASE_LOCAL_API_ORIGIN"] = f"http://127.0.0.1:{api_port}"
        frontend_env["NEXT_PUBLIC_WEB_API_PREFIX"] = "/api/local/v1"
        frontend = subprocess.Popen(
            [pnpm, "dev", "--hostname", "127.0.0.1", "--port", str(web_port)],
            cwd=WEB,
            env=frontend_env,
            stdout=frontend_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(("浏览器前端", frontend, frontend_log))
        api_ready = _wait_for_url(f"http://127.0.0.1:{api_port}/healthz", api, timeout_seconds=20)
        web_ready = _wait_for_url(f"http://127.0.0.1:{web_port}/", frontend, timeout_seconds=30)
        if not api_ready or not web_ready:
            failed = "本地 API" if not api_ready else "浏览器前端"
            print(f"本地 Web 启动失败：{failed}未能在限定时间内就绪。", file=sys.stderr)
            _print_log_tail(api_log if not api_ready else frontend_log)
            return 1
        url = f"http://127.0.0.1:{web_port}"
        print(f"律师办案工作台（离线开发模式）已启动： {url}", flush=True)
        print(f"开发数据目录：{env['CASE_WORKBENCH_LOCAL_WEB_ROOT']}", flush=True)
        model_state = "已启用模型深度分析" if (
            env.get("CASE_WORKBENCH_MODEL_ENV_FILE") or env.get("CASE_WORKBENCH_ENABLE_MODEL") == "1"
        ) else "未启用模型（仅确定性材料核对与正式数字）"
        print(f"模型状态：{model_state}；每案分析上限 ¥{env.get('CASE_WORKBENCH_MODEL_BUDGET_CNY', '2')}。", flush=True)
        print("该入口为本机单用户模式；律所多人协作由管理员在服务器启动完整服务。", flush=True)
        if env.get("LAWCASE_OPEN_BROWSER", "1") == "1":
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                print("未能自动打开浏览器；请手动打开上方入口。", file=sys.stderr)
        while True:
            stopped = next(
                ((name, log) for name, process, log in processes if process.poll() is not None),
                None,
            )
            if stopped is not None:
                name, log = stopped
                print(f"本地 Web 已停止：{name}意外退出。", file=sys.stderr)
                _print_log_tail(log)
                return 1
            time.sleep(0.5)
    except OSError as error:
        print(f"本地 Web 启动失败：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("正在关闭本地 Web…", flush=True)
        return 0
    finally:
        for _name, process, _log in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for _name, process, _log in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        api_log.close()
        frontend_log.close()


def _resolve_executable(command: str) -> str | None:
    """Resolve absolute, relative, and PATH-based commands consistently."""

    candidate = Path(command).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    return shutil.which(command)


def _print_log_tail(log: IO[str], *, lines: int = 20) -> None:
    try:
        log.flush()
        log.seek(0)
        tail = log.readlines()[-lines:]
    except (OSError, ValueError):
        return
    if not tail:
        return
    print("诊断信息：", file=sys.stderr)
    for line in tail:
        print(line.rstrip(), file=sys.stderr)


def _default_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / "LawcaseWorkbench" / "local-web"


def _resolve_port(
    env: dict[str, str],
    variable: str,
    *,
    preferred: int,
    excluded: set[int] | None = None,
) -> int:
    """Resolve an explicit port strictly, or choose a free local default.

    Port 3000 is intentionally not the default: it is commonly occupied by an
    unrelated Next.js product and previously caused lawyers to open the wrong
    application while the launcher claimed success.
    """

    excluded = excluded or set()
    configured = env.get(variable, "").strip()
    if configured:
        try:
            port = int(configured)
        except ValueError as error:
            raise ValueError(f"{variable} 必须是有效端口号。") from error
        if not 1024 <= port <= 65535:
            raise ValueError(f"{variable} 必须在 1024—65535 之间。")
        if port in excluded or not _port_is_free(port):
            raise ValueError(f"{variable} 指定的端口 {port} 已被占用；请换一个端口后重试。")
        return port

    for port in range(preferred, min(preferred + 100, 65536)):
        if port not in excluded and _port_is_free(port):
            return port
    raise ValueError(f"未找到可用的本机端口（从 {preferred} 开始检查了 100 个端口）。")


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_for_url(url: str, process: subprocess.Popen[bytes], *, timeout_seconds: float) -> bool:
    # Readiness is always a loopback probe.  urllib's default opener inherits
    # OS and environment proxy settings; on macOS in particular, 127.0.0.1 can
    # be sent to a configured HTTP proxy when the system bypass list is empty.
    # That made healthy local services look unavailable and the launcher's
    # cleanup then correctly (but unexpectedly) stopped them.  A private opener
    # with an explicit empty proxy map keeps the probe local on every platform.
    direct_opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with direct_opener.open(url, timeout=1) as response:  # nosec: loopback-only readiness probe
                if 200 <= response.status < 500:
                    return True
        except (OSError, URLError):
            time.sleep(0.25)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
