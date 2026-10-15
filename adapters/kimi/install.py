#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from compile_agent_catalog import write_compiled_catalog

CORE_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages/python/agent-platform-core"
if str(CORE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_PACKAGE_ROOT))

from agent_platform_core.managed_skills import (
    default_backup_root,
    legacy_recorded,
    platform_skill_sources,
    reconcile_skills,
    recorded_from_receipt,
)
from agent_platform_core.skill_delivery import (
    RETIRED_POLICY_BLOCK_IDS,
    delivered_policies,
    delivered_skill_sources,
    load_skill_delivery_manifest,
    policy_block_id,
    remove_managed_blocks,
)

# Which skills in <kimi home>/skills the platform installed (managed_skills).
SKILLS_RECEIPT = "agent-platform-skills.json"


MEMORY_SERVER_NAME = "agentMemory"
# Written by the sync client from Control Plane desired state (tools/install_managed_mcp_servers.py).
DEFAULT_MANAGED_MIRROR = Path.home() / ".agent-platform/client/managed-mcp-mirror.json"
# Written by the managed client: the Agent Platform channels Kimi reaches as its own agent through
# the local bridge (tools/install_app_agent_channels.py). No token is involved.
DEFAULT_APP_AGENT_CHANNELS = Path.home() / ".agent-platform/client/kimi-app-agent-channels.json"
APP_AGENT_CHANNEL_KEYS = frozenset({"agent-platform-collaboration", "agentMemory", "projectContext"})
MANAGED_KEYS_FILE = "agent-platform/managed-mcp-keys.json"
MEMORY_DEFAULT_URL = "https://app.artificial-memory.com/agent-memory/mcp"
HOOK_BLOCK_ID = "kimi-hooks"
PLUGIN_ID = "agent-platform"
DESKTOP_SEARCH_MAX_DEPTH = 12
DESKTOP_SEARCH_PRUNE = {
    "cache",
    "code cache",
    "gpucache",
    "logs",
    "node_modules",
    "temp",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install shared agent-platform behavior for Kimi.")
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--kimi-home", type=Path, action="append", help="Explicit Kimi home; repeatable")
    parser.add_argument(
        "--desktop-data-root",
        type=Path,
        action="append",
        help="Explicit Kimi Desktop data root used for bounded runtime discovery; repeatable",
    )
    parser.add_argument("--no-desktop", action="store_true")
    parser.add_argument(
        "--print-default-homes",
        action="store_true",
        help="Print homes detected while Kimi Desktop is still running, then exit",
    )
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--memory-url", default=os.environ.get("AGENT_MEMORY_URL", MEMORY_DEFAULT_URL))
    parser.add_argument("--memory-token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument("--memory-organization", default=os.environ.get("AGENT_MEMORY_ORGANIZATION_ID"))
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--managed-mcp-mirror", type=Path, default=DEFAULT_MANAGED_MIRROR)
    parser.add_argument("--app-agent-channels", type=Path, default=DEFAULT_APP_AGENT_CHANNELS)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--skill-delivery-manifest", type=Path)
    parser.add_argument("--skill-backup-root", type=Path, default=default_backup_root("kimi"))
    return parser.parse_args()


def default_desktop_data_roots() -> list[Path]:
    configured = os.environ.get("KIMI_DESKTOP_DATA_ROOT")
    if configured:
        return [Path(value).expanduser() for value in configured.split(os.pathsep) if value]
    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support/kimi-desktop"]
    if os.name == "nt":
        roots: list[Path] = []
        for variable in ("APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(variable)
            if value:
                roots.append(Path(value) / "kimi-desktop")
        return unique_paths(roots)
    return [Path.home() / ".config/kimi-desktop"]


def discover_desktop_homes(data_roots: list[Path]) -> list[Path]:
    homes: list[Path] = []
    for data_root in unique_paths(data_roots):
        if not data_root.is_dir():
            continue
        expected = data_root / "daimon-share/daimon/runtime/kimi-code/home"
        if expected.is_dir():
            homes.append(expected)
        root_depth = len(data_root.parts)
        for current_value, directory_names, _ in os.walk(data_root, followlinks=False):
            current = Path(current_value)
            depth = len(current.parts) - root_depth
            directory_names[:] = [
                name
                for name in directory_names
                if name.casefold() not in DESKTOP_SEARCH_PRUNE
                and not (current / name).is_symlink()
                and depth < DESKTOP_SEARCH_MAX_DEPTH
            ]
            if current.name == "home" and current.parent.name == "kimi-code":
                homes.append(current)
                directory_names[:] = []
    return unique_paths(homes)


def default_kimi_homes(include_desktop: bool, data_roots: list[Path] | None = None) -> list[Path]:
    configured = os.environ.get("KIMI_CODE_HOME")
    homes = [Path(configured).expanduser() if configured else Path.home() / ".kimi-code"]
    if not include_desktop:
        return homes
    homes.extend(discover_desktop_homes(data_roots or default_desktop_data_roots()))
    return unique_paths(homes)


def windows_system_tool(*parts: str) -> str:
    """The full path of a program of Windows itself, under System32 of this installation.

    Windows looks for a program named without a path in the directory of the process that
    starts it before it looks in PATH, so a `schtasks.exe` left there would run instead of the
    real one, in places with a secret in its environment. Without a SystemRoot that names a
    directory on a drive the system directory is unknown, and nothing is started. Every set of
    files delivered on its own carries this function word for word
    (tests/test_client_without_posix.py)."""
    root = os.environ.get("SystemRoot") or ""
    if not ntpath.isabs(root) or not ntpath.splitdrive(root)[0]:
        raise OSError("SystemRoot does not name the Windows directory; a system tool is not started")
    return ntpath.join(root, "System32", *parts)


def kimi_desktop_is_running() -> bool:
    if sys.platform == "darwin":
        return subprocess.run(
            ["pgrep", "-x", "Kimi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    if os.name == "nt":
        result = subprocess.run(
            [windows_system_tool("tasklist.exe"), "/FI", "IMAGENAME eq Kimi.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode == 0 and '"Kimi.exe"' in result.stdout
    return False


def unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def config_path_for_home(kimi_home: Path) -> Path:
    if (
        kimi_home.name == "home"
        and kimi_home.parent.name == "kimi-code"
        and any(part.casefold() == "kimi-desktop" for part in kimi_home.parts)
    ):
        return kimi_home.parent / "config.toml"
    return kimi_home / "config.toml"


def desktop_root_for_home(kimi_home: Path) -> Path | None:
    if kimi_home.name != "home" or kimi_home.parent.name != "kimi-code":
        return None
    for ancestor in kimi_home.parents:
        if ancestor.name == "daimon":
            return ancestor
        if ancestor.name.casefold() == "kimi-desktop":
            return ancestor
    return None


def install_directory(source: Path, destination: Path, allowed_root: Path) -> None:
    allowed = allowed_root.resolve(strict=False)
    resolved = destination.resolve(strict=False)
    if resolved != allowed and allowed not in resolved.parents:
        raise SystemExit(f"refusing to replace directory outside {allowed}: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        shutil.copytree(source, staged)
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.bak.{uuid.uuid4().hex}")
            destination.rename(backup)
            try:
                staged.rename(destination)
            except Exception:
                if not destination.exists() and backup.exists():
                    backup.rename(destination)
                raise
            else:
                resolved_backup = backup.resolve(strict=False)
                if resolved_backup.parent != destination.parent.resolve(strict=False):
                    raise SystemExit(f"refusing to delete backup outside {destination.parent}: {resolved_backup}")
                if not resolved_backup.name.startswith(f".{destination.name}.bak."):
                    raise SystemExit(f"refusing to delete unexpected backup: {resolved_backup}")
                shutil.rmtree(resolved_backup)
        else:
            staged.rename(destination)


def remove_managed_directory(destination: Path, allowed_root: Path) -> None:
    allowed = allowed_root.resolve(strict=False)
    resolved = destination.resolve(strict=False)
    if allowed not in resolved.parents:
        raise SystemExit(f"refusing to remove directory outside {allowed}: {destination}")
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink():
            raise SystemExit(f"refusing to remove unexpected managed path: {destination}")
        shutil.rmtree(destination)


def atomic_write(target: Path, content: str, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent) as handle:
        handle.write(content)
        staged = Path(handle.name)
    if mode is not None:
        staged.chmod(mode)
    staged.replace(target)


def managed_block(block_id: str, body: str) -> str:
    return f"<!-- agent-platform:{block_id}:start -->\n{body.strip()}\n<!-- agent-platform:{block_id}:end -->"


def instruction_blocks(platform_root: Path) -> dict[str, str]:
    blocks = {"kimi-base": (platform_root / "adapters/kimi/KIMI_AGENTS.md").read_text(encoding="utf-8")}
    for policy in delivered_policies(platform_root):
        blocks[policy_block_id(policy)] = policy.read_text(encoding="utf-8")
    return blocks


def render_instructions(platform_root: Path) -> str:
    return "\n\n".join(managed_block(block_id, body) for block_id, body in instruction_blocks(platform_root).items()) + "\n"


def install_instructions(platform_root: Path, target: Path) -> None:
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    updated = remove_managed_blocks(current, RETIRED_POLICY_BLOCK_IDS)
    for block_id, body in instruction_blocks(platform_root).items():
        start = f"<!-- agent-platform:{block_id}:start -->"
        end = f"<!-- agent-platform:{block_id}:end -->"
        rendered = managed_block(block_id, body)
        pattern = re.compile(f"{re.escape(start)}.*?{re.escape(end)}", re.DOTALL)
        updated = pattern.sub(rendered, updated) if pattern.search(updated) else f"{updated.rstrip()}\n\n{rendered}\n"
    atomic_write(target, updated.lstrip() if not current else updated)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"expected a JSON object in {path}")
    return value


def token_from_codex_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    server = servers.get("agentMemory") or servers.get("codexMemory")
    if not isinstance(server, dict):
        return None
    headers = server.get("http_headers")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    return authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def organization_from_codex_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = config.get("mcp_servers")
    server = (servers.get("agentMemory") or servers.get("codexMemory")) if isinstance(servers, dict) else None
    headers = server.get("http_headers") if isinstance(server, dict) else None
    value = headers.get("X-Agent-Platform-Organization") if isinstance(headers, dict) else None
    return value if isinstance(value, str) else None


def token_from_claude_config(path: Path) -> str | None:
    try:
        servers = read_json(path).get("mcpServers")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(servers, dict):
        return None
    server = servers.get("agentMemory") or servers.get("codexMemory")
    headers = server.get("headers") if isinstance(server, dict) else None
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    return authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def organization_from_claude_config(path: Path) -> str | None:
    try:
        servers = read_json(path).get("mcpServers")
    except (OSError, json.JSONDecodeError):
        return None
    server = (servers.get("agentMemory") or servers.get("codexMemory")) if isinstance(servers, dict) else None
    headers = server.get("headers") if isinstance(server, dict) else None
    value = headers.get("X-Agent-Platform-Organization") if isinstance(headers, dict) else None
    return value if isinstance(value, str) else None


def install_memory(config_path: Path, url: str, token: str, organization: str) -> None:
    config = read_json(config_path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"mcpServers must be a JSON object in {config_path}")
    servers[MEMORY_SERVER_NAME] = {
        "url": url,
        "headers": {"Authorization": f"Bearer {token}", "X-Agent-Platform-Organization": organization},
    }
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def remove_legacy_agent_hook_tables(content: str) -> str:
    """Remove only legacy Kimi hook tables owned by agent-platform.

    Older Windows installs could contain an unmarked hook command with raw
    backslashes.  Such a command makes the complete TOML document impossible
    to parse, so it cannot be migrated through the regular managed markers.
    Keep every unrelated table and setting byte-for-byte.
    """
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "[[hooks]]":
            result.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not re.match(r"^\s*\[\[?.*?\]\]?\s*(?:#.*)?$", lines[end]):
            end += 1
        table = "".join(lines[index:end])
        if "learning_gate_hook.py" not in table and "agent-platform/scripts" not in table:
            result.append(table)
        index = end
    return "".join(result)


def prepare_existing_hook_config(config_path: Path, current: str) -> str:
    start = f"# agent-platform:{HOOK_BLOCK_ID}:start"
    end = f"# agent-platform:{HOOK_BLOCK_ID}:end"
    without_managed = re.sub(
        f"\n?{re.escape(start)}.*?{re.escape(end)}\n?",
        "\n",
        current,
        flags=re.DOTALL,
    )
    cleaned = remove_legacy_agent_hook_tables(without_managed)
    if not cleaned.strip():
        return ""
    try:
        tomllib.loads(cleaned)
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(
            f"Existing Kimi config is invalid outside agent-platform-managed hooks and was left unchanged: "
            f"{config_path}: {error}"
        ) from error
    return cleaned.rstrip() + "\n"


def install_hooks(config_path: Path, kimi_home: Path) -> None:
    current = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    preserved = prepare_existing_hook_config(config_path, current)
    python = str(Path(sys.executable).resolve())
    script = str(kimi_home / "agent-platform/scripts/learning_gate_hook.py")
    command = f'{python} {json.dumps(script)}'
    body = "\n\n".join(
        "\n".join((
            "[[hooks]]",
            f"event = {toml_string(event)}",
            f"command = {toml_string(command)}",
            "timeout = 30",
        ))
        for event in ("SessionStart", "UserPromptSubmit", "Stop")
    )
    start = f"# agent-platform:{HOOK_BLOCK_ID}:start"
    end = f"# agent-platform:{HOOK_BLOCK_ID}:end"
    rendered = f"{start}\n{body}\n{end}"
    updated = f"{preserved.rstrip()}\n\n{rendered}\n" if preserved else f"{rendered}\n"
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"Refusing to write invalid Kimi config: {config_path}: {error}") from error
    atomic_write(config_path, updated, mode=0o600)


def hook_command(script: str) -> str:
    executable = str(Path(sys.executable).resolve())
    if os.name == "nt":
        return subprocess.list2cmdline([executable, script])
    return f"{shlex.quote(executable)} {shlex.quote(script)}"


def read_managed_mirror(path: Path) -> dict[str, dict[str, Any]]:
    """Managed MCP servers (for example the task server) targeted at Kimi on this computer.

    Kimi agents receive the task server here, as Codex and Claude receive it from Control Plane.
    """
    if not path.is_file():
        return {}
    servers = read_json(path).get("servers")
    if not isinstance(servers, dict) or any(
        not isinstance(key, str) or key in {MEMORY_SERVER_NAME, "codexMemory"}
        or not isinstance(entry, dict) or not isinstance(entry.get("url"), str)
        for key, entry in servers.items()
    ):
        raise SystemExit(f"managed MCP mirror is invalid: {path}")
    for key, entry in servers.items():
        if not public_https(entry["url"]):
            raise SystemExit(f"managed MCP server address must be a public HTTPS URL: {key}")
    return servers


def public_https(url: object) -> bool:
    """The addresses the service accepts for a managed server (managed_mcp.py) and the installer
    of managed servers holds (tools/install_managed_mcp_servers.py), held here too: a token is
    never sent in clear text, nor to this computer itself. Kept here rather than imported, since
    a client can load this adapter next to an installer of another release."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    return (parsed.scheme == "https" and bool(hostname) and parsed.username is None and parsed.password is None
            and not parsed.fragment and hostname not in {"localhost", "127.0.0.1", "::1"})


def read_app_agent_channels(path: Path) -> dict[str, dict[str, Any]]:
    """Kimi's own agent channels from the managed client: the local bridge, never a token."""
    if not path.is_file():
        return {}
    servers = read_json(path).get("servers")
    if not isinstance(servers, dict) or any(
        key not in APP_AGENT_CHANNEL_KEYS or not isinstance(entry, dict) or set(entry) != {"command", "args"}
        or not isinstance(entry["command"], str) or not isinstance(entry["args"], list)
        or not all(isinstance(argument, str) for argument in entry["args"])
        or "--provider" not in entry["args"] or entry["args"][entry["args"].index("--provider") + 1:][:1] != ["kimi"]
        for key, entry in servers.items()
    ):
        raise SystemExit(f"Kimi agent channels are invalid: {path}")
    return servers


def verify_mcp_servers(path: Path, expected: dict[str, dict[str, Any]], key: str = "mcpServers") -> None:
    found = read_json(path).get(key)
    found = found if isinstance(found, dict) else {}
    identity = ("command", "args", "url")
    missing = [name for name, entry in expected.items()
               if {field: (found.get(name) or {}).get(field) for field in identity}
               != {field: entry.get(field) for field in identity}]
    if missing:
        raise SystemExit(f"Kimi MCP servers missing from {path}: {', '.join(missing)}")


def recorded_managed_keys(owned_path: Path) -> set[str]:
    """The entries of mcp.json this adapter recorded as written. An earlier adapter wrote the
    same {"keys": [...]} record, naming every key it wrote, so its record is read as it is."""
    if not owned_path.is_file():
        return set()
    keys = read_json(owned_path).get("keys", [])
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise SystemExit(f"managed MCP ownership record is invalid: {owned_path}")
    return set(keys)


def install_managed_servers(kimi_home: Path, config_path: Path, managed: dict[str, dict[str, Any]]) -> list[str]:
    """Write the managed servers into Kimi's mcp.json; return the names left to another entry.

    A delivered server must not replace an entry of the same name that the person added, with
    their own token perhaps. As in the installer of managed servers
    (tools/install_managed_mcp_servers.py), only an entry this adapter recorded as written is
    replaced or removed; a delivered server whose name another entry has is not written and is
    reported as a conflict. The platform's own channel names (APP_AGENT_CHANNEL_KEYS) count as
    the platform's whoever wrote them: an earlier token install wrote agentMemory unrecorded,
    and a disconnect takes those names out of every app (sync_disconnect.py ACCESS_CONFIG_KEYS).
    """
    owned_path = kimi_home / MANAGED_KEYS_FILE
    if not managed and not owned_path.is_file():
        return []
    config = read_json(config_path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"mcpServers must be a JSON object in {config_path}")
    recorded = recorded_managed_keys(owned_path)
    conflicts = sorted(name for name in managed if name in servers and name not in recorded | APP_AGENT_CHANNEL_KEYS)
    written = {name: entry for name, entry in managed.items() if name not in conflicts}
    for name in recorded - set(written):
        servers.pop(name, None)
    servers.update(written)
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    atomic_write(owned_path, json.dumps({"keys": sorted(written)}) + "\n", mode=0o600)
    verify_mcp_servers(config_path, written)
    for name in conflicts:
        print(f"left your own MCP server in place: {name} in {config_path} (a managed server has the same name)")
    return conflicts


BRIDGE_LAUNCHER = "bin/agent-platform-bridge.cmd" if os.name == "nt" else "bin/agent-platform-bridge"


def plugin_servers(servers: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Kimi Desktop drops a plugin MCP server whose command is an absolute path: it must be a PATH
    command or start with "./" inside the plugin (kimi-code 0.31 normalizePluginMcpServer). The
    agent channels therefore start a launcher shipped in the plugin, which runs the managed
    client's bridge. Returns the plugin's entries and the launcher script, if one is needed."""
    bridged = {name: entry for name, entry in servers.items() if "command" in entry}
    if not bridged:
        return dict(servers), None
    prefixes = {(entry["command"], *entry["args"][:3]) for entry in bridged.values()}
    if len(prefixes) != 1 or next(iter(prefixes))[1:3] != ("-E", "-s"):
        raise SystemExit("Kimi agent channels must share one bridge")
    python, _, _, bridge = next(iter(prefixes))
    if os.name == "nt":
        # Text mode writes CRLF on Windows.
        script = f'@echo off\n"{python}" -E -s "{bridge}" %*\n'
    else:
        script = f"#!/bin/sh\nexec {shlex.quote(python)} -E -s {shlex.quote(bridge)} \"$@\"\n"
    entries = {name: ({"command": f"./{BRIDGE_LAUNCHER}", "args": entry["args"][3:]} if name in bridged else entry)
               for name, entry in servers.items()}
    return entries, script


def install_desktop_plugin(
    platform_root: Path,
    kimi_home: Path,
    desktop_root: Path,
    memory_url: str,
    token: str | None,
    organization: str | None,
    managed: dict[str, dict[str, Any]] | None = None,
    skills: dict[str, Path] | None = None,
    delivery: dict | None = None,
) -> None:
    managed = managed or {}
    delivery = delivery or load_skill_delivery_manifest(None)
    skills = skills if skills is not None else delivered_skills(platform_root, delivery)
    source_root = desktop_root / "plugin-packages" / PLUGIN_ID
    managed_root = kimi_home / "plugins" / "managed" / PLUGIN_ID
    with tempfile.TemporaryDirectory(prefix="agent-platform-kimi-plugin-") as temporary:
        plugin = Path(temporary) / PLUGIN_ID
        (plugin / "skills").mkdir(parents=True)
        for identifier, source in skills.items():
            shutil.copytree(source, plugin / "skills" / identifier,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        shutil.copytree(platform_root / "shared/workflows", plugin / "agent-platform/workflows")
        write_compiled_catalog(platform_root, "kimi", plugin / "agent-platform/catalog.json")
        shutil.copytree(platform_root / "adapters/kimi/scripts", plugin / "scripts")
        shutil.copytree(platform_root / "adapters/kimi/modes", plugin / "modes")
        atomic_write(plugin / "SYSTEM.md", render_instructions(platform_root))
        manifest: dict[str, Any] = {
            "$schema": "https://catalog.msh.team/schemas/kimi.plugin.schema.json",
            "name": PLUGIN_ID,
            "version": "1.0.0",
            "description": "Shared policies, skills, hooks, modes, and durable Agent Memory from Artificial Memory.",
            "skills": "./skills/",
            "systemPromptPath": "./SYSTEM.md",
            "hooks": [
                {
                    "event": event,
                    "command": hook_command("./scripts/learning_gate_hook.py"),
                    "timeout": 30,
                }
                for event in ("SessionStart", "UserPromptSubmit", "Stop")
            ],
            "interface": {
                "displayName": "Agent Platform",
                "shortDescription": "Shared memory, skills, policies, and verification workflows",
                "longDescription": "Connects Kimi to Artificial Memory: shared memory, skills, policies and workflows.",
                "developerName": "Artificial Memory",
                "category": "PRODUCTIVITY",
            },
        }
        servers, launcher = plugin_servers(managed)
        if launcher is not None:
            atomic_write(plugin / BRIDGE_LAUNCHER, launcher, mode=0o755)
        if token:
            servers[MEMORY_SERVER_NAME] = {
                "url": memory_url,
                "headers": {"Authorization": f"Bearer {token}", "X-Agent-Platform-Organization": organization},
            }
        if servers:
            manifest["mcpServers"] = servers
        atomic_write(plugin / "kimi.plugin.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o600)
        install_directory(plugin, source_root, desktop_root)
    install_directory(source_root, managed_root, kimi_home)

    registry_path = kimi_home / "plugins" / "installed.json"
    registry = read_json(registry_path)
    plugins = registry.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise SystemExit(f"plugins must be a JSON array in {registry_path}")
    existing = next((entry for entry in plugins if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID), None)
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    installed_at = existing.get("installedAt") if isinstance(existing, dict) else None
    plugins[:] = [entry for entry in plugins if not (isinstance(entry, dict) and entry.get("id") == PLUGIN_ID)]
    plugins.append({
        "id": PLUGIN_ID,
        "root": str(managed_root),
        "source": "local-path",
        "enabled": True,
        "installedAt": installed_at if isinstance(installed_at, str) else now,
        "updatedAt": now,
        "originalSource": str(source_root),
    })
    registry["version"] = 1
    atomic_write(registry_path, json.dumps(registry, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def remove_managed_text_blocks(target: Path, block_ids: list[str], prefix: str) -> None:
    if not target.is_file():
        return
    current = target.read_text(encoding="utf-8")
    updated = current
    for block_id in block_ids:
        start = f"{prefix} agent-platform:{block_id}:start"
        end = f"{prefix} agent-platform:{block_id}:end"
        updated = re.sub(f"\n?{re.escape(start)}.*?{re.escape(end)}\n?", "\n", updated, flags=re.DOTALL)
    if updated.strip():
        atomic_write(target, updated.strip() + "\n")
    else:
        target.unlink()


def delivered_skills(platform_root: Path, delivery: dict) -> dict[str, Path]:
    # Kimi gets no external-skill state from Control Plane: registry defaults decide.
    registry = read_json(platform_root / "shared/external-skills/registry.json")
    defaults = {item["id"]: bool(item.get("defaultEnabled")) for item in registry.get("skills", [])}
    return delivered_skill_sources(platform_root, delivery, defaults)


def reconcile_home_skills(platform_root: Path, kimi_home: Path, desired: dict[str, Path],
                          delivery: dict, backup_root: Path) -> dict:
    """<kimi home>/skills: a user's own skill is never changed; platform copies are tracked."""

    receipt_path = kimi_home / SKILLS_RECEIPT
    if receipt_path.is_file():
        recorded = recorded_from_receipt(read_json(receipt_path))
    else:
        agents = kimi_home / "AGENTS.md"
        installed_before = (kimi_home / "agent-platform").exists() or (
            agents.is_file() and "<!-- agent-platform:" in agents.read_text(encoding="utf-8"))
        recorded = legacy_recorded(kimi_home / "skills", platform_root, installed_before)
    result = reconcile_skills(skills_root=kimi_home / "skills", desired=desired, recorded=recorded,
                              backup_root=backup_root, platform_sources=platform_skill_sources(platform_root))
    atomic_write(receipt_path, json.dumps({
        "version": 1, "managed_skill_digests": result["managed"], "skill_conflicts": result["conflicts"],
        "skill_backups": result["backups"], "skill_delivery": delivery,
    }, ensure_ascii=False, indent=2) + "\n")
    for identifier in result["conflicts"]:
        print(f"left your own skill in place: {kimi_home / 'skills' / identifier} (a platform skill has the same name)")
    for backup in result["backups"]:
        print(f"kept the previous copy of {backup['id']} in {backup['path']}")
    return result


def migrate_legacy_desktop_install(platform_root: Path, kimi_home: Path, delivery: dict, backup_root: Path) -> None:
    # Desktop takes platform skills from the plugin; the old copies in <home>/skills go, but only
    # ours: a same-named user skill stays and a copy the user edited is backed up, never deleted
    # for having a platform skill's name.
    reconcile_home_skills(platform_root, kimi_home, {}, delivery, backup_root)
    remove_managed_directory(kimi_home / "agent-platform", kimi_home)
    agents = kimi_home / "AGENTS.md"
    if agents.is_file():
        current = agents.read_text(encoding="utf-8")
        for block_id in instruction_blocks(platform_root):
            current = re.sub(
                f"\n?{re.escape(f'<!-- agent-platform:{block_id}:start -->')}.*?"
                f"{re.escape(f'<!-- agent-platform:{block_id}:end -->')}\n?",
                "\n",
                current,
                flags=re.DOTALL,
            )
        atomic_write(agents, current.strip() + "\n") if current.strip() else agents.unlink()
    remove_managed_text_blocks(config_path_for_home(kimi_home), [HOOK_BLOCK_ID], "#")
    mcp_path = kimi_home / "mcp.json"
    if mcp_path.is_file():
        config = read_json(mcp_path)
        servers = config.get("mcpServers")
        if isinstance(servers, dict):
            servers.pop(MEMORY_SERVER_NAME, None)
            servers.pop("codexMemory", None)
            atomic_write(mcp_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def install_home(
    platform_root: Path, kimi_home: Path, memory_url: str,
    token: str | None, organization: str | None,
    managed: dict[str, dict[str, Any]] | None = None,
    delivery: dict | None = None,
    backup_root: Path | None = None,
) -> list[str]:
    """Install into one Kimi home; return the managed servers left to an entry of the person's."""
    managed = managed or {}
    delivery = delivery or load_skill_delivery_manifest(None)
    backup_root = backup_root or default_backup_root("kimi")
    kimi_home.mkdir(parents=True, exist_ok=True)
    desktop_root = desktop_root_for_home(kimi_home)
    if desktop_root is not None:
        migrate_legacy_desktop_install(platform_root, kimi_home, delivery, backup_root)
        install_desktop_plugin(platform_root, kimi_home, desktop_root, memory_url, token, organization, managed,
                               delivered_skills(platform_root, delivery), delivery)
        verify_mcp_servers(desktop_root / "plugin-packages" / PLUGIN_ID / "kimi.plugin.json", plugin_servers(managed)[0])
        # The plugin's manifest is the platform's alone: no entry of the person's is in it.
        return []
    reconcile_home_skills(platform_root, kimi_home, delivered_skills(platform_root, delivery), delivery, backup_root)
    install_directory(
        platform_root / "shared/workflows",
        kimi_home / "agent-platform/workflows",
        kimi_home,
    )
    write_compiled_catalog(
        platform_root,
        "kimi",
        kimi_home / "agent-platform/catalog.json",
    )
    install_directory(platform_root / "adapters/kimi/modes", kimi_home / "agent-platform/modes", kimi_home)
    install_directory(platform_root / "adapters/kimi/scripts", kimi_home / "agent-platform/scripts", kimi_home)
    install_instructions(platform_root, kimi_home / "AGENTS.md")
    install_hooks(config_path_for_home(kimi_home), kimi_home)
    if token:
        install_memory(kimi_home / "mcp.json", memory_url, token, organization)
    return install_managed_servers(kimi_home, kimi_home / "mcp.json", managed)


def main() -> None:
    args = parse_args()
    data_roots = unique_paths(args.desktop_data_root or default_desktop_data_roots())
    if args.print_default_homes:
        homes = default_kimi_homes(not args.no_desktop, data_roots)
        if not args.no_desktop and not discover_desktop_homes(data_roots):
            existing_roots = [path for path in data_roots if path.is_dir()]
            if existing_roots:
                roots = ", ".join(str(path) for path in existing_roots)
                raise SystemExit(
                    "Kimi Desktop data exists, but its embedded kimi-code home was not found under: "
                    f"{roots}. Open Kimi Work once, close Kimi completely, and rerun the bootstrap."
                )
        for home in homes:
            print(home)
        return
    platform_root = args.platform_root.expanduser().resolve()
    if not (platform_root / "shared/skills").is_dir() or not (platform_root / "adapters/kimi").is_dir():
        raise SystemExit(f"invalid agent-platform root: {platform_root}")
    homes = unique_paths(args.kimi_home or default_kimi_homes(not args.no_desktop, data_roots))
    detected_desktop_homes = set(discover_desktop_homes(data_roots))
    desktop_homes = {home for home in homes if desktop_root_for_home(home) is not None}
    live_desktop_homes = set(discover_desktop_homes(default_desktop_data_roots()))
    if desktop_homes.intersection(live_desktop_homes) and kimi_desktop_is_running():
        raise SystemExit(
            "Kimi Desktop is running and can overwrite its embedded runtime while exiting. "
            "Close Kimi Desktop first or use bootstrap-kimi.sh/bootstrap-kimi.ps1, "
            "which closes and reopens it safely."
        )
    token: str | None = None
    organization: str | None = None
    channels = read_app_agent_channels(args.app_agent_channels.expanduser())
    # With the managed client's agent channels, Kimi reaches Agent Memory as its own agent.
    if not args.skip_memory and MEMORY_SERVER_NAME not in channels:
        token = (
            args.memory_token
            or token_from_codex_config(args.codex_config.expanduser().resolve())
            or token_from_claude_config(args.claude_config.expanduser().resolve())
        )
        if not token:
            raise SystemExit(
                "Agent Memory token is unavailable. Set AGENT_MEMORY_TOKEN, pass --memory-token, "
                "or keep agentMemory configured in Codex or Claude."
            )
        organization = (
            args.memory_organization
            or organization_from_codex_config(args.codex_config.expanduser().resolve())
            or organization_from_claude_config(args.claude_config.expanduser().resolve())
            or ""
        ).strip()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
            raise SystemExit("Agent Memory organization is required")
    managed = {**read_managed_mirror(args.managed_mcp_mirror.expanduser()), **channels}
    delivery = load_skill_delivery_manifest(args.skill_delivery_manifest)
    conflicts: set[str] = set()
    for home in homes:
        conflicts.update(install_home(platform_root, home, args.memory_url, token, organization, managed,
                                      delivery, args.skill_backup_root.expanduser()))
        kind = "Kimi Desktop" if desktop_root_for_home(home) is not None else "Kimi CLI"
        print(f"installed {kind} adapter into {home}")
    if not args.no_desktop and not detected_desktop_homes:
        existing_roots = [path for path in data_roots if path.is_dir()]
        if existing_roots:
            roots = ", ".join(str(path) for path in existing_roots)
            raise SystemExit(
                "Kimi Desktop data exists, but its embedded kimi-code home was not found under: "
                f"{roots}. Open Kimi Work once, close Kimi completely, and rerun the bootstrap. "
                "If the runtime uses a custom location, pass it explicitly with --kimi-home."
            )
    launcher = args.bin_dir.expanduser().resolve() / "kimi-mode"
    atomic_write(launcher, (platform_root / "adapters/kimi/scripts/kimi-mode").read_text(encoding="utf-8"), mode=0o755)
    memory = "as this app's agent (managed client)" if MEMORY_SERVER_NAME in channels else (
        "skipped" if token is None else f"configured {MEMORY_SERVER_NAME} at {args.memory_url}")
    print(f"Agent Memory: {memory}")
    print(f"managed MCP servers: {', '.join(sorted(managed)) or 'none'}")
    # App -> the delivered keys left to an entry this adapter does not own, the shape of the
    # receipt of the installer of managed servers.
    print(f"managed MCP conflicts: {json.dumps({'kimi': sorted(conflicts)} if conflicts else {})}")
    print(f"mode launcher: {launcher}")


if __name__ == "__main__":
    main()
