"""The servers the apps of this computer reach through the platform, and what they hold.

Agent Memory, Project Context and the organization's managed servers are written into the
configs of Codex and Claude Code by the installers of a release; this file decides with what.
A credential an app already has is used again when its revision is the one the service
names, and fetched from the service otherwise. The inventory at the end says which servers
each app has, by name, and answers the owner's request for the address of one.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the consents and the Codex versions. "Control Plane" in its messages is the API of the service the client talks to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from sync_base import _read_local_json, app_chosen, installer_accepts, note_app_failure, release_transport, required_client_token
from sync_consents import servers_the_owner_agreed_to
from sync_codex_versions import managed_servers_for_codex

def fetch_project_context_credential(
    args: argparse.Namespace,
    client_id: str,
    expected_revision: str,
    expected_url: str,
) -> dict:
    response = release_transport().request_json(args.control_plane_url, "/v1/client-session/credential", {
        "clientId": client_id, "clientToken": required_client_token(args), "service": "projectContext",
    })
    value = response.get("credential") if isinstance(response.get("credential"), dict) else response
    if (
        value.get("credentialRevision") != expected_revision
        or value.get("url") != expected_url
        or not isinstance(value.get("token"), str)
        or not isinstance(value.get("organizationId"), str)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", value["organizationId"])
        or credential_revision(value["token"]) != expected_revision
    ):
        raise SystemExit("Agent Platform returned an invalid Project Context credential")
    return value


def fetch_agent_memory_credential(
    args: argparse.Namespace,
    client_id: str,
    expected_revision: str,
    expected_url: str,
) -> dict:
    response = release_transport().request_json(args.control_plane_url, "/v1/client-session/credential", {
        "clientId": client_id, "clientToken": required_client_token(args), "service": "agentMemory",
    })
    value = response.get("credential") if isinstance(response.get("credential"), dict) else response
    if (
        value.get("credentialRevision") != expected_revision
        or value.get("url") != expected_url
        or not isinstance(value.get("token"), str)
        or not isinstance(value.get("organizationId"), str)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", value["organizationId"])
        or credential_revision(value["token"]) != expected_revision
    ):
        raise SystemExit("Agent Platform returned an invalid Agent Memory credential")
    return value


def fetch_managed_mcp_credential(
    args: argparse.Namespace,
    client_id: str,
    server: dict,
    provider: str,
) -> str:
    response = release_transport().request_json(args.control_plane_url, "/v1/client-session/credential", {
        "clientId": client_id, "clientToken": required_client_token(args),
        "service": "managedMcp", "serverId": server.get("id"), "provider": provider,
    })
    value = response.get("credential") if isinstance(response.get("credential"), dict) else response
    token = value.get("token")
    if (
        value.get("serverId") != server.get("id")
        or value.get("url") != server.get("url")
        or value.get("credentialRevision") != server.get("credentialRevision")
        or not isinstance(token, str)
        or credential_revision(token) != server.get("credentialRevision")
    ):
        raise SystemExit("Agent Platform returned an invalid managed MCP credential")
    return token


def project_context_token_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    server = value.get("mcp_servers", {}).get("projectContext", {})
    authorization = server.get("http_headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def project_context_token_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    server = value.get("mcpServers", {}).get("projectContext", {}) if isinstance(value, dict) else {}
    authorization = server.get("headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def project_context_provider_tokens(args: argparse.Namespace, providers: list[str]) -> dict[str, str | None]:
    return {
        provider: (
            project_context_token_from_codex(args.codex_root / "config.toml")
            if provider == "codex"
            else project_context_token_from_claude(args.claude_config)
        )
        for provider in providers
    }


def project_context_url_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    server = value.get("mcp_servers", {}).get("projectContext", {})
    url = server.get("url") if isinstance(server, dict) else None
    return url if isinstance(url, str) else None


def project_context_url_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    server = value.get("mcpServers", {}).get("projectContext", {}) if isinstance(value, dict) else {}
    url = server.get("url") if isinstance(server, dict) else None
    return url if isinstance(url, str) else None


def project_context_provider_urls(args: argparse.Namespace, providers: list[str]) -> dict[str, str | None]:
    return {
        provider: (
            project_context_url_from_codex(args.codex_root / "config.toml")
            if provider == "codex"
            else project_context_url_from_claude(args.claude_config)
        )
        for provider in providers
    }


def agent_memory_token_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = value.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return None
    server = servers.get("agentMemory") or servers.get("codexMemory") or servers.get("codexmemory")
    authorization = server.get("http_headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def agent_memory_token_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = value.get("mcpServers", {}) if isinstance(value, dict) else {}
    server = (servers.get("agentMemory") or servers.get("codexMemory")) if isinstance(servers, dict) else None
    authorization = server.get("headers", {}).get("Authorization") if isinstance(server, dict) else None
    return authorization.removeprefix("Bearer ") if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def agent_memory_provider_tokens(args: argparse.Namespace, providers: list[str]) -> dict[str, str | None]:
    return {
        provider: (
            agent_memory_token_from_codex(args.codex_root / "config.toml")
            if provider == "codex"
            else agent_memory_token_from_claude(args.claude_config)
        )
        for provider in providers
    }


def agent_memory_url_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = value.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return None
    server = servers.get("agentMemory") or servers.get("codexMemory") or servers.get("codexmemory")
    url = server.get("url") if isinstance(server, dict) else None
    return url if isinstance(url, str) else None


def agent_memory_url_from_claude(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = value.get("mcpServers", {}) if isinstance(value, dict) else {}
    server = (servers.get("agentMemory") or servers.get("codexMemory")) if isinstance(servers, dict) else None
    url = server.get("url") if isinstance(server, dict) else None
    return url if isinstance(url, str) else None


def agent_memory_provider_urls(args: argparse.Namespace, providers: list[str]) -> dict[str, str | None]:
    return {
        provider: (
            agent_memory_url_from_codex(args.codex_root / "config.toml")
            if provider == "codex"
            else agent_memory_url_from_claude(args.claude_config)
        )
        for provider in providers
    }


def credential_revision(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_project_context_installer(platform_root: Path, args: argparse.Namespace) -> Path:
    bundled = platform_root / "tools" / "install_project_context_mcp.py"
    if bundled.is_file():
        return bundled
    managed = Path(__file__).resolve().with_name("install_project_context_mcp.py")
    if not managed.is_file():
        raise SystemExit("managed Project Context installer is missing; run an Agent Platform update")
    return managed


def resolve_agent_memory_installer(platform_root: Path, args: argparse.Namespace) -> Path:
    bundled = platform_root / "tools" / "install_agent_memory_mcp.py"
    if bundled.is_file():
        return bundled
    managed = Path(__file__).resolve().with_name("install_agent_memory_mcp.py")
    if not managed.is_file():
        raise SystemExit("managed Agent Memory installer is missing; run an Agent Platform update")
    return managed


def configure_project_context(
    platform_root: Path,
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
    client_id: str,
) -> None:
    if args.check_only:
        return
    server_state = desired.get("projectContext")
    if not isinstance(server_state, dict) or server_state.get("status") != "available":
        if not args.project_context_token:
            return
        expected_revision = credential_revision(args.project_context_token)
        url = args.project_context_url
    else:
        expected_revision = server_state.get("credentialRevision")
        url = server_state.get("url")
        if not isinstance(expected_revision, str) or len(expected_revision) != 64 or not isinstance(url, str):
            raise SystemExit("Control Plane returned invalid Project Context metadata")
    provider_tokens = project_context_provider_tokens(args, providers)
    reusable = next(
        (
            token for token in [args.project_context_token, *provider_tokens.values()]
            if token is not None and credential_revision(token) == expected_revision
        ),
        None,
    )
    if reusable is None:
        credential = fetch_project_context_credential(args, client_id, expected_revision, url)
        reusable = credential["token"]
        url = credential["url"]
    command = [
        sys.executable, str(resolve_project_context_installer(platform_root, args)),
        "--url", url,
        "--codex-config", str(args.codex_root / "config.toml"),
        "--claude-config", str(args.claude_config),
    ]
    command.append("--skip-endpoint-check")  # already authenticated with redirects disabled in v1 preflight
    for provider in providers:
        command.extend(["--provider", provider])
    environment = os.environ.copy()
    environment["PROJECT_CONTEXT_ACCESS_TOKEN"] = reusable
    subprocess.run(command, check=True, env=environment)


def agent_memory_requires_update(
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
) -> bool:
    server_state = desired.get("agentMemory")
    if not isinstance(server_state, dict) or server_state.get("status") != "available":
        return False
    expected_revision = server_state.get("credentialRevision")
    url = server_state.get("url")
    organization_id = server_state.get("organizationId")
    if (not isinstance(expected_revision, str) or len(expected_revision) != 64 or not isinstance(url, str)
            or not isinstance(organization_id, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization_id)):
        raise SystemExit("Control Plane returned invalid Agent Memory metadata")
    provider_tokens = agent_memory_provider_tokens(args, providers)
    provider_urls = agent_memory_provider_urls(args, providers)
    return any(
        provider_tokens[provider] is None
        or credential_revision(provider_tokens[provider]) != expected_revision
        or provider_urls[provider] != url
        for provider in providers
    )


def resolve_agent_memory_credential(
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
    client_id: str,
) -> dict | None:
    server_state = desired.get("agentMemory")
    provider_tokens = agent_memory_provider_tokens(args, providers)
    if not isinstance(server_state, dict) or server_state.get("status") != "available":
        reusable = next((token for token in [args.agent_memory_token, *provider_tokens.values()] if token), None)
        if reusable is None:
            return None
        organization_id = getattr(args, "agent_memory_organization", None)
        if not isinstance(organization_id, str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization_id
        ):
            raise SystemExit(
                "Agent Memory tenant metadata is unavailable; pass --agent-memory-organization explicitly"
            )
        return {"url": args.agent_memory_url, "token": reusable, "organizationId": organization_id}
    expected_revision = server_state.get("credentialRevision")
    url = server_state.get("url")
    organization_id = server_state.get("organizationId")
    if (not isinstance(expected_revision, str) or len(expected_revision) != 64 or not isinstance(url, str)
            or not isinstance(organization_id, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization_id)):
        raise SystemExit("Control Plane returned invalid Agent Memory metadata")
    reusable = next(
        (
            token for token in [args.agent_memory_token, *provider_tokens.values()]
            if token is not None and credential_revision(token) == expected_revision
        ),
        None,
    )
    if reusable is not None:
        return {"url": url, "token": reusable, "organizationId": organization_id, "credentialRevision": expected_revision}
    return fetch_agent_memory_credential(args, client_id, expected_revision, url)


def configure_agent_memory(
    platform_root: Path,
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
    client_id: str,
    credential: dict | None = None,
) -> None:
    if args.check_only:
        return
    if credential is None and not agent_memory_requires_update(args, providers, desired):
        return
    credential = credential or resolve_agent_memory_credential(args, providers, desired, client_id)
    if credential is None:
        raise SystemExit("Agent Memory is unavailable in Control Plane; client configuration was not changed")
    command = [
        sys.executable, str(resolve_agent_memory_installer(platform_root, args)),
        "--url", credential["url"],
        "--organization", credential["organizationId"],
        "--codex-config", str(args.codex_root / "config.toml"),
        "--claude-config", str(args.claude_config),
    ]
    command.append("--skip-endpoint-check")  # already authenticated with redirects disabled in preflight
    for provider in providers:
        command.extend(["--provider", provider])
    environment = os.environ.copy()
    environment["AGENT_MEMORY_TOKEN"] = credential["token"]
    subprocess.run(command, check=True, env=environment)


def kimi_is_installed(platform_root: Path) -> bool:
    """Kimi Code or Kimi Desktop exists here, located the way its own adapter locates it."""
    import importlib.util
    path = platform_root / "adapters" / "kimi" / "install.py"
    spec = importlib.util.spec_from_file_location("agent_platform_kimi_install", path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return any(home.is_dir() for home in module.default_kimi_homes(True))


def configure_managed_mcp_servers(
    platform_root: Path,
    args: argparse.Namespace,
    providers: list[str],
    desired: dict,
    client_id: str,
    temporary: Path,
) -> list[dict]:
    servers = desired.get("mcpServers")
    if not isinstance(servers, list):
        raise SystemExit("Control Plane returned invalid managed MCP desired state")
    credentials: dict[str, str] = {}
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get("targetProviders"), list):
            raise SystemExit("Control Plane returned invalid managed MCP desired state")
        targets = [provider for provider in providers if provider in server["targetProviders"]]
        if server.get("authType") == "bearer" and server.get("enabled") and targets:
            credentials[server["id"]] = fetch_managed_mcp_credential(
                args, client_id, server, targets[0],
            )
    manifest_path = temporary / "managed-mcp-manifest.json"
    credential_path = temporary / "managed-mcp-credentials.json"
    # Tools run without asking only where the owner of this computer agreed to it.
    servers = servers_the_owner_agreed_to(servers)
    # "writes" only where every Codex here knows it; an older one would refuse the whole config.
    servers = managed_servers_for_codex(servers, providers)
    manifest_path.write_text(json.dumps({"version": 1, "servers": servers}), encoding="utf-8")
    credential_path.write_text(json.dumps({"version": 1, "tokens": credentials}), encoding="utf-8")
    manifest_path.chmod(0o600)
    credential_path.chmod(0o600)
    installer = platform_root / "tools" / "install_managed_mcp_servers.py"
    command = [
        sys.executable, str(installer), "--manifest", str(manifest_path),
        "--credentials", str(credential_path),
        "--state", str(Path.home() / ".agent-platform/client/managed-mcp-state.json"),
        "--codex-config", str(args.codex_root / "config.toml"),
        "--claude-config", str(args.claude_config),
        # Kimi and OpenCode on this computer receive the servers targeted at them, the task
        # server among them. The installer skips OpenCode when it is absent.
        "--opencode-root", str(Path.home() / ".config/opencode"),
    ]
    if kimi_is_installed(platform_root):
        command.extend(["--kimi-mirror", str(Path.home() / ".agent-platform/client/managed-mcp-mirror.json")])
    if installer_accepts(installer, "--cursor-root") and (
            app_chosen(desired, "cursor") or cursor_has_managed_entries()):
        # Cursor when its owner chose it, or to take back what was written while it was chosen;
        # never only because ~/.cursor exists. The installer skips it when ~/.cursor is absent.
        command.extend(["--cursor-root", str(Path.home() / ".cursor")])
    for provider in providers:
        command.extend(["--provider", provider])
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or not isinstance(result.get("servers"), list):
        raise RuntimeError("Managed MCP installer returned an invalid receipt")
    note_restart_required(args, result)
    failed = result.get("failed")
    for app, reason in (failed.items() if isinstance(failed, dict) else ()):
        if isinstance(app, str) and isinstance(reason, str):
            note_app_failure(args, app, reason)
    return result["servers"]


def cursor_has_managed_entries() -> bool:
    """Whether the installer of managed servers recorded entries of its own in Cursor."""
    state = _read_local_json(Path.home() / ".agent-platform/client/managed-mcp-state.json")
    by_app = state.get("providers") if isinstance(state.get("providers"), dict) else {}
    return bool(by_app.get("cursor"))


def note_restart_required(args: argparse.Namespace, result: dict) -> None:
    """Apps whose config an installer changed and that read it only when they start (Cursor):
    their receipt asks for a restart (apply_mirrored_platform)."""
    apps = result.get("restartRequired") if isinstance(result, dict) else None
    if isinstance(apps, list):
        noted = set(getattr(args, "restart_required_apps", set()) or set())
        args.restart_required_apps = noted | {app for app in apps if isinstance(app, str)}


def apply_mcp_inventory(platform_root: Path, args: argparse.Namespace, desired: dict, reports: list[dict],
                        client_id: str) -> None:
    """After the managed servers are written: say which MCP servers each app has (names only), and
    answer the owner's requests for the address of a server connected by hand.

    Without the inventory a server added by hand in one app is not seen in the web application.
    """

    tool = platform_root / "tools" / "local_mcp.py"
    if not tool.is_file():
        return

    def run(command: str, provider: str, *extra: str) -> dict:
        config = args.codex_root / "config.toml" if provider == "codex" else args.claude_config
        completed = subprocess.run([sys.executable, str(tool), command, "--provider", provider, "--config", str(config), *extra],
                                   check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return json.loads(completed.stdout)

    requests = desired.get("mcpImports")
    for item in requests if isinstance(requests, list) else []:
        provider, key, request_id = item.get("provider"), item.get("configKey"), item.get("requestId")
        if provider not in {"codex", "claude"} or not isinstance(key, str) or not isinstance(request_id, str):
            continue
        try:
            exported = run("export", provider, "--key", key)
            release_transport().request_json(args.control_plane_url, "/v1/client-session/mcp-import", {
                "clientId": client_id, "clientToken": required_client_token(args), "requestId": request_id,
                "url": exported.get("url"), "error": exported.get("error"),
            })
        except (subprocess.CalledProcessError, ValueError, OSError):
            continue
    for report in reports:
        try:
            report["mcpInventory"] = run("inventory", report["provider"])["servers"]
        except (subprocess.CalledProcessError, ValueError, OSError, KeyError):
            continue
