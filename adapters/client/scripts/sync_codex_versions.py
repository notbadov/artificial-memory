"""Which Codex versions this computer has, and whether every one of them accepts "writes".

Every Codex on a computer reads the same `~/.codex/config.toml`, and a Codex that meets a value
it does not know refuses the whole file and does not start (Codex 0.137 with
`default_tools_approval_mode = "writes"`, 2026-10-15). An organization's server may ask for
"writes": the tools that only read run without asking, every other one asks. Codex learned that
value in 0.144.0. So the client looks for every Codex here and asks each its version; when one is
older, or one does not say, such a server is written as "prompt" (every tool asks), and the
person is told which Codex to update. Where no Codex is found the value stays: the file will be
read by a Codex installed later, which knows it.

The places looked in: `codex` on the PATH of this run, the Codex inside the ChatGPT app on macOS,
and the usual places of Homebrew, npm and nvm on macOS and Linux. The hourly job does not have
the PATH of a terminal (on macOS it has /usr/bin:/bin:/usr/sbin:/sbin), so a Codex installed with
Homebrew is found only by its place. On Windows `codex` is looked for on the PATH (`codex.cmd`
of an npm install among them).

The record written here is read by the terminal client (`artificial-memory status` and
`artificial-memory sync`) under the same file name.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from sync_base import client_platform, iso_now, write_private_json

# The rule, in one place: `default_tools_approval_mode = "writes"` needs Codex 0.144.0 or later.
# Before it the enum `AppToolApproval` (codex-rs/config/src/mcp_types.rs) was auto | prompt | approve.
WRITES_APPROVAL_SINCE = (0, 144, 0)
CODEX_VERSION_TIMEOUT_SECONDS = 10
# Under ~/.agent-platform/client; the terminal client reads the same name.
CODEX_APPROVAL_RECORD = "codex-approval.json"
# `codex-cli 0.162.1`, `codex-cli 0.162.0-alpha.17.2`.
CODEX_VERSION = re.compile(r"\b(\d{1,4})\.(\d{1,4})\.(\d{1,6})(-[0-9A-Za-z.-]{1,64})?\b")

# Runs one Codex with `--version` and returns what it printed; raises OSError or
# subprocess.SubprocessError when it cannot be run. Tests give their own.
VersionRunner = Callable[[Path], str]


def run_codex_version(binary: Path) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run([str(binary), "--version"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=CODEX_VERSION_TIMEOUT_SECONDS, check=False,
                               stdin=subprocess.DEVNULL, creationflags=flags)
    return completed.stdout + "\n" + completed.stderr


def codex_places(home: Path, system: str, which: Callable[[str], str | None] = shutil.which) -> list[Path]:
    """Where a Codex program may be on this computer."""
    places: list[Path] = []
    on_path = which("codex")
    if on_path:
        places.append(Path(on_path))
    if system == "macos":
        for applications in (Path("/Applications"), home / "Applications"):
            places.append(applications / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    if system in {"macos", "linux"}:
        places += [Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), Path("/usr/bin/codex"),
                   home / ".local/bin/codex", home / ".npm-global/bin/codex", home / ".volta/bin/codex"]
        places += sorted((home / ".nvm/versions/node").glob("*/bin/codex"))
    return places


def codex_binaries(home: Path, system: str, which: Callable[[str], str | None] = shutil.which) -> list[Path]:
    """Every Codex program found on this computer, each once."""
    found: dict[Path, Path] = {}
    for place in codex_places(home, system, which):
        try:
            if place.is_file():
                found.setdefault(place.resolve(), place)
        except OSError:
            continue
    return list(found.values())


def parse_codex_version(output: str) -> tuple[int, int, int, bool] | None:
    """(major, minor, patch, released): a pre-release of a version comes before the version."""
    match = CODEX_VERSION.search(output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4) is None


def check_codex_versions(binaries: list[Path], run: VersionRunner = run_codex_version) -> dict:
    """{"found": how many, "writes": whether every one accepts it, "tooOld": the oldest version
    that does not (None when none is too old), "unreadable": how many did not say their version}."""
    too_old: list[tuple[tuple[int, int, int, bool], str]] = []
    unreadable = 0
    for binary in binaries:
        try:
            output = run(binary)
        except (OSError, subprocess.SubprocessError, ValueError):
            output = ""
        version = parse_codex_version(output)
        if version is None:
            unreadable += 1
        elif version < (*WRITES_APPROVAL_SINCE, True):
            too_old.append((version, CODEX_VERSION.search(output).group(0)))
    return {"found": len(binaries), "writes": not too_old and unreadable == 0,
            "tooOld": min(too_old)[1] if too_old else None, "unreadable": unreadable}


def managed_servers_for_codex(servers: list[dict], providers: list[str], home: Path | None = None, *,
                              system: str | None = None, run: VersionRunner = run_codex_version,
                              which: Callable[[str], str | None] = shutil.which) -> list[dict]:
    """The managed servers as the installer is to write them: one that asks for "writes" for
    Codex is given "prompt" when a Codex here does not accept "writes". `approvalMode` is written
    into the config of Codex alone, so the other apps lose nothing.

    The record the terminal client shows names those servers and the oldest Codex that is too
    old (None when a Codex only did not say its version); it is removed when no server is held
    back. Codex is asked its version only when a server asks
    for "writes"."""
    home = home or Path.home()
    asking = [server for server in servers
              if server.get("approvalMode") == "writes" and "codex" in (server.get("targetProviders") or [])]
    held: list[dict] = []
    result = None
    if "codex" in providers and asking:
        result = check_codex_versions(codex_binaries(home, system or client_platform(), which), run)
        if not result["writes"]:
            held = asking
    record = home / ".agent-platform/client" / CODEX_APPROVAL_RECORD
    if held:
        write_private_json(record, {
            "version": 1, "checkedAt": iso_now(), "oldestCodex": result["tooOld"],
            "unreadableCodex": result["unreadable"],
            "serversAsking": sorted(str(server.get("displayName") or server.get("configKey")) for server in held),
            "writesSince": ".".join(str(part) for part in WRITES_APPROVAL_SINCE),
        })
    else:
        record.unlink(missing_ok=True)
    return [{**server, "approvalMode": "prompt"} if any(server is item for item in held) else server
            for server in servers]
