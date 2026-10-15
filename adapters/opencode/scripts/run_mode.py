#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Start OpenCode with an Agent Platform semantic mode.")
    parser.add_argument("mode", choices=("economy", "balanced-economy", "optimal"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    mapping_path = root / "model-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.is_file() else {}
    command = ["opencode"]
    model = mapping.get(args.mode)
    if isinstance(model, str) and model:
        command.extend(("--model", model))
    command.extend(args.arguments)
    environment = os.environ.copy()
    environment["AGENT_PLATFORM_PROVIDER"] = "opencode"
    environment["AGENT_PLATFORM_MODE"] = args.mode
    environment["AGENT_SKILLS_ROOT"] = str(root.parent / "skills")
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
