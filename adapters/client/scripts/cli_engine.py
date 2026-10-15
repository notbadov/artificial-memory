"""Starting the managed client, and what the terminal client reads of it.

Everything that changes AI apps is done by the managed client of the approved signed release
(`sync_client.py`), started as a process of its own; a computer without one, or with one that
does not know the terminal layout, first goes through the runtime's installer. Here are also
the file of release keys both tools write the same way, the lock a run holds, the agreements
that wait for the person, and the hourly job as the managed client's own receipt names it.

This file is a part of `command_line.py` and lies next to it in the runtime. Of the client it
imports the messages and the host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from cli_host import (Host, Refusal, client_root, managed_script, platform_root, read_object, remove_file,
                      windows_system_tool)
from cli_messages import language, text

SCRIPT_DIR = Path(__file__).resolve().parent

CONSENT_KEY = re.compile(r"(skill|mcp):[A-Za-z0-9._-]{1,120}:[0-9a-f]{16}")
JOB_NAME = re.compile(r"[A-Za-z0-9 ._()-]{1,200}")


def ensure_release_trust(host: Host, trust: dict) -> Path:
    """Writes `release-trust.json` exactly as the desktop application does, so either tool finds
    the file the other wrote unchanged. A different existing file is never replaced."""
    path = client_root(host) / "release-trust.json"
    if path.is_file():
        if read_object(path) != trust:
            raise Refusal("trust_changed")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("release-trust.json.partial")
    temporary.write_text(json.dumps(trust), encoding="utf-8")
    os.replace(temporary, path)
    return path


# ---------------------------------------------------------------- the managed client


def last_json_line(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def run_engine(host: Host, script: Path, arguments: list[str], token: str | None = None) -> subprocess.CompletedProcess:
    # Isolated like every run the desktop application starts: no user site packages, no PYTHON* variables.
    return host.run([host.python, "-E", "-s", str(script), *arguments], token=token)


def engine_capabilities(host: Host) -> list[str]:
    script = managed_script(host)
    if not script.is_file():
        return []
    result = run_engine(host, script, ["--self-check"])
    capabilities = last_json_line(result.stdout).get("capabilities") if result.returncode == 0 else None
    return [item for item in capabilities or [] if isinstance(item, str)]


def sync_arguments(host: Host, record: dict, trust: Path) -> list[str]:
    arguments = [
        "--control-plane-url", record["controlPlaneUrl"], "--client-id", record["clientId"],
        "--display-name", record["displayName"], "--update-policy", record["updatePolicy"],
        "--release-trust-file", str(trust),
        # "os": the managed client keeps its own hourly job; "manual": the person turned it off.
        "--scheduler", "os" if record["schedule"] == "on" else "manual",
        # The product's job name and, on a Mac, the Keychain item instead of a credential file.
        "--schedule-layout", "terminal", "--keychain-service", host.keychain_service,
    ]
    for provider in record["providers"]:
        arguments += ["--provider", provider]
    return arguments


RELEASE_TOO_OLD = "release_without_terminal_schedule"
# The approved release knows no Cursor, and Cursor is the only app chosen (companion_install.py).
RELEASE_WITHOUT_CURSOR = "release_without_cursor"

# What the managed client names when the client of the approved release does not start here:
# its repair failed, or the release was refused on this computer after failing twice.
CLIENT_DID_NOT_START = "client_runtime_probe_failed"
# The option of the managed client and of the runtime's installer for a repair a person asked for.
REPAIR_REQUEST = "--repair-client"


def failed_sync(answer: dict, stage: str) -> dict:
    """What a failed run of the managed client is reported as.

    Its sentences are Russian unless it says which language one is in, so on a computer of
    another language only a sentence it names as that language is shown."""
    message = answer.get("message")
    spoken = isinstance(message, str) and (language() == "ru" or answer.get("messageLanguage") == language())
    return {"status": "failed", "stage": answer["phase"] if isinstance(answer.get("phase"), str) else stage,
            "message": message if spoken else "", "reason": answer.get("reason")}


def run_sync(host: Host, record: dict, token: str, *, repair: bool = False) -> dict:
    """One synchronization. Returns {"status": "ok" | "busy" | "failed", "stage", "message", "reason"}.

    The installed managed client is started only when it knows the terminal layout. A computer
    without one, or with an older one (it used the application before, or an earlier release),
    first goes through the runtime's installer, which verifies the approved signed release and
    installs its client. An older client would stop on the options below, for ever.

    ``repair``: the person asked for a repair (`sync --repair`). The request goes to the
    installed client when it says it can be asked, and otherwise through the installer to the
    client of the approved release. Nothing else ever carries it: not `sync` without the
    option, and no hourly job."""
    trust = client_root(host) / "release-trust.json"
    script = managed_script(host)
    capabilities = engine_capabilities(host)
    # An installed client from before Cursor stops on its name. It serves the other chosen apps
    # (no reinstallation on every sync) and gets Cursor with the release that knows it; Cursor
    # alone needs the approved release's client, through the installer (companion_install.py).
    knows_the_apps = "cursor" not in record["providers"] or "cursorApp" in capabilities
    if not knows_the_apps and len(record["providers"]) > 1:
        record = {**record, "providers": [app for app in record["providers"] if app != "cursor"]}
        knows_the_apps = True
    arguments = sync_arguments(host, record, trust)
    if repair:
        arguments.append(REPAIR_REQUEST)
    if "terminalSchedule" not in capabilities or not knows_the_apps or (repair and "repairOnRequest" not in capabilities):
        result = run_engine(host, SCRIPT_DIR / "companion_install.py", arguments, token)
        outcome = last_json_line(result.stdout)
        if (result.returncode == 0 and outcome.get("status") == "installed"
                and "terminalSchedule" in engine_capabilities(host)):
            return {"status": "ok"}
        if outcome.get("detail") == RELEASE_TOO_OLD:
            return {"status": "failed", "stage": RELEASE_TOO_OLD, "message": text("release_too_old_later")}
        if outcome.get("detail") == RELEASE_WITHOUT_CURSOR:
            return {"status": "failed", "stage": RELEASE_WITHOUT_CURSOR, "message": text("release_without_cursor_later")}
        # The installer passes on the last line the managed client printed.
        try:
            detail = json.loads(outcome.get("detail") or "")
        except ValueError:
            detail = {}
        if isinstance(detail, dict) and detail.get("status") == "busy":
            return {"status": "busy"}
        return failed_sync(detail if isinstance(detail, dict) else {}, "installation")
    result = run_engine(host, script, arguments, token)
    outcome = last_json_line(result.stdout)
    if outcome.get("status") == "busy":
        return {"status": "busy"}
    if result.returncode == 0:
        return {"status": "ok"}
    return failed_sync(outcome, "start")


class SyncLock:
    """The managed client's per-user lock, held while this client does what a run must not
    overlap with: unloading the hourly job, removing the credential. Not blocking: a run in
    progress is a reason to stop, not to wait."""

    def __init__(self, host: Host):
        self.path = client_root(host) / "sync.lock"
        self.handle = None

    def __enter__(self) -> "SyncLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self.handle is not None:
                self.handle.close()
            raise Refusal("disconnect_busy") from None
        return self

    def __exit__(self, *_: object) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()


def sync_idle(host: Host) -> bool:
    """True when no run holds the managed client's lock right now (for the status only)."""
    try:
        with SyncLock(host):
            return True
    except Refusal:
        return False


