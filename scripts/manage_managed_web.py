#!/usr/bin/env python3
"""Administrator entry for the firm-managed, single-node Web deployment.

This command is intentionally separate from ``start_local_web.py``.  The
local command is an offline development aid; this command is the only bundled
entry that may start the full browser service composition.  It never silently
falls back to SQLite or setup-gated mode when ``start`` is requested.
"""

from __future__ import annotations

import argparse
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from typing import Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment" / "web"
ENV_TEMPLATE = DEPLOYMENT / ".env.example"
DEFAULT_ENV = DEPLOYMENT / ".env"
BACKEND = ROOT / "backend"
MIGRATIONS = BACKEND / "migrations"
_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_MIGRATION = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PLACEHOLDER_MARKERS = (
    "REPLACE_WITH_",
    "replace-with-",
    "replace@",
    ".example",
    "00000000-0000-4000-8000-00000000000",
)
_MANAGED_BUILD_HEADROOM_BYTES = 5 * 1024**3


@dataclass(frozen=True)
class AdminCheck:
    name: str
    ready: bool
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="律师办案工作台：管理员完整 Web 服务入口",
    )
    parser.add_argument(
        "command",
        choices=("init", "preflight", "start", "stop", "status"),
        help="init 准备配置；preflight 预检；start 启动；stop 停止；status 查看容器状态",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出预检结果")
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()

    if args.command == "init":
        return initialize_configuration(env_file)
    if args.command == "preflight":
        checks = preflight(env_file)
        print_checks(checks, as_json=args.json)
        return 0 if _ready_to_start(checks) else 2
    if args.command == "start":
        return start_managed_web(env_file)
    if args.command == "stop":
        return compose(env_file, "down")
    return compose(env_file, "ps")


def initialize_configuration(env_file: Path) -> int:
    if env_file.exists():
        print(f"配置文件已存在，未覆盖：{env_file}")
        print("下一步：由律所管理员补齐其中的身份、数据库、对象存储和 Agent 服务配置，再运行 preflight。")
        return 0
    if not ENV_TEMPLATE.is_file():
        print("部署配置模板不存在，初始化已停止。", file=sys.stderr)
        return 2
    env_file.parent.mkdir(parents=True, exist_ok=True)
    text = ENV_TEMPLATE.read_text(encoding="utf-8")
    # These credentials belong only to bundled private data/render services.
    # External OIDC/database/object-store credentials remain explicit blanks or
    # placeholders for the administrator; the wizard never invents authority.
    text = text.replace(
        "REPLACE_WITH_A_UNIQUE_32_CHARACTER_OR_LONGER_SECRET",
        secrets.token_urlsafe(48),
    ).replace(
        "REPLACE_WITH_A_DIFFERENT_UNIQUE_32_CHARACTER_OR_LONGER_SECRET",
        secrets.token_urlsafe(48),
    ).replace(
        "REPLACE_WITH_UNPADDED_BASE64URL_OF_AT_LEAST_32_RANDOM_BYTES",
        secrets.token_urlsafe(48),
    )
    _write_private_new_file(env_file, text)
    print(f"已建立仅管理员可读的配置草稿：{env_file}")
    print("尚未启动服务。请补齐律所域名、OIDC/MFA、三套数据库身份、私有对象存储和 Agent Worker 配置。")
    print(f"完成后运行：{sys.executable} {Path(__file__).resolve()} preflight --env-file {env_file}")
    return 0


def preflight(env_file: Path) -> tuple[AdminCheck, ...]:
    values, parse_error = load_env_file(env_file)
    checks: list[AdminCheck] = [
        AdminCheck("管理员配置", parse_error is None, parse_error or "配置文件格式可读取，且不会在报告中显示秘密。"),
    ]
    if parse_error is not None:
        return tuple(checks)

    docker_ready = shutil.which("docker") is not None
    checks.extend((
        AdminCheck(
            "运行方式",
            values.get("LAWCASE_WEB_RUNTIME_MODE") == "PRODUCTION_WEB",
            "已选择完整受管 Web 服务。" if values.get("LAWCASE_WEB_RUNTIME_MODE") == "PRODUCTION_WEB" else "仍是安全准备状态；完整服务不会启动。",
        ),
        AdminCheck(
            "公开入口",
            _configured(values, "LAWCASE_PUBLIC_HOST", "LAWCASE_WEB_PUBLIC_ORIGIN"),
            "律所域名与 HTTPS 来源已填写。" if _configured(values, "LAWCASE_PUBLIC_HOST", "LAWCASE_WEB_PUBLIC_ORIGIN") else "请填写律所域名和 HTTPS 公开来源。",
        ),
        _section_check(values, "OIDC / MFA", (
            "LAWCASE_WEB_OIDC_ISSUER", "LAWCASE_WEB_OIDC_AUTHORIZATION_ENDPOINT",
            "LAWCASE_WEB_OIDC_TOKEN_ENDPOINT", "LAWCASE_WEB_OIDC_JWKS_URL",
            "LAWCASE_WEB_OIDC_CLIENT_ID", "LAWCASE_WEB_OIDC_CLIENT_SECRET",
            "LAWCASE_WEB_OIDC_AUDIENCE",
        )),
        _section_check(values, "案件数据库身份", (
            "LAWCASE_WEB_APP_POSTGRES_DSN", "LAWCASE_WEB_APP_DATABASE_ROLE",
            "LAWCASE_WEB_IDENTITY_DIRECTORY_POSTGRES_DSN", "LAWCASE_WEB_IDENTITY_DIRECTORY_DATABASE_ROLE",
            "LAWCASE_WEB_SESSION_GATEWAY_POSTGRES_DSN", "LAWCASE_WEB_SESSION_GATEWAY_DATABASE_ROLE",
        )),
        _section_check(values, "私有案卷库", (
            "LAWCASE_WEB_OBJECT_STORE_ENDPOINT", "LAWCASE_WEB_OBJECT_STORE_BUCKET",
            "LAWCASE_WEB_OBJECT_STORE_ACCESS_KEY_ID", "LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY",
            "LAWCASE_WEB_OBJECT_STORE_ENCRYPTION",
        )),
        _section_check(values, "办案 Agent Worker", (
            "LAWCASE_AGENT_WORKER_RUNTIME_MODE", "LAWCASE_AGENT_WORKER_FIRM_ID",
            "LAWCASE_AGENT_WORKER_ACTOR_ID", "LAWCASE_AGENT_WORKER_POSTGRES_DSN",
            "LAWCASE_AGENT_VERIFIER_ACTOR_ID", "LAWCASE_AGENT_VERIFIER_POSTGRES_DSN",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT", "LAWCASE_AGENT_WORKER_OBJECT_STORE_BUCKET",
            "LAWCASE_AGENT_WORKER_OBJECT_STORE_ACCESS_KEY_ID", "LAWCASE_AGENT_WORKER_OBJECT_STORE_SECRET_ACCESS_KEY",
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY", "LAWCASE_AGENT_WORKER_DEEPSEEK_MODEL",
        )),
        _document_renderer_check(values),
        _migration_check(),
        _managed_build_headroom_check(),
        AdminCheck("容器运行环境", docker_ready, "Docker 已安装。" if docker_ready else "Docker 未安装。"),
    ))

    # Compose validation is meaningful only after every required deployment
    # section is present.  Avoid executing it against intentionally incomplete
    # drafts, where shell interpolation errors add noise but no new diagnosis.
    configuration_ready = all(item.ready for item in checks)
    if docker_ready and configuration_ready:
        compose_command = [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(DEPLOYMENT / "compose.yaml"),
            "--profile",
            "agent-worker",
        ]
        if _document_delivery_enabled(values):
            compose_command.extend(("--profile", "document-delivery"))
        compose_command.extend(("config", "--quiet"))
        result = subprocess.run(
            compose_command,
            cwd=DEPLOYMENT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        checks.append(AdminCheck(
            "容器编排",
            result.returncode == 0,
            "容器编排结构通过静态检查。" if result.returncode == 0 else "容器编排未通过；请由管理员核对配置，未启动任何案件服务。",
        ))
    elif not docker_ready:
        checks.append(AdminCheck("容器编排", False, "未安装 Docker Compose，未启动任何服务。"))
    else:
        checks.append(AdminCheck("容器编排", False, "先补齐上面的管理员配置，再检查容器编排；未启动任何服务。"))
    checks.append(AdminCheck(
        "商用放行",
        False,
        "预检不能替代真实 OIDC 登录、数据库迁移/恢复、对象库恢复、恶意文件和 Windows/macOS 浏览器全流程验收。",
    ))
    return tuple(checks)


def start_managed_web(env_file: Path) -> int:
    checks = preflight(env_file)
    blocking = tuple(item for item in checks if not item.ready and item.name != "商用放行")
    if blocking:
        print_checks(checks, as_json=False)
        print("完整服务未启动：请先处理上面的管理员配置项。", file=sys.stderr)
        return 2
    values, parse_error = load_env_file(env_file)
    if parse_error is not None:
        print("完整服务未启动：管理员配置在启动前变得不可读取。", file=sys.stderr)
        return 2
    profiles = ["agent-worker"]
    if _document_delivery_enabled(values):
        profiles.append("document-delivery")
    result = compose(
        env_file,
        "up",
        "--build",
        "-d",
        profiles=tuple(profiles),
    )
    if result != 0:
        return result
    print("完整单机 Web 组合已提交启动；这不是商用放行结论。")
    print("下一步由管理员核对 /readyz，再使用非敏感案件完成登录、Agent、文书和恢复验收。")
    return 0


def compose(
    env_file: Path,
    *arguments: str,
    profiles: tuple[str, ...] = (),
) -> int:
    if not env_file.is_file():
        print(f"管理员配置不存在：{env_file}。请先运行 init。", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("未安装 Docker Compose；没有启动或停止任何服务。", file=sys.stderr)
        return 2
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(DEPLOYMENT / "compose.yaml")]
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(arguments)
    return subprocess.run(command, cwd=DEPLOYMENT, check=False).returncode


def load_env_file(path: Path) -> tuple[dict[str, str], str | None]:
    if not path.is_file():
        return {}, f"配置文件不存在：{path}。请先运行 init。"
    values: dict[str, str] = {}
    try:
        for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                return {}, f"配置第 {index} 行格式不正确。"
            key, value = line.split("=", 1)
            key = key.strip()
            if not _KEY.fullmatch(key) or key in values:
                return {}, f"配置第 {index} 行的字段名无效或重复。"
            values[key] = value.strip()
    except OSError:
        return {}, "配置文件无法安全读取。"
    return values, None


def print_checks(checks: tuple[AdminCheck, ...], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "schema_version": "lawcase-managed-web-admin-preflight-v1",
            "ready_to_start": _ready_to_start(checks),
            "commercial_release_authorized": False,
            "checks": [{"name": item.name, "status": "READY" if item.ready else "BLOCKED", "message": item.message} for item in checks],
        }, ensure_ascii=False, indent=2))
        return
    for item in checks:
        print(f"[{'可用' if item.ready else '待处理'}] {item.name}：{item.message}")


