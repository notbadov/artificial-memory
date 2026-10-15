#!/usr/bin/env python3
"""Write each app's Agent Platform channels into its MCP config: tasks, Agent Memory, Project Context.

Every entry starts the local bridge (``mcp_bridge.py``) with the managed client's interpreter, so
the app reaches each service as its own agent and no token appears in its config. An earlier
entry under the same key (a hand-made managed record or an owner-token header) is replaced, and
the older Agent Memory aliases are removed. Run after install_managed_mcp_servers.py.

Codex, Claude Code, Claude Desktop, OpenCode and Cursor configs are written directly. Kimi rewrites its own configs (the CLI's
mcp.json, Kimi Desktop's plugin manifest), so its entries go to a channels file that the Kimi
adapter installs (adapters/kimi/install.py --app-agent-channels).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from install_agent_memory_mcp import codex_tool_tables
from install_managed_mcp_servers import atomic_write, opencode_config_path, opencode_module, remove_codex_server

# Service -> config key. The keys are reserved: Console does not accept them as managed tools.
CONFIG_KEYS = {
    "collaboration": "agent-platform-collaboration",
    "agentMemory": "agentMemory",
    "projectContext": "projectContext",
}
LEGACY_KEYS = {"agentMemory": ("codexMemory", "codexmemory")}
# The task tools that only read: the ones the task server publishes with `readOnlyHint` (a test
# holds the two equal). Codex runs them without asking and asks before every other task tool.
# They are approved one by one (`approval_mode = "approve"`) under `default_tools_approval_mode =
# "prompt"`, values every Codex accepts. "writes" would say the same in one line, but Codex learned
# it only in 0.144.0, and one config file is read by every Codex on the computer: 0.137 refused the
# whole file and did not start (2026-10-15).
COLLABORATION_READ_TOOL_NAMES = (
    "export_collaboration_document",
    "get_apify_job",
    "get_available_collaboration_work",
    "get_collaboration_approvals",
    "get_collaboration_artifact_download",
    "get_collaboration_artifacts",
    "get_collaboration_changes",
    "get_collaboration_decisions",
    "get_collaboration_documents",
    "get_collaboration_handoffs",
    "get_collaboration_inbox",
    "get_collaboration_launch_offers",
    "get_collaboration_run_bindings",
    "get_collaboration_runs",
    "get_collaboration_work_dispatches",
    "get_collaboration_work_item",
    "get_collaboration_workspace",
    "resolve_collaboration_project",
)
APPS = ("codex", "claude", "opencode", "kimi", "claude-desktop", "cursor")


def bridge_arguments(bridge: Path, provider: str, service: str, endpoint: str) -> list[str]:
    return ["-E", "-s", str(bridge), "--provider", provider, "--service", service, "--endpoint", endpoint]


def parse_channel(value: str) -> tuple[str, str]:
    service, separator, endpoint = value.partition("=")
    if not separator or service not in CONFIG_KEYS:
        raise argparse.ArgumentTypeError(f"unknown channel: {value}")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?", endpoint):
        raise argparse.ArgumentTypeError(f"channel endpoint must be a plain HTTPS URL: {service}")
    return service, endpoint


def install_codex(path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    blocks = []
    for service, endpoint in channels.items():
        key = CONFIG_KEYS[service]
        for name in (key, *LEGACY_KEYS.get(service, ())):
            content = remove_codex_server(content, name)
        lines = [
            f"[mcp_servers.{key}]",
            f"command = {json.dumps(str(python))}",
            "args = " + json.dumps(bridge_arguments(bridge, "codex", service, endpoint), ensure_ascii=True),
            "enabled = true",
        ]
        if service == "collaboration":
            lines.append('default_tools_approval_mode = "prompt"')
            lines.append("\n" + "\n".join(codex_tool_tables(key, (), COLLABORATION_READ_TOOL_NAMES)))
        if service == "agentMemory":
            lines.append("\n" + "\n".join(codex_tool_tables(key)))
        blocks.append("\n".join(lines) + "\n")
    updated = (content.rstrip() + "\n\n" if content.strip() else "") + "\n".join(blocks)
    tomllib.loads(updated)
    atomic_write(path, updated)


def install_claude(path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Claude config must be a JSON object: {path}")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"Claude mcpServers must be a JSON object: {path}")
    for service, endpoint in channels.items():
        for legacy in LEGACY_KEYS.get(service, ()):
            servers.pop(legacy, None)
        servers[CONFIG_KEYS[service]] = {
            "type": "stdio", "command": str(python), "args": bridge_arguments(bridge, "claude", service, endpoint),
        }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def install_claude_desktop(path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    """The Claude chat app: plain command entries (it has no "type" field), replacing the older
    mcp-remote bridge that carried the owner's token in its environment."""
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Claude Desktop config must be a JSON object: {path}")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"Claude Desktop mcpServers must be a JSON object: {path}")
    for service, endpoint in channels.items():
        for legacy in LEGACY_KEYS.get(service, ()):
            servers.pop(legacy, None)
        servers[CONFIG_KEYS[service]] = {
            "command": str(python), "args": bridge_arguments(bridge, "claude-desktop", service, endpoint),
        }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def install_cursor(path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> bool:
    """Cursor's ~/.cursor/mcp.json: stdio entries in Claude Desktop's shape
    (https://cursor.com/docs/context/mcp). Cursor reads the file when it starts, so the file is
    written only when an entry changes, and that change asks for a restart (True)."""
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    value = json.loads(before) if before is not None else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Cursor MCP config must be a JSON object: {path}")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"Cursor mcpServers must be a JSON object: {path}")
    for service, endpoint in channels.items():
        for legacy in LEGACY_KEYS.get(service, ()):
            servers.pop(legacy, None)
        servers[CONFIG_KEYS[service]] = {"command": str(python), "args": bridge_arguments(bridge, "cursor", service, endpoint)}
    if before is not None and json.loads(before) == value:
        return False
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return True


def install_opencode(root: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    path = opencode_config_path(root)
    value = opencode_module().read_json(path)
    servers = value.setdefault("mcp", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"OpenCode mcp must be a JSON object: {path}")
    for service, endpoint in channels.items():
        for legacy in LEGACY_KEYS.get(service, ()):
            servers.pop(legacy, None)
        servers[CONFIG_KEYS[service]] = {
            "type": "local", "command": [str(python), *bridge_arguments(bridge, "opencode", service, endpoint)],
            "enabled": True,
        }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def install_kimi(path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    atomic_write(path, json.dumps({"version": 1, "servers": {
        CONFIG_KEYS[service]: {"command": str(python), "args": bridge_arguments(bridge, "kimi", service, endpoint)}
        for service, endpoint in channels.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def written_entries(provider: str, path: Path) -> dict:
    if provider == "codex":
        return tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
    if provider == "opencode":
        servers = opencode_module().read_json(opencode_config_path(path)).get("mcp", {})
        # OpenCode keeps the command and its arguments in one list.
        return {key: ({**entry, "command": entry["command"][0], "args": entry["command"][1:]}
                      if isinstance(entry, dict) and isinstance(entry.get("command"), list) and entry["command"]
                      else entry) for key, entry in servers.items()}
    if provider == "kimi":
        return json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})


def verify(provider: str, path: Path, python: Path, bridge: Path, channels: dict[str, str]) -> None:
    """Read the config back: an entry that is not exactly ours, or a token left anywhere in one of
    these entries, fails the sync."""
    servers = written_entries(provider, path)
    for service, endpoint in channels.items():
        key = CONFIG_KEYS[service]
        entry = servers.get(key)
        if (not isinstance(entry, dict) or entry.get("command") != str(python)
                or entry.get("args") != bridge_arguments(bridge, provider, service, endpoint)
                or {"url", "headers", "http_headers", "env", "environment"} & set(entry)
                or any(legacy in servers for legacy in LEGACY_KEYS.get(service, ()))):
            raise SystemExit(f"Channel {key} is missing or not the local bridge in {provider} config {path}")


def utf8_streams() -> None:
    """Read and say everything in UTF-8, whatever the code page of this computer.

    Whoever starts this file reads what it prints as UTF-8. An interpreter decides otherwise:
    on Windows its streams are in the code page of the system, and started with `-E` it ignores
    PYTHONUTF8 and PYTHONIOENCODING, so every sentence that is not ASCII arrives as other
    letters. Every file that is started carries this function word for word: the files are
    delivered in separate sets that do not import each other
    (tests/test_client_output_encoding.py)."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)


def main() -> None:
    utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="append", type=parse_channel, required=True,
                        help="SERVICE=URL, for example agentMemory=https://app.example/agent-memory/mcp")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--codex-config", type=Path, required=True)
    parser.add_argument("--claude-config", type=Path, required=True)
    parser.add_argument("--opencode-root", type=Path, help="OpenCode config directory (required for opencode)")
    parser.add_argument("--kimi-channels", type=Path, help="Channels file the Kimi adapter installs (required for kimi)")
    parser.add_argument("--claude-desktop-config", type=Path, help="Claude Desktop config (required for claude-desktop)")
    parser.add_argument("--cursor-config", type=Path, help="Cursor's mcp.json, ~/.cursor/mcp.json (required for cursor)")
    parser.add_argument("--provider", action="append", choices=APPS, required=True)
    args = parser.parse_args()
    channels = dict(args.channel)
    if len(channels) != len(args.channel):
        raise SystemExit("Each channel may be given once")
    if not args.bridge.is_file() or not args.python.is_file():
        raise SystemExit("Channel bridge or interpreter is missing")
    paths = {"codex": args.codex_config, "claude": args.claude_config, "opencode": args.opencode_root,
             "kimi": args.kimi_channels, "claude-desktop": args.claude_desktop_config, "cursor": args.cursor_config}
    installers = {"codex": install_codex, "claude": install_claude, "opencode": install_opencode, "kimi": install_kimi,
                  "claude-desktop": install_claude_desktop, "cursor": install_cursor}
    if any(paths[provider] is None for provider in args.provider):
        raise SystemExit("OpenCode needs --opencode-root, Kimi --kimi-channels, Claude Desktop --claude-desktop-config, "
                         "Cursor --cursor-config")
    restart, failed, configured = [], {}, []
    for provider in args.provider:
        if provider == "cursor":
            # Cursor's file is the person's to break: one that cannot be read fails Cursor alone.
            try:
                changed = installers[provider](paths[provider], args.python, args.bridge, channels)
            except (OSError, ValueError, SystemExit):
                failed[provider] = "config_unreadable"
                continue
            try:
                verify(provider, paths[provider], args.python, args.bridge, channels)
            except (OSError, ValueError, SystemExit):
                failed[provider] = "config_not_verified"
                continue
        else:
            changed = installers[provider](paths[provider], args.python, args.bridge, channels)
            verify(provider, paths[provider], args.python, args.bridge, channels)
        if changed is True:
            restart.append(provider)
        configured.append(provider)
    print(json.dumps({"ok": True, "configKeys": sorted(CONFIG_KEYS[service] for service in channels),
                      "providers": sorted(configured), "failed": failed,
                      # Apps that read their config at start and whose config changed now (Cursor).
                      "restartRequired": sorted(restart)}))


if __name__ == "__main__":
    main()
