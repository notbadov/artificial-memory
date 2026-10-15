#!/usr/bin/env python3
"""Artificial Memory from the terminal: connect, synchronize and disconnect a computer without
the desktop application.

The npm launcher (packages/command-line) downloads the signed client runtime, checks it, and runs
this file from the runtime's `bootstrap` directory with the runtime's own interpreter. Everything
that changes AI apps is done by the same managed client the desktop application runs
(`sync_client.py` from the approved signed release); this file only decides who starts it.

One computer has one connection, whichever tool made it:

* on a Mac the credential of a connection made here is the Keychain item the desktop
  application keeps its own enrollment in (same service, same account, same record). The
  application therefore finds the connection instead of offering to make another, and from its
  first run the managed client ends the hourly job of the terminal, so there is one schedule;
* on Windows the credential is the DPAPI file and the job is the Scheduled Task of the managed
  client's own OS schedule, which the desktop application takes over, keeping the computer's
  identity;
* a computer the desktop application connected is left to it: every command here that would
  change the connection says so and stops.

The client credential is never printed, logged or passed in arguments.

This file is what the launcher starts: the parser, the commands and `main`. What the commands
are made of is in the parts next to it, delivered with it in the runtime: the messages
(`cli_messages.py`), the computer and the client's files on it (`cli_host.py`), the credential
(`cli_credentials.py`), the managed client (`cli_engine.py`), whose the connection is and how
one is authorized (`cli_connection.py`), and ending one (`cli_leaving.py`). A part imports
only parts named before it in this list, and none imports this file. The release keys this
client was built with stay here, where the launcher's tests and the verification page read
them.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The parts of this file lie next to it, in the runtime as in the repository. Started as a
# script it has its directory first on the path; a caller that loads it by path does not.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Only what this file uses is taken from its parts; a caller that needs another name imports it
# from the part that defines it.
from cli_messages import text  # noqa: E402
from cli_host import (APPS, CLIENT_ID, EXIT_FAILED, EXIT_OK, EXIT_REFUSED, Host, Refusal, application_was_here,
                      client_root, iso_now, managed_script, platform_root, read_object, remove_file,
                      terminal_path, unfinished_path, windows_system_tool, write_object)  # noqa: E402
from cli_credentials import delete_credential, load_credential, save_credential  # noqa: E402
from cli_engine import (CLIENT_DID_NOT_START, RELEASE_TOO_OLD, SyncLock, ask_consents, engine_capabilities,
                        ensure_release_trust, launcher_is_stale, pending_consents, run_sync, schedule_job,
                        schedule_registered, stop_schedule, sync_idle)  # noqa: E402
from cli_connection import authorize, ownership, refuse_for_app, require_terminal, valid_record  # noqa: E402
from cli_leaving import (abandon_new_connection, deliver_revocations, drop_revocations, end_connection,
                         forget_app_connection, pending_revocations)  # noqa: E402

# Release signing keys this client was built with (the same key the desktop application
# carries, and the npm launcher in packages/command-line/src/trust.mjs). A key the service
# names is used only when it is one of these.
TRUSTED_KEYS = {"home-server-2026-09": "0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8="}
APP_NAMES = {"codex": "Codex", "claude": "Claude Code", "kimi": "Kimi Code", "opencode": "OpenCode", "cursor": "Cursor"}
# Letters and digits of any language, spaces and . , _ - ( ) + # @ &: no quote of any kind.
COMPUTER_NAME = re.compile(r"[\w][\w .,()+#@&-]{0,199}", re.UNICODE)
# Written into the runtime directory by `uninstall` when the OS would not let this process
# delete its own interpreter; the launcher removes the directory when it finds it, and never otherwise.
RUNTIME_REMOVAL_REQUEST = "remove-on-exit"
# The launcher names each run in this variable; the request carries the name back, so a request
# left by another run is never acted on.
LAUNCH_VARIABLE = "ARTIFICIAL_MEMORY_LAUNCH"
# Written by the synchronization (sync_codex_versions.py, CODEX_APPROVAL_RECORD) when a server
# that asks for "writes" was written as "prompt" for Codex, because a Codex here is too old.
CODEX_APPROVAL_RECORD = "codex-approval.json"
VERSION_TEXT = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}(?:-[0-9A-Za-z.-]{1,64})?")


# ---------------------------------------------------------------- release trust


def compiled_in_trust(received: object) -> dict:
    """The keys of `received` this client was built with, and nothing else: the service must
    not choose the keys a computer trusts."""
    keys = {}
    entries = received.get("keys") if isinstance(received, dict) else None
    for key_id, entry in (entries.items() if isinstance(entries, dict) else ()):
        public_key = entry.get("publicKey") if isinstance(entry, dict) else None
        if isinstance(public_key, str) and TRUSTED_KEYS.get(key_id) == public_key.strip():
            keys[key_id] = entry
    if not keys:
        raise Refusal("trust_unknown")
    return {"version": 1, "keys": keys}


# ---------------------------------------------------------------- commands


def chosen_apps(host: Host, requested: list[str] | None) -> list[str]:
    if requested:
        apps = []
        for value in requested:
            for name in value.split(","):
                name = name.strip().lower()
                if name not in APPS:
                    raise Refusal("apps_unknown", EXIT_REFUSED, value=name or value)
                if name not in apps:
                    apps.append(name)
        return apps
    if not host.interactive():
        raise Refusal("apps_required", EXIT_REFUSED)
    present = [app for app, directory in (("codex", ".codex"), ("claude", ".claude"), ("kimi", ".kimi-code"),
                                          ("opencode", ".config/opencode"), ("cursor", ".cursor"))
               if (host.home / directory).is_dir()]
    default = ", ".join(present or ["codex", "claude"])
    host.out(text("choose_apps"))
    for app in APPS:
        host.out(f"  {app} — {APP_NAMES[app]}")
    answer = host.ask(text("choose_apps_hint", default=default)).strip()
    return chosen_apps(host, [answer or default])


def computer_name(requested: str | None) -> str:
    """The name of this computer in Artificial Memory: letters, digits, spaces and . , _ - ( ) + # @ &.
    The name travels to the service, into the managed client's arguments and back to terminals,
    so nothing that is a quote, a control or a direction mark is accepted."""
    if requested is not None:
        name = requested.strip()
        if not COMPUTER_NAME.fullmatch(name):
            raise Refusal("name_invalid", EXIT_REFUSED)
        return name
    name = re.sub(r"[^\w .,()+#@&-]", "-", platform.node() or "", flags=re.UNICODE).strip(" -")[:200]
    return name if COMPUTER_NAME.fullmatch(name) else "computer"


def report_failure(host: Host, outcome: dict, *, repair: bool = False) -> int:
    host.out(text("sync_failed", stage=outcome["stage"], message=outcome["message"] or text("sync_failed_hint")))
    if not repair and (outcome["stage"], outcome.get("reason")) == ("client_runtime", CLIENT_DID_NOT_START):
        # No run a person did not ask for installs that client again; this is how to ask.
        host.out(text("sync_repair_hint"))
    return EXIT_FAILED


def codex_update(host: Host) -> dict | None:
    """The servers whose tools ask before every call in Codex because a Codex here is too old,
    and that Codex's version (None when one did not say it), from the synchronization's record."""
    record = read_object(client_root(host) / CODEX_APPROVAL_RECORD)
    servers = record.get("serversAsking")
    if (record.get("version") != 1 or not isinstance(servers, list) or not servers
            or not all(isinstance(name, str) and 0 < len(name) <= 200 for name in servers)):
        return None
    oldest, since = record.get("oldestCodex"), record.get("writesSince")
    return {"oldestCodex": oldest if isinstance(oldest, str) and VERSION_TEXT.fullmatch(oldest) else None,
            "writesSince": since if isinstance(since, str) and VERSION_TEXT.fullmatch(since) else "0.144.0",
            "serversAsking": servers[:20]}


def show_codex_update(host: Host, update: dict | None) -> None:
    if update is None:
        return
    servers = ", ".join(f"«{name}»" for name in update["serversAsking"])
    if update["oldestCodex"] is None:
        host.out(text("codex_update_unknown", since=update["writesSince"], servers=servers))
    else:
        host.out(text("codex_update_old", version=update["oldestCodex"], since=update["writesSince"], servers=servers))


def finish_sync(host: Host, record: dict, token: str, outcome: dict, *, repair: bool = False) -> int:
    """Reports one synchronization and asks about what waits; an approval is followed by one more run."""
    if outcome["status"] == "busy":
        host.out(text("sync_busy"))
        return EXIT_OK
    if outcome["status"] == "failed":
        return report_failure(host, outcome, repair=repair)
    if ask_consents(host):
        host.out(text("syncing"))
        # An ordinary run: the request was for one attempt and it has been made.
        outcome = run_sync(host, record, token)
        if outcome["status"] == "failed":
            return report_failure(host, outcome)
    host.out(text("sync_done"))
    show_codex_update(host, codex_update(host))
    return EXIT_OK


def command_connect(host: Host, args: argparse.Namespace) -> int:
    deliver_revocations(host)
    owned = ownership(host)
    refuse_for_app(owned, "connect")
    if owned["owner"] == "terminal":
        raise Refusal("already_connected", EXIT_REFUSED, client=owned["clientId"])
    name = computer_name(args.name)
    apps = chosen_apps(host, args.app)
    # A record that never got its credential belongs to a connection nobody here can use.
    remove_file(unfinished_path(host))
    redemption = authorize(host, name, apps, not args.no_browser)
    client = redemption["client"]
    token = client["clientToken"]
    # Everything that can refuse comes before anything is written.
    # The person may have connected the application while the browser was open: then the
    # connection made here is one too many, and nothing of the application's is touched.
    if ownership(host)["owner"] != "none":
        raise abandon_new_connection(host, client, "connected_meanwhile")
    try:
        trust = compiled_in_trust(redemption.get("releaseTrust"))
        existing = read_object(client_root(host) / "release-trust.json")
        if existing and existing != trust:
            raise Refusal("trust_changed")
    except Refusal as refusal:
        raise abandon_new_connection(host, client, refusal.key) from None
    displayed = client.get("displayName")
    record = {"version": 1, "clientId": client["id"],
              # The service's spelling is kept only when it is one this client would accept itself.
              "displayName": displayed if isinstance(displayed, str) and COMPUTER_NAME.fullmatch(displayed) else name,
              "providers": client["providers"], "updatePolicy": client["updatePolicy"],
              "controlPlaneUrl": host.api_origin, "schedule": "on", "connectedAt": iso_now()}
    # From here the connection can always be found and ended, wherever this stops. The release
    # keys go first: they hold no secret, and a connection found again later needs them.
    # Windows: the record then waits next to the credential file until the credential is
    # written; macOS: the Keychain item carries the whole enrollment, and the record is written
    # again from it when it is missing.
    try:
        ensure_release_trust(host, trust)
        if host.platform == "windows":
            write_object(unfinished_path(host), record)
        save_credential(host, {**redemption, "client": {**client, "displayName": record["displayName"]}}, trust)
        write_object(terminal_path(host), record)
        remove_file(unfinished_path(host))
    except (OSError, subprocess.SubprocessError, ValueError):
        # Only what this run wrote is removed; `delete_credential` never touches another program's item.
        delete_credential(host)
        for leftover in ("terminal.json", "terminal.json.partial"):
            remove_file(client_root(host) / leftover)
        raise abandon_new_connection(host, client, "credential_not_saved") from None
    host.out(text("connected", name=record["displayName"], client=record["clientId"]))
    host.out(text("installing"))
    outcome = run_sync(host, record, token)
    if outcome["status"] == "failed" and outcome["stage"] == RELEASE_TOO_OLD:
        # Nothing can be installed until the next release, and a connection that cannot install
        # is invisible to the desktop application: it is ended instead of being left behind.
        result = end_connection(host, {"owner": "terminal", "clientId": record["clientId"], "record": record,
                                       "supersededBy": None})
        host.out(text("release_too_old"))
        if result["revocation"] != "revoked":
            host.out(text("remove_in_console", client=record["clientId"]))
        return EXIT_FAILED
    if outcome["status"] == "failed":
        host.out(text("install_failed", detail=f" ({outcome['stage']})"))
        return EXIT_FAILED
    code = finish_sync(host, record, token, outcome)
    host.out(text("schedule_on"))
    return code


def command_sync(host: Host, args: argparse.Namespace) -> int:
    deliver_revocations(host)
    owned = require_terminal(host, "sync")
    token = load_credential(host, owned["clientId"])
    host.out(text("syncing"))
    repair = bool(getattr(args, "repair", False))
    return finish_sync(host, owned["record"], token, run_sync(host, owned["record"], token, repair=repair),
                       repair=repair)


def command_consents(host: Host, args: argparse.Namespace) -> int:
    # Agreements are kept by the managed client for the computer, whichever tool connected it.
    if not pending_consents(host):
        host.out(text("consent_none"))
        return EXIT_OK
    if not managed_script(host).is_file():
        raise Refusal("client_missing")
    approved = ask_consents(host)
    owned = ownership(host)
    if approved and owned["owner"] == "terminal" and owned["supersededBy"] is None:
        token = load_credential(host, owned["clientId"])
        host.out(text("syncing"))
        return finish_sync(host, owned["record"], token, run_sync(host, owned["record"], token))
    return EXIT_OK


def command_schedule(host: Host, args: argparse.Namespace) -> int:
    deliver_revocations(host)
    owned = require_terminal(host, "schedule")
    record = dict(owned["record"])
    if args.state == "off":
        if "schedulerManual" not in engine_capabilities(host):
            raise Refusal("schedule_needs_update")
        # Under the run lock: unloading the job would stop a run it started in the middle of its
        # work, and no run can start between the check and the unloading.
        with SyncLock(host):
            job = schedule_job(host, record["clientId"])
            if job is not None:
                stop_schedule(host, job)
            record["schedule"] = "off"
            write_object(terminal_path(host), record)
        host.out(text("schedule_off"))
        return EXIT_OK
    record["schedule"] = "on"
    write_object(terminal_path(host), record)
    job = schedule_job(host, record["clientId"])
    if job is not None and job["kind"] == "task":
        host.run([windows_system_tool("schtasks.exe"), "/Change", "/TN", job["name"], "/ENABLE"], timeout=30)
    # The managed client registers its own job during a run and reports it to the service.
    token = load_credential(host, record["clientId"])
    outcome = run_sync(host, record, token)
    if outcome["status"] == "busy":
        raise Refusal("disconnect_busy")
    if outcome["status"] == "failed":
        return report_failure(host, outcome)
    job = schedule_job(host, record["clientId"])
    if job is None or not schedule_registered(host, job):
        raise Refusal("schedule_failed", detail="job")
    host.out(text("schedule_on"))
    return EXIT_OK


def collect_status(host: Host) -> dict:
    owned = ownership(host)
    client_id = owned["clientId"]
    receipt = read_object(client_root(host) / "last-run.json")
    last_run = None
    if client_id and receipt.get("clientId") == client_id and receipt.get("status") in ("succeeded", "failed"):
        last_run = {"status": receipt["status"], "completedAt": receipt.get("completedAt"),
                    "stage": receipt.get("stage"), "reason": receipt.get("reason")}
    unfinished = read_object(unfinished_path(host)) if host.platform == "windows" else {}
    status = {
        "version": 1, "owner": owned["owner"], "clientId": client_id,
        "supersededBy": owned.get("supersededBy"),
        "displayName": owned["record"]["displayName"] if owned["owner"] == "terminal" else None,
        "apps": owned["record"]["providers"] if owned["owner"] == "terminal" else None,
        "installed": managed_script(host).is_file(),
        "running": owned["owner"] != "none" and not sync_idle(host),
        "lastRun": last_run,
        "schedule": None,
        "pendingConsents": len(pending_consents(host)),
        "pendingRevocations": [item["clientId"] for item in pending_revocations(host)],
        # A connection the service made whose key never reached this computer.
        "unfinished": unfinished["clientId"] if valid_record(unfinished) and owned["owner"] == "none" else None,
        # The desktop application ended a connection made here and keeps its own.
        "revokedByApp": revoked_by_app(host),
        "runtime": read_object(platform_root(host) / "runtime" / "current.json").get("runtimeId"),
        "codexUpdate": codex_update(host),
    }
    if owned["owner"] == "terminal":
        job = schedule_job(host, client_id)
        status["schedule"] = {
            "wanted": owned["record"]["schedule"], "job": job["name"] if job else None,
            "registered": bool(job) and schedule_registered(host, job),
            "stale": owned["record"]["schedule"] == "on" and launcher_is_stale(host),
        }
    return status


def command_status(host: Host, args: argparse.Namespace) -> int:
    remaining = deliver_revocations(host, quiet=args.json)
    status = collect_status(host)
    if args.json:
        # ASCII only: whatever a name holds, a terminal receives nothing that could act on it.
        host.out(json.dumps(status, ensure_ascii=True))
        return EXIT_OK
    client = f" ({status['clientId']})" if status["clientId"] else ""
    if status["owner"] == "none":
        host.out(text("status_none"))
        if status["unfinished"]:
            host.out(text("remove_in_console", client=status["unfinished"]))
    elif status["owner"] == "app":
        host.out(text("status_app", client=client))
    else:
        host.out(text("status_terminal", name=status["displayName"], client=status["clientId"]))
        host.out(text("status_apps", apps=", ".join(APP_NAMES[app] for app in status["apps"])))
        if status["supersededBy"] is not None:
            host.out(text("superseded", client=status["clientId"], other=status["supersededBy"] or text("superseded_unknown")))
        if not status["installed"]:
            host.out(text("status_installing"))
        schedule = status["schedule"]
        if schedule["wanted"] == "off":
            host.out(text("status_schedule_off"))
        elif schedule["stale"]:
            host.out(text("schedule_stale"))
        elif schedule["registered"]:
            host.out(text("status_schedule_on", job=schedule["job"]))
        else:
            host.out(text("status_schedule_broken"))
    if status["owner"] != "none":
        if status["running"]:
            host.out(text("status_running"))
        last = status["lastRun"]
        if last is None:
            host.out(text("status_never"))
        elif last["status"] == "succeeded":
            host.out(text("status_last_ok", at=last["completedAt"]))
        else:
            host.out(text("status_last_failed", at=last["completedAt"],
                          stage=f" ({last['stage']})" if last["stage"] else ""))
    if status["pendingConsents"]:
        host.out(text("status_consents", count=status["pendingConsents"]))
    show_codex_update(host, status["codexUpdate"])
    for item in remaining:
        host.out(text("revocation_waiting", client=item["clientId"]))
    return EXIT_OK


def report_disconnect(host: Host, result: dict) -> None:
    host.out(text({"revoked": "disconnected", "pending": "disconnected_pending",
                   "unrevoked": "disconnected_unrevoked"}[result["revocation"]]))
    if result["revocation"] == "unrevoked":
        host.out(text("remove_in_console", client=result["clientId"] or text("this_computer")))
    if result["left"]:
        host.out(text("local_access_left", apps=", ".join(result["left"])))


def command_disconnect(host: Host, args: argparse.Namespace) -> int:
    deliver_revocations(host)
    owned = ownership(host)
    if owned["owner"] == "app" and args.without_app:
        report_disconnect(host, forget_app_connection(host, owned))
        return EXIT_OK
    refuse_for_app(owned, "disconnect")
    if owned["owner"] == "none":
        raise Refusal("not_connected", EXIT_REFUSED)
    report_disconnect(host, end_connection(host, owned))
    return EXIT_OK


# What a person decided on this computer and what it learned about withdrawn releases. Both
# outlive a connection: the first is theirs, and the second must only ever grow.
KEPT_CLIENT_FILES = ("consents.json", "withdrawn-releases.json")


def command_uninstall(host: Host, args: argparse.Namespace) -> int:
    owned = ownership(host)
    # The runtime, the client and the caches below are the desktop application's too.
    refuse_for_app(owned, "uninstall")
    if owned["owner"] == "terminal":
        report_disconnect(host, end_connection(host, owned))
        # The desktop application may have its own connection here; then the files stay for it.
        refuse_for_app(ownership(host), "uninstall")
    remaining = deliver_revocations(host)
    if remaining and not args.forget_pending:
        raise Refusal("uninstall_pending", client=", ".join(item["clientId"] for item in remaining))
    drop_revocations(host, {item["clientId"] for item in remaining})
    root = platform_root(host)
    if application_was_here(host):
        # Only what belongs to the terminal connection went, above.
        host.out(text("uninstalled_shared_kept", root=str(root)))
        return EXIT_OK
    client = client_root(host)
    for path in client.iterdir() if client.is_dir() else ():
        if path.name in KEPT_CLIENT_FILES:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            remove_file(path)
    shutil.rmtree(root / "releases", ignore_errors=True)
    # The runtime holds the interpreter running this. Where the OS refuses to delete it
    # (Windows), the launcher removes the directory after this process has exited, and only
    # because this file asks for it: the request is the one signal the launcher acts on.
    runtime = root / "runtime"
    shutil.rmtree(runtime, ignore_errors=True)
    if runtime.exists():
        launch = os.environ.get(LAUNCH_VARIABLE, "")
        (runtime / RUNTIME_REMOVAL_REQUEST).write_text((launch if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", launch) else "") + "\n",
                                                       encoding="utf-8")
    host.out(text("uninstalled", root=str(root)))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artificial-memory",
        description="Artificial Memory in the terminal: connect this computer, synchronize it, disconnect it.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    connect = commands.add_parser("connect", help="connect this computer (a confirmation code is entered in the browser)")
    connect.add_argument("--app", action="append", metavar="APP", help="AI app to connect: codex, claude, kimi, opencode, cursor (repeatable)")
    connect.add_argument("--name", help="how this computer is named in Artificial Memory (default: the host name)")
    connect.add_argument("--no-browser", action="store_true", help="print the page address instead of opening it")
    sync = commands.add_parser("sync", help="synchronize now")
    # Typing `sync` is not a request to repair: a person may type it every hour, and then no
    # release would ever stay refused. The repair is asked for by name.
    sync.add_argument("--repair", action="store_true",
                      help="also try once more to install a release whose synchronization client "
                           "did not start on this computer and is no longer installed by itself")
    status = commands.add_parser("status", help="what is connected, the last synchronization, the background schedule")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    commands.add_parser("consents", help="review skills with programs and servers that wait for your approval")
    schedule = commands.add_parser("schedule", help="turn the hourly background synchronization on or off")
    schedule.add_argument("state", choices=("on", "off"))
    disconnect = commands.add_parser("disconnect", help="end the connection: revoke access on the server and remove it here")
    disconnect.add_argument("--without-app", action="store_true",
                            help="the desktop app that connected this computer is no longer installed: "
                                 "remove the connection from this computer anyway")
    uninstall = commands.add_parser("uninstall", help="disconnect and remove everything this client keeps on the computer")
    uninstall.add_argument("--forget-pending", action="store_true",
                           help="also delete access keys whose revocation the server has not confirmed yet")
    return parser


def revoked_path(host: Host) -> Path:
    """Written by the desktop application (from 0.4.4) when it keeps its own connection and
    ends one made in the terminal."""
    return client_root(host) / "terminal-revoked.json"


def revoked_by_app(host: Host) -> str | None:
    record = read_object(revoked_path(host))
    client_id = record.get("clientId")
    return client_id if record.get("version") == 1 and isinstance(client_id, str) and CLIENT_ID.fullmatch(client_id) else None


def announce_revocation(host: Host, args: argparse.Namespace) -> None:
    """Says once, before whatever command the person ran, that the application ended the
    terminal connection; a plain "not connected" would leave them guessing. The note stays
    until a person has seen it: a machine-readable status reports it and leaves it."""
    client_id = revoked_by_app(host)
    if client_id is None or getattr(args, "json", False):
        return
    print(text("revoked_by_app", client=client_id), file=sys.stderr, flush=True)
    remove_file(revoked_path(host))


COMMANDS = {"connect": command_connect, "sync": command_sync, "status": command_status,
            "consents": command_consents, "schedule": command_schedule, "disconnect": command_disconnect,
            "uninstall": command_uninstall}


def utf8_streams() -> None:
    """Read and say everything in UTF-8, whatever the code page of this computer.

    Whoever starts this file reads what it prints as UTF-8. An interpreter decides otherwise:
    on Windows its streams are in the code page of the system, and started with `-E` it ignores
    PYTHONUTF8 and PYTHONIOENCODING, so every sentence that is not ASCII arrives as other
    letters. Every file that is started carries this function word for word: the files are
    delivered in separate sets that do not import each other
    (tests/test_client_output_encoding.py)."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        host = host or Host()
        announce_revocation(host, args)
        return COMMANDS[args.command](host, args)
    except Refusal as refusal:
        print(str(refusal), file=sys.stderr, flush=True)
        return refusal.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
