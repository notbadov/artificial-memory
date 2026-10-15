"""Taking this computer's access to its organization out of the apps on it.

What is access: the entries this client wrote into an app's config, the files it keeps for
one connection, and the copies of organization data under the person's home. Everything else
in a config belongs to its owner, so an entry is cut out of the text of a config that has
comments instead of the config being written again, and a file that names another connection
is left to that connection.

The command that ends a connection is here too (`disconnect`, `remove_local_access`). The
service is told first; what is on the computer goes whether or not it could be told, and the
hourly job with it, under either of its names.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the instruction rules, the launchers and the schedule; the installer of managed servers, which
an installed client has next to it and a release under tools/, and the identity of the app
agents are imported where they are used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from sync_base import _read_local_json, release_transport, windows_system_tool
from sync_launchers import SCHEDULE_LAYOUTS
from sync_messages import text
from sync_rules import remove_instruction_rules
from sync_schedule import launch_agent_label, scheduled_task_name

# Config entries that reach an organization from this computer: the platform's own channels,
# their older names, and the organization's managed servers recorded in the ownership state.
ACCESS_CONFIG_KEYS = ("agent-platform-collaboration", "agentMemory", "projectContext", "codexMemory", "codexmemory")
# What stays on the computer belongs to no organization: the platform's skills, the verified
# releases and the interpreter. What goes is every key, credential and copy of organization data.
ACCESS_DIRECTORIES = ("agents", "organization-skills", "context-passports", "onboarding")
ACCESS_CLIENT_FILES = (
    "managed-mcp-state.json", "managed-mcp-mirror.json", "kimi-app-agent-channels.json",
    "mirrored-apps.json", "last-run.json", "client-recovery.json",
)
# What belongs to one connection by its computer id: its credential for the OS schedule, the
# command-line client's record of it, and the note of who starts its runs. A computer can hold a
# second connection (the desktop application 0.4.3 on Windows does not see one made in the
# terminal), and leaving one must leave the other whole and usable.
CONNECTION_CLIENT_FILES = ("credential", "credential.dpapi", "terminal.json", "terminal.json.partial", "schedule-owner.json")


def connection_file_owner(client_root: Path, name: str) -> str | None:
    """The computer id a per-connection file belongs to; None when nothing on disk says."""
    if name in ("terminal.json", "terminal.json.partial", "schedule-owner.json"):
        owner = _read_local_json(client_root / name).get("clientId")
        return owner if isinstance(owner, str) else None
    # A credential file names no one itself: the record written next to it does, and for a
    # client from before the command-line client, the receipt of its job.
    for record in ("terminal.json", "terminal.json.partial", "scheduled-task.json"):
        owner = _read_local_json(client_root / record).get("clientId")
        if isinstance(owner, str):
            return owner
    return None


def _managed_config_keys(client_root: Path, app: str | None = None) -> set[str]:
    """The managed servers this client wrote into `app`, as the installer's ownership state
    records them per app (codex, claude, opencode). For another app, or a state written before
    that record, every key the state names: those were written everywhere."""
    try:
        state = json.loads((client_root / "managed-mcp-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    keys = state.get("configKeys") if isinstance(state, dict) else None
    by_app = state.get("providers") if isinstance(state, dict) else None
    if app == "cursor" and not isinstance(by_app, dict):
        # A state from before the record per app was written before Cursor was an app: none of
        # its keys were written into Cursor, and a server of that name there is the person's.
        return set()
    if app is not None and isinstance(by_app, dict):
        keys = by_app.get(app) if isinstance(by_app.get(app), list) else []
    return {key for key in keys or [] if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key)}


def _jsonc_tokens(content: str) -> list[tuple[str, int, int]]:
    """Strings, punctuation and bare values of a JSON-with-comments text, with their positions."""

    tokens: list[tuple[str, int, int]] = []
    index, length = 0, len(content)
    while index < length:
        char = content[index]
        if char.isspace():
            index += 1
        elif content.startswith("//", index):
            end = content.find("\n", index)
            index = length if end < 0 else end
        elif content.startswith("/*", index):
            end = content.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            index = end + 2
        elif char == '"':
            end = index + 1
            while end < length and content[end] != '"':
                end += 2 if content[end] == "\\" else 1
            if end >= length:
                raise ValueError("unterminated string")
            tokens.append(("string", index, end + 1))
            index = end + 1
        elif char in "{}[]:,":
            tokens.append((char, index, index + 1))
            index += 1
        else:
            end = index
            while end < length and not content[end].isspace() and content[end] not in '{}[]:,"/':
                end += 1
            if end == index:
                raise ValueError("unexpected character")
            tokens.append(("value", index, end))
            index = end
    return tokens


def remove_jsonc_properties(content: str, parent: str, keys: set[str]) -> str:
    """`content` without the named properties of its top-level object `parent`.

    Comments and everything else stay byte for byte, which rewriting the parsed value would not
    do. Raises ValueError when the text is not what it is taken for; the caller then leaves the
    file alone.
    """

    tokens = _jsonc_tokens(content)

    def source_of(token: tuple[str, int, int]) -> str:
        return content[token[1]:token[2]]

    def after_value(index: int) -> int:
        if tokens[index][0] not in "{[":
            return index + 1
        depth = 0
        while True:
            depth += tokens[index][0] in "{[" and 1 or (tokens[index][0] in "}]" and -1 or 0)
            index += 1
            if depth == 0:
                return index

    def members(index: int):
        """(key token index, index after the value) of an object that opens at `index`."""
        if tokens[index][0] != "{":
            raise ValueError("an object is expected")
        index += 1
        while tokens[index][0] != "}":
            if tokens[index][0] != "string" or tokens[index + 1][0] != ":":
                raise ValueError("a property is expected")
            end = after_value(index + 2)
            yield index, end
            index = end + 1 if tokens[end][0] == "," else end

    spans: list[tuple[int, int]] = []
    try:
        for key_index, _ in members(0):
            if json.loads(source_of(tokens[key_index])) != parent or tokens[key_index + 2][0] != "{":
                continue
            for member, end in members(key_index + 2):
                if json.loads(source_of(tokens[member])) not in keys:
                    continue
                start, stop = tokens[member][1], tokens[end - 1][2]
                if tokens[end][0] == ",":
                    stop = tokens[end][2]
                elif tokens[member - 1][0] == ",":
                    start = tokens[member - 1][1]
                spans.append((start, stop))
    except IndexError:
        raise ValueError("the text ends early") from None
    for start, stop in sorted(spans, reverse=True):
        content = content[:start] + content[stop:]
    return content


def _without_jsonc_comments(content: str) -> str:
    tokens = _jsonc_tokens(content)
    return " ".join(content[start:end] for _, start, end in tokens)


def remove_opencode_jsonc_access(path: Path, keys: set[str]) -> bool:
    """Take this computer's access out of an OpenCode config that has comments. True when changed."""

    from install_managed_mcp_servers import atomic_write

    original = path.read_text(encoding="utf-8")
    before = json.loads(_without_jsonc_comments(original))
    servers = before.get("mcp") if isinstance(before, dict) else None
    if not isinstance(servers, dict) or not keys & set(servers):
        return False
    updated = remove_jsonc_properties(original, "mcp", keys)
    expected = {**before, "mcp": {name: value for name, value in servers.items() if name not in keys}}
    if json.loads(_without_jsonc_comments(updated)) != expected:
        raise ValueError("the config would not keep its other content")
    atomic_write(path, updated)
    return True