def _ready_to_start(checks: tuple[AdminCheck, ...]) -> bool:
    return all(item.ready or item.name == "商用放行" for item in checks)


def _managed_build_headroom_check(*, free_bytes: int | None = None) -> AdminCheck:
    """Block a production ``up --build`` before it can destabilise the host."""

    try:
        available = shutil.disk_usage(ROOT).free if free_bytes is None else free_bytes
    except OSError:
        return AdminCheck(
            "完整重建磁盘余量",
            False,
            "无法核验宿主可用空间；完整 Web 服务不会停止旧容器或开始构建。",
        )
    if type(available) is not int or available < 0:
        return AdminCheck(
            "完整重建磁盘余量",
            False,
            "宿主可用空间读数无效；完整 Web 服务不会停止旧容器或开始构建。",
        )
    ready = available >= _MANAGED_BUILD_HEADROOM_BYTES
    available_gib = available / 1024**3
    required_gib = _MANAGED_BUILD_HEADROOM_BYTES / 1024**3
    return AdminCheck(
        "完整重建磁盘余量",
        ready,
        (
            f"宿主可用空间 {available_gib:.1f}GiB，满足完整镜像重建所需的 {required_gib:.0f}GiB 余量。"
            if ready
            else (
                f"宿主可用空间仅 {available_gib:.1f}GiB，低于完整镜像重建所需的 "
                f"{required_gib:.0f}GiB；完整 Web 服务不会停止旧容器或开始构建。"
            )
        ),
    )


