#!/usr/bin/env python3
"""Install the platform's own part of Cursor: the work-session hooks.

Cursor follows a computer's release like Kimi Code and OpenCode. Its MCP entries are the
managed client's: the channels of Cursor's own agent (tools/install_app_agent_channels.py) and
the managed servers (tools/install_managed_mcp_servers.py) in `~/.cursor/mcp.json`. This
adapter places the shared work-session script under `~/.cursor/agent-platform/scripts/` and
adds one entry per work-session event to `~/.cursor/hooks.json`
(https://cursor.com/docs/agent/hooks: `{"version": 1, "hooks": {event: [{command}]}}`).

An entry is the platform's when its command names a script under
`~/.cursor/agent-platform/scripts/`; every other hook, the person's own and another tool's,
stays exactly as it is. A run that changes nothing leaves the file untouched. Cursor rereads
hooks.json when it is saved, so the hooks need no restart.

    python adapters/cursor/install.py --platform-root <release> [--python <interpreter>]
    python adapters/cursor/install.py --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# The moments of a conversation the work-session hook reports (adapters/common/work_session_hook.py).
HOOK_EVENTS = ("sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd")
HOOK_SCRIPT = "work_session_hook.py"
RECEIPT = "install.json"


def default_cursor_root() -> Path:
    # Cursor keeps its user configuration in ~/.cursor on macOS, Linux and Windows alike.
    return Path.home() / ".cursor"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--cursor-root", type=Path, default=default_cursor_root())
    # The managed client's interpreter: the hook loads the agent's identity, which needs the
    # client's dependencies. Absolute, since Cursor promises no PATH to a hook.
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--release-id")
    parser.add_argument("--agent-platform-sha")
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args(argv)


def runtime_root(cursor_root: Path) -> Path:
    return cursor_root / "agent-platform"


def scripts_root(cursor_root: Path) -> Path:
    return runtime_root(cursor_root) / "scripts"


# How Cursor 3.24 runs a hook command (its extension host, ExtHostShellExec.$executeHookDirect;
# the documentation says only "a shell string"): with the system shell on macOS and Linux, and
# with PowerShell on Windows (pwsh, else Windows PowerShell, -NoProfile -NonInteractive
# -ExecutionPolicy Bypass) as `Get-Content -LiteralPath <input> -Raw | & { $input | <command> }`.
# So a Windows command is a PowerShell call: `& 'path' 'path' args`, a quote doubled inside.
POWERSHELL_WORD = re.compile(r"'((?:[^']|'')*)'|\"([^\"]*)\"|(\S+)")


def on_windows(windows: bool | None) -> bool:
    return os.name == "nt" if windows is None else windows


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell_call(words: list[str]) -> str:
    """A PowerShell call of a program with its arguments, each word a literal string."""
    return "& " + " ".join(powershell_literal(word) if index < 2 else word for index, word in enumerate(words))


def script_path(cursor_root: Path, windows: bool | None = None) -> str:
    if on_windows(windows):
        return str(PureWindowsPath(str(cursor_root)) / "agent-platform" / "scripts" / HOOK_SCRIPT)
    return str(scripts_root(cursor_root) / HOOK_SCRIPT)


def hook_command(cursor_root: Path, python: str, windows: bool | None = None) -> str:
    """The command Cursor runs for each work-session event, in the shell Cursor uses here."""
    script = script_path(cursor_root, windows)
    if on_windows(windows):
        return powershell_call([python, script, "--provider", "cursor"])
    return shlex.join([python, script, "--provider", "cursor"])


def command_words(command: str, windows: bool) -> list[str]:
    """The words of a hook command as its shell reads them; [] when it cannot be read."""
    if windows:
        words = [single.replace("''", "'") if single is not None else double if double is not None else bare
                 for single, double, bare in POWERSHELL_WORD.findall(command)]
        return words[1:] if words[:1] == ["&"] else words
    try:
        return shlex.split(command)
    except ValueError:
        return []


def is_ours(entry: Any, cursor_root: Path, windows: bool | None = None) -> bool:
    """An entry whose command runs a script the platform put under ~/.cursor/agent-platform/scripts,
    recognised on the command as its shell parses it (a home with a quote in its name included)."""
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str):
        return False
    windows = on_windows(windows)
    pure = PureWindowsPath if windows else PurePosixPath
    ours = pure(script_path(cursor_root, windows)).parent

    def same(left, right) -> bool:
        return str(left).casefold() == str(right).casefold() if windows else left == right

    return any(same(pure(word).parent, ours) for word in command_words(command, windows))


def read_hooks(path: Path) -> tuple[dict, str | None]:
    """The hooks file and its text; refuses anything that is not Cursor's format, so a file the
    platform does not understand is never rewritten."""
    if not path.is_file():
        return {"version": 1, "hooks": {}}, None
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Cursor hooks file is not valid JSON, left as it is: {path}: {error}") from error
    if not isinstance(value, dict) or value.get("version", 1) != 1 or not isinstance(value.get("hooks", {}), dict):
        raise SystemExit(f"Cursor hooks file is not version 1 with a hooks object, left as it is: {path}")
    hooks = value.setdefault("hooks", {})
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise SystemExit(f"Cursor hooks event {event} is not a list, left as it is: {path}")
    value.setdefault("version", 1)
    return value, text


def with_our_hooks(config: dict, cursor_root: Path, command: str | None) -> dict:
    """The config with every entry of ours taken out and, unless `command` is None, one entry
    per work-session event added at the end. An event left with no entry by our removal goes;
    an event the person keeps empty stays."""
    hooks = config["hooks"]
    for event in list(hooks):
        kept = [entry for entry in hooks[event] if not is_ours(entry, cursor_root)]
        if len(kept) != len(hooks[event]) and not kept and (command is None or event not in HOOK_EVENTS):
            del hooks[event]
        else:
            hooks[event] = kept
    if command is not None:
        for event in HOOK_EVENTS:
            hooks.setdefault(event, []).append({"command": command})
    return config


def write_text(path: Path, content: str) -> None:
    """Replace a text file in one step (UTF-8, newlines as written), keeping its permissions."""
    write_bytes(path, content.encode("utf-8"))


def place_hooks(cursor_root: Path, command: str | None) -> bool:
    """Bring hooks.json to the platform's entries (none for `command` None). True when written."""
    path = cursor_root / "hooks.json"
    config, before = read_hooks(path)
    if before is None and command is None:
        return False
    rendered = json.dumps(with_our_hooks(config, cursor_root, command), ensure_ascii=False, indent=2) + "\n"
    if before is not None and json.loads(before) == json.loads(rendered):
        return False
    write_text(path, rendered)
    return True


