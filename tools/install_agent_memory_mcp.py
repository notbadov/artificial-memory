#!/usr/bin/env python3
"""Install the authenticated Agent Memory MCP on trusted clients."""

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


SERVER_NAME = "agentMemory"
DEFAULT_URL = "https://app.artificial-memory.com/agent-memory/mcp"
TOOL_NAMES = (
    "search_project_memory",
    "read_project_memory_note",
    "fetch_project_memory",
    "list_project_memory_projects",
    "list_project_memory_tree",
    "inspect_project_memory",
    "audit_project_memory",
    "retrieve_project_memory_for_task",
    "list_agent_thread_archive_cards",
    "generate_agent_thread_archive_memory_proposals",
    "list_agent_skills",
    "generate_agent_skill_memory_proposals",
    "propose_post_task_memory_capture",
    "read_project_memory_note_history",
    "generate_project_memory_frontmatter_proposals",
    "generate_project_memory_quality_proposals",
    "propose_project_memory_note",
    "list_project_memory_proposals",
    "fetch_project_memory_proposal",
    "propose_learning_case",
    "retrieve_learning_cases_for_task",
    # Delegated review: an agent plans; the owner applies it or delegates applying it.
    "create_project_memory_review_session",
    "get_project_memory_review_session",
    "submit_project_memory_review_plan",
    "list_project_memory_review_plans",
    "get_project_memory_review_plan",
    "request_memory_review_delegation",
    "apply_project_memory_review_plan",
)
# The tools that only read: all of those the memory service marks read-only (its
# READ_ONLY_MEMORY_TOOLS, the set its read capability serves), not only the ones listed
# above, because a connection sees every tool the server publishes. Codex runs them
# without asking. A Codex started without a person (`codex exec`, the task dispatcher) cannot
# approve anything, and before this it answered without memory. Proposals and review steps
# still ask.
READ_TOOL_NAMES = (
    "audit_project_memory",
    "audit_project_memory_knowledge_impact",
    "fetch_project_memory",
    "fetch_project_memory_proposal",
    "get_console_memory_overview",
    "get_project_memory_review_operation",
    "get_project_memory_review_session",
    "get_project_memory_review_plan",
    "list_project_memory_review_plans",
    "get_context",
    "get_memory_context_profile",
    "get_memory_temporal_graph",
    "get_project_memory_catalog_status",
    "get_project_memory_proposal_catalog_status",
    "inspect_project_memory",
    "list_agent_skills",
    "list_agent_thread_archive_cards",
    "list_memory_ingestion_jobs",
    "list_memory_connector_health",
    "list_project_memory_projects",
    "list_project_memory_proposals",
    "list_project_memory_tree",
    "read_project_memory_note",
    "read_project_memory_notes",
    "read_project_memory_note_history",
    "retrieve_learning_cases_for_task",
    "retrieve_project_memory_for_task",
    "search_memory",
    "search_project_memory",
)


def codex_tool_tables(server: str, tool_names: tuple[str, ...] = TOOL_NAMES,
                      read_tool_names: tuple[str, ...] = READ_TOOL_NAMES) -> list[str]:
    """One table per tool of a server in a Codex config (Agent Memory's by default); the reads
    run without asking. `approval_mode = "approve"` per tool is a value every Codex accepts."""
    tables = []
    for name in (*tool_names, *(name for name in read_tool_names if name not in tool_names)):
        tables.append(f"[mcp_servers.{server}.tools.{name}]")
        if name in read_tool_names:
            tables.append('approval_mode = "approve"')
    return tables


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
    servers = value.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return None
    server = servers.get(SERVER_NAME) or servers.get("codexMemory") or servers.get("codexmemory")
    authorization = server.get("http_headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def token_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = value.get("mcpServers", {}) if isinstance(value, dict) else {}
    server = (servers.get(SERVER_NAME) or servers.get("codexMemory")) if isinstance(servers, dict) else None
    authorization = server.get("headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def validate_token(token: str | None) -> str:
    normalized = (token or "").strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise SystemExit("Agent Memory token is unavailable; use managed enrollment or set AGENT_MEMORY_TOKEN")
    return normalized


def install_codex(path: Path, url: str, token: str, organization: str) -> None:
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.(?:agentMemory|codexMemory|codexmemory)(?:\.[^\]]+)?\]\n.*?(?=^\[|\Z)")
    current = pattern.sub("", current).rstrip()
    blocks = [
        "[mcp_servers.agentMemory]",
        f"url = {json.dumps(url)}",
        "",
        "[mcp_servers.agentMemory.http_headers]",
        f"Authorization = {json.dumps(f'Bearer {token}')}",
        f"X-Agent-Platform-Organization = {json.dumps(organization)}",
        "",
    ]
    blocks.extend(codex_tool_tables(SERVER_NAME))
    block = "\n".join(blocks) + "\n"
    atomic_write(path, f"{current}\n\n{block}" if current else block)


def install_claude(path: Path, url: str, token: str, organization: str) -> None:
    value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(value, dict):
        raise SystemExit(f"Claude config must be a JSON object: {path}")
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"Claude mcpServers must be a JSON object: {path}")
    servers.pop("codexMemory", None)
    servers[SERVER_NAME] = {
        "type": "http",
        "url": url,
        "headers": {
            "Authorization": f"Bearer {token}",
            "X-Agent-Platform-Organization": organization,
        },
    }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check_endpoint(url: str, token: str, organization: str) -> None:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "agent-platform-bootstrap", "version": "1"},
        },
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Agent-Platform-Organization": organization,
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise SystemExit(f"Agent Memory MCP endpoint returned HTTP {response.status}")


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
    parser.add_argument("--url", default=os.environ.get("AGENT_MEMORY_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument("--organization", default=os.environ.get("AGENT_MEMORY_ORGANIZATION_ID"))
    parser.add_argument("--prompt", action="store_true", help="Prompt for the token without echoing it")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--skip-endpoint-check", action="store_true")
    args = parser.parse_args()
    providers = args.provider or ["codex", "claude"]
    candidate = args.token or token_from_codex(args.codex_config) or token_from_claude(args.claude_config)
    if not candidate and args.prompt:
        candidate = getpass.getpass("Agent Memory bearer token: ")
    token = validate_token(candidate)
    organization = (args.organization or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
        raise SystemExit("Agent Memory organization is required and must be a valid identifier")
    if not args.skip_endpoint_check:
        check_endpoint(args.url, token, organization)
    if "codex" in providers:
        install_codex(args.codex_config, args.url, token, organization)
    if "claude" in providers:
        install_claude(args.claude_config, args.url, token, organization)
    print(json.dumps({"ok": True, "server": SERVER_NAME, "url": args.url, "organizationId": organization, "providers": providers}))


if __name__ == "__main__":
    main()
