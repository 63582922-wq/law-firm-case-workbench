"""Read-only, fail-closed desktop deployment preflight.

This command intentionally does *not* connect to PostgreSQL, start the
sidecar, read Keychain material, or modify any file.  It verifies the static
prerequisites that can be checked safely before an administrator enables a
desktop environment.  A passing static preflight is not a production release
authorization: database migration evidence and a real lawyer enrollment still
need their own controlled operational confirmation.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import platform
import stat
from typing import Callable, Mapping, Sequence

from case_api.desktop_persistent_runtime import (
    DesktopPersistentRuntimeBlocked,
    _configured_managed_root,
)
from case_api.desktop_trust_bootstrap import (
    DesktopTrustBootstrapBlocked,
    load_desktop_enrollment_trust,
)
from case_kernel.runtime import (
    RuntimeConfigurationBlocked,
    RuntimeMode,
    RuntimeSettings,
)
from psycopg.conninfo import conninfo_to_dict
from scripts.finalize_macos_bundle import (
    BundleFinalizationBlocked,
    EXPECTED_SIDECAR_NAME,
    inspect_bundle,
)


REPORT_SCHEMA_VERSION = "lawcase-desktop-deployment-readiness-v1"

# Keep this explicit. Adding a migration must update this manifest and its
# corresponding review/test rather than silently changing a desktop release.
REQUIRED_MIGRATIONS = (
    "0001_core.sql",
    "0002_case_ledgers.sql",
    "0003_evidence_manifest.sql",
    "0004_evidence_derivative_runs.sql",
    "0005_formal_calculations.sql",
    "0006_legal_source_rules.sql",
    "0007_submission_compilation.sql",
    "0008_official_source_capture_runs.sql",
    "0009_legal_source_license_review.sql",
    "0010_local_folder_intake.sql",
    "0011_evidence_intake_runs.sql",
    "0012_evidence_normalized_representations.sql",
    "0013_submission_work_product_review_binding.sql",
    "0014_office_render_verification.sql",
    "0015_reviewable_office_draft_pairs.sql",
    "0016_agent_execution_ledger.sql",
    "0017_external_request_ledger.sql",
    "0018_external_request_cost_currency.sql",
    "0019_document_consistency_reviews.sql",
    "0020_official_lpr_observations.sql",
    "0021_agent_draft_candidates.sql",
    "0022_ocr_review_candidates.sql",
)


class DeploymentTarget(str, Enum):
    """The only targets this source tree may assess."""

    INTERNAL_PREVIEW = "internal-preview"
    COMMERCIAL_PRODUCTION = "commercial-production"


class CheckStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ReadinessCheck:
    check_id: str
    title: str
    status: CheckStatus
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class DeploymentReadinessReport:
    target: str
    checks: tuple[ReadinessCheck, ...]
    manual_gates: tuple[str, ...]

    @property
    def static_preflight_passed(self) -> bool:
        return all(check.status is not CheckStatus.BLOCKED for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "target": self.target,
            "static_preflight_passed": self.static_preflight_passed,
            "release_authorized": False,
            "checks": [
                {
                    **asdict(check),
                    "status": check.status.value,
                }
                for check in self.checks
            ],
            "manual_gates": list(self.manual_gates),
        }


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_migration_root() -> Path:
    return project_root() / "backend" / "migrations"


def assess_deployment_readiness(
    *,
    environ: Mapping[str, str],
    target: DeploymentTarget = DeploymentTarget.COMMERCIAL_PRODUCTION,
    migration_root: Path | None = None,
    trust_bootstrap: Path | None = None,
    app_bundle: Path | None = None,
    sidecar: Path | None = None,
    platform_name: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DeploymentReadinessReport:
    """Return only static prerequisite evidence; never open an external service."""

    if app_bundle is not None and sidecar is not None:
        raise ValueError("app_bundle and sidecar are mutually exclusive")

    checks: list[ReadinessCheck] = []
    actual_platform = platform_name or platform.system()
    checks.append(_check_macos_platform(actual_platform))
    runtime_check, settings = _check_runtime_settings(environ, target=target)
    checks.append(runtime_check)
    checks.append(_check_migration_manifest(migration_root or default_migration_root()))
    checks.append(_check_enrollment_trust(trust_bootstrap, clock=clock))
    checks.append(_check_native_sidecar(app_bundle=app_bundle, sidecar=sidecar))
    checks.append(_check_managed_artifact_root(environ, settings=settings))
    if target is DeploymentTarget.COMMERCIAL_PRODUCTION:
        checks.append(_check_commercial_runtime_boundary(settings))

    return DeploymentReadinessReport(
        target=target.value,
        checks=tuple(checks),
        manual_gates=(
            "本工具不会连接数据库；须由受控迁移流程保存实际数据库迁移完成与回滚验证证据。",
            "本工具不会读取 Keychain；须在目标 Mac 上由律所完成登记、会话和逐案权限验证。",
            "静态检查通过不等于商用放行；发布责任人仍须确认安装包签名/公证、漏洞处置、备份恢复和撤销演练。",
            "本工具不替代真实案件、备份恢复、签名公证及律师工作流验收。",
        ),
    )


def _check_macos_platform(platform_name: str) -> ReadinessCheck:
    if platform_name == "Darwin":
        return ReadinessCheck(
            "macos_platform", "macOS 运行平台", CheckStatus.PASS, "目标平台为 macOS。"
        )
    return ReadinessCheck(
        "macos_platform",
        "macOS 运行平台",
        CheckStatus.BLOCKED,
        "桌面登记、Keychain 和发布包仅支持 macOS；当前平台不能启用该桌面环境。",
    )


def _check_runtime_settings(
    environ: Mapping[str, str], *, target: DeploymentTarget
) -> tuple[ReadinessCheck, RuntimeSettings | None]:
    try:
        settings = RuntimeSettings.from_environment(environ)
    except RuntimeConfigurationBlocked:
        target_label = (
            "commercial-production"
            if target is DeploymentTarget.COMMERCIAL_PRODUCTION
            else "postgres-internal-preview"
        )
        return (
            ReadinessCheck(
                "runtime_configuration",
                "运行时与数据库配置",
                CheckStatus.BLOCKED,
                "运行时配置不符合现有 fail-closed 规则。",
                f"使用受控环境变量配置 {target_label}；不要在此工具或日志中粘贴连接串。",
            ),
            None,
        )
    expected_mode = (
        RuntimeMode.COMMERCIAL_PRODUCTION
        if target is DeploymentTarget.COMMERCIAL_PRODUCTION
        else RuntimeMode.POSTGRES_INTERNAL_PREVIEW
    )
    if settings.mode is not expected_mode:
        target_label = expected_mode.value
        return (
            ReadinessCheck(
                "runtime_configuration",
                "运行时与数据库配置",
                CheckStatus.BLOCKED,
                f"当前运行时与目标 {target_label} 不匹配，不能将预览或演示配置当作该目标环境。",
                f"配置 CASE_WORKBENCH_RUNTIME_MODE={target_label} 及该模式要求的明确确认开关。",
            ),
            settings,
        )
    # RuntimeSettings has already parsed this string without opening a network
    # connection.  Re-read only the database *name* so a test database cannot
    # be mistaken for an operator desktop environment.
    try:
        database_name = conninfo_to_dict(settings.postgres_dsn or "").get("dbname", "")
    except Exception:
        database_name = ""
    normalized_database_name = database_name.lower()
    if target is DeploymentTarget.INTERNAL_PREVIEW and normalized_database_name.endswith("_test"):
        return (
            ReadinessCheck(
                "runtime_configuration",
                "运行时与数据库配置",
                CheckStatus.BLOCKED,
                "测试数据库不得用于律师桌面启用。",
                "改用隔离的 preview 数据库，并经独立迁移与恢复流程核验。",
            ),
            settings,
        )
    if target is DeploymentTarget.COMMERCIAL_PRODUCTION:
        if not normalized_database_name.endswith("_production"):
            return (
                ReadinessCheck(
                    "runtime_configuration",
                    "运行时与数据库配置",
                    CheckStatus.BLOCKED,
                    "commercial-production 只接受名称以 _production 结尾的独立数据库。",
                    "使用受控命名的商用 PostgreSQL 数据库，并保留迁移与恢复验证记录。",
                ),
                settings,
            )
        message = "commercial-production 已经独立确认，数据库名称带有受控 production 标记；本工具未连接数据库。"
    else:
        message = "运行时配置形状符合隔离的内部 preview 边界；本工具未连接数据库。"
    return (
        ReadinessCheck(
            "runtime_configuration", "运行时与数据库配置", CheckStatus.PASS, message
        ),
        settings,
    )


def _check_commercial_runtime_boundary(
    settings: RuntimeSettings | None,
) -> ReadinessCheck:
    """Make the production-mode distinction visible in a static report.

    ``RuntimeSettings`` is already parsed without opening a connection.  This
    second, explicit report item prevents a green checklist from obscuring
    whether the administrator actually selected the commercial runtime rather
    than merely supplying otherwise plausible PostgreSQL settings.
    """

    if settings is None or settings.mode is not RuntimeMode.COMMERCIAL_PRODUCTION:
        return ReadinessCheck(
            "commercial_runtime_boundary",
            "商用运行时边界",
            CheckStatus.BLOCKED,
            "未选择经明确确认的 commercial-production 运行时。",
            "设置 CASE_WORKBENCH_RUNTIME_MODE=commercial-production，并完成对应确认开关。",
        )
    if not settings.commercial_production_confirmed:
        return ReadinessCheck(
            "commercial_runtime_boundary",
            "商用运行时边界",
            CheckStatus.BLOCKED,
            "commercial-production 缺少独立明确确认，不能启动持久案件路由。",
            "仅在受控运维流程确认后设置 CASE_WORKBENCH_ENABLE_COMMERCIAL_PRODUCTION=YES。",
        )
    return ReadinessCheck(
        "commercial_runtime_boundary",
        "商用运行时边界",
        CheckStatus.PASS,
        "已选择并明确确认 commercial-production；仍须通过人工放行门后才能接入真实案件。",
    )


def _check_migration_manifest(root: Path) -> ReadinessCheck:
    try:
        if root.is_symlink() or not root.is_dir():
            raise OSError
        actual_paths = sorted(root.glob("*.sql"), key=lambda item: item.name)
        if any(path.is_symlink() or not path.is_file() or path.stat().st_size == 0 for path in actual_paths):
            raise OSError
    except OSError:
        return ReadinessCheck(
            "migration_manifest",
            "数据库迁移清单",
            CheckStatus.BLOCKED,
            "迁移目录不可用，或含有符号链接、非普通文件或空迁移。",
        )
    actual = tuple(path.name for path in actual_paths)
    if actual != REQUIRED_MIGRATIONS:
        return ReadinessCheck(
            "migration_manifest",
            "数据库迁移清单",
            CheckStatus.BLOCKED,
            "迁移文件与受审查清单不一致，不能假设实际数据库结构正确。",
            "核对新增、缺失或改名的迁移，并在代码审查后更新受控清单。",
        )
    return ReadinessCheck(
        "migration_manifest",
        "数据库迁移清单",
        CheckStatus.PASS,
        f"已找到顺序固定的 {len(REQUIRED_MIGRATIONS)} 个迁移文件；未执行数据库连接或迁移。",
    )


def _check_enrollment_trust(
    bootstrap: Path | None, *, clock: Callable[[], datetime] | None
) -> ReadinessCheck:
    try:
        trust = load_desktop_enrollment_trust(bootstrap, clock=clock)
    except DesktopTrustBootstrapBlocked:
        return ReadinessCheck(
            "enrollment_trust",
            "律所登记信任目录",
            CheckStatus.BLOCKED,
            "登记信任目录缺失、损坏或未通过门限/时效核验。",
        )
    if trust.phase != "READY":
        return ReadinessCheck(
            "enrollment_trust",
            "律所登记信任目录",
            CheckStatus.BLOCKED,
            "律所登记信任目录尚未配置；真实律师身份保持禁用。",
            "由律所安全运营替换发布包内的 NOT_CONFIGURED bootstrap，不得在浏览器或环境变量中配置根密钥。",
        )
    return ReadinessCheck(
        "enrollment_trust",
        "律所登记信任目录",
        CheckStatus.PASS,
        "登记信任目录已完成静态签名、时效与回滚链核验；未读取任何 Keychain 凭证。",
    )


def _check_native_sidecar(*, app_bundle: Path | None, sidecar: Path | None) -> ReadinessCheck:
    if app_bundle is not None:
        try:
            _, resolved_sidecar = inspect_bundle(app_bundle)
            metadata = resolved_sidecar.stat()
            if not metadata.st_mode & stat.S_IXUSR:
                raise BundleFinalizationBlocked("sidecar is not executable")
        except (BundleFinalizationBlocked, OSError):
            return ReadinessCheck(
                "native_sidecar",
                "原生本机服务",
                CheckStatus.BLOCKED,
                "macOS 应用包或其受控本机 sidecar 不符合固定发布布局。",
            )
        return ReadinessCheck(
            "native_sidecar",
            "原生本机服务",
            CheckStatus.PASS,
            "应用包中的受控本机 sidecar 存在、为普通可执行文件且位于固定布局内。",
        )
    if sidecar is None:
        return ReadinessCheck(
            "native_sidecar",
            "原生本机服务",
            CheckStatus.BLOCKED,
            "未提供应用包或受控本机 sidecar，不能验证桌面本机服务前置条件。",
            "使用 --app-bundle 指向待发布 .app，或使用 --sidecar 指向固定名称的本机服务。",
        )
    try:
        metadata = sidecar.stat()
        if (
            sidecar.name != EXPECTED_SIDECAR_NAME
            or sidecar.is_symlink()
            or not sidecar.is_file()
            or not metadata.st_mode & stat.S_IXUSR
        ):
            raise OSError
    except OSError:
        return ReadinessCheck(
            "native_sidecar",
            "原生本机服务",
            CheckStatus.BLOCKED,
            "受控本机 sidecar 缺失、名称不符、不是普通可执行文件或为符号链接。",
        )
    return ReadinessCheck(
        "native_sidecar",
        "原生本机服务",
        CheckStatus.PASS,
        "受控本机 sidecar 为固定名称的普通可执行文件。",
    )


def _check_managed_artifact_root(
    environ: Mapping[str, str], *, settings: RuntimeSettings | None
) -> ReadinessCheck:
    if settings is None or not settings.mode.is_persistent:
        return ReadinessCheck(
            "managed_artifact_root",
            "受管加密对象目录",
            CheckStatus.BLOCKED,
            "持久化运行时未通过，不能核验受管加密对象目录。",
        )
    try:
        _configured_managed_root(environ)
    except DesktopPersistentRuntimeBlocked:
        return ReadinessCheck(
            "managed_artifact_root",
            "受管加密对象目录",
            CheckStatus.BLOCKED,
            "受管加密对象目录未预先创建为私有、绝对、非符号链接目录。",
            "配置 CASE_WORKBENCH_MANAGED_ARTIFACT_ROOT 为权限仅限当前用户的独立目录。",
        )
    return ReadinessCheck(
        "managed_artifact_root",
        "受管加密对象目录",
        CheckStatus.PASS,
        "受管加密对象目录满足静态私有路径与权限前置条件；未读取 Keychain 密钥。",
    )


def render_text(report: DeploymentReadinessReport) -> str:
    result = "通过" if report.static_preflight_passed else "未通过"
    lines = [
        "律所案件 AI 工作台｜桌面部署前置检查",
        f"静态前置检查：{result}",
        "说明：本检查不连接数据库、不启动服务、不读取 Keychain，也不授予生产放行。",
        "",
    ]
    for check in report.checks:
        label = {CheckStatus.PASS: "通过", CheckStatus.BLOCKED: "阻断", CheckStatus.NOT_APPLICABLE: "不适用"}[check.status]
        lines.append(f"[{label}] {check.title}：{check.message}")
        if check.remediation:
            lines.append(f"  处理：{check.remediation}")
    lines.extend(("", "仍须由受控流程完成："))
    lines.extend(f"- {gate}" for gate in report.manual_gates)
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="律所案件 AI 工作台的只读桌面部署前置检查")
    parser.add_argument(
        "--target",
        choices=tuple(target.value for target in DeploymentTarget),
        default=DeploymentTarget.COMMERCIAL_PRODUCTION.value,
        help="仅检查所选目标的静态前置条件；commercial-production 仍须经人工放行。",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--trust-bootstrap", type=Path)
    sidecar_group = parser.add_mutually_exclusive_group()
    sidecar_group.add_argument("--app-bundle", type=Path)
    sidecar_group.add_argument("--sidecar", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    report = assess_deployment_readiness(
        environ=os.environ,
        target=DeploymentTarget(arguments.target),
        trust_bootstrap=arguments.trust_bootstrap,
        app_bundle=arguments.app_bundle,
        sidecar=arguments.sidecar,
    )
    if arguments.format == "json":
        print(json.dumps(report.as_dict(), ensure_ascii=False, separators=(",", ":")))
    else:
        print(render_text(report))
    return 0 if report.static_preflight_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
