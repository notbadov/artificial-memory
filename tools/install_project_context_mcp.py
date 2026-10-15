#!/usr/bin/env python3
"""Install the authenticated read-only Project Context MCP on trusted clients."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path


SERVER_NAME = "projectContext"
DEFAULT_URL = "https://app.artificial-memory.com/project-context/mcp"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        handle.write(content)
        staged = Path(handle.name)
    staged.chmod(0o600)
    staged.replace(path)


def token_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    server = value.get("mcp_servers", {}).get(SERVER_NAME, {})
    authorization = server.get("http_headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def token_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    server = value.get("mcpServers", {}).get(SERVER_NAME, {}) if isinstance(value, dict) else {}
    authorization = server.get("headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def validate_token(token: str | None) -> str:
    normalized = (token or "").strip()
    if (not normalized.startswith("apm_") or not 36 <= len(normalized) <= 128
            or "\n" in normalized or "\r" in normalized):
        raise SystemExit("Project Context access credential is unavailable; set PROJECT_CONTEXT_ACCESS_TOKEN or pass --token")
    return normalized


def install_codex(path: Path, url: str, token: str) -> None:
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.projectContext(?:\.[^\]]+)?\]\n.*?(?=^\[|\Z)")
    current = pattern.sub("", current).rstrip()
    block = (
        "[mcp_servers.projectContext]\n"
        f"url = {json.dumps(url)}\n\n"
        "[mcp_servers.projectContext.http_headers]\n"
        f"Authorization = {json.dumps(f'Bearer {token}')}\n"
    )
    atomic_write(path, f"{current}\n\n{block}" if current else block)


def install_claude(path: Path, url: str, token: str) -> None:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Claude config must be a JSON object: {path}")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"Claude mcpServers must be a JSON object: {path}")
    servers[SERVER_NAME] = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check_endpoint(url: str, token: str) -> None:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "agent-platform-bootstrap", "version": "1"}},
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise SystemExit(f"Project Context MCP endpoint returned HTTP {response.status}")


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
    parser.add_argument("--provider", action="append", choices=("codex", "claude"))
    parser.add_argument("--url", default=os.environ.get("PROJECT_CONTEXT_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("PROJECT_CONTEXT_ACCESS_TOKEN"))
    parser.add_argument("--prompt", action="store_true", help="Prompt for the token without echoing it")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--skip-endpoint-check", action="store_true")
    args = parser.parse_args()
    providers = args.provider or ["codex", "claude"]
    candidate = args.token or token_from_codex(args.codex_config) or token_from_claude(args.claude_config)
    if not candidate and args.prompt:
        candidate = getpass.getpass("Project Context bearer token: ")
    token = validate_token(candidate)
    if not args.skip_endpoint_check:
        check_endpoint(args.url, token)
    if "codex" in providers:
        install_codex(args.codex_config, args.url, token)
    if "claude" in providers:
        install_claude(args.claude_config, args.url, token)
    print(json.dumps({"ok": True, "server": SERVER_NAME, "url": args.url, "providers": providers}))


if __name__ == "__main__":
    main()
