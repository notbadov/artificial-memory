"""Skills on one computer beyond the platform's own: the organization's skills and an inventory.

- ``install_organization_skills`` places the organization's active skills (fetched by the sync
  client from Control Plane, digest-checked here) into the provider's skills directory, or
  into the selected projects, through managed_skills: a user's own skill with the same name is
  never changed, and an edited copy is backed up before an update or removal.
- ``skill_inventory`` lists skill names (never content) on the computer by owner: the
  platform, the organization, the user, and provider plugins (Claude Code marketplace plugins
  in ``~/.claude/plugins``), so Console can show what each app really has.
- A delivered skill marked ``owner: user`` is the computer owner's own skill kept in Console;
  it installs the same way and the inventory lists it as ``personal``.
- Plugins are listed for Claude Code and Codex with their versions; on the owner's request one
  skill of a plugin can be read too (``export_plugin_skill``), to become a personal copy.
- ``export_skill`` reads a skill the user keeps on this computer, on the owner's request, so it
  can become such a personal skill. It returns exactly the files ``skill_digest`` counts: the
  local copy then equals the delivered one and is adopted instead of reported as a conflict.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

from .managed_skills import _ignored, reconcile_skills, recorded_from_receipt, skill_digest

PROJECT_SKILL_DIRECTORIES = {"codex": Path(".agents/skills"), "claude": Path(".claude/skills")}
# Kimi and OpenCode get skills for the whole computer only: they have no project
# skills directory of their own that the platform owns.
GLOBAL_ONLY_PROVIDERS = frozenset({"kimi", "opencode"})
MAX_INVENTORY_SKILLS = 300
MAX_PLUGINS = 50
# The package rules of Control Plane (organization_skills.py); kept equal by tests.
EXPORT_TEXT_SUFFIXES = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".sh", ".ps1", ".js", ".mjs", ".ts",
    ".csv", ".html", ".css", ".svg", ".xml", ".sql",
    ".cjs", ".jsx", ".tsx", ".swift", ".rb", ".go", ".rs", ".java", ".kt", ".php", ".lua", ".ini", ".cfg", ".tsv",
})
EXPORT_ASSET_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"})
EXPORT_MAX_FILES = 500
EXPORT_MAX_FILE_BYTES = 1024 * 1024
EXPORT_MAX_TOTAL_BYTES = 4 * 1024 * 1024


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staged.replace(path)


def _project_key(project_id: str, project_root: Path) -> str:
    return f"{project_id}-{hashlib.sha256(str(project_root).encode('utf-8')).hexdigest()[:12]}"


def _exclude_from_git(project_root: Path, provider: str, skill_ids: list[str]) -> None:
    exclude = project_root / ".git" / "info" / "exclude"
    if not (project_root / ".git").is_dir() or not skill_ids:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    base = PROJECT_SKILL_DIRECTORIES[provider].as_posix()
    missing = [pattern for pattern in (f"/{base}/{skill}/" for skill in skill_ids) if pattern not in existing.splitlines()]
    if missing:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + separator + "\n".join(missing) + "\n", encoding="utf-8")


def install_organization_skills(
    *,
    provider: str,
    skills_root: Path,
    staging: Path,
    delivered: list[dict],
    projects: list[dict],
    receipt_root: Path,
    backup_root: Path,
) -> dict:
    """Bring this provider's organization skills to ``delivered``.

    ``delivered`` is the desired-state list ({id, version, digest, scope, projectIds});
    ``staging/<id>`` holds each skill's fetched files; ``projects`` are this computer's mapped
    projects ({projectId, projectRoot}). Returns installed, conflicts, backups and failed.
    """

    summary: dict = {"installed": [], "conflicts": [], "backups": [], "failed": []}
    # A skill limited to some apps (``providers``) is not for this one: it is left out, and a
    # copy placed earlier is taken back like any skill that is no longer delivered.
    delivered = [item for item in delivered if not item.get("providers") or provider in item["providers"]]
    verified: dict[str, Path] = {}
    for item in delivered:
        source = staging / item["id"]
        if not (source / "SKILL.md").is_file():
            summary["failed"].append({"id": item["id"], "reason": "not_fetched"})
        elif skill_digest(source) != item["digest"]:
            summary["failed"].append({"id": item["id"], "reason": "digest_mismatch"})
        else:
            verified[item["id"]] = source
    failed = {entry["id"] for entry in summary["failed"]}
    personal = {item["id"] for item in delivered if item.get("owner") == "user"}

    def apply(target_root: Path, desired: dict[str, Path], receipt_path: Path, extra: dict) -> list[str]:
        receipt = _read_json(receipt_path)
        recorded = recorded_from_receipt(receipt)
        # A skill that failed to arrive keeps its previous copy: never remove on a fetch error.
        desired = {**{key: value for key, value in desired.items()}}
        keep = {key: recorded[key] for key in recorded if key in failed}
        result = reconcile_skills(skills_root=target_root, desired=desired,
                                  recorded={key: value for key, value in recorded.items() if key not in failed},
                                  backup_root=backup_root)
        managed = {**result["managed"], **keep}
        _write_json(receipt_path, {"version": 1, "managed_skill_digests": managed,
                                   "personal_skills": sorted(set(managed) & personal),
                                   "skill_conflicts": result["conflicts"], **extra})
        summary["installed"] += result["installed"]
        summary["conflicts"] += result["conflicts"]
        summary["backups"] += result["backups"]
        return sorted(result["managed"])

    organization_wide = {item["id"]: verified[item["id"]] for item in delivered
                         if item["scope"] == "organization" and item["id"] in verified}
    apply(skills_root, organization_wide, receipt_root / f"{provider}.json", {})

    if provider in GLOBAL_ONLY_PROVIDERS:
        summary["conflicts"] = sorted(set(summary["conflicts"]))
        return summary
    project_receipts = receipt_root / "projects"
    seen: set[str] = set()
    for project in projects:
        root = Path(project["projectRoot"])
        key = _project_key(project["projectId"], root)
        seen.add(key)
        desired = {item["id"]: verified[item["id"]] for item in delivered
                   if item["scope"] == "projects" and project["projectId"] in item["projectIds"] and item["id"] in verified}
        managed = apply(root / PROJECT_SKILL_DIRECTORIES[provider], desired, project_receipts / key / f"{provider}.json",
                        {"projectId": project["projectId"], "projectRoot": str(root)})
        _exclude_from_git(root, provider, managed)
    # Projects this computer no longer maps: take back what was placed there.
    for receipt_path in sorted(project_receipts.glob(f"*/{provider}.json")) if project_receipts.is_dir() else []:
        if receipt_path.parent.name in seen:
            continue
        receipt = _read_json(receipt_path)
        root = Path(receipt.get("projectRoot", ""))
        if receipt.get("projectRoot") and root.is_dir():
            apply(root / PROJECT_SKILL_DIRECTORIES[provider], {}, receipt_path,
                  {"projectId": receipt.get("projectId"), "projectRoot": str(root)})
    summary["conflicts"] = sorted(set(summary["conflicts"]))
    return summary


def _reportable(name: str) -> bool:
    """Names Control Plane accepts (contracts._skill_names); anything else is left out."""
    return 0 < len(name) <= 100 and "\\" not in name and not any(ord(character) < 32 for character in name)


def _skill_names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if path.is_dir() and not path.name.startswith(".") and _reportable(path.name)
                  and (path / "SKILL.md").is_file())


def _version(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 64 and _reportable(value) else None


def _claude_plugin_roots(plugins_root: Path, settings_path: Path) -> list[dict]:
    """Claude Code plugins installed from a marketplace (CLI) or synced by the desktop app."""

    enabled = _read_json(settings_path).get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    found: list[dict] = []
    installed = _read_json(plugins_root / "installed_plugins.json").get("plugins")
    if isinstance(installed, dict):
        for plugin_id, entries in sorted(installed.items()):
            if not _reportable(plugin_id) or "/" in plugin_id:
                continue
            for entry in entries if isinstance(entries, list) else [entries]:
                path = Path(entry.get("installPath", "")) if isinstance(entry, dict) else None
                if path and path.is_dir():
                    found.append({"id": plugin_id, "enabled": bool(enabled.get(plugin_id, False)),
                                  "version": _version(entry.get("version")), "root": path})
                    break
    synced = plugins_root / "synced"
    if synced.is_dir():
        for manifest in sorted(synced.glob("*/*/.claude-plugin/plugin.json")):
            data = _read_json(manifest)
            name = data.get("name")
            if isinstance(name, str) and _reportable(name) and "/" not in name:
                found.append({"id": name, "enabled": True, "version": _version(data.get("version")),
                              "root": manifest.parent.parent})
    return found[:MAX_PLUGINS]


def _codex_plugin_roots(codex_root: Path) -> list[dict]:
    """Codex plugins: ``[plugins."name@marketplace"]`` in config.toml, files in the plugin cache
    or in a local marketplace, so Console lists them as it lists Claude Code's."""

    try:
        import tomllib
        config = tomllib.loads((codex_root / "config.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return []
    plugins = config.get("plugins")
    marketplaces = config.get("marketplaces") if isinstance(config.get("marketplaces"), dict) else {}
    found: list[dict] = []
    for plugin_id, settings in sorted(plugins.items()) if isinstance(plugins, dict) else []:
        name, separator, marketplace = plugin_id.rpartition("@")
        if not separator or not _reportable(plugin_id) or "/" in plugin_id or not name or name in {".", ".."}:
            continue
        candidates: list[Path] = []
        cache = codex_root / "plugins" / "cache"
        for market in (marketplace, f"{marketplace}-remote"):
            base = cache / market / name
            if "/" not in market and base.is_dir():
                candidates += sorted((item for item in base.iterdir() if item.is_dir()),
                                     key=lambda item: item.stat().st_mtime, reverse=True)
        source = marketplaces.get(marketplace, {}).get("source") if isinstance(marketplaces.get(marketplace), dict) else None
        if isinstance(source, str):
            candidates += [Path(source) / "plugins" / name, Path(source) / name]
        root = next((item for item in candidates if (item / ".codex-plugin" / "plugin.json").is_file()
                     or (item / "skills").is_dir()), None)
        version = _read_json(root / ".codex-plugin" / "plugin.json").get("version") if root else None
        found.append({"id": plugin_id, "enabled": bool(isinstance(settings, dict) and settings.get("enabled", False)),
                      "version": _version(version), "root": root})
    return found[:MAX_PLUGINS]


def plugin_roots(provider: str, provider_root: Path) -> list[dict]:
    """Plugins of one app with the directory each lives in ({id, enabled, version, root})."""

    if provider == "claude":
        return _claude_plugin_roots(provider_root / "plugins", provider_root / "settings.json")
    if provider == "codex":
        return _codex_plugin_roots(provider_root)
    return []


def _plugin_entries(roots: list[dict]) -> list[dict]:
    return [{"id": item["id"], "enabled": item["enabled"],
             **({"version": item["version"]} if item["version"] else {}),
             "skills": _skill_names(item["root"] / "skills") if item["root"] else []} for item in roots]


def app_plugins(provider: str, provider_root: Path) -> list[dict]:
    """Names of an app's plugins and of the skills each brings (never content)."""

    return _plugin_entries(plugin_roots(provider, provider_root))


def skill_inventory(*, skills_root: Path, platform_receipt: Path, organization_receipt: Path,
                    plugins: list[dict] | None = None, platform_names: set[str] | None = None) -> dict:
    """``platform_names`` (the release's own skill names) stand in for an app that keeps no
    receipt of platform skills, such as a Kimi or OpenCode installed before receipts existed."""

    platform = set(recorded_from_receipt(_read_json(platform_receipt)))
    if not platform_receipt.is_file() and platform_names:
        platform = set(platform_names)
    receipt = _read_json(organization_receipt)
    delivered = set(recorded_from_receipt(receipt))
    personal = delivered & {name for name in receipt.get("personal_skills", []) if isinstance(name, str)}
    organization = delivered - personal
    present = _skill_names(skills_root)
    return {
        "platform": sorted(name for name in present if name in platform)[:MAX_INVENTORY_SKILLS],
        "organization": sorted(name for name in present if name in organization)[:MAX_INVENTORY_SKILLS],
        "personal": sorted(name for name in present if name in personal)[:MAX_INVENTORY_SKILLS],
        "user": sorted(name for name in present if name not in platform | delivered)[:MAX_INVENTORY_SKILLS],
        "plugins": [{**plugin, "skills": plugin["skills"][:100]} for plugin in (plugins or [])],
    }


def export_skill(*, skills_root: Path, skill_id: str) -> dict:
    """Files of the skill ``skills_root/<skill_id>``: {"files": [...]} or {"error": code}.

    Codes: not_found, unsupported_file (a file Control Plane does not accept), too_large, unreadable.
    Text files travel as utf8, small pictures as base64.
    """

    if not _reportable(skill_id) or "/" in skill_id or skill_id in {".", ".."}:
        return {"error": "not_found"}
    root = skills_root / skill_id
    if not (root / "SKILL.md").is_file():
        return {"error": "not_found"}
    files: list[dict] = []
    total = 0
    try:
        for item in sorted(
            root.rglob("*"),
            key=lambda candidate: PurePosixPath(candidate.relative_to(root).as_posix()).parts,
        ):
            relative = item.relative_to(root)
            if _ignored(relative) or item.is_dir():
                continue
            suffix = relative.suffix.lower()
            if not item.is_file() or item.is_symlink():
                return {"error": "unsupported_file"}
            if suffix not in EXPORT_TEXT_SUFFIXES | EXPORT_ASSET_SUFFIXES and relative.name not in {"LICENSE", "NOTICE"}:
                return {"error": "unsupported_file"}
            data = item.read_bytes()
            total += len(data)
            if len(files) >= EXPORT_MAX_FILES or len(data) > EXPORT_MAX_FILE_BYTES or total > EXPORT_MAX_TOTAL_BYTES:
                return {"error": "too_large"}
            if suffix in EXPORT_ASSET_SUFFIXES:
                files.append({"path": relative.as_posix(), "encoding": "base64",
                              "content": base64.b64encode(data).decode("ascii")})
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"error": "unsupported_file"}
            files.append({"path": relative.as_posix(), "encoding": "utf8", "content": content})
    except OSError:
        return {"error": "unreadable"}
    return {"files": files}


def export_plugin_skill(*, provider: str, provider_root: Path, plugin_id: str, skill_id: str) -> dict:
    """Files of a skill one of the app's plugins brings, with the plugin's version.

    Only a plugin the app itself lists is read, and only its ``skills`` directory.
    """

    plugin = next((item for item in plugin_roots(provider, provider_root) if item["id"] == plugin_id), None)
    if plugin is None or plugin["root"] is None:
        return {"error": "not_found"}
    exported = export_skill(skills_root=plugin["root"] / "skills", skill_id=skill_id)
    return exported if "error" in exported else {**exported, "pluginVersion": plugin["version"]}
