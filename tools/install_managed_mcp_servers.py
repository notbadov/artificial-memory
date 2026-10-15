#!/usr/bin/env python3
"""Atomically reconcile Artificial Memory-managed remote MCP server entries.

An entry of an app's config is this installer's only when its state records it as written
there, per app. A delivered server whose name another entry already has (one the person added,
with their own token) leaves that entry as it is and is reported as a conflict, as a skill of
the person's own is (agent_platform_core/managed_skills.py); only recorded entries are replaced
or removed. A state written before the record per app named the keys alone, and its keys are
taken as this installer's in every app, where it had written them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path


KEY = re.compile(r"[a-z][a-z0-9_-]{1,62}")
SYNCHRONIZED = {"codex", "claude"}
# Kimi, OpenCode and Cursor are not synchronized providers; they receive the same entries on the
# computer that runs Codex or Claude. OpenCode and Cursor read their config files on start, so
# they are written here. Kimi Desktop must be closed while its plugin changes, so its adapter
# consumes a mirror.
MIRRORED = {"kimi", "opencode", "cursor"}
# The apps whose configs this installer writes entries into, each with its own record.
OWNED_IN = ("codex", "claude", "opencode", "cursor")
# The apps a state written before the record per app had written its keys into: never Cursor.
LEGACY_OWNED_IN = ("codex", "claude", "opencode")


def public_https(url: object) -> bool:
    """The addresses the service accepts for a managed server (managed_mcp.py), held here too:
    a token is never sent in clear text, nor to this computer itself."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    return (parsed.scheme == "https" and bool(hostname) and parsed.username is None and parsed.password is None
            and not parsed.fragment and hostname not in {"localhost", "127.0.0.1", "::1"})


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"Managed MCP input must be a JSON object: {path}")
    return value


