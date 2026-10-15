"""MCP servers one app has on this computer, by name, and the address of one on request.

Console shows which tools every app really has, so a server added by hand in one app is seen,
and so is its absence in another. The report carries a server's name,
whether it is remote or a local program, the host of a remote one, and whether the platform put
it there. A command line, a path, a query string or a token never leaves the computer.

``export_mcp_server`` answers the owner's explicit request to take a remote server into Console:
only an HTTPS address without credentials or a query is returned.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

MAX_SERVERS = 100
# Channels every computer gets from the platform itself.
BUILT_IN_KEYS = frozenset({"agentMemory", "projectContext", "agentmemory", "projectcontext", "agent-platform-collaboration"})


def _servers(provider: str, config_path: Path) -> dict:
    """Raw server table of the app's user-level config: Codex config.toml or ~/.claude.json."""

    try:
        if provider == "codex":
            import tomllib
            value = tomllib.loads(config_path.read_text(encoding="utf-8")).get("mcp_servers")
        else:
            value = json.loads(config_path.read_text(encoding="utf-8")).get("mcpServers")
    except (ImportError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reportable(key: object) -> bool:
    return (isinstance(key, str) and 0 < len(key) <= 100 and "/" not in key and "\\" not in key
            and not any(ord(character) < 32 for character in key))


def _url(server: object) -> str | None:
    value = server.get("url") if isinstance(server, dict) else None
    return value if isinstance(value, str) and value else None


def mcp_inventory(provider: str, *, config_path: Path, managed_keys: set[str]) -> list[dict]:
    platform = set(managed_keys) | BUILT_IN_KEYS
    result = []
    for key, server in sorted(_servers(provider, config_path).items()):
        if not _reportable(key):
            continue
        url = _url(server)
        host = urlsplit(url).hostname if url else None
        result.append({"key": key, "transport": "remote" if url else "local",
                       "host": host if host and len(host) <= 253 and all(c.isalnum() or c in ".-" for c in host) else None,
                       "source": "platform" if key in platform else "user"})
    return result[:MAX_SERVERS]


def export_mcp_server(provider: str, *, config_path: Path, key: str) -> dict:
    """{"url": ...} for a remote server, or {"error": not_found | not_remote | has_secret}."""

    servers = _servers(provider, config_path)
    if key not in servers:
        return {"error": "not_found"}
    url = _url(servers[key])
    if url is None:
        return {"error": "not_remote"}
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return {"error": "not_remote"}
    if parts.username or parts.password or parts.query or parts.fragment:
        return {"error": "has_secret"}
    return {"url": url}


def managed_keys(state_path: Path) -> set[str]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8")).get("configKeys")
    except (OSError, ValueError, AttributeError):
        return set()
    return {key for key in value if isinstance(key, str)} if isinstance(value, list) else set()