KIMI_MANAGED_KEYS_FILE = "agent-platform/managed-mcp-keys.json"  # adapters/kimi/install.py MANAGED_KEYS_FILE


def remove_kimi_access(kimi_home: Path, keys: set[str]) -> bool:
    """Take this computer's access out of Kimi Code: the entries this platform wrote into its
    `mcp.json` (named in the installer's own list) and the platform's channels (`keys`). True
    when changed.

    Kimi's adapter, not the installer of managed servers, writes Kimi's mcp.json and records
    what it wrote there; a name that installer wrote into another app may be the person's own
    server in Kimi, so `keys` is the channels alone."""

    from install_managed_mcp_servers import atomic_write

    config_path, owned_path = kimi_home / "mcp.json", kimi_home / KIMI_MANAGED_KEYS_FILE
    owned: set[str] = set()
    if owned_path.is_file():
        listed = json.loads(owned_path.read_text(encoding="utf-8")).get("keys", [])
        owned = {name for name in listed if isinstance(name, str)}
    changed = False
    if config_path.is_file():
        value = json.loads(config_path.read_text(encoding="utf-8"))
        servers = value.get("mcpServers") if isinstance(value, dict) else None
        present = sorted((keys | owned) & set(servers)) if isinstance(servers, dict) else []
        if present:
            for name in present:
                del servers[name]
            atomic_write(config_path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            changed = True
    if owned_path.is_file():
        owned_path.unlink()
        changed = True
    return changed


def _is_bridge_entry(entry: object, provider: str) -> bool:
    """An app config entry this client wrote for `provider`: the local bridge, started as that app."""
    if not isinstance(entry, dict):
        return False
    command, arguments = entry.get("command"), entry.get("args", [])
    words = list(command) if isinstance(command, list) else [command, *(arguments if isinstance(arguments, list) else [])]
    words = [word for word in words if isinstance(word, str)]
    named = any(first == "--provider" and second == provider for first, second in zip(words, words[1:]))
    return named and any(Path(word).name == "mcp_bridge.py" for word in words)


# The words of a PowerShell call, the form of a Cursor hook command on Windows.
_POWERSHELL_WORD = re.compile(r"'((?:[^']|'')*)'|\"([^\"]*)\"|(\S+)")


def is_cursor_platform_hook(entry: object, cursor_root: Path, windows: bool | None = None) -> bool:
    """A Cursor hook whose command runs a script the platform put under
    ~/.cursor/agent-platform/scripts, read the way its shell reads it. The same rule as
    adapters/cursor/install.py (is_ours), which is delivered apart from the client."""

    import shlex
    from pathlib import PurePosixPath, PureWindowsPath

    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str):
        return False
    windows = os.name == "nt" if windows is None else windows
    if windows:
        words = [single.replace("''", "'") if single is not None else double if double is not None else bare
                 for single, double, bare in _POWERSHELL_WORD.findall(command)]
        words = words[1:] if words[:1] == ["&"] else words
        ours = PureWindowsPath(str(cursor_root)) / "agent-platform" / "scripts"
        return any(str(PureWindowsPath(word).parent).casefold() == str(ours).casefold() for word in words)
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    ours = PurePosixPath(str(cursor_root)) / "agent-platform" / "scripts"
    return any(PurePosixPath(word).parent == ours for word in words)


