"""Apply a local ad-hoc signature and verify the generated macOS Alpha bundle."""

from __future__ import annotations

import plistlib
from pathlib import Path
import platform
import subprocess
from typing import Protocol, Sequence


EXPECTED_BUNDLE_ID = "cn.lawcase.workbench"
EXPECTED_PRODUCT_NAME = "律所案件 AI 工作台.app"
EXPECTED_SIDECAR_NAME = "lawcase-local-api"


class BundleFinalizationBlocked(RuntimeError):
    """Raised when the generated bundle does not match the fixed desktop layout."""


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str]) -> None: ...


def _run_checked(command: Sequence[str]) -> None:
    subprocess.run(
        list(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _require_contained_file(app_path: Path, candidate: Path, label: str) -> Path:
    if candidate.is_symlink() or not candidate.is_file():
        raise BundleFinalizationBlocked(f"{label} 缺失或不是普通文件")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(app_path.resolve(strict=True))
    except ValueError as exc:
        raise BundleFinalizationBlocked(f"{label} 逃逸出应用包") from exc
    return resolved


def inspect_bundle(app_path: Path) -> tuple[Path, Path]:
    if app_path.name != EXPECTED_PRODUCT_NAME or app_path.is_symlink() or not app_path.is_dir():
        raise BundleFinalizationBlocked("应用包路径或产品名不符合固定桌面构建")
    info_path = _require_contained_file(app_path, app_path / "Contents" / "Info.plist", "Info.plist")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != EXPECTED_BUNDLE_ID:
        raise BundleFinalizationBlocked("应用包标识与固定产品标识不一致")
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name or "/" in executable_name:
        raise BundleFinalizationBlocked("主程序名称无效")
    executable_root = app_path / "Contents" / "MacOS"
    main_executable = _require_contained_file(
        app_path,
        executable_root / executable_name,
        "主程序",
    )
    sidecar = _require_contained_file(
        app_path,
        executable_root / EXPECTED_SIDECAR_NAME,
        "本机 API sidecar",
    )
    return main_executable, sidecar


def finalize_macos_bundle(app_path: Path, *, runner: CommandRunner = _run_checked) -> None:
    main_executable, sidecar = inspect_bundle(app_path)
    runner(["codesign", "--force", "--sign", "-", "--timestamp=none", str(sidecar)])
    runner(["codesign", "--force", "--sign", "-", "--timestamp=none", str(main_executable)])
    runner(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            "--identifier",
            EXPECTED_BUNDLE_ID,
            str(app_path.resolve(strict=True)),
        ]
    )
    runner(["codesign", "--verify", "--deep", "--strict", str(app_path.resolve(strict=True))])


def main() -> int:
    if platform.system() != "Darwin":
        print("非 macOS 构建：跳过 macOS 应用包签名终检")
        return 0
    project_root = Path(__file__).resolve().parents[2]
    app_path = (
        project_root
        / "web"
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / EXPECTED_PRODUCT_NAME
    )
    finalize_macos_bundle(app_path)
    print(f"macOS 应用包已完成本地临时签名和严格校验：{app_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
