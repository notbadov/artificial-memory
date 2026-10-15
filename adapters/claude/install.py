#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from compile_agent_catalog import write_compiled_catalog

PLATFORM_SOURCE_ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGE_ROOT = PLATFORM_SOURCE_ROOT / "packages/python/agent-platform-core"
if str(CORE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_PACKAGE_ROOT))

from agent_platform_core.external_skills import (
    default_external_skill_state_manifest,
    external_skill_registry_digest,
    load_external_skill_registry,
    utc_now,
    validate_external_skill_state_manifest,
)
from agent_platform_core.managed_skills import (
    default_backup_root,
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


MEMORY_SERVER_NAME = "agentMemory"
MEMORY_DEFAULT_URL = "https://app.artificial-memory.com/agent-memory/mcp"
# Every hook the platform installs runs a script from this directory. An entry that does so
# is the platform's own and is replaced on each install; anything else is the person's.
MANAGED_HOOK_MARKER = "agent-platform/scripts/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install shared agent-platform behavior for Claude Code.")
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--memory-url", default=os.environ.get("AGENT_MEMORY_URL", MEMORY_DEFAULT_URL))
    parser.add_argument("--memory-token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument("--memory-organization", default=os.environ.get("AGENT_MEMORY_ORGANIZATION_ID"))
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--external-skill-state-manifest", type=Path)
    parser.add_argument("--external-skill-reconciliation-receipt", type=Path)
    parser.add_argument("--skill-delivery-manifest", type=Path)
    parser.add_argument("--skill-backup-root", type=Path, default=default_backup_root("claude"))
    return parser.parse_args()


def install_directory(source: Path, destination: Path, allowed_root: Path, extra: dict[str, Path] | None = None) -> None:
    """Replace ``destination`` with a copy of ``source`` in one rename. ``extra`` adds files kept
    elsewhere in the release ({name in the directory: source file}); they are in place before
    the directory is, so a hook that runs meanwhile never finds its script missing."""
    allowed_root = allowed_root.resolve(strict=False)
    resolved_destination = destination.resolve(strict=False)
    if resolved_destination != allowed_root and allowed_root not in resolved_destination.parents:
        raise SystemExit(f"refusing to replace directory outside {allowed_root}: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        shutil.copytree(source, staged)
        for name, file in (extra or {}).items():
            shutil.copy2(file, staged / name)
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
                if destination.parent.resolve(strict=False) not in resolved_backup.parents:
                    raise SystemExit(f"refusing to remove backup outside {destination.parent}: {backup}")
                shutil.rmtree(resolved_backup)
        else:
            staged.rename(destination)


def atomic_write(target: Path, content: str, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent) as handle:
        handle.write(content)
        staged = Path(handle.name)
    if mode is not None:
        staged.chmod(mode)
    staged.replace(target)


def managed_block(block_id: str, body: str) -> str:
    return (
        f"<!-- agent-platform:{block_id}:start -->\n"
        f"{body.strip()}\n"
        f"<!-- agent-platform:{block_id}:end -->"
    )


def install_instructions(platform_root: Path, target: Path) -> None:
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    blocks = {
        "claude-base": (platform_root / "adapters" / "claude" / "CLAUDE.md").read_text(encoding="utf-8"),
    }
    for policy in delivered_policies(platform_root):
        blocks[policy_block_id(policy)] = policy.read_text(encoding="utf-8")
    updated = remove_managed_blocks(current, RETIRED_POLICY_BLOCK_IDS)
    for block_id, body in blocks.items():
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


def load_external_state(platform_root: Path, path: Path | None) -> tuple[dict, dict]:
    registry = load_external_skill_registry(platform_root)
    state = (
        default_external_skill_state_manifest(registry)
        if path is None else read_json(path)
    )
    errors = validate_external_skill_state_manifest(state, registry)
    if errors:
        raise SystemExit("invalid external skill state manifest:\n- " + "\n- ".join(errors))
    return registry, state


def install_skills(
    platform_root: Path,
    claude_root: Path,
    registry: dict,
    state: dict,
    delivery: dict,
    backup_root: Path,
) -> tuple[dict, list[dict]]:
    """Reconcile ~/.claude/skills; a user's own skill is never changed (managed_skills)."""

    state_by_id = {item["id"]: item for item in state["skills"]}
    desired = delivered_skill_sources(
        platform_root, delivery, {identifier: item["enabled"] for identifier, item in state_by_id.items()}
    )
    for identifier, item in state_by_id.items():
        if item["enabled"] and identifier not in delivery["disabled"] and identifier not in desired:
            raise SystemExit(f"enabled external skill has no canonical Claude source: {identifier}")
    result = reconcile_skills(
        skills_root=claude_root / "skills",
        desired=desired,
        recorded=recorded_from_receipt(read_json(claude_root / "agent-platform-install.json")),
        backup_root=backup_root,
        platform_sources=platform_skill_sources(platform_root),
    )
    reconciliation = []
    for identifier, item in sorted(state_by_id.items()):
        destination = claude_root / "skills" / identifier
        if identifier in result["managed"]:
            actual_state = "installed"
            evidence = {"destination": f"skills/{identifier}", "skillDigest": result["managed"][identifier], "managedCopyRemoved": False}
        else:
            actual_state = "not_installed"
            evidence = {"destination": f"skills/{identifier}", "destinationAbsent": not destination.exists(),
                        "managedCopyRemoved": identifier in result["removed"],
                        "conflict": identifier in result["conflicts"]}
        reconciliation.append({
            "id": identifier,
            "enabled": item["enabled"],
            "revision": item["revision"],
            "actualState": actual_state,
            "evidence": evidence,
        })
    return result, reconciliation


def merge_hook_settings(settings: dict[str, Any], hook_fragment: dict[str, Any]) -> None:
    target_hooks = settings.setdefault("hooks", {})
    if not isinstance(target_hooks, dict):
        raise SystemExit("Claude settings hooks must be a JSON object")
    source_hooks = hook_fragment.get("hooks", {})
    if not isinstance(source_hooks, dict):
        raise SystemExit("Claude hook fragment is invalid")
    for event_name, entries in source_hooks.items():
        if not isinstance(entries, list):
            raise SystemExit(f"Claude hook fragment event must be a list: {event_name}")
        existing = target_hooks.setdefault(event_name, [])
        if not isinstance(existing, list):
            raise SystemExit(f"Claude settings hook event must be a list: {event_name}")
        filtered = [entry for entry in existing if MANAGED_HOOK_MARKER not in json.dumps(entry, ensure_ascii=False)]
        target_hooks[event_name] = [*filtered, *entries]


def install_settings(platform_root: Path, target: Path) -> None:
    settings = read_json(target)
    fragment = read_json(platform_root / "adapters" / "claude" / "settings.fragment.json")
    hooks_root = platform_root / "adapters" / "claude" / "hooks"
    # All fragments are merged in one pass: the merge first drops the platform's previous
    # entries of an event, so merging file by file would drop the entries of the file before.
    combined: dict[str, list[Any]] = {}
    for source in (fragment, *(read_json(hook_file) for hook_file in sorted(hooks_root.glob("*.json")))):
        source_hooks = source.get("hooks", {})
        if not isinstance(source_hooks, dict):
            raise SystemExit("Claude hook fragment is invalid")
        for event_name, entries in source_hooks.items():
            if not isinstance(entries, list):
                raise SystemExit(f"Claude hook fragment event must be a list: {event_name}")
            combined.setdefault(event_name, []).extend(entries)
    merge_hook_settings(settings, {"hooks": combined})
    environment = settings.setdefault("env", {})
    if not isinstance(environment, dict):
        raise SystemExit("Claude settings env must be a JSON object")
    environment["AGENT_SKILLS_ROOT"] = str(target.parent / "skills")
    environment["AGENT_LEARNING_GATE_ROOT"] = str(target.parent.parent / ".agent-platform" / "learning-loop" / "state")
    atomic_write(target, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")


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
    if not isinstance(headers, dict):
        return None
    authorization = headers.get("Authorization")
    if not isinstance(authorization, str):
        return None
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else None


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


def install_memory(config_path: Path, url: str, token: str, organization: str) -> None:
    config = read_json(config_path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"mcpServers must be a JSON object in {config_path}")
    servers[MEMORY_SERVER_NAME] = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}", "X-Agent-Platform-Organization": organization},
    }
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def install_launcher(source: Path, destination: Path) -> None:
    atomic_write(destination, source.read_text(encoding="utf-8"), mode=0o755)


def main() -> None:
    args = parse_args()
    platform_root = args.platform_root.resolve()
    claude_root = args.claude_root.expanduser().resolve()
    claude_config = args.claude_config.expanduser().resolve()
    adapter_root = platform_root / "adapters" / "claude"
    if not (platform_root / "shared" / "skills").is_dir() or not adapter_root.is_dir():
        raise SystemExit(f"invalid agent-platform root: {platform_root}")

    claude_root.mkdir(parents=True, exist_ok=True)
    external_registry, external_state = load_external_state(
        platform_root, args.external_skill_state_manifest
    )
    delivery = load_skill_delivery_manifest(args.skill_delivery_manifest)
    skills, reconciliation = install_skills(
        platform_root, claude_root, external_registry, external_state, delivery,
        args.skill_backup_root.expanduser(),
    )
    registry_ids = {item["id"] for item in external_registry["skills"]}
    managed_external = sorted(identifier for identifier in skills["managed"] if identifier in registry_ids)

    install_directory(
        platform_root / "shared" / "workflows",
        claude_root / "agent-platform" / "workflows",
        claude_root,
    )
    write_compiled_catalog(
        platform_root,
        "claude",
        claude_root / "agent-platform" / "catalog.json",
    )

    install_directory(adapter_root / "modes", claude_root / "agent-platform" / "modes", claude_root)
    # The work-session hook is shared with Codex and kept once, in adapters/common; it joins the
    # Claude scripts here, where the hooks in settings.json look for it.
    install_directory(
        adapter_root / "scripts", claude_root / "agent-platform" / "scripts", claude_root,
        extra={"work_session_hook.py": platform_root / "adapters" / "common" / "work_session_hook.py"},
    )
    install_instructions(platform_root, claude_root / "CLAUDE.md")
    install_settings(platform_root, claude_root / "settings.json")
    install_launcher(adapter_root / "scripts" / "claude-mode", args.bin_dir.expanduser().resolve() / "claude-mode")

    memory_status = "skipped"
    servers = read_json(claude_config).get("mcpServers")
    current_memory = servers.get(MEMORY_SERVER_NAME) if isinstance(servers, dict) else None
    # On a computer with the managed client Claude reaches Agent Memory as its own agent through the
    # local bridge; that entry is the client's, and no token exists to replace it with.
    if isinstance(current_memory, dict) and current_memory.get("type") == "stdio" and any(
            str(part).endswith("mcp_bridge.py") for part in current_memory.get("args") or []):
        memory_status = f"{MEMORY_SERVER_NAME} as this app's agent (managed client)"
    elif not args.skip_memory:
        token = args.memory_token or token_from_codex_config(args.codex_config.expanduser().resolve())
        if not token:
            raise SystemExit(
                "Agent Memory token is unavailable. Set AGENT_MEMORY_TOKEN, pass --memory-token, "
                "or keep a configured agentMemory server in ~/.codex/config.toml."
            )
        organization = (
            args.memory_organization
            or organization_from_codex_config(args.codex_config.expanduser().resolve())
            or ""
        ).strip()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
            raise SystemExit("Agent Memory organization is required")
        install_memory(claude_config, args.memory_url, token, organization)
        memory_status = f"configured {MEMORY_SERVER_NAME} at {args.memory_url}"

    release_manifest = read_json(args.release_manifest) if args.release_manifest else {}
    sources = release_manifest.get("sources")
    source = sources.get("agent-platform") if isinstance(sources, dict) else None
    receipt = {
        "release_id": release_manifest.get("release_id"),
        "agent_platform_sha": source.get("sha") if isinstance(source, dict) else None,
        "skills": sorted(skills["managed"]),
        # Every platform skill on this computer and its digest: what the next run may change.
        "managed_skill_digests": skills["managed"],
        "skill_conflicts": skills["conflicts"],
        "skill_backups": skills["backups"],
        "skill_delivery": delivery,
        # Read by older installers after a rollback.
        "managed_external_skills": managed_external,
        "managed_external_skill_digests": {identifier: skills["managed"][identifier] for identifier in managed_external},
        "external_skill_state_source": external_state["source"],
        "external_skill_registry_digest": external_skill_registry_digest(external_registry),
    }
    atomic_write(
        claude_root / "agent-platform-install.json",
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )
    if args.external_skill_reconciliation_receipt is not None:
        atomic_write(
            args.external_skill_reconciliation_receipt,
            json.dumps({
                "version": 1,
                "provider": "claude",
                "releaseId": release_manifest.get("release_id"),
                "registryDigest": external_skill_registry_digest(external_registry),
                "generatedAt": utc_now(),
                "skills": reconciliation,
            }, ensure_ascii=False, indent=2) + "\n",
        )

    print(f"installed Claude Code adapter into {claude_root}")
    print(f"verified {len(skills['managed'])} canonical Claude skills")
    for identifier in skills["conflicts"]:
        print(f"left your own skill in place: skills/{identifier} (a platform skill has the same name)")
    for backup in skills["backups"]:
        print(f"kept the previous copy of skills/{backup['id']} in {backup['path']}")
    print(f"Agent Memory: {memory_status}")
    print(f"mode launcher: {args.bin_dir.expanduser().resolve() / 'claude-mode'}")


if __name__ == "__main__":
    main()
