#!/usr/bin/env python3
"""List the MCP servers an app has on this computer (names only), or give the address of one
remote server its owner asked for.

Run by the sync client from the verified release (agent_platform_core.mcp_inventory).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/python/agent-platform-core"))

from agent_platform_core.mcp_inventory import export_mcp_server, managed_keys, mcp_inventory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "export"))
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--config", type=Path, required=True, help="Codex config.toml or Claude's ~/.claude.json")
    parser.add_argument("--managed-state", type=Path, default=Path.home() / ".agent-platform/client/managed-mcp-state.json")
    parser.add_argument("--key", help="export: the server's name in the app's config")
    args = parser.parse_args()
    if args.command == "export":
        result = export_mcp_server(args.provider, config_path=args.config, key=args.key or "")
    else:
        result = {"servers": mcp_inventory(args.provider, config_path=args.config, managed_keys=managed_keys(args.managed_state))}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
