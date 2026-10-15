"""Hooks and mods one app has on this computer: where each comes from and when it runs.

A hook is a command the app runs by itself on an event; a mod is a plugin whose code runs inside
Claude Code. Kimi Code keeps hooks as ``[[hooks]]`` tables in its config.toml; OpenCode has no
command hooks, its plugins are code inside OpenCode and are listed like mods. Both act with the user's permissions, so Console shows them. The report
says the event, the tool filter, the kind of handler and the owner (the platform, the user, or a
plugin). The command, its arguments, a URL or a prompt never leave the computer.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MAX_ENTRIES = 200
EVENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,59}$")
MATCHER = re.compile(r"^[A-Za-z0-9_|*.:-]{1,80}$")
HANDLER_TYPES = frozenset({"command", "http", "prompt", "agent", "mcp_tool"})


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def managed_settings_directory() -> Path:
    """Where Claude Code reads administrator-level settings files on this operating system."""

    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if sys.platform.startswith("win"):
        return Path(r"C:\Program Files\ClaudeCode")
    return Path("/etc/claude-code")


def managed_mods_only(directory: Path) -> bool:
    """Whether the administrator settings files allow only the organization's mods.

    Claude Code takes ``allowManagedModsOnly`` from managed settings alone (the option of its
    built-in guard). Only the files are read here: a policy delivered by MDM or by the provider's
    admin console is not visible to this check, so ``False`` means "not found in the files".
    """

    files = [directory / "managed-settings.json"]
    drop_ins = directory / "managed-settings.d"
    try:
        files += sorted(path for path in drop_ins.iterdir() if path.suffix == ".json" and not path.name.startswith("."))
    except OSError:
        pass
    allowed = False
    for path in files:  # later files win, as Claude Code merges them
        configs = _read_json(path).get("pluginConfigs")
        options = configs.get("cc-plugin-sec-default@builtin", {}).get("options") if isinstance(configs, dict) else None
        if isinstance(options, dict) and isinstance(options.get("allowManagedModsOnly"), bool):
            allowed = options["allowManagedModsOnly"]
    return allowed


def _jsonc(text: str) -> dict:
    """JSON with // and /* */ comments (OpenCode's opencode.jsonc); comment marks inside strings stay."""

    result, index, in_string = [], 0, False
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if character == "\\" and index + 1 < len(text):
                result.append(text[index + 1])
                index += 1
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
            result.append(character)
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                break
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        else:
            result.append(character)
        index += 1
    try:
        value = json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(result)))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _kimi_hooks(provider_root: Path, owner) -> list[dict]:
    """Kimi Code: ``[[hooks]]`` tables in config.toml, each with an event and a command."""

    try:
        import tomllib
        tables = tomllib.loads((provider_root / "config.toml").read_text(encoding="utf-8")).get("hooks")
    except (ImportError, OSError, ValueError):
        return []
    counted: dict[tuple, int] = {}
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict) or not isinstance(table.get("event"), str) or not EVENT.fullmatch(table["event"]):
            continue
        matcher = table.get("matcher")
        key = (owner(table), table["event"], matcher if isinstance(matcher, str) and MATCHER.fullmatch(matcher) else None, "command")
        counted[key] = counted.get(key, 0) + 1
    return [{"source": key[0], "plugin": None, "event": key[1], "matcher": key[2], "type": key[3], "count": count}
            for key, count in sorted(counted.items(), key=lambda item: tuple(map(str, item[0])))]


def _opencode_plugins(provider_root: Path) -> list[str]:
    """OpenCode has no command hooks: its plugins are code that runs inside OpenCode, like mods.

    Local plugin files (plugins/ or plugin/) and packages named in opencode.json(c) "plugin".
    The platform's own plugin is not listed.
    """

    names: set[str] = set()
    for directory in ("plugins", "plugin"):
        try:
            names |= {path.name for path in (provider_root / directory).iterdir()
                      if path.is_file() and path.suffix in {".js", ".ts", ".mjs", ".cjs"} and not path.name.startswith(".")}
        except OSError:
            pass
    for name in ("opencode.jsonc", "opencode.json"):
        try:
            declared = _jsonc((provider_root / name).read_text(encoding="utf-8")).get("plugin")
        except OSError:
            continue
        names |= {item.rsplit("/", 1)[-1] for item in declared if isinstance(item, str)} if isinstance(declared, list) else set()
    return sorted(name for name in names if name != "agent-platform.js" and 0 < len(name) <= 200
                  and "\\" not in name and not any(ord(character) < 32 for character in name))[:50]


def _entries(hooks: object, classify) -> dict[tuple, int]:
    counted: dict[tuple, int] = {}
    for event, groups in hooks.items() if isinstance(hooks, dict) else []:
        if not isinstance(event, str) or not EVENT.fullmatch(event) or not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            matcher = matcher if isinstance(matcher, str) and MATCHER.fullmatch(matcher) else None
            for handler in group.get("hooks") if isinstance(group.get("hooks"), list) else []:
                if not isinstance(handler, dict):
                    continue
                kind = handler.get("type") if handler.get("type") in HANDLER_TYPES else "other"
                key = (classify(handler), event, matcher, kind)
                counted[key] = counted.get(key, 0) + 1
    return counted


def hook_inventory(provider: str, *, provider_root: Path, platform_skills: set[str], plugins: list[dict],
                   managed_directory: Path | None = None) -> dict:
    """``plugins`` are local_skills.plugin_roots entries ({id, enabled, version, root})."""

    def owner(handler: dict) -> str:
        command = handler.get("command")
        text = command if isinstance(command, str) else ""
        ours = "/agent-platform/" in text or any(f"/skills/{name}/" in text for name in platform_skills)
        return "platform" if ours else "user"

    if provider == "kimi":
        return {"disabled": False, "hooks": _kimi_hooks(provider_root, owner)[:MAX_ENTRIES], "mods": []}
    if provider == "opencode":
        return {"disabled": False, "hooks": [],
                "mods": [{"plugin": name, "enabled": True} for name in _opencode_plugins(provider_root)]}
    settings = _read_json(provider_root / "settings.json") if provider == "claude" else {}
    source = settings if provider == "claude" else _read_json(provider_root / "hooks.json")
    hooks = [{"source": key[0], "plugin": None, "event": key[1], "matcher": key[2], "type": key[3], "count": count}
             for key, count in sorted(_entries(source.get("hooks"), owner).items(), key=lambda item: tuple(map(str, item[0])))]
    mods = []
    for plugin in plugins:
        if plugin.get("root") is None:
            continue
        declared = _read_json(Path(plugin["root"]) / "hooks" / "hooks.json")
        hooks += [{"source": "plugin", "plugin": plugin["id"], "event": key[1], "matcher": key[2], "type": key[3], "count": count}
                  for key, count in sorted(_entries(declared.get("hooks"), lambda _handler: "plugin").items(),
                                           key=lambda item: tuple(map(str, item[0])))]
        # A plugin whose hooks.json names a module is a mod: code that runs inside Claude Code.
        if isinstance(declared.get("modules"), list) and declared["modules"]:
            mods.append({"plugin": plugin["id"], "enabled": bool(plugin.get("enabled"))})
    return {"disabled": settings.get("disableAllHooks") is True, "hooks": hooks[:MAX_ENTRIES], "mods": mods[:50],
            # Mods exist in Claude Code only.
            **({"managedModsOnly": managed_mods_only(managed_directory or managed_settings_directory())}
               if provider == "claude" else {})}