def remove_cursor_hooks(cursor_root: Path) -> bool:
    """Take the platform's work-session hooks out of ~/.cursor/hooks.json and its directory out of
    ~/.cursor. They start the client's interpreter, which an uninstall deletes. Every other hook
    stays where it was. True when something was removed; a hooks file that is not Cursor's
    format raises ValueError and is left alone."""

    import shutil
    from install_managed_mcp_servers import atomic_write

    changed = False
    path = cursor_root / "hooks.json"
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        hooks = value.get("hooks") if isinstance(value, dict) else None
        if not isinstance(hooks, dict) or any(not isinstance(entries, list) for entries in hooks.values()):
            raise ValueError("Cursor hooks file is not version 1 with a hooks object")
        for event in list(hooks):
            kept = [entry for entry in hooks[event] if not is_cursor_platform_hook(entry, cursor_root)]
            if len(kept) != len(hooks[event]):
                changed = True
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if changed:
            mode = path.stat().st_mode & 0o777
            atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            os.chmod(path, mode)
    runtime = cursor_root / "agent-platform"
    if runtime.is_dir():
        shutil.rmtree(runtime)
        changed = True
    return changed


def remove_cursor_access(cursor_root: Path, managed: set[str]) -> bool:
    """Take this computer's access out of Cursor's ~/.cursor/mcp.json. True when changed.

    Only what the client wrote goes: the channels of Cursor's agent (bridge entries started as
    `cursor`) and the managed servers the installer recorded as written into Cursor. A server
    of the person's under one of those names stays. The hooks go separately (remove_cursor_hooks)."""

    from install_managed_mcp_servers import atomic_write

    path = cursor_root / "mcp.json"
    if not path.is_file():
        return False
    value = json.loads(path.read_text(encoding="utf-8"))
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    if not isinstance(servers, dict):
        return False
    ours = sorted({key for key in ACCESS_CONFIG_KEYS if _is_bridge_entry(servers.get(key), "cursor")}
                  | (managed & set(servers)))
    for key in ours:
        del servers[key]
    if ours:
        atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return bool(ours)


def _claude_desktop_configs(home: Path) -> list[Path]:
    return [home / "Library/Application Support/Claude/claude_desktop_config.json",
            home / "AppData/Roaming/Claude/claude_desktop_config.json",
            home / ".config/Claude/claude_desktop_config.json"]


