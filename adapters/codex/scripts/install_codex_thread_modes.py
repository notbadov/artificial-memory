#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _codex_install import install_profile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install canonical Codex thread mode profiles into CODEX_HOME.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--codex-root", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    codex_root = args.codex_root.resolve()
    source_dir = repo_root / "adapters" / "codex" / "profiles"
    if not source_dir.is_dir():
        raise SystemExit(f"profile source directory not found: {source_dir}")

    installed = []
    for source in sorted(source_dir.glob("*.config.toml")):
        destination = codex_root / source.name
        install_profile(source, destination)
        installed.append(destination)

    if not installed:
        raise SystemExit(f"no profile files found in {source_dir}")

    print(f"installed {len(installed)} Codex thread profiles into {codex_root}")
    for path in installed:
        print(f" - {path.name}")
    print("Use: codex --profile balanced-economy")


if __name__ == "__main__":
    main()