# ---------------------------------------------------------------- consents


def pending_consents(host: Host) -> list[dict]:
    items = read_object(client_root(host) / "pending-consents.json").get("items")
    waiting = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("key"), str) and CONSENT_KEY.fullmatch(item["key"]):
            waiting.append({name: str(item.get(name) or "")[:200] for name in ("key", "kind", "title", "detail")})
    return waiting[:50]


def show_consent(host: Host, item: dict) -> None:
    host.out(text("consent_mcp" if item["kind"] == "mcp" else "consent_skill", title=item["title"], detail=item["detail"]))


def ask_consents(host: Host) -> int:
    """Asks about everything that waits; returns how many the person approved. An answer is
    taken only from a terminal: a background or piped run never approves anything."""
    waiting = pending_consents(host)
    if not waiting:
        return 0
    host.out(text("consent_waiting", count=len(waiting)))
    if not host.interactive():
        for item in waiting:
            show_consent(host, item)
        host.out(text("consent_hint"))
        return 0
    approved = 0
    for item in waiting:
        show_consent(host, item)
        try:
            answer = host.ask(text("consent_ask")).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes", "д", "да"):
            host.out(text("consent_declined"))
            continue
        result = run_engine(host, managed_script(host), ["--grant-consent", item["key"]])
        if result.returncode == 0 and last_json_line(result.stdout).get("granted") is True:
            approved += 1
            host.out(text("consent_recorded"))
        else:
            host.out(text("consent_failed"))
    return approved