def remove_local_access(args: argparse.Namespace, home: Path | None = None) -> dict:
    """Take this computer's access to its organization out of every app on it.

    This is how a connected computer leaves, and so can join another account.
    Every entry removed here is one this client wrote: the platform's channels and the managed
    servers named in its own ownership state. Anything else in an app's config is left exactly
    as it was. A config that cannot be read or rewritten safely is reported and not touched.
    """

    import shutil
    import tomllib
    from install_managed_mcp_servers import atomic_write, remove_codex_server

    home = home or Path.home()
    client_root = home / ".agent-platform" / "client"
    keys = set(ACCESS_CONFIG_KEYS) | _managed_config_keys(client_root)

    def keys_of(app: str) -> set[str]:
        return set(ACCESS_CONFIG_KEYS) | _managed_config_keys(client_root, app)
    report: dict[str, list[str]] = {"cleaned": [], "left": [], "removed": []}

    codex = args.codex_root / "config.toml"
    if codex.is_file():
        try:
            original = codex.read_text(encoding="utf-8")
            content = original
            for key in sorted(keys_of("codex")):
                content = remove_codex_server(content, key)
            updated = content + "\n" if content else ""
            if updated.strip() != original.strip():
                tomllib.loads(updated)
                atomic_write(codex, updated)
                report["cleaned"].append("codex")
        except (OSError, ValueError):
            report["left"].append("codex")

    opencode_root = home / ".config" / "opencode"
    json_configs = [("claude", args.claude_config, "mcpServers"), ("opencode", opencode_root / "opencode.json", "mcp")]
    json_configs += [("claude-desktop", path, "mcpServers") for path in _claude_desktop_configs(home)]
    for app, path, field in json_configs:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            servers = value.get(field) if isinstance(value, dict) else None
            if not isinstance(servers, dict):
                continue
            present = sorted((keys_of(app) if app in ("claude", "opencode") else keys) & set(servers))
            if present:
                for key in present:
                    del servers[key]
                atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
                report["cleaned"].append(app)
        except (OSError, ValueError):
            report["left"].append(app)
    # OpenCode with comments in its config: the entries are cut out of the text, so the
    # comments stay and the config does not keep its access.
    jsonc = opencode_root / "opencode.jsonc"
    if jsonc.is_file() and not (opencode_root / "opencode.json").is_file():
        try:
            if remove_opencode_jsonc_access(jsonc, keys_of("opencode")):
                report["cleaned"].append("opencode")
        except (OSError, ValueError):
            report["left"].append("opencode")
    configured_kimi = os.environ.get("KIMI_CODE_HOME")
    kimi_home = Path(configured_kimi).expanduser() if configured_kimi and home == Path.home() else home / ".kimi-code"
    if kimi_home.is_dir():
        try:
            if remove_kimi_access(kimi_home, set(ACCESS_CONFIG_KEYS)):
                report["cleaned"].append("kimi")
        except (OSError, ValueError, AttributeError):
            report["left"].append("kimi")

    cursor_root = home / ".cursor"
    if (cursor_root / "mcp.json").is_file():
        try:
            if remove_cursor_access(cursor_root, _managed_config_keys(client_root, "cursor")):
                report["cleaned"].append("cursor")
        except (OSError, ValueError):
            report["left"].append("cursor")
    if cursor_root.is_dir():
        try:
            if remove_cursor_hooks(cursor_root):
                report["removed"].append("cursor-hooks")
        except (OSError, ValueError):
            report["left"].append("cursor-hooks")

    if remove_instruction_rules(args, home):
        report["removed"].append("instruction-rules")
    for name in ACCESS_DIRECTORIES:
        path = home / ".agent-platform" / name
        if path.is_dir():
            shutil.rmtree(path)
            report["removed"].append(name)
    for name in ACCESS_CLIENT_FILES:
        path = client_root / name
        if path.is_file():
            path.unlink()
            report["removed"].append(name)
    for name in CONNECTION_CLIENT_FILES:
        path = client_root / name
        # Only the files of this connection. If they went whatever computer they belonged to,
        # disconnecting in the desktop application would leave a terminal connection active at
        # the service with no credential here.
        if path.is_file() and connection_file_owner(client_root, name) in (None, args.client_id):
            path.unlink()
            report["removed"].append(name)
    # Whatever could not be removed above must not speak for the connection that just ended.
    from agent_identity import record_connection
    record_connection(None, home)
    for layout in SCHEDULE_LAYOUTS if args.client_id else ():
        agent = home / "Library/LaunchAgents" / (launch_agent_label(args.client_id, layout) + ".plist")
        if agent.is_file():
            if sys.platform == "darwin" and home == Path.home():
                subprocess.run(["launchctl", "unload", str(agent)], capture_output=True, check=False)
            agent.unlink()
            report["removed"].append("launch-agent")
        if os.name == "nt":
            subprocess.run([windows_system_tool("schtasks.exe"), "/Delete", "/TN",
                            scheduled_task_name(args.client_id, layout), "/F"],
                           capture_output=True, check=False)
    return {key: sorted(set(value)) for key, value in report.items()}


def disconnect(args: argparse.Namespace, home: Path | None = None) -> int:
    """End this computer's connection. The service is told first, so the credential and the app
    agents stop working there; the local access is removed whether or not it could be told."""

    if not args.client_id or not args.control_plane_url:
        raise SystemExit(text("disconnect_arguments_required"))
    notified = False
    token = args.client_token
    if token:
        try:
            release_transport().request_json(args.control_plane_url, "/v1/client-session/disconnect", {
                "clientId": args.client_id, "clientToken": token,
            })
            notified = True
        except (Exception, SystemExit):
            notified = False
    local = remove_local_access(args, home)
    print(json.dumps({"clientId": args.client_id, "status": "disconnected", "serverNotified": notified, **local},
                     ensure_ascii=False))
    return 0
