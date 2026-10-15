#!/usr/bin/env python3
"""Validate or install project-owned Agent Platform profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/python/agent-platform-core"))

from agent_platform_core.project_profiles import (  # noqa: E402
    install_project_profiles,
    load_project_profile_catalog,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    install = subparsers.add_parser("install")
    install.add_argument("--project-root", type=Path, required=True)
    install.add_argument("--provider", choices=("codex", "claude"), required=True)
    install.add_argument("--assignment-file", type=Path)
    install.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".agent-platform/projects",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "validate":
        catalog = load_project_profile_catalog(root)
        print(json.dumps({"ok": True, "profiles": len(catalog["profiles"])}, ensure_ascii=False))
        return
    receipt = install_project_profiles(
        platform_root=root,
        project_root=args.project_root.resolve(),
        provider=args.provider,
        state_root=args.state_root.resolve(),
        assignment=(json.loads(args.assignment_file.read_text(encoding="utf-8")) if args.assignment_file else None),
    )
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
