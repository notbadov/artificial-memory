#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

CORE_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "packages/python/agent-platform-core"
if str(CORE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_PACKAGE_ROOT))

from agent_platform_core.skill_delivery import delivered_skill_sources, load_skill_delivery_manifest


MEMORY_SERVER_NAME = "agentMemory"
MEMORY_DEFAULT_URL = "https://app.artificial-memory.com/agent-memory/mcp"
AUTH_ENV_NAME = "AGENT_MEMORY_AUTH_HEADER"
ORGANIZATION_ENV_NAME = "AGENT_MEMORY_ORGANIZATION_ID"
MANIFEST_VERSION = 1
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def default_desktop_config() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if sys_platform() == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def sys_platform() -> str:
    import sys

    return sys.platform


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and install the Claude Desktop agent-platform adapter.")
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--desktop-config", type=Path, default=default_desktop_config())
    parser.add_argument(
        "--packages-dir",
        type=Path,
        default=Path.home() / ".agent-platform/claude-desktop/skills",
    )
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--claude-code-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--memory-url", default=os.environ.get("AGENT_MEMORY_URL", MEMORY_DEFAULT_URL))
    parser.add_argument("--memory-token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument("--memory-organization", default=os.environ.get(ORGANIZATION_ENV_NAME))
    parser.add_argument("--npx-command")
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--skip-skills", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--skill-delivery-manifest", type=Path)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"expected a JSON object in {path}")
    return value


def atomic_write(target: Path, content: str, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent) as handle:
        handle.write(content)
        staged = Path(handle.name)
    if mode is not None:
        staged.chmod(mode)
    staged.replace(target)


def token_from_codex_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    servers = config.get("mcp_servers")
    server = servers.get("agentMemory") or servers.get("codexMemory") if isinstance(servers, dict) else None
    headers = server.get("http_headers") if isinstance(server, dict) else None
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    return authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def token_from_claude_code_config(path: Path) -> str | None:
    try:
        servers = read_json(path).get("mcpServers")
    except (OSError, json.JSONDecodeError):
        return None
    server = servers.get("agentMemory") or servers.get("codexMemory") if isinstance(servers, dict) else None
    headers = server.get("headers") if isinstance(server, dict) else None
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    return authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def token_from_desktop_config(path: Path) -> str | None:
    try:
        servers = read_json(path).get("mcpServers")
    except (OSError, json.JSONDecodeError):
        return None
    server = servers.get(MEMORY_SERVER_NAME) if isinstance(servers, dict) else None
    environment = server.get("env") if isinstance(server, dict) else None
    authorization = environment.get(AUTH_ENV_NAME) if isinstance(environment, dict) else None
    return authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else None


def resolve_npx(explicit: str | None) -> str:
    if explicit:
        return str(Path(explicit).expanduser().resolve(strict=False))
    candidates = ("npx.cmd", "npx") if os.name == "nt" else ("npx",)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return str(Path(found).resolve())
    raise SystemExit("npx is required for the Claude Desktop Agent Memory bridge")


