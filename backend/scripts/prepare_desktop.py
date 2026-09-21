"""Prepare the bundled sidecar, then build or serve the static frontend."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from build_desktop_sidecar import build_desktop_sidecar


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true")
    arguments = parser.parse_args()
    project_root = Path(__file__).resolve().parents[2]
    web_root = project_root / "web"
    build_desktop_sidecar()
    subprocess.run(
        ["pnpm", "dev" if arguments.dev else "build"],
        check=True,
        cwd=web_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
