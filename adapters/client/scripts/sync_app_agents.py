"""The apps of this computer as agents of their own.

Each chosen app registers a key of its own and reaches the platform's channels through the
local bridge as that agent. This file gives a chosen app its channels, takes them from an app
that is no longer chosen, and forgets the identity of an app that belongs to another
connection. The Claude chat app saves its config from memory while it runs, so it is asked
whether it runs before its config is written.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the removal of access and the servers; the identity of an app agent, the bridge and the
installer of managed servers are imported where they are used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sync_base import client_platform, installer_accepts, note_app_failure, required_client_token, windows_system_tool
from sync_disconnect import (ACCESS_CONFIG_KEYS, _claude_desktop_configs, _is_bridge_entry, _without_jsonc_comments,
                             remove_opencode_jsonc_access)
from sync_mcp import kimi_is_installed, note_restart_required

APP_AGENT_SERVICE_REASON = {"collaboration": "collaboration", "agentMemory": "agent_memory",
                            "projectContext": "project_context"}


KIMI_APP_AGENT_CHANNELS = Path.home() / ".agent-platform/client/kimi-app-agent-channels.json"


def claude_desktop_config(platform_root: Path) -> Path:
    """The Claude chat app's config, located the way its adapter locates it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "agent_platform_claude_desktop_install", platform_root / "adapters" / "claude-desktop" / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.default_desktop_config()


