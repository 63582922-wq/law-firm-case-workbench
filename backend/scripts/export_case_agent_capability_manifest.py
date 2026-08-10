"""Synchronize the UI-safe Agent capability manifest from the Python registry."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from case_kernel.agent_capability_manifest import render_case_agent_capability_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "case-skill-manifest.json"
    rendered = render_case_agent_capability_manifest()
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    if args.check:
        if existing != rendered:
            print("case Agent capability manifest is stale", file=sys.stderr)
            return 1
        return 0
    target.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