def validate(manifest: dict, credentials: dict) -> list[dict]:
    if manifest.get("version") != 1 or not isinstance(manifest.get("servers"), list):
        raise SystemExit("Managed MCP manifest is invalid")
    if set(credentials) != {"version", "tokens"} or credentials["version"] != 1 or not isinstance(credentials["tokens"], dict):
        raise SystemExit("Managed MCP credentials are invalid")
    result = []
    seen = set()
    for raw in manifest["servers"]:
        required = {
            "id", "configKey", "displayName", "url", "authType", "credentialRevision",
            "targetProviders", "enabledTools", "approvalMode", "enabled", "revision",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise SystemExit("Managed MCP server descriptor is invalid")
        key = raw["configKey"]
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in seen:
            raise SystemExit("Managed MCP configuration key is invalid")
        seen.add(key)
        if not public_https(raw["url"]):
            raise SystemExit("Managed MCP server address must be a public HTTPS URL")
        if raw["authType"] not in {"none", "bearer", "oauth"}:
            raise SystemExit("Managed MCP authentication type is invalid")
        if raw["approvalMode"] not in {"prompt", "writes", "approve"}:
            raise SystemExit("Managed MCP approval mode is invalid")
        if not isinstance(raw["targetProviders"], list) or not set(raw["targetProviders"]).issubset(SYNCHRONIZED | MIRRORED):
            raise SystemExit("Managed MCP target providers are invalid")
        if not isinstance(raw["enabledTools"], list) or any(not isinstance(item, str) for item in raw["enabledTools"]):
            raise SystemExit("Managed MCP tool allowlist is invalid")
        token = credentials["tokens"].get(raw["id"])
        if raw["authType"] == "bearer" and raw["enabled"]:
            if not isinstance(token, str) or hashlib.sha256(token.encode()).hexdigest() != raw["credentialRevision"]:
                raise SystemExit("Managed MCP bearer credential does not match desired state")
        elif token is not None:
            raise SystemExit("Managed MCP credentials contain an unexpected secret")
        result.append(raw)
    return result


def remove_codex_server(content: str, key: str) -> str:
    escaped = re.escape(key)
    pattern = re.compile(
        rf"(?ms)^\[mcp_servers\.{escaped}(?:\.[^\]]+)?\]\n.*?(?=^\[(?!mcp_servers\.{escaped}(?:\.|\]))|\Z)"
    )
    return pattern.sub("", content).rstrip()


def codex_block(server: dict, token: str | None) -> str:
    key = server["configKey"]
    lines = [
        f"[mcp_servers.{key}]",
        f"url = {json.dumps(server['url'])}",
        f"enabled = {str(bool(server['enabled'])).lower()}",
        f"default_tools_approval_mode = {json.dumps(server['approvalMode'])}",
    ]
    if server["authType"] == "oauth":
        lines.append('auth = "oauth"')
    if server["enabledTools"]:
        lines.append("enabled_tools = " + json.dumps(server["enabledTools"], ensure_ascii=True))
    if token is not None:
        lines.extend([
            "",
            f"[mcp_servers.{key}.http_headers]",
            f"Authorization = {json.dumps('Bearer ' + token)}",
        ])
    return "\n".join(lines) + "\n"


def conflicting(wanted: list[dict], present: object, owned: set[str]) -> list[str]:
    """The keys of `wanted` that an entry this installer does not own already has."""
    names = set(present) if isinstance(present, dict) else set()
    return sorted(server["configKey"] for server in wanted if server["configKey"] in names - owned)


def install_codex(path: Path, servers: list[dict], tokens: dict, owned: set[str]) -> tuple[list[str], list[str]]:
    """(keys written, keys left to another entry of that name)."""
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    wanted = [server for server in servers if "codex" in server["targetProviders"]]
    conflicts = conflicting(wanted, tomllib.loads(content).get("mcp_servers"), owned)
    for key in owned:
        content = remove_codex_server(content, key)
    wanted = [server for server in wanted if server["configKey"] not in conflicts]
    blocks = [codex_block(server, tokens.get(server["id"])) for server in wanted]
    updated = content.rstrip()
    if blocks:
        updated = (updated + "\n\n" if updated else "") + "\n".join(blocks)
    if updated:
        updated += "\n"
    tomllib.loads(updated)
    atomic_write(path, updated)
    return [server["configKey"] for server in wanted], conflicts


def install_claude(path: Path, servers: list[dict], tokens: dict, owned: set[str]) -> tuple[list[str], list[str]]:
    """(keys written, keys left to another entry of that name)."""
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Claude config must be a JSON object: {path}")
    configured = value.setdefault("mcpServers", {})
    if not isinstance(configured, dict):
        raise SystemExit(f"Claude mcpServers must be a JSON object: {path}")
    wanted = [server for server in servers if "claude" in server["targetProviders"] and server["enabled"]]
    conflicts = conflicting(wanted, configured, owned)
    for key in owned:
        configured.pop(key, None)
    wanted = [server for server in wanted if server["configKey"] not in conflicts]
    for server in wanted:
        entry = {"type": "http", "url": server["url"]}
        token = tokens.get(server["id"])
        if token is not None:
            entry["headers"] = {"Authorization": "Bearer " + token}
        configured[server["configKey"]] = entry
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return [server["configKey"] for server in wanted], conflicts


def remote_entry(server: dict, tokens: dict) -> dict:
    entry: dict = {"url": server["url"]}
    token = tokens.get(server["id"])
    if token is not None:
        entry["headers"] = {"Authorization": "Bearer " + token}
    return entry


def mirrored(servers: list[dict], provider: str) -> list[dict]:
    """Enabled servers for a mirrored provider; OAuth needs the provider's own sign-in flow."""
    return [
        server for server in servers
        if provider in server["targetProviders"] and server["enabled"] and server["authType"] != "oauth"
    ]


def opencode_module():
    path = Path(__file__).resolve().parents[1] / "adapters/opencode/install.py"
    spec = importlib.util.spec_from_file_location("agent_platform_opencode_install", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def opencode_config_path(root: Path) -> Path:
    json_path, jsonc_path = root / "opencode.json", root / "opencode.jsonc"
    return json_path if json_path.is_file() or not jsonc_path.is_file() else jsonc_path


def install_opencode(root: Path, servers: list[dict], tokens: dict, owned: set[str]) -> tuple[list[str], list[str]]:
    """(keys written, keys left to another entry of that name)."""
    path = opencode_config_path(root)
    value = opencode_module().read_json(path)
    configured = value.setdefault("mcp", {})
    if not isinstance(configured, dict):
        raise SystemExit(f"OpenCode mcp must be a JSON object: {path}")
    wanted = mirrored(servers, "opencode")
    conflicts = conflicting(wanted, configured, owned)
    for key in owned:
        configured.pop(key, None)
    wanted = [server for server in wanted if server["configKey"] not in conflicts]
    for server in wanted:
        configured[server["configKey"]] = {
            "type": "remote", **remote_entry(server, tokens), "enabled": True, "oauth": False,
        }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return [server["configKey"] for server in wanted], conflicts


def cursor_config_path(root: Path) -> Path:
    return root / "mcp.json"


def install_cursor(root: Path, servers: list[dict], tokens: dict, owned: set[str]) -> tuple[list[str], list[str], bool]:
    """(keys written, keys left to another entry of that name, whether the file changed).

    Cursor's ~/.cursor/mcp.json takes remote entries as {url, headers}
    (https://cursor.com/docs/context/mcp). It is read when Cursor starts, so it is written only
    when an entry changes; a change asks for a restart. OAuth servers need Cursor's own sign-in."""
    path = cursor_config_path(root)
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    value = json.loads(before) if before is not None else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Cursor MCP config must be a JSON object: {path}")
    configured = value.setdefault("mcpServers", {})
    if not isinstance(configured, dict):
        raise SystemExit(f"Cursor mcpServers must be a JSON object: {path}")
    wanted = mirrored(servers, "cursor")
    conflicts = conflicting(wanted, configured, owned)
    for key in owned:
        configured.pop(key, None)
    wanted = [server for server in wanted if server["configKey"] not in conflicts]
    for server in wanted:
        configured[server["configKey"]] = remote_entry(server, tokens)
    changed = before is None and bool(configured) or before is not None and json.loads(before) != value
    if changed:
        atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return [server["configKey"] for server in wanted], conflicts, changed


def owned_keys(previous: dict) -> dict[str, set[str]]:
    """App -> the keys this installer wrote there, from its state. A state written before the
    record per app names the keys alone: they were written into every app of that day."""
    keys = previous.get("configKeys", [])
    by_app = previous.get("providers")
    if by_app is None:
        by_app = {app: keys for app in LEGACY_OWNED_IN}
    if (not isinstance(keys, list) or not isinstance(by_app, dict) or not set(by_app) <= set(OWNED_IN)
            or any(not isinstance(names, list) for names in by_app.values())
            or any(not isinstance(key, str) or not KEY.fullmatch(key) for key in [*keys, *sum(by_app.values(), [])])):
        raise SystemExit("Managed MCP ownership state is invalid")
    return {app: set(by_app.get(app, [])) for app in OWNED_IN}


def write_kimi_mirror(path: Path, servers: list[dict], tokens: dict) -> list[str]:
    wanted = mirrored(servers, "kimi")
    atomic_write(path, json.dumps({
        "version": 1,
        "servers": {server["configKey"]: remote_entry(server, tokens) for server in wanted},
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return [server["configKey"] for server in wanted]


def verify_written(provider: str, path: Path, servers: list[dict], keys: list[str]) -> None:
    """Read the provider config back: a server that is not there must fail the sync, rather
    than a sync that reports success while the task server never reached the app."""
    urls = {server["configKey"]: server["url"] for server in servers}
    if provider == "codex":
        found = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
    elif provider == "opencode":
        found = opencode_module().read_json(opencode_config_path(path)).get("mcp", {})
    elif provider == "kimi":
        found = json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    elif provider == "cursor":
        config = cursor_config_path(path)
        found = json.loads(config.read_text(encoding="utf-8")).get("mcpServers", {}) if config.is_file() else {}
    else:
        found = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    missing = [key for key in keys if not isinstance(found.get(key), dict) or found[key].get("url") != urls[key]]
    if missing:
        raise SystemExit(f"Managed MCP servers missing from {provider} config {path}: {', '.join(missing)}")


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
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--codex-config", type=Path, required=True)
    parser.add_argument("--claude-config", type=Path, required=True)
    # None when the computer was connected for Kimi Code or OpenCode only.
    parser.add_argument("--provider", action="append", choices=("codex", "claude"), default=[])
    parser.add_argument("--opencode-root", type=Path, help="OpenCode config directory; skipped when OpenCode is not installed")
    parser.add_argument("--kimi-mirror", type=Path, help="Entries for the Kimi adapter to install")
    parser.add_argument("--cursor-root", type=Path, help="Cursor's directory (~/.cursor); skipped when Cursor is not installed")
    args = parser.parse_args()
    manifest = load_json(args.manifest)
    credentials = load_json(args.credentials)
    servers = validate(manifest, credentials)
    previous = load_json(args.state) if args.state.is_file() else {"version": 1, "configKeys": [], "providers": {}}
    owned = owned_keys(previous)
    tokens = credentials["tokens"]
    written: dict[str, list[str]] = {}
    conflicts: dict[str, list[str]] = {}
    if "codex" in args.provider:
        written["codex"], conflicts["codex"] = install_codex(args.codex_config, servers, tokens, owned["codex"])
        verify_written("codex", args.codex_config, servers, written["codex"])
    if "claude" in args.provider:
        written["claude"], conflicts["claude"] = install_claude(args.claude_config, servers, tokens, owned["claude"])
        verify_written("claude", args.claude_config, servers, written["claude"])
    if args.opencode_root is not None and args.opencode_root.is_dir():
        written["opencode"], conflicts["opencode"] = install_opencode(
            args.opencode_root, servers, tokens, owned["opencode"])
        verify_written("opencode", args.opencode_root, servers, written["opencode"])
    restart: list[str] = []
    # App -> why its config was left as it was. Cursor's file is the person's to break: a file
    # that is not JSON fails Cursor alone, and the others are configured and recorded as always.
    failed: dict[str, str] = {}
    if args.cursor_root is not None and args.cursor_root.is_dir():
        try:
            cursor_written, cursor_conflicts, changed = install_cursor(args.cursor_root, servers, tokens, owned["cursor"])
        except (OSError, ValueError, SystemExit):
            failed["cursor"] = "config_unreadable"
        else:
            try:
                verify_written("cursor", args.cursor_root, servers, cursor_written)
            except (OSError, ValueError, SystemExit):
                failed["cursor"] = "config_not_verified"
            else:
                written["cursor"], conflicts["cursor"] = cursor_written, cursor_conflicts
            if changed:
                restart.append("cursor")
    # What this run wrote is the installer's own; an app it did not reach keeps its record.
    recorded = {app: sorted(written[app] if app in written else keys)
                for app, keys in owned.items() if app in written or keys}
    if args.kimi_mirror is not None:
        written["kimi"] = write_kimi_mirror(args.kimi_mirror, servers, tokens)
        verify_written("kimi", args.kimi_mirror, servers, written["kimi"])
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    atomic_write(args.state, json.dumps({
        "version": 1,
        # Every key written into some app: what a disconnect looks for (sync_disconnect.py).
        "configKeys": sorted(set().union(*recorded.values())),
        "providers": recorded,
        "desiredStateDigest": digest,
    }, sort_keys=True, indent=2) + "\n")
    print(json.dumps({
        "ok": True, "desiredStateDigest": digest,
        # App -> the delivered keys left to an entry this installer does not own.
        "conflicts": {app: keys for app, keys in sorted(conflicts.items()) if keys},
        # Apps that read their config at start and whose config changed now (Cursor).
        "restartRequired": restart,
        "failed": failed,
        "servers": [{
            "id": server["id"], "configKey": server["configKey"],
            "status": "authorization_required" if server["enabled"] and server["authType"] == "oauth" else "configured",
            "providers": sorted(provider for provider, keys in written.items() if server["configKey"] in keys),
        } for server in servers],
    }))


if __name__ == "__main__":
    main()
