"""Read-only production Web deployment preflight.

This command proves only static configuration and local executable/migration
prerequisites. It never opens a database, calls an identity provider, reads an
object-store credential, or authorizes a release. The final report deliberately
keeps real browser, backup/recovery, and cross-platform lawyer acceptance as
manual gates.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from case_api.web_runtime import WebRuntimeConfigurationBlocked, WebRuntimeSettings


REPORT_SCHEMA_VERSION = "lawcase-web-deployment-readiness-v1"
_MIGRATION_FILENAME = re.compile(r"^(?P<sequence>[0-9]{4})_[a-z0-9_]+[.]sql$")


def _default_migration_root() -> Path:
    return Path(__file__).resolve().parents[1] / "migrations"


def _discover_migration_chain(root: Path) -> tuple[str, ...]:
    """Discover the repository chain; every numeric slot must be unique/continuous."""

    if not isinstance(root, Path) or not root.is_dir():
        return ()
    rows: list[tuple[int, str]] = []
    for path in root.glob("*.sql"):
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        rows.append((int(match.group("sequence")), path.name))
    rows.sort()
    if not rows:
        return ()
    sequences = tuple(sequence for sequence, _name in rows)
    if sequences != tuple(range(1, sequences[-1] + 1)):
        return ()
    return tuple(name for _sequence, name in rows)


# This public compatibility constant is derived from the checked-in files, so
# adding 0040+ automatically extends deployment preflight without editing this
# script.  ``_check_migrations`` separately rejects a broken/duplicated chain.
REQUIRED_MIGRATIONS = _discover_migration_chain(_default_migration_root())


class CheckStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class WebReadinessCheck:
    check_id: str
    title: str
    status: CheckStatus
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class WebReadinessReport:
    target: str
    checks: tuple[WebReadinessCheck, ...]
    manual_gates: tuple[str, ...]

    @property
    def static_preflight_passed(self) -> bool:
        return all(check.status is CheckStatus.PASS for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "target": self.target,
            "static_preflight_passed": self.static_preflight_passed,
            "release_authorized": False,
            "checks": [{**asdict(check), "status": check.status.value} for check in self.checks],
            "manual_gates": list(self.manual_gates),
        }


def assess_web_deployment_readiness(
    *,
    environ: Mapping[str, str],
    migration_root: Path | None = None,
) -> WebReadinessReport:
    checks: list[WebReadinessCheck] = []
    settings: WebRuntimeSettings | None = None
    try:
        settings = WebRuntimeSettings.from_environment(environ)
    except WebRuntimeConfigurationBlocked:
        checks.append(WebReadinessCheck(
            "runtime_configuration",
            "Web 生产配置",
            CheckStatus.BLOCKED,
            "Web 生产环境变量不完整或未通过 fail-closed 校验。",
            "按部署说明填写 OIDC、三套 PostgreSQL、私有对象存储、扫描器和 PDF 渲染器配置。",
        ))
    else:
        checks.append(WebReadinessCheck(
            "runtime_configuration",
            "Web 生产配置",
            CheckStatus.PASS,
            "PRODUCTION_WEB 配置结构、来源和安全边界通过静态校验。",
        ))

    checks.append(_check_migrations(migration_root or _default_migration_root()))
    if settings is not None:
        checks.append(_check_executable(settings.clamav_executable, "ClamAV 扫描器"))
        checks.append(_check_executable(settings.pdftoppm_executable, "PDF 页面渲染器"))
        checks.append(WebReadinessCheck(
            "system_workers",
            "律所系统 Worker 身份",
            CheckStatus.PASS if settings.system_worker_ids_by_firm else CheckStatus.BLOCKED,
            "已配置每律所 SYSTEM_WORKER 身份。" if settings.system_worker_ids_by_firm else "未配置 SYSTEM_WORKER 身份。",
            None if settings.system_worker_ids_by_firm else "设置 LAWCASE_WEB_SYSTEM_WORKERS_JSON。",
        ))
    else:
        checks.extend((
            WebReadinessCheck("clamav", "ClamAV 扫描器", CheckStatus.BLOCKED, "因生产配置未通过，暂不能检查扫描器。"),
            WebReadinessCheck("pdftoppm", "PDF 页面渲染器", CheckStatus.BLOCKED, "因生产配置未通过，暂不能检查 PDF 渲染器。"),
            WebReadinessCheck("system_workers", "律所系统 Worker 身份", CheckStatus.BLOCKED, "因生产配置未通过，暂不能检查 Worker 身份。"),
        ))

    return WebReadinessReport(
        target="commercial-production-web",
        checks=tuple(checks),
        manual_gates=(
            "在目标数据库执行迁移并保存成功、回滚和备份恢复证据；本工具不会连接数据库。",
            "使用真实 OIDC+MFA 账号在 Chrome、Edge、Firefox 或 Safari 完成登录、建案和退出验收。",
            "用一份非敏感测试 PDF 完成上传、断线待核验、逐页决定、红框、锁定、生成和下载回执验收。",
            "用同一测试案件完成事实/交易确认、规则包审批、确定性利息测算与独立复核，并确认前置不完整时页面和接口均阻断。",
            "核对提交台账只读页、文书审批、证据/法律/测算绑定和提交包锁定；未完成最终核验不得出现法院提交下载。",
            "验证对象存储、ClamAV、Poppler、Worker 重启/租约恢复和跨律所访问隔离；静态通过不等于放行。",
        ),
    )


def _check_migrations(root: Path) -> WebReadinessCheck:
    discovered = _discover_migration_chain(root)
    if not discovered:
        return WebReadinessCheck(
            "web_migrations",
            "Web 数据库迁移文件",
            CheckStatus.BLOCKED,
            "迁移目录不存在、为空，或数字序号不连续/不唯一。",
            "保留从 0001 开始、无缺号的唯一迁移链。",
        )
    missing = [name for name in REQUIRED_MIGRATIONS if not (root / name).is_file()]
    if missing:
        return WebReadinessCheck(
            "web_migrations",
            "Web 数据库迁移文件",
            CheckStatus.BLOCKED,
            "缺少 Web 身份、会话、原件绑定或上传状态迁移文件。",
            "补齐：" + "、".join(missing),
        )
    latest = int(_MIGRATION_FILENAME.fullmatch(discovered[-1]).group("sequence"))
    return WebReadinessCheck(
        "web_migrations",
        "Web 数据库迁移文件",
        CheckStatus.PASS,
        f"0001—{latest:04d} 连续迁移文件齐全。",
    )


def _check_executable(path: Path, title: str) -> WebReadinessCheck:
    try:
        metadata = path.lstat()
    except OSError:
        return WebReadinessCheck(f"executable:{title}", title, CheckStatus.BLOCKED, "指定可执行文件不存在或无法读取。", f"确认路径：{path}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        return WebReadinessCheck(f"executable:{title}", title, CheckStatus.BLOCKED, "指定文件不是可直接执行的非符号链接文件。", f"确认路径和容器执行权限：{path}")
    return WebReadinessCheck(f"executable:{title}", title, CheckStatus.PASS, "可执行文件存在且权限满足静态要求。")


def main() -> int:
    parser = argparse.ArgumentParser(description="律所案件 Web 商用部署只读预检")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--migration-root", type=Path, default=None)
    args = parser.parse_args()
    report = assess_web_deployment_readiness(environ=os.environ, migration_root=args.migration_root)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"目标：{report.target}")
        for check in report.checks:
            print(f"[{check.status.value}] {check.title}：{check.message}")
        print("静态预检：" + ("通过（仍需人工放行）" if report.static_preflight_passed else "阻断"))
    return 0 if report.static_preflight_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
