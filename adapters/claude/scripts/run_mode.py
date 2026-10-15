#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch Claude Code with a semantic agent-platform work mode.")
    parser.add_argument("mode", choices=("economy", "balanced-economy", "optimal"))
    parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    parser.add_argument(
        "--claude-root",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mode_path = args.claude_root.expanduser().resolve() / "agent-platform" / "modes" / f"{args.mode}.json"
    if not mode_path.is_file():
        raise SystemExit(f"Claude mode is not installed: {mode_path}")
    mode = json.loads(mode_path.read_text(encoding="utf-8"))
    model = mode.get("model")
    effort = mode.get("effort")
    if not isinstance(model, str) or not isinstance(effort, str):
        raise SystemExit(f"invalid installed Claude mode: {mode_path}")
    environment = os.environ.copy()
    environment["AGENT_PLATFORM_MODE"] = args.mode
    environment["AGENT_SKILLS_ROOT"] = str(args.claude_root.expanduser().resolve() / "skills")
    command = ["claude", "--model", model]
    if effort != "default":
        command.extend(["--effort", effort])
    command.extend(args.claude_args)
    os.execvpe("claude", command, environment)


if __name__ == "__main__":
    main()
