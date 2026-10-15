"""What the hourly job of a computer starts: the text of its launchers.

A launcher is the file the OS schedule starts every hour: a shell script on a Mac; on Windows
a PowerShell script behind a VBScript that keeps its window hidden, with the command in a
JSON file next to it. Each starts the launch guard first (`sync_launch_guard.py`, a copy of
which lies next to the launcher) and then the client, or the installer of the client runtime
when no client can start and none can be put back.

Here are the pieces a launcher is put together from: the command lines, the preambles that
start the guard and do what it says, the copy of the guard, the two layouts the job of a
connection can have with the Keychain names of the second, and the sealing of the credential
on Windows. The job itself is written and registered by `sync_schedule.py`.

Every computer compares what is written here with what it has: one changed byte writes the
hourly job of every connected computer again. `tests/test_client_launcher_golden.py` holds
each file to the recorded one.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base and
the launch guard.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from sync_base import windows_system_tool
from sync_launch_guard import (GUARD_FILES_BUSY, GUARD_NOTHING_TO_RESTORE, GUARD_RESTORE_FAILED,
                               GUARD_RUN_IN_PROGRESS, LAUNCH_GUARD_FILE, LAUNCHER_EVENTS_FILE, read_busy,
                               remove_busy, replace_busy)
from sync_messages import text

# How long a self-check may take, for this client's own probes and for a launcher's guard.
CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS = 60
# How long a launcher's guard waits for a run that holds the client's lock before it leaves the
# client to that run.
LAUNCHER_LOCK_WAIT_SECONDS = 30

# Two layouts of the OS schedule.
#
# "legacy": every client installed before the command-line client. Its job keeps the name it was
# installed with. It is not renamed in place: the desktop application up to 0.4.3 finds a Mac's
# job by the old label only, and a client rolled back to an earlier release would create the
# old-named job next to the renamed one. These jobs end when the application takes the
# computer over.
# "terminal": a connection made by the command-line client. The job carries the product's name;
# on macOS the credential is the Keychain item the desktop application uses, so no credential
# file exists and either tool finds the connection the other made.
SCHEDULE_LAYOUTS = ("legacy", "terminal")
KEYCHAIN_SERVICE = "com.artificial-memory.agent-platform"
KEYCHAIN_ACCOUNT = "managed-client"
KEYCHAIN_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def schedule_layout(args: object) -> str:
    return "terminal" if getattr(args, "schedule_layout", "legacy") == "terminal" else "legacy"


def keychain_service(args: object) -> str:
    value = getattr(args, "keychain_service", None) or KEYCHAIN_SERVICE
    if not KEYCHAIN_SERVICE_NAME.fullmatch(value):
        raise SystemExit(text("keychain_service_invalid"))
    return value


def runtime_installer(python: str) -> Path | None:
    """The installer of the client runtime an interpreter belongs to, or None.

    The terminal command and the desktop application run the client on a downloaded, signed
    runtime: its interpreter and, next to it, an installer that fetches the approved release,
    verifies its signature and starts that release's own entry point. A launcher that finds no
    client it can start hands the run to this installer; it is no file of the client. An
    interpreter of any other kind has none, and nothing is guessed for it."""
    interpreter = Path(python)
    # A runtime keeps its interpreter under python/ (python/bin/python3, python/python.exe)
    # and its installer under bootstrap/.
    for directory in list(interpreter.parents)[1:3]:
        installer = directory / "bootstrap" / "companion_install.py"
        if interpreter.relative_to(directory).parts[0] == "python" and installer.is_file():
            return installer
    return None


def reinstall_command(python: str, args: argparse.Namespace, client_id: str, display_name: str,
                      providers: list[str]) -> list[str] | None:
    """What a launcher starts when the client cannot start and nothing can be restored: the
    runtime's installer, with what this connection was made with. None when there is no such
    installer, or when the apps were given places of their own, which the installer cannot be
    told: it would install into the default ones."""
    installer = runtime_installer(python)
    if installer is None:
        return None
    home = Path.home()
    defaults = {"codex_root": home / ".codex", "claude_root": home / ".claude", "claude_config": home / ".claude.json",
                "bin_dir": home / ".local" / "bin", "project_mappings": home / ".agent-platform/project-mappings.json"}
    for name, default in defaults.items():
        path = getattr(args, name, None)
        if path is not None and Path(path).resolve() != default.resolve():
            return None
    layout = schedule_layout(args)
    command = [
        python, "-E", "-s", str(installer), "--control-plane-url", args.control_plane_url, "--client-id", client_id,
        "--display-name", display_name, "--update-policy", args.update_policy,
        "--release-trust-file", str(args.release_trust_file.resolve()),
        "--release-cache", str(args.release_cache.resolve()),
        "--scheduler", "os", "--schedule-layout", layout,
    ]
    if layout == "terminal":
        command += ["--keychain-service", keychain_service(args)]
    for provider in providers:
        command += ["--provider", provider]
    return command


def macos_launcher(command: list[str], client_root: Path, read_token: str, reinstall: list[str] | None) -> str:
    """The script the hourly job of a Mac starts.

    It starts the launch guard, which checks the client and puts the previous one back when
    the installed one cannot start, and then the client. With ``reinstall`` (see
    reinstall_command) a client that cannot start and has no previous one is not started at
    all: the runtime's installer is, which installs the approved release from its verified
    bundle."""
    source = ("#!/bin/sh\n"
              "set -eu\n"
              + runtime_restore_shell(command[0], client_root, reinstall=reinstall is not None)
              + read_token
              + "export AGENT_PLATFORM_CLIENT_TOKEN\n")
    if reinstall is not None:
        source += ('if [ -n "$reinstall" ]; then\n'
                   "  echo 'agent-platform: no sync client can start; installing the approved release again' >&2\n"
                   f"  exec {' '.join(shlex.quote(part) for part in reinstall)}\n"
                   "fi\n")
    return source + f"exec {' '.join(shlex.quote(part) for part in command)}\n"


def launch_guard_arguments() -> str:
    """What every launcher hands its guard: this client's own limits. Numbers only, so the text
    of a launcher stays the same on every computer."""
    return f"--lock-wait {LAUNCHER_LOCK_WAIT_SECONDS:g} --self-check-limit {CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS:g}"


def write_launch_guard(client_root: Path) -> None:
    """Put the launch guard next to a launcher: a copy of this client's own file, byte for byte.

    A launcher starts the copy and not the file of the installed client, because the guard is
    what puts a damaged client right: it must not be one of the files an installation or a
    restore replaces. It changes when the launcher is written again, and only then."""
    client_root.mkdir(parents=True, exist_ok=True)
    source = read_busy(Path(__file__).with_name("sync_launch_guard.py"))
    descriptor, temporary = tempfile.mkstemp(prefix=f".{LAUNCH_GUARD_FILE}-", dir=client_root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        replace_busy(temporary, client_root / LAUNCH_GUARD_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)


def launch_guard_is_current(client_root: Path) -> bool:
    """Whether the guard next to the launcher is this client's. One that is missing, damaged or
    of another client makes the job not current, so the launcher and its guard are written again."""
    try:
        return (read_busy(client_root / LAUNCH_GUARD_FILE)
                == read_busy(Path(__file__).with_name("sync_launch_guard.py")))
    except OSError:
        return False


def remove_launch_guard_without_launcher(client_root: Path) -> None:
    """A guard is a file of a launcher. Where the OS schedule is not this client's (the desktop
    application starts the runs, or nothing does) and the launchers are gone, it goes too."""
    if not any((client_root / name).exists() for name in ("sync-client.sh", "sync-client.ps1")):
        remove_busy(client_root / LAUNCH_GUARD_FILE)


def runtime_restore_shell(python: str, client_root: Path, *, reinstall: bool = False) -> str:
    """POSIX launcher preamble: start the launch guard and do what it says.

    The guard (sync_launch_guard.py, a copy of which lies next to the launcher) checks the
    client under the run lock, ends a self-check that does not return, puts the previous client
    back and writes each outcome to launcher-events.jsonl for the next report. What is left to
    the shell is its answer:

    * 0: the client can start, as it was or restored;
    * nothing could be restored: with ``reinstall`` the preamble leaves `$reinstall` set, for
      the launcher to start the runtime's installer instead of the client; without, the client
      is started as it is and the person connects the computer again;
    * a run holds the lock: this start ends here, that run is the synchronization;
    * the restore did not finish (a held file, or a file that cannot be copied): nothing is
      started and the job ends with the guard's code, so the next start restores again;
    * anything else: the guard did not run. That is written down and the client is started
      unchecked: the run that follows reports it and writes the guard again."""
    python = shlex.quote(python)
    client = shlex.quote(str(client_root))
    events = shlex.quote(str(client_root / LAUNCHER_EVENTS_FILE))
    return (
        ("reinstall=\n" if reinstall else "")
        + "guard=0\n"
        f"{python} -E -s {client}/{LAUNCH_GUARD_FILE} {launch_guard_arguments()} || guard=$?\n"
        'case "$guard" in\n'
        "  0) ;;\n"
        f"  {GUARD_NOTHING_TO_RESTORE}) {'reinstall=1' if reinstall else ':'} ;;\n"
        f"  {GUARD_RUN_IN_PROGRESS}) exit 0 ;;\n"
        f'  {GUARD_RESTORE_FAILED}|{GUARD_FILES_BUSY}) exit "$guard" ;;\n'
        "  *)\n"
        "    printf '{\"event\":\"%s\",\"at\":\"%s\"}\\n' guard_failed "
        f"\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> {events}\n"
        "    echo 'agent-platform: the launch guard did not run; starting the sync client unchecked' >&2\n"
        "    ;;\n"
        "esac\n"
    )


def runtime_restore_powershell() -> str:
    """Windows launcher preamble with the same contract as runtime_restore_shell.

    Constant text: the interpreter is `$python`, the client directory `$clientRoot` and the
    arguments `$launch`, which the launcher sets from its own location and its arguments file.
    Nothing is written into the source of a script (see install_scheduled_task).

    `$reinstall` is left true when nothing could be restored and the arguments file names what
    installs the approved release again (reinstall_command); the launcher then starts that
    instead of the client.

    The guard is started inside a function: there `$ErrorActionPreference` can be 'Continue',
    which Windows PowerShell 5.1 needs when the output of a program is redirected, while the
    script around it stops at any error. The exit code is set beforehand to one the guard
    never returns, so an interpreter that cannot be started reads as a guard that did not run."""
    return (
        "$reinstall = $false\n"
        f"$guard = Join-Path $clientRoot {powershell_literal(LAUNCH_GUARD_FILE)}\n"
        f"$launcherEvents = Join-Path $clientRoot {powershell_literal(LAUNCHER_EVENTS_FILE)}\n"
        "function Write-AgentPlatformEvent([string]$name) {\n"
        "  $at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)\n"
        "  Add-Content -LiteralPath $launcherEvents -Encoding UTF8 "
        "-Value ('{\"event\":\"' + $name + '\",\"at\":\"' + $at + '\"}')\n"
        "}\n"
        "function Invoke-AgentPlatformGuard {\n"
        "  $ErrorActionPreference = 'Continue'\n"
        "  $global:LASTEXITCODE = 70\n"
        f"  & $python -E -s $guard {launch_guard_arguments()} *> $null\n"
        "  return $LASTEXITCODE\n"
        "}\n"
        "$guardExit = Invoke-AgentPlatformGuard\n"
        f"if ($guardExit -eq {GUARD_RUN_IN_PROGRESS}) {{ exit 0 }}\n"
        f"if (($guardExit -eq {GUARD_RESTORE_FAILED}) -or ($guardExit -eq {GUARD_FILES_BUSY})) {{ exit $guardExit }}\n"
        f"if ($guardExit -eq {GUARD_NOTHING_TO_RESTORE}) {{\n"
        "  $reinstall = ($launch.reinstallCommandLine -is [string]) -and "
        "(-not [string]::IsNullOrWhiteSpace($launch.reinstallCommandLine))\n"
        "} elseif ($guardExit -ne 0) {\n"
        "  Write-AgentPlatformEvent 'guard_failed'\n"
        "  [Console]::Error.WriteLine('agent-platform: the launch guard did not run; "
        "starting the sync client unchecked')\n"
        "}\n"
    )


def powershell_literal(value: str) -> str:
    """A constant of this file as a PowerShell single-quoted string. Only for names this file
    defines itself: PowerShell also closes such a string at ‘ ’ ‚ ‛, so a value that came from
    a person or from the service must never be written into a script with this."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError("Only this client's own file names are written into a script")
    return "'" + value + "'"


