#!/usr/bin/env python3
"""Build and operate the isolated, production-gated local managed Alpha.

This entrypoint never changes /etc/hosts, the macOS Keychain, or the production
deployment under ``deployment/web``.  It creates a short-lived private CA and
synthetic credentials under the ignored ``runtime`` directory, while real
provider credentials remain an explicit fail-closed administrator input.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from getpass import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal as process_signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from typing import Iterable, Mapping
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment" / "local-managed-test"
TEMPLATE = DEPLOYMENT / ".env.example"
COMPOSE_FILE = DEPLOYMENT / "compose.yaml"
RUNTIME = DEPLOYMENT / "runtime"
ENV_FILE = RUNTIME / "local-managed.env"
TLS = RUNTIME / "tls"
KES_CONFIG = RUNTIME / "kes" / "config.yml"
KEYCLOAK_REALM = RUNTIME / "keycloak" / "realm.json"
KEYCLOAK_CONTRACT = RUNTIME / "keycloak" / "oidc-contract.json"
FRONTEND_DOCKERFILE = ROOT / "deployment" / "web" / "frontend.Dockerfile"
LOGIN_FILE = RUNTIME / "TEST_LOGIN.txt"
TRUST_FILE = RUNTIME / "TRUST_CA_MANUALLY.txt"
CA_CERT = TLS / "ca.crt"
CA_KEY = TLS / "ca.key"
MIGRATIONS = ROOT / "backend" / "migrations"
KEYCLOAK_RENDERER = DEPLOYMENT / "keycloak" / "render_realm.py"
KEYCLOAK_VERIFIER = DEPLOYMENT / "keycloak" / "verify_realm.py"

_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_MIGRATION = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PLACEHOLDERS = ("replace_with_", "replace-with-", ".example", "your-key")
_PRIVATE_CERT_KEYS = (
    "ca.key",
    "edge.key",
    "postgres.key",
    "minio.key",
    "kes-server.key",
    "kes-client.key",
)
_REQUIRED_PROVIDER_KEYS = (
    "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY",
    "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY",
    "LAWCASE_AGENT_WORKER_QWEN_API_KEY",
    "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY",
)
_PINNED_IMAGE_KEYS = (
    "LAWCASE_CADDY_IMAGE",
    "LAWCASE_NODE_IMAGE",
    "LAWCASE_PYTHON_IMAGE",
    "LAWCASE_POSTGRES_IMAGE",
    "LAWCASE_API_IMAGE",
    "LAWCASE_MINIO_IMAGE",
    "LAWCASE_MC_IMAGE",
    "LAWCASE_KES_IMAGE",
    "LAWCASE_KEYCLOAK_IMAGE",
    "LAWCASE_KEYCLOAK_RUNTIME_IMAGE",
)
_ONE_SHOT_SERVICES = (
    "trust-bundle-bootstrap",
    "clamav-runtime-bootstrap",
    "clamav-signatures",
    "clamav-probe",
    "document-renderer-probe",
    "postgres-migrate",
    "postgres-assert",
    "postgres-tenant-probe",
    "kes-runtime-bootstrap",
    "kes-ready-probe",
    "object-storage-runtime-bootstrap",
    "object-storage-bootstrap",
    "object-storage-sse-probe",
    "provider-preflight",
)
_START_BARRIER_CONSUMERS = (
    "gateway",
    "api",
    "case-agent-worker",
    "frontend",
    "document-renderer",
    "keycloak",
    "clamd",
)
_LOCAL_BUILD_SERVICES = ("api", "keycloak", "document-renderer", "frontend")
_PERSISTENT_RECREATE_SERVICES = (
    "gateway",
    "api",
    "case-agent-worker",
    "frontend",
    "document-renderer",
    "keycloak",
    "clamd",
    "object-storage",
    "kes",
    "postgres",
)
_MANAGED_BUILD_HEADROOM_BYTES = 5 * 1024**3


@dataclass(frozen=True)
class Check:
    name: str
    ready: bool
    message: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="律师办案工作台：本机受管完整测试环境",
    )
    parser.add_argument(
        "command",
        choices=(
            "init",
            "configure-providers",
            "import-keychain-providers",
            "preflight",
            "start",
            "resume-provider-gate",
            "verify",
            "status",
            "stop",
            "login-info",
            "trust-info",
        ),
    )
    parser.add_argument("--json", action="store_true", help="用 JSON 输出检查结果")
    parser.add_argument(
        "--qwen-workspace-id",
        help="Qwen Workspace ID（非密钥；仅用于 Keychain 导入命令）",
    )
    args = parser.parse_args()

    if args.command == "init":
        return initialize()
    if args.command == "configure-providers":
        return configure_providers()
    if args.command == "import-keychain-providers":
        return import_keychain_providers(args.qwen_workspace_id)
    if args.command == "preflight":
        checks = preflight()
        print_checks(checks, as_json=args.json)
        return 0 if all(item.ready for item in checks) else 2
    if args.command == "start":
        checks = preflight()
        if not all(item.ready for item in checks):
            print_checks(checks, as_json=False)
            print("本机受管环境未启动：完整门禁尚未通过。", file=sys.stderr)
            return 2
        result = start_managed_stack()
        if result == 0:
            print("本机受管组合的分阶段启动与一次性安全门已全部通过；请运行 verify。")
        return result
    if args.command == "resume-provider-gate":
        checks = preflight()
        if not all(item.ready for item in checks):
            print_checks(checks, as_json=False)
            print("本机受管环境未续跑：静态门禁已变化。", file=sys.stderr)
            return 2
        return resume_after_provider_gate()
    if args.command == "verify":
        checks = verify_running_stack()
        print_checks(checks, as_json=args.json, scope="INFRA_ONLY")
        if not args.json:
            print(
                "[INFRA_ONLY] 此命令不冒充真实用户验收；仍须在浏览器登录会话中断言 "
                "can_run_case_agent、can_run_agent_ledger_extraction，并完成上传、执行与文书交付。"
            )
        return 0 if all(item.ready for item in checks) else 2
    if args.command == "status":
        return compose("ps")
    if args.command == "stop":
        return compose("down")
    if args.command == "login-info":
        return show_private_note(LOGIN_FILE, label="测试登录说明")
    return show_private_note(TRUST_FILE, label="本机 CA 信任说明")


def initialize() -> int:
    if ENV_FILE.exists():
        try:
            values = _load_env(ENV_FILE)
            template_values = _load_env(TEMPLATE)
            for key in _PINNED_IMAGE_KEYS:
                values[key] = template_values[key]
            _write_private_atomic(ENV_FILE, _serialize_env(values))
            _ensure_private_directory(RUNTIME / "keycloak")
            _render_keycloak_realm()
        except Exception:
            print("已有私有配置未覆盖，但 OIDC 派生文件刷新失败。", file=sys.stderr)
            return 2
        print(f"本机受管配置已存在，未覆盖：{ENV_FILE}")
        print("已按当前固定模板重新派生并验证 OIDC realm/contract。")
        print("如尚未设置真实供应商凭据，下一步运行 configure-providers。")
        return 0
    if not TEMPLATE.is_file():
        print("本机受管配置模板缺失。", file=sys.stderr)
        return 2
    if shutil.which("openssl") is None:
        print("缺少 OpenSSL，未生成任何证书或配置。", file=sys.stderr)
        return 2

    if RUNTIME.exists() and any(RUNTIME.iterdir()):
        print(f"发现没有完整配置的非空 runtime，未覆盖：{RUNTIME}", file=sys.stderr)
        return 2

    _ensure_private_directory(RUNTIME)
    _ensure_private_directory(TLS)
    _ensure_private_directory(RUNTIME / "kes")
    _ensure_private_directory(RUNTIME / "keycloak")

    generated = {
        "LAWCASE_POSTGRES_SUPERUSER_PASSWORD": _secret(),
        "LAWCASE_POSTGRES_MIGRATOR_PASSWORD": _secret(),
        "LAWCASE_WEB_APP_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_WEB_IDENTITY_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_WEB_SESSION_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_AGENT_WORKER_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_AGENT_VERIFIER_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_KEYCLOAK_POSTGRES_PASSWORD": _secret(),
        "LAWCASE_KEYCLOAK_ADMIN_PASSWORD": _secret(),
        "LAWCASE_TEST_LEAD_PASSWORD": _secret(),
        "LAWCASE_OIDC_CLIENT_SECRET": _secret(),
        "LAWCASE_OBJECT_ROOT_PASSWORD": _secret(),
        "LAWCASE_WEB_OBJECT_ACCESS_KEY_ID": f"lcweb{secrets.token_hex(12)}",
        "LAWCASE_WEB_OBJECT_SECRET_ACCESS_KEY": _secret(),
        "LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID": f"lcworker{secrets.token_hex(12)}",
        "LAWCASE_WORKER_OBJECT_SECRET_ACCESS_KEY": _secret(),
        "LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET": secrets.token_urlsafe(48),
    }
    rendered = _render_environment_template(generated)
    previous_umask = os.umask(0o077)
    try:
        _write_private_new_file(ENV_FILE, rendered)
        values = _load_env(ENV_FILE)
        _generate_certificates(values)
        _render_kes_config()
        _render_keycloak_realm()
        _write_operator_notes(values)
    except Exception:
        # This directory was proven empty immediately above and contains only
        # this command's generated output, so a failed initialization cannot
        # leave a half-authoritative credential set for a later start.
        shutil.rmtree(RUNTIME, ignore_errors=True)
        print("本机受管权限材料生成未完成；未启动任何服务。", file=sys.stderr)
        return 2
    finally:
        os.umask(previous_umask)

    print(f"已生成隔离的本机受管配置：{ENV_FILE}")
    print("未修改 /etc/hosts，未信任测试 CA，也未启动容器。")
    print("下一步：运行 configure-providers，安全录入真实 DeepSeek 和 Qwen 凭据。")
    return 0


def configure_providers() -> int:
    try:
        values = _load_env(ENV_FILE)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("凭据仅写入本机 0600 配置，不显示、不写入浏览器或 Git。")
    deepseek = _prompt_secret("DeepSeek API Key（同时用于规划与台账提取）: ")
    qwen = _prompt_secret("Qwen API Key: ")
    workspace = input("Qwen Workspace ID: ").strip()
    if (
        any(not _real(value) or any(character.isspace() for character in value) for value in (deepseek, qwen))
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", workspace) is None
    ):
        print("凭据存在空值、占位符或空白字符，未写入。", file=sys.stderr)
        return 2
    values.update(
        {
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": deepseek,
            "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY": deepseek,
            "LAWCASE_AGENT_WORKER_QWEN_API_KEY": qwen,
            "LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID": workspace,
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": qwen,
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID": workspace,
        }
    )
    _write_private_atomic(ENV_FILE, _serialize_env(values))
    print("真实供应商凭据已写入；值未回显。下一步运行 preflight。")
    return 0


def import_keychain_providers(workspace_argument: str | None) -> int:
    """Copy two explicitly named provider keys out of macOS Keychain.

    The command is read-only with respect to Keychain. Secret bytes are read
    through a captured pipe and written only to the existing 0600 runtime env;
    they are never placed in argv, stdout, stderr, or a shell expression.
    """

    try:
        values = _load_env(ENV_FILE)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if sys.platform != "darwin" or shutil.which("security") is None:
        print("macOS Keychain 读取工具不可用；未修改私有配置。", file=sys.stderr)
        return 2
    try:
        deepseek = _read_keychain_password("deepseek-api-key-v1")
        qwen = _read_keychain_password("qwen-api-key-v1")
    except RuntimeError:
        print("Keychain 中指定的模型凭据不可读；未修改私有配置。", file=sys.stderr)
        return 2
    if any(not _real(value) or any(character.isspace() for character in value) for value in (deepseek, qwen)):
        print("Keychain 凭据格式无效；未修改私有配置。", file=sys.stderr)
        return 2

    supplied_workspace = (
        workspace_argument or os.environ.get("LAWCASE_IMPORT_QWEN_WORKSPACE_ID", "")
    ).strip()
    if supplied_workspace and re.fullmatch(
        r"[a-z0-9][a-z0-9-]{2,62}", supplied_workspace
    ) is None:
        print("Qwen Workspace ID 格式无效；未修改私有配置。", file=sys.stderr)
        return 2
    workspace = (
        supplied_workspace
        or values.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID", "").strip()
    )
    workspace_ready = (
        re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", workspace) is not None
    )
    values.update(
        {
            "LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY": deepseek,
            "LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY": deepseek,
            "LAWCASE_AGENT_WORKER_QWEN_API_KEY": qwen,
            "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY": qwen,
        }
    )
    if workspace_ready:
        values["LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID"] = workspace
        values["LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID"] = workspace
    _write_private_atomic(ENV_FILE, _serialize_env(values))
    print("DeepSeek 与 Qwen 凭据已从指定 Keychain 项导入；值未回显，Keychain 未修改。")
    if workspace_ready:
        print("Qwen Workspace ID 已写入；下一步运行 preflight。")
    else:
        print("Qwen Workspace ID 仍缺；补齐前完整启动会继续拒绝。")
    return 0


def preflight() -> tuple[Check, ...]:
    checks: list[Check] = []
    try:
        values = _load_env(ENV_FILE)
    except ValueError as error:
        return (Check("私有配置", False, str(error)),)

    env_mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
    checks.append(
        Check(
            "私有配置",
            env_mode & 0o077 == 0,
            "配置仅当前用户可读。" if env_mode & 0o077 == 0 else "配置权限过宽，必须收紧到 0600。",
        )
    )
    checks.append(_provider_check(values))
    local_boundary = (
        values.get("LAWCASE_BIND_ADDRESS") == "127.0.0.1"
        and values.get("LAWCASE_HTTPS_PORT") == "443"
        and values.get("LAWCASE_WORKBENCH_HOST") == "workbench.127.0.0.1.nip.io"
        and values.get("LAWCASE_IDENTITY_HOST") == "identity.127.0.0.1.nip.io"
    )
    checks.append(
        Check(
            "loopback HTTPS 边界",
            local_boundary,
            "仅绑定 127.0.0.1:443，使用固定非保留测试主机。" if local_boundary else "本机受管主机或绑定边界被改动。",
        )
    )
    checks.append(_migration_check())
    checks.append(_canonical_runtime_source_check())
    checks.extend(_certificate_checks(values))
    checks.append(_kes_check())
    checks.append(_object_store_policy_check(values))
    checks.append(_clamd_check())
    checks.append(_keycloak_check(values))
    checks.append(_dns_check(values))
    headroom = _managed_build_headroom_check()
    checks.append(headroom)

    docker = shutil.which("docker") is not None
    if docker:
        result = _bounded_run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            timeout_seconds=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        docker = result.returncode == 0
    checks.append(Check("容器引擎", docker, "Docker 引擎可用。" if docker else "Docker 引擎不可用。"))

    required_files = (
        COMPOSE_FILE,
        FRONTEND_DOCKERFILE,
        DEPLOYMENT / "Caddyfile",
        DEPLOYMENT / "api.Dockerfile",
        DEPLOYMENT / "keycloak.Dockerfile",
        DEPLOYMENT / "clamav" / "clamd.conf",
        DEPLOYMENT / "clamav" / "clamdscan",
        DEPLOYMENT / "object-store" / "bootstrap.sh",
        DEPLOYMENT / "probes" / "clamav_probe.py",
        DEPLOYMENT / "probes" / "document_renderer_probe.py",
        DEPLOYMENT / "probes" / "postgres_tenant_probe.py",
        DEPLOYMENT / "probes" / "provider_probe.py",
        DEPLOYMENT / "probes" / "kes_probe.py",
        DEPLOYMENT / "probes" / "s3_sse_probe.py",
        DEPLOYMENT / "postgres" / "postgres-entrypoint.sh",
        DEPLOYMENT / "postgres" / "migrate-and-seed.sh",
        DEPLOYMENT / "postgres" / "assert-runtime.sh",
        DEPLOYMENT / "postgres" / "pg_hba.conf",
        DEPLOYMENT / "postgres" / "initdb" / "010-roles-and-databases.sh",
        KEYCLOAK_RENDERER,
        KEYCLOAK_VERIFIER,
    )
    files_ready = all(item.is_file() for item in required_files)
    checks.append(Check("装配文件", files_ready, "隔离装配文件齐全。" if files_ready else "隔离装配文件不完整。"))

    if _compose_validation_allowed(
        docker_available=docker,
        files_ready=files_ready,
        headroom_ready=headroom.ready,
    ):
        result = _bounded_run(
            _compose_command("config", "--format", "json"),
            timeout_seconds=30,
            cwd=DEPLOYMENT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            rendered_compose = json.loads(result.stdout) if result.returncode == 0 else None
        except json.JSONDecodeError:
            rendered_compose = None
        checks.append(
            Check(
                "容器编排",
                isinstance(rendered_compose, dict),
                "完整编排通过静态校验。"
                if isinstance(rendered_compose, dict)
                else "完整编排静态校验失败。",
            )
        )
        checks.append(_runtime_contract_check(rendered_compose))
    else:
        if not headroom.ready:
            compose_message = (
                "完整重建磁盘余量不足，未执行容器编排静态校验；未启动任何服务。"
            )
        elif not docker:
            compose_message = "Docker 引擎不可用，未执行容器编排静态校验；未启动任何服务。"
        else:
            compose_message = "装配文件不完整，未执行容器编排静态校验；未启动任何服务。"
        checks.append(Check("容器编排", False, compose_message))
        checks.append(Check("生产运行合同", False, "编排尚不可验证。"))
    return tuple(checks)


def _managed_build_headroom_check(*, free_bytes: int | None = None) -> Check:
    """Fail before Compose stops consumers or starts a multi-image rebuild."""

    try:
        available = shutil.disk_usage(ROOT).free if free_bytes is None else free_bytes
    except OSError:
        return Check(
            "完整重建磁盘余量",
            False,
            "无法核验宿主可用空间；未允许停止服务或构建镜像。",
        )
    if type(available) is not int or available < 0:
        return Check(
            "完整重建磁盘余量",
            False,
            "宿主可用空间读数无效；未允许停止服务或构建镜像。",
        )
    ready = available >= _MANAGED_BUILD_HEADROOM_BYTES
    available_gib = available / 1024**3
    required_gib = _MANAGED_BUILD_HEADROOM_BYTES / 1024**3
    return Check(
        "完整重建磁盘余量",
        ready,
        (
            f"宿主可用空间 {available_gib:.1f}GiB，满足完整镜像重建所需的 {required_gib:.0f}GiB 余量。"
            if ready
            else (
                f"宿主可用空间仅 {available_gib:.1f}GiB，低于完整镜像重建所需的 "
                f"{required_gib:.0f}GiB；未停止消费者、未构建镜像、未启动 Agent。"
            )
        ),
    )


def _compose_validation_allowed(
    *,
    docker_available: bool,
    files_ready: bool,
    headroom_ready: bool,
) -> bool:
    """Keep Compose parsing behind the same host-safety gate as a rebuild."""

    return docker_available and files_ready and headroom_ready


def _compose_status_read_failure_check(returncode: int) -> Check:
    """Avoid misreporting an unavailable Docker API as stopped legal services."""

    timed_out = returncode == 124
    return Check(
        "容器运行状态读取",
        False,
        "Docker 状态读取超时；无法判断服务是否仍在运行，未启动、停止或重建任何服务。"
        if timed_out
        else "Docker 状态读取失败；无法判断服务是否仍在运行，未启动、停止或重建任何服务。",
    )


def _canonical_runtime_source_check(*, compose_text: str | None = None) -> Check:
    """Reject a canonical stack that would execute host Python sources.

    Read-only configuration, certificates, probes and synthetic fixtures may
    be mounted for the local Alpha. Application Python code must instead be
    baked into the image built from ``api.Dockerfile``. Otherwise a healthy
    container could silently run a newer host tree than its image provenance.
    """

    try:
        source = (
            COMPOSE_FILE.read_text(encoding="utf-8")
            if compose_text is None
            else compose_text
        )
    except OSError:
        return Check(
            "运行镜像源代码边界",
            False,
            "无法读取规范 Compose；未允许把宿主 Python 源码作为运行时代码。",
        )
    overlays = tuple(
        line.strip()
        for line in source.splitlines()
        if _is_runtime_python_source_bind(line)
    )
    return Check(
        "运行镜像源代码边界",
        not overlays,
        "规范 Compose 不挂载宿主 Python 源码；运行代码必须来自构建镜像。"
        if not overlays
        else "规范 Compose 含宿主 Python 源码挂载；完整启动已拒绝。",
    )


def _is_runtime_python_source_bind(line: str) -> bool:
    normalized = line.strip()
    return (
        normalized.startswith("- ")
        and ":/app/backend/" in normalized
        and ".py" in normalized
    )


def _runtime_python_source_overlays(detail: Mapping[str, object]) -> tuple[str, ...]:
    """Return host Python mount targets without exposing their host paths."""

    mounts = detail.get("Mounts")
    if not isinstance(mounts, list):
        return ()
    destinations: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, Mapping):
            continue
        destination = mount.get("Destination")
        if (
            mount.get("Type") == "bind"
            and isinstance(destination, str)
            and destination.startswith("/app/backend/")
            and destination.endswith(".py")
        ):
            destinations.add(destination)
    return tuple(sorted(destinations))


def verify_running_stack() -> tuple[Check, ...]:
    try:
        values = _load_env(ENV_FILE)
    except ValueError as error:
        return (Check("私有配置", False, str(error)),)
    checks: list[Check] = []
    ps = _bounded_run(
        _compose_command("ps", "--all", "--format", "json"),
        timeout_seconds=30,
        cwd=DEPLOYMENT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if ps.returncode != 0:
        return (_compose_status_read_failure_check(ps.returncode),)
    live_names: set[str] = set()
    completed_names: set[str] = set()
    unhealthy_names: set[str] = set()
    container_names: dict[str, str] = {}
    if ps.returncode == 0:
        records: list[object] = []
        try:
            decoded = json.loads(ps.stdout)
            records = decoded if isinstance(decoded, list) else [decoded]
        except json.JSONDecodeError:
            for line in ps.stdout.splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for item in records:
            if not isinstance(item, Mapping):
                continue
            service = str(item.get("Service", ""))
            container_name = str(item.get("Name") or item.get("Names") or "")
            state = str(item.get("State", "")).lower()
            health = str(item.get("Health", "")).lower()
            if state == "running":
                live_names.add(service)
                if container_name:
                    container_names[service] = container_name
                if health and health != "healthy":
                    unhealthy_names.add(service)
            if state == "exited" and str(item.get("ExitCode", "")) == "0":
                completed_names.add(service)
    expected = {
        "gateway",
        "frontend",
        "api",
        "postgres",
        "keycloak",
        "kes",
        "clamd",
        "object-storage",
        "case-agent-worker",
        "document-renderer",
    }
    missing = sorted(expected.difference(live_names))
    unhealthy = sorted(expected.intersection(unhealthy_names))
    checks.append(
        Check(
            "受管服务",
            not missing and not unhealthy,
            "全部持久服务正在运行，且声明了健康检查的服务均为 healthy。"
            if not missing and not unhealthy
            else "; ".join(
                part
                for part in (
                    f"未运行服务：{', '.join(missing)}" if missing else "",
                    f"未健康服务：{', '.join(unhealthy)}" if unhealthy else "",
                )
                if part
            ),
        )
    )
    restarted: list[str] = []
    oom_killed: list[str] = []
    inspect_failed: list[str] = []
    runtime_source_overlays: list[str] = []
    for service in sorted(expected.intersection(live_names)):
        container_name = container_names.get(service)
        if not container_name:
            inspect_failed.append(service)
            continue
        inspected = _bounded_run(
            ["docker", "inspect", container_name],
            timeout_seconds=15,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            detail = json.loads(inspected.stdout)[0]
            if not isinstance(detail, Mapping):
                raise TypeError("container inspect payload is not an object")
            if int(detail.get("RestartCount", 0)) != 0:
                restarted.append(service)
            state_detail = detail.get("State", {})
            if isinstance(state_detail, Mapping) and state_detail.get("OOMKilled") is True:
                oom_killed.append(service)
            runtime_source_overlays.extend(
                f"{service}:{destination}"
                for destination in _runtime_python_source_overlays(detail)
            )
        except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            inspect_failed.append(service)
    stable = not missing and not restarted and not oom_killed and not inspect_failed
    checks.append(
        Check(
            "运行稳定性",
            stable,
            "全部持久服务 restart=0 且没有 OOMKilled。"
            if stable
            else "; ".join(
                part
                for part in (
                    f"未运行：{', '.join(missing)}" if missing else "",
                    f"发生重启：{', '.join(restarted)}" if restarted else "",
                    f"发生 OOM：{', '.join(oom_killed)}" if oom_killed else "",
                    f"无法检查：{', '.join(inspect_failed)}" if inspect_failed else "",
                )
                if part
            ),
        )
    )
    checks.append(
        Check(
            "运行时代码镜像完整性",
            not runtime_source_overlays,
            "全部运行中服务均执行镜像内代码，未发现宿主 Python 源码挂载。"
            if not runtime_source_overlays
            else (
                "发现宿主 Python 源码挂载："
                f"{', '.join(sorted(runtime_source_overlays))}；当前只能作局部验收，不能作为可发布镜像。"
            ),
        )
    )
    incomplete = sorted(set(_ONE_SHOT_SERVICES).difference(completed_names))
    checks.append(
        Check(
            "一次性安全门禁",
            not incomplete,
            "迁移、跨律所写入拒绝、TLS/KES/SSE 与恶意文件探针均成功完成。"
            if not incomplete
            else f"未成功完成：{', '.join(incomplete)}",
        )
    )
    context = ssl.create_default_context(cafile=str(CA_CERT))
    workbench = f"https://{values['LAWCASE_WORKBENCH_HOST']}"
    identity = f"https://{values['LAWCASE_IDENTITY_HOST']}"
    checks.append(_https_json_check(f"{workbench}/readyz", context, label="Web 就绪"))
    discovery_url = f"{identity}/realms/{values['LAWCASE_KEYCLOAK_REALM']}/.well-known/openid-configuration"
    oidc = _https_json_check(discovery_url, context, label="OIDC 发现")
    checks.append(oidc)
    if oidc.ready:
        try:
            with urlopen(discovery_url, context=context, timeout=5) as response:
                payload = json.loads(response.read(256 * 1024))
            expected_issuer = f"{identity}/realms/{values['LAWCASE_KEYCLOAK_REALM']}"
            issuer_ready = payload.get("issuer") == expected_issuer
        except Exception:
            issuer_ready = False
        checks.append(
            Check(
                "OIDC issuer 绑定",
                issuer_ready,
                "浏览器与 API 使用同一 HTTPS issuer。" if issuer_ready else "OIDC issuer 与本机受管主机不一致。",
            )
        )
    return tuple(checks)


def _bounded_run(
    command: list[str],
    *,
    timeout_seconds: int,
    **popen_kwargs: object,
) -> subprocess.CompletedProcess:
    """Run a local infrastructure command without permitting an infinite wait.

    Docker Desktop can leave both the CLI and a plugin-metadata child waiting
    forever when its backend is unhealthy.  A new process group lets the
    timeout terminate that whole diagnostic tree without restarting or
    mutating Docker itself.
    """

    if not command or not 1 <= timeout_seconds <= 1_800:
        raise ValueError("bounded command or timeout is invalid")
    process = subprocess.Popen(
        command,
        start_new_session=True,
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, process_signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, 124, stdout, stderr)
    return subprocess.CompletedProcess(
        command,
        int(process.returncode or 0),
        stdout,
        stderr,
    )


def compose(*arguments: str) -> int:
    if not ENV_FILE.is_file():
        print("本机受管配置不存在；请先运行 init。", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("Docker 不可用。", file=sys.stderr)
        return 2
    timeout_seconds = 1_800 if arguments and arguments[0] == "up" else 300
    return _bounded_run(
        _compose_command(*arguments),
        timeout_seconds=timeout_seconds,
        cwd=DEPLOYMENT,
    ).returncode


def _service_state(service: str) -> Mapping[str, object] | None:
    container = _bounded_run(
        _compose_command("ps", "--all", "--quiet", service),
        timeout_seconds=15,
        cwd=DEPLOYMENT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    identifiers = [line.strip() for line in container.stdout.splitlines() if line.strip()]
    if container.returncode != 0 or len(identifiers) != 1:
        return None
    inspected = _bounded_run(
        ["docker", "inspect", identifiers[0]],
        timeout_seconds=15,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        payload = json.loads(inspected.stdout)
        state = payload[0]["State"]
        if inspected.returncode != 0 or not isinstance(state, Mapping):
            return None
        result = dict(state)
        result["RestartCount"] = payload[0].get("RestartCount")
        return result
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _wait_for_service(service: str, condition: str, timeout_seconds: int) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        state = _service_state(service)
        if state is not None:
            status = str(state.get("Status", "")).lower()
            exit_code = int(state.get("ExitCode", -1))
            health_detail = state.get("Health")
            health = (
                str(health_detail.get("Status", "")).lower()
                if isinstance(health_detail, Mapping)
                else ""
            )
            if state.get("OOMKilled") is True:
                print(f"{service} 被 OOM 终止；未继续启动。", file=sys.stderr)
                return False
            if condition == "healthy" and status == "running" and health == "healthy":
                return True
            if condition == "running" and status == "running":
                return True
            if condition == "completed" and status == "exited" and exit_code == 0:
                return True
            if status in {"dead", "removing"} or (status == "exited" and exit_code != 0):
                print(
                    f"{service} 在到达 {condition} 前退出（exit={exit_code}）。",
                    file=sys.stderr,
                )
                return False
        sleep(2)
    print(
        f"{service} 未在 {timeout_seconds} 秒内到达 {condition}；未放行后续服务。",
        file=sys.stderr,
    )
    return False


def _start_phase(
    service: str,
    condition: str,
    timeout_seconds: int,
    *,
    no_deps: bool = False,
) -> bool:
    print(f"启动阶段：{service} -> {condition}")
    arguments = ["up", "--no-build", "-d"]
    if no_deps:
        arguments.append("--no-deps")
    arguments.append(service)
    return compose(*arguments) == 0 and _wait_for_service(
        service, condition, timeout_seconds
    )


def start_managed_stack() -> int:
    """Re-establish every fail-closed barrier before consumers may run.

    A stopped one-shot container with exit code zero is historical evidence,
    not proof for the migrations and configuration currently mounted from the
    workspace.  Stop the database consumers and ClamAV daemon, remove only the
    disposable barrier containers, then let Compose rebuild the dependency
    graph in order.  Persistent data volumes are never removed here.
    """

    if compose("stop", *_START_BARRIER_CONSUMERS) != 0:
        print("未能停止旧的受管消费者；未复用历史门禁结果。", file=sys.stderr)
        return 2
    # MinIO consumes KES. Stop it first and only then stop KES so neither
    # bootstrap can rewrite a mounted volume underneath a live peer.
    if compose("stop", "object-storage") != 0 or compose("stop", "kes") != 0:
        print("未能停止旧的 KES；未复用历史门禁结果。", file=sys.stderr)
        return 2
    # Keycloak was stopped with the consumers, so PostgreSQL can now be
    # recreated without an in-flight identity or case transaction.
    if compose("stop", "postgres") != 0:
        print("未能停止旧的 PostgreSQL；未复用历史门禁结果。", file=sys.stderr)
        return 2
    if compose(
        "rm",
        "-sf",
        *_ONE_SHOT_SERVICES,
        *_PERSISTENT_RECREATE_SERVICES,
    ) != 0:
        print("未能清除历史一次性门禁容器；未启动消费者。", file=sys.stderr)
        return 2

    # Build the four local images sequentially. Parallel BuildKit sessions are
    # unreliable on the small Docker Desktop VM and can also starve clamd's
    # signature loader. No persistent service is running during this phase.
    for service in _LOCAL_BUILD_SERVICES:
        print(f"顺序构建：{service}")
        if compose("build", service) != 0:
            print(f"{service} 镜像构建失败；未启动任何消费者。", file=sys.stderr)
            return 2

    phases = (
        ("postgres", "healthy", 300),
        ("postgres-tenant-probe", "completed", 300),
        # clamd gets the constrained VM to itself while loading its full
        # signature database; Keycloak and LibreOffice start only afterwards.
        ("clamd", "healthy", 600),
        ("clamav-probe", "completed", 180),
        ("kes", "running", 120),
        ("kes-ready-probe", "completed", 120),
        ("object-storage", "healthy", 180),
        ("object-storage-sse-probe", "completed", 180),
        ("keycloak", "healthy", 600),
        ("document-renderer", "healthy", 300),
        ("document-renderer-probe", "completed", 300),
        ("frontend", "healthy", 180),
        ("provider-preflight", "completed", 600),
    )
    for service, condition, timeout_seconds in phases:
        if not _start_phase(service, condition, timeout_seconds):
            return 2

    # Every dependency has just passed in ``phases``.  Starting consumers with
    # dependency traversal would restart Compose one-shots, including the paid
    # provider preflight, and invalidate the explicit call cap.
    if not _start_phase("api", "healthy", 300, no_deps=True):
        return 2
    if not _start_phase("case-agent-worker", "running", 120, no_deps=True):
        return 2
    if not _start_phase("gateway", "running", 120, no_deps=True):
        return 2
    return 0


def resume_after_provider_gate() -> int:
    """Resume the same staged start after a provider-account-only failure.

    This path is deliberately narrower than ``start``.  It may reuse only the
    still-running upstream services and successful one-shot barriers from the
    interrupted start.  Any missing, unhealthy, restarted or failed upstream
    component requires a full staged start instead.
    """

    persistent = (
        "postgres",
        "clamd",
        "kes",
        "object-storage",
        "keycloak",
        "document-renderer",
        "frontend",
    )
    one_shots = tuple(
        service for service in _ONE_SHOT_SERVICES if service != "provider-preflight"
    )
    problems: list[str] = []
    for service in persistent:
        state = _service_state(service)
        if state is None or str(state.get("Status", "")).lower() != "running":
            problems.append(f"{service} 未运行")
            continue
        if state.get("OOMKilled") is True:
            problems.append(f"{service} 曾被 OOM 终止")
        if int(state.get("RestartCount", -1)) != 0:
            problems.append(f"{service} 已发生重启")
        health_detail = state.get("Health")
        if isinstance(health_detail, Mapping) and str(
            health_detail.get("Status", "")
        ).lower() != "healthy":
            problems.append(f"{service} 未健康")
    for service in one_shots:
        state = _service_state(service)
        if (
            state is None
            or str(state.get("Status", "")).lower() != "exited"
            or int(state.get("ExitCode", -1)) != 0
        ):
            problems.append(f"{service} 未成功完成")
    for service in ("api", "case-agent-worker", "gateway"):
        state = _service_state(service)
        if state is not None and str(state.get("Status", "")).lower() == "running":
            problems.append(f"{service} 已运行，不能按失败续跑处理")
    if problems:
        print("不能只续跑供应商门：" + "；".join(problems), file=sys.stderr)
        print("请改用完整 start，重新建立全部门禁。", file=sys.stderr)
        return 2
    if compose("rm", "-sf", "provider-preflight") != 0:
        print("未能清除失败的供应商门容器；未启动 Agent。", file=sys.stderr)
        return 2
    if not _start_phase("provider-preflight", "completed", 600):
        return 2
    # The strict state audit above and the newly completed provider gate prove
    # every dependency.  Do not let Compose traverse and restart paid or
    # destructive one-shot barriers while bringing up consumers.
    if not _start_phase("api", "healthy", 300, no_deps=True):
        return 2
    if not _start_phase("case-agent-worker", "running", 120, no_deps=True):
        return 2
    if not _start_phase("gateway", "running", 120, no_deps=True):
        return 2
    print("供应商门续跑通过，API、Agent Worker 与网关已启动；请运行 verify。")
    return 0


def _compose_command(*arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        *arguments,
    ]


def _generate_certificates(values: Mapping[str, str]) -> None:
    _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(CA_KEY))
    ca_csr = TLS / "ca.csr"
    ca_ext = TLS / "ca.ext"
    _openssl("req", "-new", "-key", str(CA_KEY), "-subj", "/CN=Lawcase Local Managed Test CA", "-out", str(ca_csr))
    _write_private_new_file(
        ca_ext,
        "[v3_ca]\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always\n",
    )
    _openssl(
        "x509", "-req", "-in", str(ca_csr), "-signkey", str(CA_KEY),
        "-out", str(CA_CERT), "-days", "30", "-sha256", "-extfile", str(ca_ext), "-extensions", "v3_ca",
    )
    ca_csr.unlink()
    ca_ext.unlink()

    _issue_certificate(
        "edge",
        common_name=values["LAWCASE_WORKBENCH_HOST"],
        dns_names=(values["LAWCASE_WORKBENCH_HOST"], values["LAWCASE_IDENTITY_HOST"]),
        extended_usage="serverAuth",
    )
    _issue_certificate("postgres", common_name="postgres", dns_names=("postgres",), extended_usage="serverAuth")
    _issue_certificate("minio", common_name="object-storage", dns_names=("object-storage",), extended_usage="serverAuth")
    _issue_certificate("kes-server", common_name="kes", dns_names=("kes",), extended_usage="serverAuth")
    _issue_certificate("kes-client", common_name="lawcase-minio-kes-client", dns_names=(), extended_usage="clientAuth")

    os.chmod(CA_KEY, 0o600)
    for name in _PRIVATE_CERT_KEYS[1:]:
        os.chmod(TLS / name, 0o600)
    for certificate in TLS.glob("*.crt"):
        os.chmod(certificate, 0o644)


def _issue_certificate(
    name: str,
    *,
    common_name: str,
    dns_names: tuple[str, ...],
    extended_usage: str,
) -> None:
    key = TLS / f"{name}.key"
    csr = TLS / f"{name}.csr"
    cert = TLS / f"{name}.crt"
    ext = TLS / f"{name}.ext"
    _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key))
    _openssl("req", "-new", "-key", str(key), "-subj", f"/CN={common_name}", "-out", str(csr))
    lines = [
        "[leaf]",
        "basicConstraints=critical,CA:FALSE",
        "keyUsage=critical,digitalSignature,keyEncipherment",
        f"extendedKeyUsage={extended_usage}",
        "subjectKeyIdentifier=hash",
        "authorityKeyIdentifier=keyid,issuer",
    ]
    if dns_names:
        lines.append("subjectAltName=" + ",".join(f"DNS:{item}" for item in dns_names))
    _write_private_new_file(ext, "\n".join(lines) + "\n")
    serial = TLS / "ca.srl"
    serial_args = ("-CAserial", str(serial)) if serial.exists() else ("-CAcreateserial",)
    _openssl(
        "x509", "-req", "-in", str(csr), "-CA", str(CA_CERT), "-CAkey", str(CA_KEY),
        *serial_args, "-out", str(cert), "-days", "30", "-sha256", "-extfile", str(ext), "-extensions", "leaf",
    )
    csr.unlink()
    ext.unlink()


def _render_kes_config() -> None:
    client_pem = _run_capture(
        ["openssl", "x509", "-in", str(TLS / "kes-client.crt"), "-pubkey", "-noout"]
    )
    public_der = _run_capture(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input_bytes=client_pem,
    )
    identity = sha256(public_der).hexdigest()
    config = (
        "version: v1\n"
        "address: 0.0.0.0:7373\n"
        "admin:\n"
        "  identity: disabled\n"
        "tls:\n"
        "  key: /config/kes-server.key\n"
        "  cert: /config/kes-server.crt\n"
        "  ca: /config/ca.crt\n"
        "api:\n"
        "  /v1/ready:\n"
        "    skip_auth: true\n"
        "    timeout: 10s\n"
        "policy:\n"
        "  minio-server:\n"
        "    allow:\n"
        "      - /v1/key/create/minio-*\n"
        "      - /v1/key/generate/minio-*\n"
        "      - /v1/key/decrypt/minio-*\n"
        "      - /v1/key/bulk/decrypt\n"
        "      - /v1/status\n"
        "      - /v1/ready\n"
        "    identities:\n"
        f"      - {identity}\n"
        "cache:\n"
        "  expiry:\n"
        "    any: 5m0s\n"
        "    unused: 20s\n"
        "    offline: 0s\n"
        "log:\n"
        "  error: on\n"
        "  audit: on\n"
        "keystore:\n"
        "  fs:\n"
        "    path: /keys\n"
    )
    _write_private_new_file(KES_CONFIG, config)


def _render_keycloak_realm() -> None:
    if not KEYCLOAK_RENDERER.is_file():
        raise RuntimeError("Keycloak realm renderer is missing")
    result = subprocess.run(
        [
            sys.executable,
            str(KEYCLOAK_RENDERER),
            "--env-file",
            str(ENV_FILE),
            "--output",
            str(KEYCLOAK_REALM),
            "--contract-output",
            str(KEYCLOAK_CONTRACT),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if (
        result.returncode != 0
        or not KEYCLOAK_REALM.is_file()
        or not KEYCLOAK_CONTRACT.is_file()
    ):
        raise RuntimeError("Keycloak realm rendering failed")
    os.chmod(KEYCLOAK_REALM, 0o600)
    os.chmod(KEYCLOAK_CONTRACT, 0o600)


def _write_operator_notes(values: Mapping[str, str]) -> None:
    login = (
        "本文件只包含合成测试账号，不得用于真实律所。\n\n"
        f"入口: https://{values['LAWCASE_WORKBENCH_HOST']}\n"
        f"用户名: {values['LAWCASE_TEST_LEAD_USERNAME']}\n"
        f"初始密码: {values['LAWCASE_TEST_LEAD_PASSWORD']}\n\n"
        "首次登录必须在 Keycloak 完成 TOTP 绑定；未绑定不会进入工作台。\n"
        "绑定完成后如首次回调被拒绝，请退出后再用密码 + TOTP 登录一次。\n"
    )
    _write_private_new_file(LOGIN_FILE, login)
    fingerprint = _run_capture(
        ["openssl", "x509", "-in", str(CA_CERT), "-noout", "-fingerprint", "-sha256"]
    ).decode("utf-8").strip()
    trust = (
        "本机受管测试 CA 仅有 30 天有效期，只签发本测试栈证书。\n"
        "Codex 不会自动修改 macOS Keychain。\n\n"
        f"CA 文件: {CA_CERT}\n"
        f"指纹: {fingerprint}\n\n"
        "由你确认后，在 macOS “钥匙串访问”中导入该 CA，并仅对本机测试设为信任。\n"
        "验收结束后删除该信任项；删除容器不会自动删除 Keychain 项。\n"
    )
    _write_private_new_file(TRUST_FILE, trust)


def _certificate_checks(values: Mapping[str, str]) -> list[Check]:
    expected = (
        CA_CERT,
        *(TLS / name for name in _PRIVATE_CERT_KEYS),
        *(TLS / name for name in ("edge.crt", "postgres.crt", "minio.crt", "kes-server.crt", "kes-client.crt")),
    )
    present = all(path.is_file() for path in expected)
    checks = [Check("TLS 材料", present, "短期本机 CA 与分服务证书齐全。" if present else "TLS 材料不完整。")]
    if not present:
        return checks
    private_modes = all(stat.S_IMODE((TLS / name).stat().st_mode) & 0o077 == 0 for name in _PRIVATE_CERT_KEYS)
    checks.append(Check("TLS 私钥权限", private_modes, "私钥仅当前用户可读。" if private_modes else "TLS 私钥权限过宽。"))
    verified = True
    for name in ("edge", "postgres", "minio", "kes-server", "kes-client"):
        result = subprocess.run(
            ["openssl", "verify", "-CAfile", str(CA_CERT), str(TLS / f"{name}.crt")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        lifetime = subprocess.run(
            ["openssl", "x509", "-checkend", "86400", "-noout", "-in", str(TLS / f"{name}.crt")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        verified = verified and result.returncode == 0 and lifetime.returncode == 0
    checks.append(Check("TLS 链路", verified, "所有服务证书可由本机 CA 验证且未临近过期。" if verified else "TLS 证书链或有效期校验失败。"))
    try:
        decoded = ssl._ssl._test_decode_cert(str(TLS / "edge.crt"))  # type: ignore[attr-defined]
        names = {
            value
            for kind, value in decoded.get("subjectAltName", ())
            if kind == "DNS" and isinstance(value, str)
        }
        hostnames = {
            values["LAWCASE_WORKBENCH_HOST"],
            values["LAWCASE_IDENTITY_HOST"],
        }.issubset(names)
    except Exception:
        hostnames = False
    checks.append(Check("HTTPS 主机绑定", hostnames, "工作台与 OIDC 主机均绑定到证书 SAN。" if hostnames else "HTTPS 证书主机绑定不完整。"))
    return checks


def _provider_check(values: Mapping[str, str]) -> Check:
    missing = [key for key in _REQUIRED_PROVIDER_KEYS if not _real(values.get(key, ""))]
    workspace = values.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID", "")
    lawyer_workspace = values.get(
        "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID", ""
    )
    shape_ready = (
        not missing
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", workspace) is not None
        and lawyer_workspace == workspace
        and all(not any(character.isspace() for character in values[key]) for key in _REQUIRED_PROVIDER_KEYS)
    )
    return Check(
        "真实核心模型",
        shape_ready,
        "DeepSeek 规划/台账与 Qwen 视觉凭据已填写，仍须在真实调用中验收。"
        if shape_ready
        else "完整核心测试凭据未齐或格式无效，不会用假模型或离线回退。",
    )


def _runtime_contract_check(payload: object) -> Check:
    ready = False
    try:
        assert isinstance(payload, Mapping)
        services = payload["services"]
        assert isinstance(services, Mapping)
        api = services["api"]
        worker = services["case-agent-worker"]
        tenant_probe = services["postgres-tenant-probe"]
        provider = services["provider-preflight"]
        keycloak = services["keycloak"]
        renderer = services["document-renderer"]
        renderer_probe = services["document-renderer-probe"]
        frontend = services["frontend"]
        assert (
            isinstance(api, Mapping)
            and isinstance(worker, Mapping)
            and isinstance(tenant_probe, Mapping)
            and isinstance(provider, Mapping)
            and isinstance(keycloak, Mapping)
            and isinstance(renderer, Mapping)
            and isinstance(renderer_probe, Mapping)
            and isinstance(frontend, Mapping)
        )
        api_env = api["environment"]
        worker_env = worker["environment"]
        tenant_probe_env = tenant_probe["environment"]
        provider_env = provider["environment"]
        keycloak_env = keycloak["environment"]
        renderer_env = renderer["environment"]
        renderer_probe_env = renderer_probe["environment"]
        frontend_build = frontend["build"]
        frontend_health = frontend["healthcheck"]
        assert (
            isinstance(api_env, Mapping)
            and isinstance(worker_env, Mapping)
            and isinstance(tenant_probe_env, Mapping)
            and isinstance(provider_env, Mapping)
            and isinstance(keycloak_env, Mapping)
            and isinstance(renderer_env, Mapping)
            and isinstance(renderer_probe_env, Mapping)
            and isinstance(frontend_build, Mapping)
            and isinstance(frontend_health, Mapping)
        )

        def tls_postgres_dsn(value: object, role: str) -> bool:
            return (
                isinstance(value, str)
                and value.startswith(f"postgresql://{role}:")
                and "@postgres:5432/lawcase?" in value
                and "sslmode=verify-full" in value
                and "sslrootcert=/run/lawcase-postgres-ca/ca.crt" in value
            )

        def dependency(service: Mapping[str, object], name: str, condition: str) -> bool:
            dependencies = service.get("depends_on")
            if not isinstance(dependencies, Mapping):
                return False
            detail = dependencies.get(name)
            return isinstance(detail, Mapping) and detail.get("condition") == condition

        ready = (
            api_env.get("LAWCASE_WEB_RUNTIME_MODE") == "PRODUCTION_WEB"
            and api.get("read_only") is True
            and set(api.get("networks", {})) == {"edge", "data"}
            and dependency(api, "postgres-assert", "service_completed_successfully")
            and dependency(
                api,
                "postgres-tenant-probe",
                "service_completed_successfully",
            )
            and dependency(api, "keycloak", "service_healthy")
            and dependency(api, "object-storage-sse-probe", "service_completed_successfully")
            and dependency(api, "clamav-probe", "service_completed_successfully")
            and dependency(api, "provider-preflight", "service_completed_successfully")
            and api_env.get("LAWCASE_WEB_PUBLIC_ORIGIN")
            == "https://workbench.127.0.0.1.nip.io"
            and api_env.get("LAWCASE_WEB_OIDC_REQUIRED_AMR") == "mfa"
            and api_env.get("LAWCASE_WEB_OIDC_ISSUER")
            == "https://identity.127.0.0.1.nip.io/realms/lawcase-test"
            and tls_postgres_dsn(
                api_env.get("LAWCASE_WEB_APP_POSTGRES_DSN"),
                "lawcase_web_application",
            )
            and tls_postgres_dsn(
                api_env.get("LAWCASE_WEB_IDENTITY_DIRECTORY_POSTGRES_DSN"),
                "lawcase_identity_directory",
            )
            and tls_postgres_dsn(
                api_env.get("LAWCASE_WEB_SESSION_GATEWAY_POSTGRES_DSN"),
                "lawcase_web_session_gateway",
            )
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_ENDPOINT")
            == "https://object-storage:9000"
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_BUCKET")
            == "lawcase-private-alpha"
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_ENCRYPTION") == "AES256"
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_KMS_KEY_ID") == ""
            and api_env.get("LAWCASE_WEB_CLAMAV_EXECUTABLE")
            == "/opt/lawcase-clamav/clamdscan"
            and api_env.get("LAWCASE_WEB_CLAMAV_TIMEOUT_SECONDS") == "120"
            and worker_env.get("LAWCASE_AGENT_WORKER_RUNTIME_MODE")
            == "PRODUCTION_AGENT_WORKER"
            and worker.get("read_only") is True
            and set(worker.get("networks", {}))
            == {"data", "document-render", "provider-egress"}
            and dependency(worker, "postgres-assert", "service_completed_successfully")
            and dependency(
                worker,
                "postgres-tenant-probe",
                "service_completed_successfully",
            )
            and tenant_probe.get("read_only") is True
            and set(tenant_probe.get("networks", {})) == {"data"}
            and dependency(
                tenant_probe,
                "postgres-assert",
                "service_completed_successfully",
            )
            and tenant_probe.get("command")
            == ["python", "/opt/lawcase-probes/postgres_tenant_probe.py"]
            and tls_postgres_dsn(
                tenant_probe_env.get("LAWCASE_WEB_APP_POSTGRES_DSN"),
                "lawcase_web_application",
            )
            and tenant_probe_env.get("LAWCASE_TEST_FIRM_ID")
            == "11111111-1111-4111-8111-111111111111"
            and dependency(
                worker,
                "object-storage-sse-probe",
                "service_completed_successfully",
            )
            and dependency(worker, "provider-preflight", "service_completed_successfully")
            and dependency(worker, "document-renderer", "service_healthy")
            and dependency(
                worker,
                "document-renderer-probe",
                "service_completed_successfully",
            )
            and tls_postgres_dsn(
                worker_env.get("LAWCASE_AGENT_WORKER_POSTGRES_DSN"),
                "lawcase_agent_worker",
            )
            and tls_postgres_dsn(
                worker_env.get("LAWCASE_AGENT_VERIFIER_POSTGRES_DSN"),
                "lawcase_agent_verifier",
            )
            and worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_ENDPOINT")
            == "https://object-storage:9000"
            and worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_BUCKET")
            == "lawcase-private-alpha"
            and worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_ENCRYPTION")
            == "AES256"
            and worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_KMS_KEY_ID") == ""
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_ACCESS_KEY_ID")
            != worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_ACCESS_KEY_ID")
            and api_env.get("LAWCASE_WEB_OBJECT_STORE_SECRET_ACCESS_KEY")
            != worker_env.get("LAWCASE_AGENT_WORKER_OBJECT_STORE_SECRET_ACCESS_KEY")
            and worker_env.get("LAWCASE_AGENT_WORKER_DEEPSEEK_MODEL")
            == "deepseek-v4-pro"
            and worker_env.get("LAWCASE_AGENT_WORKER_DEEPSEEK_ALLOWED_MODELS")
            == "deepseek-v4-pro"
            and worker_env.get("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL")
            == "deepseek-v4-pro"
            and worker_env.get("LAWCASE_AGENT_WORKER_QWEN_PDFTOPPM_EXECUTABLE")
            == "/usr/bin/pdftoppm"
            and worker_env.get("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY")
            == worker_env.get("LAWCASE_AGENT_WORKER_QWEN_API_KEY")
            and worker_env.get("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID")
            == worker_env.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID")
            and worker_env.get("LAWCASE_AGENT_WORKER_DOCUMENT_DELIVERY_ENABLED")
            == "true"
            and worker_env.get("LAWCASE_AGENT_WORKER_BRAVE_SEARCH_API_KEY", "") == ""
            and provider_env.get("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY")
            == worker_env.get("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY")
            and provider_env.get("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY")
            == worker_env.get("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY")
            and provider_env.get("LAWCASE_AGENT_WORKER_QWEN_API_KEY")
            == worker_env.get("LAWCASE_AGENT_WORKER_QWEN_API_KEY")
            and provider_env.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID")
            == worker_env.get("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID")
            and provider_env.get("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY")
            == worker_env.get("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY")
            and provider_env.get(
                "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID"
            )
            == worker_env.get(
                "LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID"
            )
            and provider.get("read_only") is True
            and provider.get("command")
            == ["python", "/opt/lawcase-probes/provider_probe.py"]
            and set(provider.get("networks", {})) == {"provider-egress"}
            and renderer.get("read_only") is True
            and set(renderer.get("networks", {})) == {"document-render"}
            and renderer_env.get("LAWCASE_DOCUMENT_RENDERER_SOFFICE_EXECUTABLE")
            == "/usr/lib/libreoffice/program/soffice"
            and renderer_env.get("LAWCASE_DOCUMENT_RENDERER_SHARED_SECRET")
            == worker_env.get("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET")
            and renderer_probe.get("read_only") is True
            and set(renderer_probe.get("networks", {})) == {"document-render"}
            and renderer_probe.get("command")
            == ["python", "/opt/lawcase-probes/document_renderer_probe.py"]
            and dependency(renderer_probe, "document-renderer", "service_healthy")
            and renderer_probe_env.get(
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_ENDPOINT"
            )
            == "http://document-renderer:8090"
            and renderer_probe_env.get(
                "LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET"
            )
            == worker_env.get("LAWCASE_AGENT_WORKER_DOCUMENT_RENDERER_SHARED_SECRET")
            and frontend.get("read_only") is True
            and frontend_build.get("dockerfile")
            == "../deployment/web/frontend.Dockerfile"
            and isinstance(frontend_health.get("test"), list)
            and len(frontend_health["test"]) == 2
            and frontend_health["test"][0] == "CMD-SHELL"
            and all(
                required in frontend_health["test"][1]
                for required in (
                    "/app/web/server.js",
                    "/app/web/.next/BUILD_ID",
                    "/app/web/.next/static",
                    "/app/web/public",
                    "http://127.0.0.1:3000/",
                )
            )
            and keycloak.get("read_only") is True
            and keycloak.get("command") == ["start", "--optimized", "--import-realm"]
            and set(keycloak.get("networks", {})) == {"edge", "identity-data"}
            and keycloak_env.get("KC_HOSTNAME")
            == "https://identity.127.0.0.1.nip.io"
            and keycloak_env.get("KC_DB_URL")
            == (
                "jdbc:postgresql://postgres:5432/keycloak?sslmode=verify-full&"
                "sslrootcert=/run/lawcase-postgres-ca/ca.crt"
            )
            and keycloak_env.get("JAVA_OPTS_KC_HEAP") == "-Xms128m -Xmx512m"
        )
    except (AssertionError, KeyError, TypeError):
        ready = False
    return Check(
        "生产运行合同",
        ready,
        "Web/Worker fail-closed 模式、跨律所数据库负测、真实 provider 门、DeepSeek v4-pro、Qwen OCR 与文书交付已锁定。"
        if ready
        else "Web/Worker 核心运行合同不完整。",
    )
def _migration_check() -> Check:
    numbers = sorted(
        int(match.group(1))
        for path in MIGRATIONS.iterdir()
        if path.is_file() and (match := _MIGRATION.fullmatch(path.name)) is not None
    )
    expected = list(range(1, numbers[-1] + 1)) if numbers else [1]
    # Exact equality rejects both gaps and two files claiming the same number.
    ready = bool(numbers) and numbers == expected and numbers[-1] >= 52
    return Check(
        "PostgreSQL 迁移链",
        ready,
        f"将按顺序安装 0001—{numbers[-1]:04d}。"
        if ready
        else "PostgreSQL 迁移链缺号、重号或低于 0052。",
    )


def _kes_check() -> Check:
    if not KES_CONFIG.is_file():
        return Check("KES 密钥服务", False, "KES 配置不存在。")
    text = KES_CONFIG.read_text(encoding="utf-8")
    identity = next((line.strip()[2:] for line in text.splitlines() if re.fullmatch(r"\s+- [0-9a-f]{64}", line)), "")
    ready = bool(identity) and "keystore:\n  fs:" in text and "/v1/key/generate/minio-*" in text
    return Check("KES 密钥服务", ready, "KES mTLS 身份、最小策略与持久测试 keystore 已绑定。" if ready else "KES 权限或 keystore 绑定不完整。")


def _object_store_policy_check(values: Mapping[str, str]) -> Check:
    expected = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                ],
                "Resource": ["arn:aws:s3:::lawcase-private-alpha"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:AbortMultipartUpload",
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:ListMultipartUploadParts",
                    "s3:PutObject",
                ],
                "Resource": ["arn:aws:s3:::lawcase-private-alpha/*"],
            },
        ],
    }
    try:
        policies = [
            json.loads(
                (DEPLOYMENT / "object-store" / filename).read_text(encoding="utf-8")
            )
            for filename in ("web-policy.json", "worker-policy.json")
        ]
        credentials_are_separate = (
            values["LAWCASE_WEB_OBJECT_ACCESS_KEY_ID"]
            != values["LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID"]
            and values["LAWCASE_WEB_OBJECT_SECRET_ACCESS_KEY"]
            != values["LAWCASE_WORKER_OBJECT_SECRET_ACCESS_KEY"]
            and values["LAWCASE_WEB_OBJECT_ACCESS_KEY_ID"]
            != values["LAWCASE_OBJECT_ROOT_USER"]
            and values["LAWCASE_WORKER_OBJECT_ACCESS_KEY_ID"]
            != values["LAWCASE_OBJECT_ROOT_USER"]
            and values["LAWCASE_WEB_OBJECT_SECRET_ACCESS_KEY"]
            != values["LAWCASE_OBJECT_ROOT_PASSWORD"]
            and values["LAWCASE_WORKER_OBJECT_SECRET_ACCESS_KEY"]
            != values["LAWCASE_OBJECT_ROOT_PASSWORD"]
        )
        ready = (
            values.get("LAWCASE_OBJECT_BUCKET") == "lawcase-private-alpha"
            and policies == [expected, expected]
            and credentials_are_separate
        )
    except (KeyError, OSError, json.JSONDecodeError):
        ready = False
    return Check(
        "对象库最小权限",
        ready,
        "Web/Worker 独立凭据与单桶精确动作策略已锁定。"
        if ready
        else "对象库凭据未隔离、桶名错位或策略超出固定最小权限。",
    )


def _clamd_check() -> Check:
    config = DEPLOYMENT / "clamav" / "clamd.conf"
    wrapper = DEPLOYMENT / "clamav" / "clamdscan"
    try:
        text = config.read_text(encoding="utf-8")
        wrapper_mode = stat.S_IMODE(wrapper.stat().st_mode)
        ready = (
            "LocalSocket /run/lawcase-clamav/clamd.sock" in text
            and "MaxFileSize 256M" in text
            and "MaxScanSize 512M" in text
            and "StreamMaxLength 256M" in text
            and "AlertExceedsMax yes" in text
            and wrapper.is_file()
            and bool(wrapper_mode & stat.S_IXUSR)
        )
    except OSError:
        ready = False
    return Check(
        "恶意文件扫描合同",
        ready,
        "常驻 clamd、私有 Unix socket、256 MiB 流上限与超限拒绝已锁定。"
        if ready
        else "clamd 常驻扫描、上传上限或 fail-closed 超限合同不完整。",
    )


def _keycloak_check(values: Mapping[str, str]) -> Check:
    try:
        payload = json.loads(KEYCLOAK_REALM.read_text(encoding="utf-8"))
        contract = json.loads(KEYCLOAK_CONTRACT.read_text(encoding="utf-8"))
        users = payload.get("users", [])
        clients = payload.get("clients", [])
        user = next(item for item in users if item.get("username") == values["LAWCASE_TEST_LEAD_USERNAME"])
        client = next(item for item in clients if item.get("clientId") == values["LAWCASE_KEYCLOAK_CLIENT_ID"])
        origin = f"https://{values['LAWCASE_WORKBENCH_HOST']}"
        verified = subprocess.run(
            [
                sys.executable,
                str(KEYCLOAK_VERIFIER),
                str(KEYCLOAK_REALM),
                "--expected-workbench-origin",
                origin,
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        ready = (
            verified
            and payload.get("realm") == values["LAWCASE_KEYCLOAK_REALM"]
            and user.get("id") == values["LAWCASE_TEST_LEAD_SUBJECT"]
            and "CONFIGURE_TOTP" in user.get("requiredActions", [])
            and client.get("secret") == values["LAWCASE_OIDC_CLIENT_SECRET"]
            and contract.get("issuer") == values["LAWCASE_TEST_OIDC_ISSUER"]
            and contract.get("audience") == values["LAWCASE_KEYCLOAK_CLIENT_ID"]
            and contract.get("required_amr") == ["mfa"]
        )
    except Exception:
        ready = False
    return Check("OIDC / TOTP", ready, "Keycloak 客户端、固定 subject 与强制 TOTP 已绑定。" if ready else "Keycloak realm 未绑定固定 subject/client/TOTP。")


def _dns_check(values: Mapping[str, str]) -> Check:
    ready = True
    for key in ("LAWCASE_WORKBENCH_HOST", "LAWCASE_IDENTITY_HOST"):
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(values[key], 443, type=socket.SOCK_STREAM)}
        except OSError:
            addresses = set()
        ready = ready and "127.0.0.1" in addresses
    return Check("HTTPS 本机解析", ready, "nip.io 两个非保留主机都解析到 127.0.0.1。" if ready else "nip.io 本机解析失败；未修改 /etc/hosts。")


def _https_json_check(url: str, context: ssl.SSLContext, *, label: str) -> Check:
    try:
        with urlopen(url, context=context, timeout=5) as response:
            body = response.read(256 * 1024)
            payload = json.loads(body)
            ready = response.status == 200 and isinstance(payload, dict)
    except Exception:
        ready = False
    return Check(label, ready, f"{label}通过本机 CA 的 HTTPS 验证。" if ready else f"{label}尚未通过 HTTPS/JSON 验证。")


def _render_environment_template(generated: Mapping[str, str]) -> str:
    result: list[str] = []
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            result.append(raw)
            continue
        key, value = raw.split("=", 1)
        result.append(f"{key}={generated.get(key, value)}")
    return "\n".join(result) + "\n"


def _load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"私有配置不存在：{path}。请先运行 init。")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("私有配置不可读。") from error
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"私有配置第 {number} 行格式错误。")
        key, value = line.split("=", 1)
        if not _KEY.fullmatch(key) or key in values or "\x00" in value or "\n" in value:
            raise ValueError(f"私有配置第 {number} 行字段无效或重复。")
        values[key] = value
    return values


def _serialize_env(values: Mapping[str, str]) -> str:
    # Preserve the documented template order and comments, while applying the
    # current private values.  Unknown administrator fields are appended.
    seen: set[str] = set()
    lines: list[str] = []
    for raw in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            lines.append(raw)
            continue
        key, _ = raw.split("=", 1)
        seen.add(key)
        lines.append(f"{key}={values.get(key, '')}")
    for key in sorted(set(values).difference(seen)):
        lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def _secret() -> str:
    return secrets.token_urlsafe(36)


def _prompt_secret(prompt: str) -> str:
    try:
        return getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未写入任何凭据。", file=sys.stderr)
        raise SystemExit(130)


def _read_keychain_password(account: str) -> str:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "cn.lawcase.workbench.model-provider",
                "-a",
                account,
                "-w",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Keychain authorization timed out") from error
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("Keychain credential is unavailable")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError("Keychain credential is invalid") from error


def _real(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= 8
        and not any(marker in value.lower() for marker in _PLACEHOLDERS)
        and "\x00" not in value
    )


def _openssl(*arguments: str) -> None:
    result = subprocess.run(
        ["openssl", *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenSSL certificate operation failed")


def _run_capture(command: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("security material inspection failed")
    return result.stdout


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private_new_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _write_private_atomic(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def show_private_note(path: Path, *, label: str) -> int:
    if not path.is_file():
        print(f"{label}不存在；请先运行 init。", file=sys.stderr)
        return 2
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def print_checks(
    checks: Iterable[Check],
    *,
    as_json: bool,
    scope: str = "PREFLIGHT",
) -> None:
    items = tuple(checks)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": "lawcase-local-managed-alpha-preflight-v1",
                    "scope": scope,
                    "ready": all(item.ready for item in items),
                    "full_user_e2e_verified": False,
                    "commercial_release_authorized": False,
                    "checks": [
                        {
                            "name": item.name,
                            "status": "READY" if item.ready else "BLOCKED",
                            "message": item.message,
                        }
                        for item in items
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for item in items:
        print(f"[{'PASS' if item.ready else 'BLOCKED'}] {item.name}：{item.message}")


if __name__ == "__main__":
    raise SystemExit(main())