# ---------------------------------------------------------------- the background schedule


def schedule_job(host: Host, client_id: str) -> dict | None:
    """The job the managed client registered for this connection, from its own receipt."""
    receipt = read_object(client_root(host) / "scheduled-task.json")
    name = receipt.get("taskName")
    if receipt.get("clientId") != client_id or not isinstance(name, str) or not JOB_NAME.fullmatch(name):
        return None
    if host.platform == "macos" and receipt.get("launcher") == "launchd" and " " not in name:
        return {"kind": "launchd", "name": name, "plist": host.home / "Library" / "LaunchAgents" / f"{name}.plist"}
    if host.platform == "windows" and receipt.get("launcher") == "wscript.exe":
        return {"kind": "task", "name": name}
    return None


def schedule_registered(host: Host, job: dict) -> bool:
    try:
        if job["kind"] == "launchd":
            if not job["plist"].is_file():
                return False
            return host.run(["/bin/launchctl", "print", f"gui/{host.user_id()}/{job['name']}"], timeout=30).returncode == 0
        result = host.run([windows_system_tool("schtasks.exe"), "/Query", "/TN", job["name"], "/XML"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    settings = re.search(r"<Settings>.*?</Settings>", result.stdout, re.S | re.I)
    disabled = bool(settings and re.search(r"<Enabled>\s*false\s*</Enabled>", settings.group(0), re.I))
    return result.returncode == 0 and not disabled


def stop_schedule(host: Host, job: dict) -> None:
    """macOS: the job is unloaded and its file removed. Windows: the task is disabled, not
    deleted, because the desktop application takes a terminal connection over by disabling
    that task and its version 0.4.3 gives the takeover up when the task is missing."""
    if job["kind"] == "launchd":
        host.run(["/bin/launchctl", "bootout", f"gui/{host.user_id()}/{job['name']}"], timeout=30)
        remove_file(job["plist"])
        return
    result = host.run([windows_system_tool("schtasks.exe"), "/Change", "/TN", job["name"], "/DISABLE"], timeout=30)
    if result.returncode != 0:
        raise Refusal("schedule_failed", detail=f"schtasks {result.returncode}")


def delete_schedule(host: Host, job: dict) -> None:
    if job["kind"] == "launchd":
        host.run(["/bin/launchctl", "bootout", f"gui/{host.user_id()}/{job['name']}"], timeout=30)
        remove_file(job["plist"])
    else:
        host.run([windows_system_tool("schtasks.exe"), "/Delete", "/TN", job["name"], "/F"], timeout=30)


def launcher_is_stale(host: Host) -> bool:
    """The hourly job starts an interpreter by its path, and a runtime is removed two runtime
    releases after it was current."""
    root = client_root(host)
    try:
        if host.platform == "windows":
            source = (root / "sync-client.arguments.json").read_text(encoding="utf-8")
        else:
            source = (root / "sync-client.sh").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    # Written for another runtime than the one in use now: the next synchronization rewrites it.
    runtime = str(platform_root(host) / "runtime")
    return (runtime in source or json.dumps(runtime)[1:-1] in source) and (
        host.python not in source and json.dumps(host.python)[1:-1] not in source)