def write_bytes(path: Path, content: bytes) -> None:
    """Replace a file in one step with exactly these bytes: no newline is translated, so the
    copy compares equal to its source on the next run on every system."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.is_file() else 0o644
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, prefix=f".{path.name}-") as handle:
        handle.write(content)
        staged = Path(handle.name)
    try:
        staged.chmod(mode)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def install_scripts(platform_root: Path, cursor_root: Path) -> None:
    source = platform_root / "adapters" / "common" / HOOK_SCRIPT
    if not source.is_file():
        raise SystemExit(f"invalid agent-platform root: {platform_root}")
    target = scripts_root(cursor_root) / HOOK_SCRIPT
    content = source.read_bytes()
    if not target.is_file() or target.read_bytes() != content:
        write_bytes(target, content)


def write_receipt(cursor_root: Path, release_id: str | None, sha: str | None) -> None:
    path = runtime_root(cursor_root) / RECEIPT
    receipt = {"version": 1, "release_id": release_id, "agent_platform_sha": sha, "skills": [],
               "hooks": list(HOOK_EVENTS)}
    rendered = json.dumps(receipt, indent=2) + "\n"
    if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
        write_text(path, rendered)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cursor_root = args.cursor_root.expanduser()
    if args.uninstall:
        changed = place_hooks(cursor_root, None)
        if runtime_root(cursor_root).is_dir():
            shutil.rmtree(runtime_root(cursor_root))
        print(json.dumps({"ok": True, "cursorRoot": str(cursor_root), "hooks": "removed" if changed else "current",
                          "restartRequired": False}))
        return 0
    if not cursor_root.is_dir():
        raise SystemExit(f"Cursor is not on this computer: {cursor_root}")
    python = str(Path(args.python).expanduser())
    if not Path(python).is_absolute():
        raise SystemExit("--python must be an absolute path: Cursor runs a hook without a promised PATH")
    platform_root = args.platform_root.expanduser().resolve()
    install_scripts(platform_root, cursor_root)
    changed = place_hooks(cursor_root, hook_command(cursor_root, python))
    write_receipt(cursor_root, args.release_id, args.agent_platform_sha)
    # Read back: exactly one entry of ours per event, with this interpreter.
    config, _ = read_hooks(cursor_root / "hooks.json")
    for event in HOOK_EVENTS:
        ours = [entry for entry in config["hooks"].get(event, []) if is_ours(entry, cursor_root)]
        if [entry.get("command") for entry in ours] != [hook_command(cursor_root, python)]:
            raise SystemExit(f"Cursor hook {event} is not the platform's after installation")
    print(json.dumps({"ok": True, "cursorRoot": str(cursor_root), "hooks": "changed" if changed else "current",
                      "restartRequired": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