def claude_desktop_running() -> bool:
    """The Claude chat app saves its whole config from memory while it runs and drops entries
    another program wrote (channels written while it ran were gone a minute later), so its
    channels are written only while it is closed. Matched by the app's path: on Windows Claude
    Code's CLI is also claude.exe. A failed check counts as running."""
    try:
        if client_platform() == "macos":
            # -a: macOS pgrep otherwise skips its own ancestors, and a sync may be started from Claude.
            return subprocess.run(["pgrep", "-a", "-f", "/Claude.app/Contents/MacOS/Claude$"],
                                  capture_output=True, check=False).returncode == 0
        if client_platform() == "windows":
            count = subprocess.run([
                windows_system_tool("WindowsPowerShell", "v1.0", "powershell.exe"), "-NoProfile", "-NonInteractive",
                "-Command",
                "@(Get-Process -Name claude -ErrorAction SilentlyContinue | "
                "Where-Object { $_.Path -like '*\\AnthropicClaude\\*' }).Count",
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30).stdout.strip()
            return count != "0"
    except (OSError, subprocess.SubprocessError):
        return True
    return False


def installed_local_apps(platform_root: Path) -> list[str]:
    """Local inventory only. Discovery must never authorize an app or install its channels."""
    apps = []
    if (Path.home() / ".config/opencode").is_dir():
        apps.append("opencode")
    if kimi_is_installed(platform_root):
        apps.append("kimi")
    if claude_desktop_config(platform_root).parent.is_dir():
        apps.append("claude-desktop")
    if (Path.home() / ".cursor").is_dir():
        apps.append("cursor")
    return apps


def remove_app_agent_channels(provider: str, args: argparse.Namespace, home: Path | None = None) -> bool:
    """Take the platform's channels out of one app's config. True when the config changed.

    Only bridge entries written for this app go; an entry a person made under the same name, and
    everything else in the config, stays. Raises OSError or ValueError when a config cannot be
    read or rewritten safely; the caller reports it and leaves the file alone.
    """

    import tomllib
    from install_managed_mcp_servers import atomic_write, remove_codex_server

    home = home or Path.home()

    def clean_json(path: Path, field: str) -> bool:
        if not path.is_file():
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        servers = value.get(field) if isinstance(value, dict) else None
        if not isinstance(servers, dict):
            return False
        ours = [key for key in ACCESS_CONFIG_KEYS if _is_bridge_entry(servers.get(key), provider)]
        for key in ours:
            del servers[key]
        if ours:
            atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return bool(ours)

    if provider == "codex":
        path = args.codex_root / "config.toml"
        if not path.is_file():
            return False
        content = path.read_text(encoding="utf-8")
        servers = tomllib.loads(content).get("mcp_servers", {})
        ours = [key for key in ACCESS_CONFIG_KEYS if _is_bridge_entry(servers.get(key), provider)]
        for key in ours:
            content = remove_codex_server(content, key)
        if ours:
            updated = content + "\n" if content else ""
            tomllib.loads(updated)
            atomic_write(path, updated)
        return bool(ours)
    if provider == "claude":
        return clean_json(args.claude_config, "mcpServers")
    if provider == "claude-desktop":
        return any([clean_json(path, "mcpServers") for path in _claude_desktop_configs(home)])
    if provider == "cursor":
        return clean_json(home / ".cursor" / "mcp.json", "mcpServers")
    if provider == "opencode":
        root = home / ".config" / "opencode"
        if (root / "opencode.json").is_file():
            return clean_json(root / "opencode.json", "mcp")
        jsonc = root / "opencode.jsonc"
        if not jsonc.is_file():
            return False
        value = json.loads(_without_jsonc_comments(jsonc.read_text(encoding="utf-8")))
        servers = value.get("mcp") if isinstance(value, dict) else None
        ours = {key for key in ACCESS_CONFIG_KEYS if isinstance(servers, dict) and _is_bridge_entry(servers.get(key), provider)}
        return bool(ours) and remove_opencode_jsonc_access(jsonc, ours)
    if provider == "kimi":
        configured = os.environ.get("KIMI_CODE_HOME")
        kimi_home = Path(configured).expanduser() if configured and home == Path.home() else home / ".kimi-code"
        changed = clean_json(kimi_home / "mcp.json", "mcpServers")
        channels = home / ".agent-platform/client/kimi-app-agent-channels.json"
        if channels.is_file():
            channels.unlink()
            changed = True
        return changed
    return False


def record_current_connection(args: argparse.Namespace, home: Path | None = None) -> None:
    """Called when a sync starts: from here on, only this connection's app agents are answered."""
    from agent_identity import record_connection

    record_connection(args.client_id, home)


def retire_stale_app_agents(args: argparse.Namespace, apps: list[str], home: Path | None = None) -> dict[str, list[str]]:
    """Leave on this computer only the app agents of its current connection.

    A computer can be moved from one account to another. A sync that visited only the selected
    apps would leave the agent of an app that is not selected in the new account untouched: it
    would keep the key and the identity of the previous organization and go on reading that
    organization's tasks and memory.

    Every identity that is not this connection's goes with its key, whether or not its app is
    selected: a selected app then registers a new key instead of carrying one that another
    organization still trusts. An app that is not selected also loses the bridge entries in its
    config. The Claude chat app saves its config over ours while it runs, so its entries wait until
    it is closed; without an identity its bridge already refuses.
    """
    from agent_identity import APP_AGENT_PROVIDERS, AppAgentIdentity, agents_root, bound_to_connection

    home = home or Path.home()
    report: dict[str, list[str]] = {"forgotten": [], "cleaned": [], "deferred": [], "left": []}
    root = agents_root(home)
    known = [path.name for path in sorted(root.iterdir()) if path.is_dir()] if root.is_dir() else []
    for provider in known:
        if provider not in APP_AGENT_PROVIDERS:
            continue
        identity = AppAgentIdentity(provider, home=home)
        recorded = identity.recorded()
        current = provider in apps and (
            recorded is None or bound_to_connection(recorded, provider, args.client_id, args.control_plane_url))
        if not current and identity.forget():
            report["forgotten"].append(provider)
    for provider in APP_AGENT_PROVIDERS:
        if provider in apps:
            continue
        if provider == "claude-desktop" and claude_desktop_running():
            if any(path.is_file() for path in _claude_desktop_configs(home)):
                report["deferred"].append(provider)
            continue
        try:
            if remove_app_agent_channels(provider, args, home):
                report["cleaned"].append(provider)
        except (OSError, ValueError):
            report["left"].append(provider)
    return report


def configure_app_agent_channels(
    platform_root: Path,
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
    channels: dict[str, str],
) -> dict[str, dict]:
    """Give explicitly selected apps the platform's channels as their own agents.

    Each app registers its public key, then every channel is initialized through the local
    bridge as that agent; only then is the app's config rewritten. An app whose agent cannot be
    registered (no recorded owner, revoked by the owner) or cannot reach a channel is reported
    with that reason and its config is left as it was; the rest of the sync continues."""
    from agent_identity import AgentIdentityError, AppAgentIdentity
    from mcp_bridge import Bridge, BridgeError

    sha = desired["release"]["agentPlatformSha"]
    receipts: dict[str, dict] = {}
    ready: list[str] = []
    # The apps named at enrollment or chosen in the web application are authoritative, never
    # local discovery. A service that does not send the field leaves the enrollment's apps.
    apps = desired.get("appAgentProviders", providers)
    # Before anything registers: an identity of another connection is never renewed or reused.
    retired = retire_stale_app_agents(args, apps)
    if any(retired.values()):
        print(json.dumps({"ok": True, "staleAppAgents": retired}, ensure_ascii=False))
    for provider in apps:
        try:
            identity = AppAgentIdentity(provider)
            agent = identity.register(args.control_plane_url, args.client_id, required_client_token(args), sha)
        except AgentIdentityError as error:
            receipts[provider] = {"status": "failed", "reason": str(error)}
            continue
        failure = None
        for service, endpoint in channels.items():
            try:
                Bridge(identity, endpoint, output=None, service=service).check()
            except BridgeError as error:
                failure = f"{APP_AGENT_SERVICE_REASON[service]}_{error}"
                break
            except AgentIdentityError as error:
                failure = str(error)
                break
        if failure:
            receipts[provider] = {"status": "failed", "reason": failure}
            continue
        if provider == "claude-desktop" and claude_desktop_running():
            receipts[provider] = {"status": "deferred", "reason": "claude_desktop_running"}
            continue
        receipts[provider] = {"status": "configured", "participantId": agent["participantId"]}
        ready.append(provider)
    installer = platform_root / "tools" / "install_app_agent_channels.py"
    if "cursor" in ready and not installer_accepts(installer, "--cursor-config"):
        # A release from before Cursor has no installer for its channels; the other apps go on.
        ready.remove("cursor")
        receipts["cursor"] = {"status": "failed", "reason": "app_without_installer"}
    if ready:
        bridge = Path.home() / ".agent-platform/client/mcp_bridge.py"
        command = [
            sys.executable, str(installer),
            "--python", sys.executable, "--bridge", str(bridge),
            "--codex-config", str(args.codex_root / "config.toml"), "--claude-config", str(args.claude_config),
            "--opencode-root", str(Path.home() / ".config/opencode"), "--kimi-channels", str(KIMI_APP_AGENT_CHANNELS),
            "--claude-desktop-config", str(claude_desktop_config(platform_root)),
        ]
        if "cursor" in ready:
            command.extend(["--cursor-config", str(Path.home() / ".cursor" / "mcp.json")])
        for service, endpoint in channels.items():
            command.extend(["--channel", f"{service}={endpoint}"])
        for provider in ready:
            command.extend(["--provider", provider])
        completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (AttributeError, IndexError, TypeError, ValueError):
            result = {}
        # Cursor reads its mcp.json when it starts: a changed one asks for a restart.
        note_restart_required(args, result)
        failed = result.get("failed") if isinstance(result, dict) else None
        for app, reason in (failed.items() if isinstance(failed, dict) else ()):
            # A config of the person's the installer could not read: that app alone failed.
            if app in receipts and isinstance(reason, str):
                receipts[app] = {"status": "failed", "reason": reason}
                note_app_failure(args, app, reason)
    return receipts
