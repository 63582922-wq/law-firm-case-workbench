"""Build the Python loopback service as a Tauri external binary."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def build_desktop_sidecar() -> Path:
    backend_root = Path(__file__).resolve().parents[1]
    project_root = backend_root.parent
    entrypoint = backend_root / "case_api" / "desktop_sidecar.py"
    trust_bootstrap = (
        backend_root / "case_api" / "deployment" / "enrollment_trust_bootstrap.json"
    )
    destination_root = project_root / "web" / "src-tauri" / "binaries"
    target = subprocess.run(
        ["rustc", "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{5,120}", target):
        raise RuntimeError("Rust target triple is invalid")

    with TemporaryDirectory(prefix="lawcase-sidecar-build-") as temporary:
        temporary_root = Path(temporary)
        dist_root = temporary_root / "dist"
        work_root = temporary_root / "work"
        spec_root = temporary_root / "spec"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--name",
                "lawcase-local-api",
                "--paths",
                str(backend_root),
                "--add-data",
                f"{trust_bootstrap}{os.pathsep}case_api/deployment",
                "--distpath",
                str(dist_root),
                "--workpath",
                str(work_root),
                "--specpath",
                str(spec_root),
                str(entrypoint),
            ],
            check=True,
            cwd=backend_root,
            timeout=300,
        )
        built = dist_root / "lawcase-local-api"
        if not built.is_file():
            raise RuntimeError("desktop sidecar binary was not produced")
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / f"lawcase-local-api-{target}"
        shutil.copy2(built, destination)
        destination.chmod(0o755)
        print(destination)
    return destination


def main() -> int:
    build_desktop_sidecar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
