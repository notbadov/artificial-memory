#!/usr/bin/env python3
"""Install the canonical Agent Platform adapter into OpenCode."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import uuid
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
from agent_platform_core.skill_delivery import delivered_policies, delivered_skill_sources, load_skill_delivery_manifest

# Which skills in <config root>/skills the platform installed (managed_skills).
SKILLS_RECEIPT = "agent-platform-skills.json"


MEMORY_NAME = "agentMemory"
MEMORY_URL = "https://app.artificial-memory.com/agent-memory/mcp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config-root", type=Path, default=Path.home() / ".config/opencode")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--memory-url", default=os.environ.get("AGENT_MEMORY_URL", MEMORY_URL))
    parser.add_argument("--memory-token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument("--memory-organization", default=os.environ.get("AGENT_MEMORY_ORGANIZATION_ID"))
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--skip-ollama", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--mode-model", action="append", default=[], metavar="MODE=PROVIDER/MODEL")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--skill-delivery-manifest", type=Path)
    parser.add_argument("--skill-backup-root", type=Path, default=default_backup_root("opencode"))
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise SystemExit(f"OpenCode config must be valid JSON: {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"OpenCode config root must be an object: {path}")
    return value


def strip_jsonc(content: str) -> str:
    """Remove JSONC comments without touching comment-like text in strings."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(content):
        char = content[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if content.startswith("//", index):
            end = content.find("\n", index)
            index = len(content) if end < 0 else end
            continue
        if content.startswith("/*", index):
            end = content.find("*/", index + 2)
            if end < 0:
                raise SystemExit("unterminated block comment in OpenCode config")
            index = end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        handle.write(content)
        staged = Path(handle.name)
    if mode is not None:
        staged.chmod(mode)
    staged.replace(path)


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
                if not destination.exists():
                    backup.rename(destination)
                raise
            shutil.rmtree(backup)
        else:
            staged.rename(destination)


def bearer_from_headers(headers: Any) -> str | None:
    value = headers.get("Authorization") if isinstance(headers, dict) else None
    return value[7:] if isinstance(value, str) and value.startswith("Bearer ") else None


def token_from_codex(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = config.get("mcp_servers")
    server = (servers.get(MEMORY_NAME) or servers.get("codexMemory")) if isinstance(servers, dict) else None
    return bearer_from_headers(server.get("http_headers")) if isinstance(server, dict) else None


def token_from_claude(path: Path) -> str | None:
    try:
        servers = read_json(path).get("mcpServers")
    except (OSError, SystemExit):
        return None
    server = (servers.get(MEMORY_NAME) or servers.get("codexMemory")) if isinstance(servers, dict) else None
    return bearer_from_headers(server.get("headers")) if isinstance(server, dict) else None


def render_instructions(root: Path) -> str:
    parts = [(root / "adapters/opencode/OPENCODE_AGENTS.md").read_text(encoding="utf-8")]
    parts.extend(path.read_text(encoding="utf-8")
                 for path in delivered_policies(root))
    return "\n\n".join(part.strip() for part in parts) + "\n"


def discover_ollama(base_url: str) -> list[str]:
    request = urllib.request.Request(base_url.rstrip("/") + "/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return sorted({entry["name"] for entry in models if isinstance(entry, dict) and isinstance(entry.get("name"), str)})


def parse_mode_models(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        mode, separator, model = value.partition("=")
        if not separator or mode not in {"economy", "balanced-economy", "optimal"} or "/" not in model:
            raise SystemExit(f"invalid --mode-model {value!r}; expected MODE=PROVIDER/MODEL")
        result[mode] = model
    return result


def install_launcher(bin_dir: Path, runtime_root: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = str(Path(sys.executable).resolve())
    runner = str(runtime_root / "scripts/run_mode.py")
    if os.name == "nt":
        target = bin_dir / "opencode-mode.cmd"
        content = f'@echo off\r\n"{python}" "{runner}" %*\r\n'
    else:
        target = bin_dir / "opencode-mode"
        content = f'#!/bin/sh\nexec {json.dumps(python)} {json.dumps(runner)} "$@"\n'
    atomic_write(target, content, mode=0o755)
    return target


def main() -> None:
    args = parse_args()
    root = args.platform_root.expanduser().resolve()
    config_root = args.config_root.expanduser().resolve()
    if not (root / "shared/skills").is_dir():
        raise SystemExit(f"invalid agent-platform root: {root}")
    json_path = config_root / "opencode.json"
    jsonc_path = config_root / "opencode.jsonc"
    config_path = json_path if json_path.is_file() or not jsonc_path.is_file() else jsonc_path
    config = read_json(config_path)
    # What the file says now: it is rewritten only when the installer changes something, so a
    # sync that changes nothing keeps the person's own formatting and comments.
    config_before = json.dumps(config, sort_keys=True)

    runtime_root = config_root / "agent-platform"
    installed_before = runtime_root.exists()
    delivery = load_skill_delivery_manifest(args.skill_delivery_manifest)
    install_directory(root / "adapters/opencode/modes", runtime_root / "modes", config_root)
    install_directory(root / "adapters/opencode/scripts", runtime_root / "scripts", config_root)
    install_directory(root / "shared/workflows", runtime_root / "workflows", config_root)
    write_compiled_catalog(root, "opencode", runtime_root / "catalog.json")
    atomic_write(runtime_root / "AGENTS.md", render_instructions(root))
    atomic_write(runtime_root / "compaction.md", (root / "adapters/opencode/compaction.md").read_text(encoding="utf-8"))

    # <config root>/skills: a user's own skill is never changed (agent_platform_core.managed_skills).
    registry = read_json(root / "shared/external-skills/registry.json")
    defaults = {item["id"]: bool(item.get("defaultEnabled")) for item in registry.get("skills", [])}
    receipt_path = config_root / SKILLS_RECEIPT
    recorded = (recorded_from_receipt(read_json(receipt_path)) if receipt_path.is_file()
                else legacy_recorded(config_root / "skills", root, installed_before))
    skills = reconcile_skills(skills_root=config_root / "skills",
                              desired=delivered_skill_sources(root, delivery, defaults),
                              recorded=recorded, backup_root=args.skill_backup_root.expanduser(),
                              platform_sources=platform_skill_sources(root))
    atomic_write(receipt_path, json.dumps({
        "version": 1, "managed_skill_digests": skills["managed"], "skill_conflicts": skills["conflicts"],
        "skill_backups": skills["backups"], "skill_delivery": delivery,
    }, ensure_ascii=False, indent=2) + "\n")
    # The plugin's defect-learning gate runs the skill's own script. An organization that has
    # the skill switched off gets no gate: the script is not installed (install_directory above
    # replaced the scripts directory, so an earlier copy is already gone) and the plugin does
    # nothing without it (learn-from-defect is a skill an organization may switch off).
    if (config_root / "skills/learn-from-defect/SKILL.md").is_file():
        shutil.copy2(root / "shared/skills/learn-from-defect/scripts/learning_gate.py", runtime_root / "scripts/learning_gate.py")
    for identifier in skills["conflicts"]:
        print(f"left your own skill in place: skills/{identifier} (a platform skill has the same name)")
    for backup in skills["backups"]:
        print(f"kept the previous copy of skills/{backup['id']} in {backup['path']}")
    config_root.joinpath("plugins").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "adapters/opencode/plugins/agent-platform.js", config_root / "plugins/agent-platform.js")

    instructions = config.setdefault("instructions", [])
    if not isinstance(instructions, list):
        raise SystemExit(f"instructions must be an array in {config_path}")
    instruction_path = str(runtime_root / "AGENTS.md")
    if instruction_path not in instructions:
        instructions.append(instruction_path)

    token: str | None = None
    current_memory = (config.get("mcp") or {}).get(MEMORY_NAME) if isinstance(config.get("mcp"), dict) else None
    # On a computer with the managed client OpenCode reaches Agent Memory as its own agent through
    # the local bridge; that entry is the client's, and no token exists to replace it with.
    bridged = isinstance(current_memory, dict) and current_memory.get("type") == "local" and any(
        str(part).endswith("mcp_bridge.py") for part in current_memory.get("command") or [])
    if not args.skip_memory and not bridged:
        token = args.memory_token or token_from_codex(args.codex_config.expanduser()) or token_from_claude(args.claude_config.expanduser())
        if not token:
            raise SystemExit("Agent Memory token unavailable; set AGENT_MEMORY_TOKEN or configure it in Codex/Claude")
        organization = (args.memory_organization or "").strip()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
            raise SystemExit("Agent Memory organization is required")
        mcp = config.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise SystemExit(f"mcp must be an object in {config_path}")
        mcp[MEMORY_NAME] = {
            "type": "remote", "url": args.memory_url, "enabled": True, "oauth": False,
            "headers": {"Authorization": f"Bearer {token}", "X-Agent-Platform-Organization": organization},
        }

    ollama_models: list[str] = []
    if not args.skip_ollama:
        ollama_models = discover_ollama(args.ollama_url)
        if ollama_models:
            providers = config.setdefault("provider", {})
            if not isinstance(providers, dict):
                raise SystemExit(f"provider must be an object in {config_path}")
            existing = providers.get("ollama") if isinstance(providers.get("ollama"), dict) else {}
            models = existing.get("models") if isinstance(existing.get("models"), dict) else {}
            for name in ollama_models:
                models.setdefault(name, {"name": name})
            existing.update({
                "npm": "@ai-sdk/openai-compatible", "name": "Ollama (local)",
                "options": {"baseURL": args.ollama_url.rstrip("/") + "/v1"}, "models": models,
            })
            providers["ollama"] = existing

    if json.dumps(config, sort_keys=True) != config_before or not config_path.is_file():
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    mappings_path = runtime_root / "model-map.json"
    mappings = read_json(mappings_path)
    mappings.update(parse_mode_models(args.mode_model))
    atomic_write(mappings_path, json.dumps(mappings, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    launcher = install_launcher(args.bin_dir.expanduser().resolve(), runtime_root)
    print(f"installed OpenCode adapter into {config_root}")
    memory = "as this app's agent (managed client)" if bridged else (
        "skipped" if token is None else f"configured at {args.memory_url}")
    print(f"Agent Memory: {memory}")
    print(f"Ollama: {'skipped/unavailable' if not ollama_models else f'registered {len(ollama_models)} local model(s)'}")
    print(f"mode launcher: {launcher}")


if __name__ == "__main__":
    main()
