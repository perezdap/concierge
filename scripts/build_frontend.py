#!/usr/bin/env python3
"""Build the Concierge Admin Console SPA (Vite) into frontend/dist."""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _npm_cmd() -> str:
    return "npm.cmd" if platform.system() == "Windows" else "npm"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Concierge admin frontend")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Run npm ci before build (recommended in CI/Docker)",
    )
    args = parser.parse_args()
    if not (FRONTEND / "package.json").is_file():
        print(f"missing {FRONTEND / 'package.json'}", file=sys.stderr)
        return 1
    npm = _npm_cmd()
    if args.install:
        subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
    subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)
    dist = FRONTEND / "dist"
    if not dist.is_dir():
        print("build did not produce frontend/dist", file=sys.stderr)
        return 1
    print(f"admin UI built at {dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())