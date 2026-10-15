"""The hourly job of a computer: its names, whether it is the one this client writes, and what
is reported about it.

A Mac has a launchd job and Windows a Scheduled Task, under one of two names: the one every
client had before the terminal client, and the product's own for a connection made in the
terminal. `install_scheduled_task` writes the launcher files (`sync_launchers.py` has their
text) and registers the job; its receipt holds a fingerprint of everything the job is made
from, and `scheduled_task_is_current` compares it on every run, so a job is written again
only when something it is made from has changed.

Who starts the runs is one of three: the OS, the desktop application or nobody (`--scheduler`).
`ensure_scheduled_task` does what that choice means and answers with the report of the
schedule, which carries the receipt of the last run (`last-run.json`, written by the entry
point and read here).

Removing the job that started the very run that removes it would end the run, so such a
command waits in `_AT_EXIT_COMMANDS` until the entry point has written its receipt.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the launch guard and the launchers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sync_base import (_private_write_text, _read_local_json, _write_json_atomic, client_platform, iso_now,
                       required_client_token, windows_system_tool)
from sync_launch_guard import LAUNCH_GUARD_FILE, replace_busy
from sync_launchers import (KEYCHAIN_ACCOUNT, WINDOWS_ARGUMENTS_FILE, _scheduled_command, keychain_service,
                            launch_guard_is_current, macos_launcher, powershell_literal, protect_windows_token,
                            reinstall_command, remove_launch_guard_without_launcher, runtime_restore_powershell,
                            schedule_layout, vbscript_literal, write_launch_guard)
from sync_messages import text

SCHEDULED_TASK_SCHEMA_VERSION = 1
WINDOWS_LAUNCHER_REVISION = 9
MACOS_LAUNCHER_REVISION = 6
LOCAL_RUN_RECEIPT_VERSION = 1


def scheduled_task_name(client_id: str, layout: str = "legacy") -> str:
    if layout == "terminal":
        return f"Artificial Memory Sync ({client_id})"
    return f"Agent Platform Sync ({client_id})"


def scheduled_task_receipt_path(client_root: Path | None = None) -> Path:
    return (client_root or (Path.home() / ".agent-platform" / "client")) / "scheduled-task.json"


def scheduled_task_fingerprint(
    script: Path,
    args: argparse.Namespace,
    client_id: str,
    display_name: str,
    providers: list[str],
) -> str:
    value = {
        "version": SCHEDULED_TASK_SCHEMA_VERSION,
        "launcherRevision": WINDOWS_LAUNCHER_REVISION if client_platform() == "windows" else MACOS_LAUNCHER_REVISION,
        # The launcher starts this interpreter by its path. The command-line client runs on a
        # downloaded runtime whose directory changes with every runtime release, so a launcher
        # written for the previous one has to be written again.
        "python": sys.executable,
        "layout": schedule_layout(args),
        "keychainService": keychain_service(args) if schedule_layout(args) == "terminal" else None,
        "script": str(script.resolve()),
        "clientId": client_id,
        "displayName": display_name,
        "providers": providers,
        "updatePolicy": args.update_policy,
        "controlPlaneUrl": getattr(args, "control_plane_url", None),
        "releaseTrustFile": str(getattr(args, "release_trust_file", None)),
        "releaseCache": str(getattr(args, "release_cache", None)),
        "installationPaths": {name: str(getattr(args, name, None)) for name in
                              ("codex_root", "claude_root", "claude_config", "bin_dir", "project_mappings")},
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def scheduled_task_is_current(
    script: Path,
    args: argparse.Namespace,
    client_id: str,
    display_name: str,
    providers: list[str],
    client_root: Path | None = None,
) -> bool:
    receipt_path = scheduled_task_receipt_path(client_root)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    # The launcher starts the guard that lies next to it before anything else.
    if not launch_guard_is_current(client_root or Path.home() / ".agent-platform" / "client"):
        return False
    expected = scheduled_task_fingerprint(script, args, client_id, display_name, providers)
    platform_name = client_platform()
    expected_launcher = "wscript.exe" if platform_name == "windows" else "launchd" if platform_name == "macos" else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("version") != SCHEDULED_TASK_SCHEMA_VERSION
        or receipt.get("clientId") != client_id
        or receipt.get("fingerprint") != expected
        or receipt.get("launcher") != expected_launcher
    ):
        return False
    layout = schedule_layout(args)
    if platform_name == "macos":
        label = launch_agent_label(client_id, layout)
        plist = launch_agent_path(client_id, layout)
        try:
            source = plist.read_text(encoding="utf-8")
        except OSError:
            return False
        if (plist.is_symlink() or f"<string>{label}</string>" not in source
                or "AGENT_PLATFORM_CLIENT_TOKEN" in source or "--client-token" in source):
            return False
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        return result.returncode == 0
    if platform_name != "windows":
        return False
    # The files the task starts. They are compared by being there: their text is put together
    # only where it is written (install_scheduled_task), and everything it is made from is in
    # the fingerprint above. One that is gone (a program that guards the computer put a script
    # away) is what the fingerprint cannot see, and the task would fail every hour.
    launcher_files = ["sync-client.vbs"]
    if getattr(args, "control_plane_url", None):
        launcher_files += ["sync-client.ps1", WINDOWS_ARGUMENTS_FILE, "credential.dpapi"]
    launcher_root = client_root or Path.home() / ".agent-platform" / "client"
    if not all((launcher_root / name).is_file() for name in launcher_files):
        return False
    # schtasks answers in the console's code page, not in UTF-8. Only ASCII words are looked for
    # below, and they read the same in both; a letter that does not decode is replaced. The
    # answer names UTF-16 in its first line: were it ever written so, each of these letters
    # would be followed by a zero byte, which is taken out.
    result = subprocess.run(
        [windows_system_tool("schtasks.exe"), "/Query", "/TN", scheduled_task_name(client_id, layout), "/XML"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    task_xml = result.stdout.replace("\x00", "").lower()
    return (
        result.returncode == 0
        and "wscript.exe" in task_xml
        and "sync-client.vbs" in task_xml
        and "//b" in task_xml
        and "//nologo" in task_xml
    )


def scheduled_task_report(
    script: Path,
    args: argparse.Namespace,
    client_id: str,
    display_name: str,
    providers: list[str],
    client_root: Path | None = None,
) -> dict | None:
    platform_name = client_platform()
    if platform_name not in ("windows", "macos"):
        return None
    current = scheduled_task_is_current(script, args, client_id, display_name, providers, client_root)
    report = {
        "status": "installed" if current else "failed",
        "schemaVersion": SCHEDULED_TASK_SCHEMA_VERSION if current else None,
        "launcher": ("wscript.exe" if platform_name == "windows" else "launchd") if current else None,
        "error": None if current else text("background_check_unconfirmed"),
    }
    last_run = read_local_run_receipt(client_root or (Path.home() / ".agent-platform" / "client"), client_id)
    if last_run is not None:
        report["lastRun"] = last_run
    return report


def launch_agent_label(client_id: str, layout: str = "legacy") -> str:
    suffix = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
    if layout == "terminal":
        return f"com.artificial-memory.sync.{suffix}"
    return f"ru.notbadov.agent-platform-sync.{suffix}"


def launch_agent_path(client_id: str, layout: str = "legacy") -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (launch_agent_label(client_id, layout) + ".plist")


# What has to happen after everything else in this process: unloading the launchd job, or
# deleting the Scheduled Task, that started this very run ends the run.
_AT_EXIT_COMMANDS: list[list[str]] = []


def run_deferred_commands() -> None:
    while _AT_EXIT_COMMANDS:
        subprocess.run(_AT_EXIT_COMMANDS.pop(0), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def retire_legacy_job(client_id: str) -> bool:
    """A terminal connection keeps one job, under the product's name. A job with the earlier name
    for the same computer is removed, on every run, so an interrupted run cannot leave two.
    True when one was found."""
    platform_name = client_platform()
    if platform_name == "macos":
        label = launch_agent_label(client_id)
        plist = launch_agent_path(client_id)
        if not plist.is_file() and not plist.is_symlink():
            return False
        plist.unlink()
        bootout = ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"]
        if os.environ.get("XPC_SERVICE_NAME") == label:
            _AT_EXIT_COMMANDS.append(bootout)
        else:
            subprocess.run(bootout, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return True
    if platform_name == "windows":
        name = scheduled_task_name(client_id)
        found = subprocess.run([windows_system_tool("schtasks.exe"), "/Query", "/TN", name], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if found.returncode != 0:
            return False
        _AT_EXIT_COMMANDS.append([windows_system_tool("schtasks.exe"), "/Delete", "/TN", name, "/F"])
        return True
    return False


def record_schedule_owner(client_id: str, scheduler: str, client_root: Path | None = None) -> None:
    """Who starts the runs of this computer's connection: "companion" (the desktop application),
    "os" (the hourly job) or "manual". The command-line client reads it to know that the application
    has taken a connection over: on a Mac the two share one Keychain item, so nothing else on
    the computer says whose the connection is now."""
    path = (client_root or (Path.home() / ".agent-platform" / "client")) / "schedule-owner.json"
    try:
        _write_json_atomic(path, {"version": 1, "clientId": client_id, "scheduler": scheduler, "at": iso_now()})
    except OSError:
        pass


def retire_terminal_schedule(client_id: str, client_root: Path | None = None) -> bool:
    """The desktop application starts this run, so it owns the schedule now. The hourly job of a
    terminal connection for the same computer ends here: the two share one Keychain item on a
    Mac, and the application of any version reaches this line on its first run. True when it
    ended one."""
    if client_platform() != "macos":
        # On Windows the application disables and deletes the task itself and puts it back when
        # its takeover fails.
        return False
    client_root = client_root or (Path.home() / ".agent-platform" / "client")
    label = launch_agent_label(client_id, "terminal")
    plist = launch_agent_path(client_id, "terminal")
    receipt = _read_local_json(scheduled_task_receipt_path(client_root))
    # The label is derived from the computer's id, so only this connection's job is found; the
    # command-line client's own record (terminal.json) is its to keep or remove.
    if not plist.is_file() and receipt.get("taskName") != label:
        return False
    plist.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if receipt.get("taskName") == label:
        scheduled_task_receipt_path(client_root).unlink(missing_ok=True)
        (client_root / "sync-client.sh").unlink(missing_ok=True)
        (client_root / LAUNCH_GUARD_FILE).unlink(missing_ok=True)
    return True


def install_scheduled_task(script: Path, args: argparse.Namespace, client_id: str, display_name: str,
                           providers: list[str], client_root: Path | None = None) -> dict | None:
    platform_name = client_platform()
    if platform_name not in ("windows", "macos"):
        return None
    command = _scheduled_command(script, args, client_id, display_name, providers)
    rendered = subprocess.list2cmdline(command)
    task_command = rendered
    client_root = client_root or (Path.home() / ".agent-platform" / "client")
    client_root.mkdir(parents=True, exist_ok=True)
    # Before the launcher that starts it, so a launcher never names a guard that is not there.
    write_launch_guard(client_root)
    if platform_name == "macos":
        os.chmod(client_root, 0o700)
        if not getattr(args, "control_plane_url", None):
            raise RuntimeError(text("macos_schedule_needs_service"))
        token = required_client_token(args)
        layout = schedule_layout(args)
        launcher = client_root / "sync-client.sh"
        if layout == "terminal":
            # The enrollment is the Keychain item the command-line client wrote; nothing is
            # written here. `security` created that item, so it reads it without a dialog, and
            # the record is ASCII JSON, which `security -w` prints as it is.
            read_token = (
                "AGENT_PLATFORM_CLIENT_TOKEN=$(/usr/bin/security find-generic-password "
                f"-s {shlex.quote(keychain_service(args))} -a {shlex.quote(KEYCHAIN_ACCOUNT)} -w "
                "| /usr/bin/plutil -extract client.clientToken raw -o - -)\n"
                '[ -n "$AGENT_PLATFORM_CLIENT_TOKEN" ]\n'
            )
        else:
            token_path = client_root / "credential"
            _private_write_text(token_path, token + "\n")
            read_token = f"IFS= read -r AGENT_PLATFORM_CLIENT_TOKEN < {shlex.quote(str(token_path))}\n"
        _private_write_text(
            launcher,
            macos_launcher(command, script.parent, read_token,
                           reinstall_command(command[0], args, client_id, display_name, providers)),
            mode=0o700,
        )
        label = launch_agent_label(client_id, layout)
        plist = launch_agent_path(client_id, layout)
        log_root = client_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        os.chmod(log_root, 0o700)
        import xml.sax.saxutils
        def escaped(value: str) -> str:
            return xml.sax.saxutils.escape(value)
        plist_source = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'<key>Label</key><string>{escaped(label)}</string>\n'
            '<key>ProgramArguments</key><array>'
            f'<string>{escaped(str(launcher))}</string></array>\n'
            '<key>RunAtLoad</key><true/>\n'
            '<key>StartInterval</key><integer>3600</integer>\n'
            '<key>ProcessType</key><string>Background</string>\n'
            f'<key>StandardOutPath</key><string>{escaped(str(log_root / "sync.log"))}</string>\n'
            f'<key>StandardErrorPath</key><string>{escaped(str(log_root / "sync-error.log"))}</string>\n'
            '</dict></plist>\n'
        )
        try:
            previous_plist = plist.read_text(encoding="utf-8")
        except OSError:
            previous_plist = None
        _private_write_text(plist, plist_source)
        domain = f"gui/{os.getuid()}"
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], check=False,
                                capture_output=True, text=True, encoding="utf-8", errors="replace").returncode == 0
        # launchd re-reads the launcher script on every run. Reload only when the job
        # definition changed or the job is missing: when this process is the job itself,
        # `bootout` terminates it before `bootstrap` and leaves the schedule unloaded.
        if previous_plist != plist_source or not loaded:
            subprocess.run(["launchctl", "bootout", domain, str(plist)], check=False,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
            subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=True,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        receipt = {
            "version": SCHEDULED_TASK_SCHEMA_VERSION,
            "clientId": client_id,
            "displayName": display_name,
            "providers": providers,
            "controlPlaneUrl": args.control_plane_url,
            "taskName": label,
            "fingerprint": scheduled_task_fingerprint(script, args, client_id, display_name, providers),
            "launcher": "launchd",
            "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        receipt_path = scheduled_task_receipt_path(client_root)
        _private_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        return receipt
    if getattr(args, "control_plane_url", None):
        token_path = client_root / "credential.dpapi"
        launcher = client_root / "sync-client.ps1"
        sealed = protect_windows_token(required_client_token(args))
        descriptor, temporary = tempfile.mkstemp(prefix='.credential-', dir=client_root)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(sealed)
                stream.flush(); os.fsync(stream.fileno())
            replace_busy(temporary, token_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        # The command is never written into the script. PowerShell closes a single-quoted string
        # at ‘ ’ ‚ ‛ as well as at ', so a computer name like  x’; calc; ‘  (typed in the
        # terminal, or set for the computer in the web application) would become code that runs
        # every hour. The script is therefore the same text for every computer: the command is
        # data in a file next to it, and every path is derived from where the script lies.
        #
        # The file is one JSON object with two strings, and the script hands the command line to
        # the interpreter as that one string, through .NET, never as PowerShell arguments:
        # * a JSON array would come out of ConvertFrom-Json as one object or as its elements
        #   depending on the PowerShell version; an object with string properties is the same
        #   in Windows PowerShell 5.1 and in PowerShell 7;
        # * Windows PowerShell 5.1 does not escape a quotation mark inside an argument it passes
        #   to a program, so a computer name with one (a name can be set in the web application)
        #   would split into further options. list2cmdline writes the line by the rules
        #   python.exe reads it with, and nothing re-quotes it on the way.
        launch = {"version": 1, "python": command[0], "commandLine": subprocess.list2cmdline(command[1:])}
        # What the script starts instead when the client cannot start and nothing can be
        # restored: the runtime's installer, with the same interpreter. Absent on a computer
        # whose interpreter is no client runtime; the script then starts the client as before.
        reinstall = reinstall_command(command[0], args, client_id, display_name, providers)
        if reinstall is not None:
            launch["reinstallCommandLine"] = subprocess.list2cmdline(reinstall[1:])
        _private_write_text(client_root / WINDOWS_ARGUMENTS_FILE, json.dumps(launch, ensure_ascii=True) + "\n")
        launcher.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            # Read by the desktop application up to 0.4.3 when it takes this client over; one of
            # two fixed words.
            f"# --update-policy {'manual' if args.update_policy == 'manual' else 'automatic'}\n"
            "$clientRoot = $PSScriptRoot\n"
            "function Stop-AgentPlatformLauncher([string]$message) {\n"
            "  $logRoot = Join-Path $clientRoot 'logs'\n"
            "  New-Item -ItemType Directory -Force -Path $logRoot | Out-Null\n"
            "  $at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)\n"
            "  Add-Content -LiteralPath (Join-Path $logRoot 'sync-error.log') -Encoding UTF8 -Value ($at + ' ' + $message)\n"
            "  [Console]::Error.WriteLine($message)\n"
            "  exit 64\n"
            "}\n"
            "$launch = $null\n"
            "try {\n"
            f"  $launchText = Get-Content -LiteralPath (Join-Path $clientRoot {powershell_literal(WINDOWS_ARGUMENTS_FILE)}) -Raw -Encoding UTF8\n"
            "  $launch = ConvertFrom-Json -InputObject $launchText\n"
            "} catch {\n"
            "  Stop-AgentPlatformLauncher 'agent-platform: sync-client.arguments.json cannot be read; run a synchronization from the app or the terminal to write it again'\n"
            "}\n"
            "if (($null -eq $launch) -or ($launch -is [array]) -or ($launch.version -ne 1) -or "
            "(-not ($launch.python -is [string])) -or (-not ($launch.commandLine -is [string])) -or "
            "([string]::IsNullOrWhiteSpace($launch.python)) -or ([string]::IsNullOrWhiteSpace($launch.commandLine))) {\n"
            "  Stop-AgentPlatformLauncher 'agent-platform: sync-client.arguments.json is not the expected object; run a synchronization from the app or the terminal to write it again'\n"
            "}\n"
            "$python = $launch.python\n"
            + runtime_restore_powershell()
            + "$securityModule = Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'\n"
            "Import-Module $securityModule -Force\n"
            "$secure = Get-Content -LiteralPath (Join-Path $clientRoot 'credential.dpapi') | ConvertTo-SecureString\n"
            "$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)\n"
            "try {\n"
            "  $env:AGENT_PLATFORM_CLIENT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)\n"
            "  $start = New-Object System.Diagnostics.ProcessStartInfo\n"
            "  $start.FileName = $python\n"
            "  $start.Arguments = if ($reinstall) { $launch.reinstallCommandLine } else { $launch.commandLine }\n"
            "  $start.UseShellExecute = $false\n"
            "  $start.CreateNoWindow = $true\n"
            "  $process = [System.Diagnostics.Process]::Start($start)\n"
            "  $process.WaitForExit()\n"
            "  exit $process.ExitCode\n"
            "} finally {\n"
            "  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)\n"
            "  Remove-Item Env:AGENT_PLATFORM_CLIENT_TOKEN -ErrorAction SilentlyContinue\n"
            "}\n",
            encoding="utf-8-sig",
        )
        task_command = subprocess.list2cmdline([
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(launcher),
        ])
    hidden_launcher = client_root / "sync-client.vbs"
    hidden_launcher.write_text(
        "Option Explicit\n"
        "Dim shell, result\n"
        "Set shell = CreateObject(\"WScript.Shell\")\n"
        f"result = shell.Run({vbscript_literal(task_command)}, 0, True)\n"
        "WScript.Quit result\n",
        encoding="utf-16",
    )
    task_command = subprocess.list2cmdline([
        "wscript.exe", "//B", "//Nologo", str(hidden_launcher),
    ])
    # No /RU and no /RP: the task belongs to the person who runs this and Windows registers it
    # as «Interactive only», so it runs only while that person is signed in. That is the task
    # this client can have: one that also runs after a sign-out needs the account's password
    # stored with the task, which the client never asks for, and the credential the run reads
    # is sealed by DPAPI for this very user. The page https://app.artificial-memory.com/cli and
    # the README of the npm package (packages/command-line) say the same.
    subprocess.run([
        windows_system_tool("schtasks.exe"), "/Create", "/F", "/SC", "HOURLY", "/MO", "1",
        "/TN", scheduled_task_name(client_id, schedule_layout(args)), "/TR", task_command,
    ], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    receipt = {
        "version": SCHEDULED_TASK_SCHEMA_VERSION,
        "clientId": client_id,
        "displayName": display_name,
        "providers": providers,
        "controlPlaneUrl": args.control_plane_url,
        "taskName": scheduled_task_name(client_id, schedule_layout(args)),
        "fingerprint": scheduled_task_fingerprint(script, args, client_id, display_name, providers),
        "launcher": "wscript.exe",
        "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    receipt_path = scheduled_task_receipt_path(client_root)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_receipt = receipt_path.with_suffix(".tmp")
    temporary_receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replace_busy(temporary_receipt, receipt_path)
    return receipt


def read_local_run_receipt(root: Path, client_id: str) -> dict | None:
    path = root / "last-run.json"
    try:
        raw = path.read_bytes()
        value = json.loads(raw) if len(raw) <= 8192 else None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    fields = {"version", "clientId", "status", "code", "exitCode", "completedAt", "action"}
    # stage/reason are local diagnostics for the desktop application; the service accepts
    # only `fields`.
    local_fields = {"stage", "reason"}
    if not isinstance(value, dict) or not fields <= set(value) <= fields | local_fields:
        return None
    if any(not isinstance(value[key], str) or not LOCAL_OUTCOME_CODE.fullmatch(value[key])
           for key in local_fields & set(value)):
        return None
    if value.get("version") != LOCAL_RUN_RECEIPT_VERSION or value.get("clientId") != client_id:
        return None
    if value.get("status") not in {"succeeded", "failed"}:
        return None
    if not isinstance(value.get("code"), str) or not re.fullmatch(r"[a-z0-9_]{1,64}", value["code"]):
        return None
    if not isinstance(value.get("exitCode"), int) or isinstance(value.get("exitCode"), bool) or not 0 <= value["exitCode"] <= 255:
        return None
    if not isinstance(value.get("completedAt"), str) or not re.fullmatch(r"[0-9T:.+Z-]{20,40}", value["completedAt"]):
        return None
    action = value.get("action")
    if action is not None and (not isinstance(action, str) or not action or len(action) > 300 or "\n" in action or "\r" in action):
        return None
    return {key: value[key] for key in fields}


LOCAL_OUTCOME_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def companion_scheduler_report(client_id: str, client_root: Path | None = None) -> dict:
    """The desktop application owns the schedule; the client keeps no OS scheduler entry.

    A second trigger would mean double runs and a keychain read nobody can answer at login.
    """
    report = {"status": "installed", "schemaVersion": SCHEDULED_TASK_SCHEMA_VERSION, "launcher": "companion", "error": None}
    last_run = read_local_run_receipt(client_root or (Path.home() / ".agent-platform" / "client"), client_id)
    if last_run is not None:
        report["lastRun"] = last_run
    return report


def manual_scheduler_report(client_id: str, client_root: Path | None = None) -> dict:
    """The person turned the background schedule off in the command-line client and starts every
    run there. The run neither installs an OS scheduler entry nor reports one it does not have."""
    report = {"status": "installed", "schemaVersion": SCHEDULED_TASK_SCHEMA_VERSION, "launcher": "manual", "error": None}
    last_run = read_local_run_receipt(client_root or (Path.home() / ".agent-platform" / "client"), client_id)
    if last_run is not None:
        report["lastRun"] = last_run
    return report


def ensure_scheduled_task(
    script: Path,
    args: argparse.Namespace,
    client_id: str,
    display_name: str,
    providers: list[str],
) -> dict | None:
    if client_platform() not in ("windows", "macos") or getattr(args, "check_only", False):
        return None
    record_schedule_owner(client_id, getattr(args, "scheduler", "os"))
    if getattr(args, "scheduler", "os") == "companion":
        retire_terminal_schedule(client_id)
        remove_launch_guard_without_launcher(Path.home() / ".agent-platform" / "client")
        return companion_scheduler_report(client_id)
    if getattr(args, "scheduler", "os") == "manual":
        remove_launch_guard_without_launcher(Path.home() / ".agent-platform" / "client")
        return manual_scheduler_report(client_id)
    if not scheduled_task_is_current(script, args, client_id, display_name, providers):
        install_scheduled_task(script, args, client_id, display_name, providers)
    if schedule_layout(args) == "terminal":
        retire_legacy_job(client_id)
    return scheduled_task_report(script, args, client_id, display_name, providers)