def _configured(values: Mapping[str, str], *keys: str) -> bool:
    return all(_is_real_value(values.get(key, "")) for key in keys)


def _is_real_value(value: str) -> bool:
    return bool(value) and not any(marker.lower() in value.lower() for marker in _PLACEHOLDER_MARKERS)


def _section_check(values: Mapping[str, str], name: str, keys: tuple[str, ...]) -> AdminCheck:
    ready = _configured(values, *keys)
    return AdminCheck(name, ready, f"{name}配置已填写。" if ready else f"{name}仍有空值或模板占位符。")


def _document_delivery_enabled(values: Mapping[str, str]) -> bool:
    return any(
        values.get(name, "false") == "true"
        for name in (
            "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED",
            "LAWCASE_WEB_DOCUMENT_WORKER_ENABLED",
        )
    )


def _document_renderer_check(values: Mapping[str, str]) -> AdminCheck:
    flags = {
        "Agent 文书交付": values.get(
            "LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED", "false"
        ),
        "Web 文书候选": values.get("LAWCASE_WEB_DOCUMENT_WORKER_ENABLED", "false"),
    }
    if any(flag not in {"", "false", "true"} for flag in flags.values()):
        return AdminCheck(
            "隔离文书渲染",
            False,
            "文书交付开关只能是 true 或 false。",
        )
    agent_enabled = flags["Agent 文书交付"] == "true"
    web_enabled = flags["Web 文书候选"] == "true"
    if not agent_enabled and not web_enabled:
        return AdminCheck(
            "隔离文书渲染",
            True,
            "动态 Word/Excel 交付与 Web 文书候选均未启用；系统不会登记该能力。",
        )
    legacy = (
        values.get("LAWCASE_AGENT_WORKER_DOCUMENT_SOFFICE_EXECUTABLE", ""),
        values.get("LAWCASE_AGENT_WORKER_DOCUMENT_PDFTOPPM_EXECUTABLE", ""),
    )
    if any(legacy):
        return AdminCheck(
            "隔离文书渲染",
            False,
            "生产 Agent Worker 不能配置进程内 LibreOffice 或 PDF 转换路径。",
        )
    secret = values.get("LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET", "")
    try:
        client_timeouts: list[int] = []
        if agent_enabled:
            _validate_internal_renderer_endpoint(
                values.get("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT", "")
            )
            client_timeouts.append(
                int(
                    values.get(
                        "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_TIMEOUT_SECONDS",
                        "180",
                    )
                )
            )
        if web_enabled:
            _validate_internal_renderer_endpoint(
                values.get("LAWCASE_WEB_DOCUMENT_RENDERER_ENDPOINT", "")
            )
            client_timeouts.append(
                int(
                    values.get("LAWCASE_WEB_DOCUMENT_RENDERER_TIMEOUT_SECONDS", "180")
                )
            )
        decoded = urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        if (
            not 32 <= len(decoded) <= 128
            or urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != secret
        ):
            raise ValueError
        server_timeout = int(
            values.get("LAWCASE_DOCUMENT_RENDERER_TIMEOUT_SECONDS", "180")
        )
        clock_skew = int(
            values.get("LAWCASE_DOCUMENT_RENDERER_MAX_CLOCK_SKEW_SECONDS", "60")
        )
        replay_capacity = int(
            values.get("LAWCASE_DOCUMENT_RENDERER_REPLAY_CAPACITY", "20000")
        )
        if (
            not client_timeouts
            or any(not 10 <= client_timeout <= 180 for client_timeout in client_timeouts)
            or not 10 <= server_timeout <= 300
            or not 10 <= clock_skew <= 300
            or not 1_000 <= replay_capacity <= 100_000
        ):
            raise ValueError
    except (ValueError, TypeError, OverflowError, UnicodeError, BinasciiError):
        return AdminCheck(
            "隔离文书渲染",
            False,
            "动态文书已启用，但内部地址、共享密钥或安全边界参数不完整。",
        )
    return AdminCheck(
        "隔离文书渲染",
        True,
        "内部渲染服务配置完整；启动时仍须通过真实可执行程序与 HMAC 健康预检。",
    )


def _validate_internal_renderer_endpoint(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "http"
        or not parts.hostname
        or parts.port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError


def _migration_check() -> AdminCheck:
    sequences = sorted(
        int(match.group(1))
        for item in MIGRATIONS.iterdir()
        if item.is_file() and (match := _MIGRATION.fullmatch(item.name)) is not None
    )
    if not sequences:
        return AdminCheck("数据库迁移链", False, "未找到数据库迁移文件。")
    missing = sorted(set(range(1, sequences[-1] + 1)).difference(sequences))
    if missing:
        return AdminCheck("数据库迁移链", False, "迁移编号不连续；未启动服务。")
    return AdminCheck("数据库迁移链", True, f"代码包含 0001—{sequences[-1]:04d} 连续迁移；目标数据库是否已应用仍须单独留证。")


def _write_private_new_file(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