WINDOWS_ARGUMENTS_FILE = "sync-client.arguments.json"


def vbscript_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _scheduled_command(script: Path, args: argparse.Namespace, client_id: str,
                       display_name: str, providers: list[str]) -> list[str]:
    command = [
        sys.executable, str(script), "--client-id", client_id,
        "--display-name", display_name, "--update-policy", args.update_policy,
    ]
    command.extend([
        "--control-plane-url", args.control_plane_url,
        "--release-trust-file", str(args.release_trust_file.resolve()),
        "--release-cache", str(args.release_cache.resolve()),
    ])
    for name in ("codex_root", "claude_root", "claude_config", "bin_dir", "project_mappings"):
        path = getattr(args, name, None)
        if path is not None:
            command.extend(["--" + name.replace("_", "-"), str(path.resolve())])
    for provider in providers:
        command.extend(["--provider", provider])
    if schedule_layout(args) == "terminal":
        command.extend(["--schedule-layout", "terminal", "--keychain-service", keychain_service(args)])
    return command


def protect_windows_token(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is unavailable on this platform")
    environment = os.environ.copy()
    environment.pop("PSModulePath", None)
    environment["AGENT_PLATFORM_CLIENT_SECRET"] = value
    code = (
        '$ErrorActionPreference="Stop"; '
        '$module=Join-Path $PSHOME "Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1"; '
        'Import-Module $module -Force; '
        '$s=ConvertTo-SecureString $env:AGENT_PLATFORM_CLIENT_SECRET -AsPlainText -Force; '
        '[Console]::Write(($s | ConvertFrom-SecureString))'
    )
    executable = windows_system_tool("WindowsPowerShell", "v1.0", "powershell.exe")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", code],
        env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(text("windows_protection_failed"))
    return result.stdout.strip()