def short_description(skill_root: Path) -> str:
    metadata = skill_root / "agents/openai.yaml"
    if not metadata.is_file():
        raise SystemExit(f"Desktop skill metadata is missing: {metadata}")
    match = re.search(r'^\s*short_description:\s*["\'](.+)["\']\s*$', metadata.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit(f"short_description is missing from {metadata}")
    description = match.group(1).strip()
    if not description or len(description) > 200:
        raise SystemExit(f"Desktop skill description must contain 1-200 characters: {metadata}")
    return description


def render_desktop_skill(skill_root: Path) -> str:
    source = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n", source)
    if not match:
        raise SystemExit(f"SKILL.md frontmatter is invalid: {skill_root}")
    frontmatter = match.group(1)
    if not re.search(r"(?m)^name:\s*", frontmatter):
        raise SystemExit(f"SKILL.md name is missing: {skill_root}")
    rendered_frontmatter, count = re.subn(
        r"(?m)^description:\s*.*$",
        f"description: {json.dumps(short_description(skill_root), ensure_ascii=False)}",
        frontmatter,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"SKILL.md description is missing: {skill_root}")
    return f"---\n{rendered_frontmatter}\n---\n\n{source[match.end():].lstrip()}"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def add_zip_file(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def build_skill_package(skill_root: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=target.parent, suffix=".zip") as handle:
        staged = Path(handle.name)
    try:
        with zipfile.ZipFile(staged, "w") as archive:
            for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
                relative = path.relative_to(skill_root)
                content = render_desktop_skill(skill_root).encode("utf-8") if relative.as_posix() == "SKILL.md" else path.read_bytes()
                add_zip_file(archive, f"{skill_root.name}/{relative.as_posix()}", content)
        staged.replace(target)
    finally:
        if staged.exists():
            staged.unlink()


def delivered_skills(platform_root: Path, delivery: dict | None = None) -> dict[str, Path]:
    """The skills this organization's computers receive (skill_delivery); registry defaults decide
    external skills, since Claude Desktop gets no external-skill state."""

    registry = read_json(platform_root / "shared/external-skills/registry.json")
    defaults = {item["id"]: bool(item.get("defaultEnabled")) for item in registry.get("skills", [])}
    return delivered_skill_sources(platform_root, delivery or load_skill_delivery_manifest(None), defaults)


def build_skill_packages(platform_root: Path, packages_dir: Path, delivery: dict | None = None) -> dict[str, Any]:
    skills_root = platform_root / "shared/skills"
    if not skills_root.is_dir():
        raise SystemExit(f"invalid agent-platform root: {platform_root}")
    packages_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    expected_names: set[str] = set()
    for skill_root in delivered_skills(platform_root, delivery).values():
        package = packages_dir / f"{skill_root.name}.zip"
        build_skill_package(skill_root, package)
        expected_names.add(package.name)
        entries.append({
            "name": skill_root.name,
            "source_sha256": tree_digest(skill_root),
            "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        })
    for stale in packages_dir.glob("*.zip"):
        if stale.name not in expected_names:
            stale.unlink()
    manifest = {"schema_version": MANIFEST_VERSION, "skills": entries}
    atomic_write(packages_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def install_memory_bridge(config_path: Path, url: str, token: str, organization: str, npx_command: str) -> None:
    config = read_json(config_path)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise SystemExit(f"mcpServers must be a JSON object in {config_path}")
    servers[MEMORY_SERVER_NAME] = {
        "command": npx_command,
        "args": [
            "-y",
            "mcp-remote",
            url,
            "--header",
            f"Authorization:${{{AUTH_ENV_NAME}}}",
            "--header",
            f"X-Agent-Platform-Organization:${{{ORGANIZATION_ENV_NAME}}}",
        ],
        "env": {AUTH_ENV_NAME: f"Bearer {token}", ORGANIZATION_ENV_NAME: organization},
    }
    rendered = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    current = config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    if current == rendered:
        return
    if current is not None:
        backup = config_path.with_name(f"{config_path.name}.agent-platform.bak")
        atomic_write(backup, current, mode=0o600)
    atomic_write(config_path, rendered, mode=0o600)


def verify_packages(platform_root: Path, packages_dir: Path, delivery: dict | None = None) -> list[str]:
    errors: list[str] = []
    manifest_path = packages_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing package manifest: {manifest_path}"]
    manifest = read_json(manifest_path)
    entries = manifest.get("skills")
    if not isinstance(entries, list):
        return [f"invalid package manifest: {manifest_path}"]
    expected = delivered_skills(platform_root, delivery)
    actual_names = {entry.get("name") for entry in entries if isinstance(entry, dict)}
    if actual_names != set(expected):
        errors.append("packaged skill names do not match the delivered skills")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            errors.append("invalid skill manifest entry")
            continue
        name = entry["name"]
        package = packages_dir / f"{name}.zip"
        source = expected.get(name)
        if source is None or not package.is_file():
            errors.append(f"missing source or package for {name}")
            continue
        if entry.get("source_sha256") != tree_digest(source):
            errors.append(f"source checksum mismatch for {name}")
        if entry.get("package_sha256") != hashlib.sha256(package.read_bytes()).hexdigest():
            errors.append(f"package checksum mismatch for {name}")
        with zipfile.ZipFile(package) as archive:
            skill_path = f"{name}/SKILL.md"
            if skill_path not in archive.namelist():
                errors.append(f"{name} package lacks root skill folder and SKILL.md")
                continue
            rendered = archive.read(skill_path).decode("utf-8")
            description = re.search(r"(?m)^description:\s*(.+)$", rendered)
            if description is None or len(json.loads(description.group(1))) > 200:
                errors.append(f"{name} Desktop description is invalid")
    return errors


def verify_memory_bridge(config_path: Path, url: str) -> list[str]:
    try:
        servers = read_json(config_path).get("mcpServers")
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot read Desktop config: {error}"]
    server = servers.get(MEMORY_SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return ["Claude Desktop agentMemory bridge is missing"]
    args = server.get("args")
    environment = server.get("env")
    errors: list[str] = []
    if not isinstance(args, list) or "mcp-remote" not in args or url not in args:
        errors.append("Claude Desktop agentMemory bridge command is invalid")
    authorization = environment.get(AUTH_ENV_NAME) if isinstance(environment, dict) else None
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        errors.append("Claude Desktop agentMemory authorization is missing")
    organization = environment.get(ORGANIZATION_ENV_NAME) if isinstance(environment, dict) else None
    if not isinstance(organization, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
        errors.append("Claude Desktop agentMemory organization is missing")
    return errors


def memory_is_bridged(config_path: Path) -> bool:
    """On a computer with the managed client the Claude chat app reaches Agent Memory as its own
    agent through the local bridge; that entry is the client's, and no token exists to replace it."""
    servers = read_json(config_path).get("mcpServers") if config_path.is_file() else None
    entry = servers.get(MEMORY_SERVER_NAME) if isinstance(servers, dict) else None
    return isinstance(entry, dict) and any(str(part).endswith("mcp_bridge.py") for part in entry.get("args") or [])


def main() -> None:
    args = parse_args()
    platform_root = args.platform_root.expanduser().resolve()
    packages_dir = args.packages_dir.expanduser().resolve()
    desktop_config = args.desktop_config.expanduser().resolve()
    if not args.verify_only:
        if not args.skip_skills:
            build_skill_packages(platform_root, packages_dir, load_skill_delivery_manifest(args.skill_delivery_manifest))
        bridged = memory_is_bridged(desktop_config)
        if not args.skip_memory and not bridged:
            token = (
                args.memory_token
                or token_from_codex_config(args.codex_config.expanduser().resolve())
                or token_from_claude_code_config(args.claude_code_config.expanduser().resolve())
                or token_from_desktop_config(desktop_config)
            )
            if not token:
                raise SystemExit(
                    "Agent Memory token is unavailable. Set AGENT_MEMORY_TOKEN, pass --memory-token, "
                    "or keep agentMemory configured in Codex, Claude Code, or Claude Desktop."
                )
            organization = (args.memory_organization or "").strip()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", organization):
                raise SystemExit("Agent Memory organization is required")
            install_memory_bridge(desktop_config, args.memory_url, token, organization, resolve_npx(args.npx_command))
    errors: list[str] = []
    if not args.skip_skills:
        errors.extend(verify_packages(platform_root, packages_dir, load_skill_delivery_manifest(args.skill_delivery_manifest)))
    if not args.skip_memory and not memory_is_bridged(desktop_config):
        errors.extend(verify_memory_bridge(desktop_config, args.memory_url))
    if errors:
        raise SystemExit("Claude Desktop adapter verification failed:\n- " + "\n- ".join(errors))
    package_count = len(list(packages_dir.glob("*.zip"))) if not args.skip_skills else 0
    print(f"Claude Desktop adapter verified: {package_count} skill packages")
    if memory_is_bridged(desktop_config):
        print("Agent Memory: as this app's agent (managed client)")
    elif not args.skip_memory:
        print(f"Agent Memory: configured {MEMORY_SERVER_NAME} at {args.memory_url}")
    print("restart_required: true")


if __name__ == "__main__":
    main()
