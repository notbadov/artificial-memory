#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start Kimi Code with a shared semantic mode.")
    parser.add_argument("mode")
    parser.add_argument("kimi_args", nargs=argparse.REMAINDER)
    parser.add_argument("--kimi-root", type=Path, default=Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code")))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.kimi_root.expanduser().resolve()
    mode_path = root / "agent-platform" / "modes" / f"{args.mode}.json"
    try:
        mode = json.loads(mode_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"unknown Kimi mode: {args.mode}") from error
    command = ["kimi", "--thinking" if mode.get("thinking") else "--no-thinking", *args.kimi_args]
    environment = os.environ.copy()
    environment["AGENT_PLATFORM_MODE"] = args.mode
    environment["AGENT_SKILLS_ROOT"] = str(root / "skills")
    effort = mode.get("effort")
    if isinstance(effort, str) and mode.get("thinking"):
        environment["KIMI_MODEL_THINKING_EFFORT"] = effort
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
