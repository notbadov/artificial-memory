"""Ending a connection: at the service, and then everything of it on this computer.

A connection always ends here. When the service does not answer, the credential is kept apart
until it does, and delivered by a later command; when the credential cannot be read, the
person is told where to remove the computer. The order survives an interruption: once the
service is told, or the credential is kept for later, the job and the credential go first.

This file is a part of `command_line.py` and lies next to it in the runtime. Of the client it
imports the messages, the host, the credential, the managed client and the ownership.
"""

from __future__ import annotations

import json
import subprocess
import time

from cli_connection import POLL_SECONDS, forgotten_path, valid_record
from cli_credentials import (application_credentials, delete_credential, keychain_delete, keychain_ours,
                             keychain_read, keychain_write, load_credential, protect, reveal, valid_token)
from cli_engine import SyncLock, delete_schedule, last_json_line, run_engine, schedule_job
from cli_host import (APP_KEYCHAIN_ACCOUNT, CLIENT_ID, EXIT_FAILED, EXIT_REFUSED, Host, Refusal,
                      application_installed, checked_origin, client_root, credential_path, iso_now, managed_script,
                      read_object, remove_file, revocations_path, terminal_path, write_object)
from cli_messages import text

DISCONNECT_WAIT_SECONDS = 60

# Connections this client left while the service did not answer: one Keychain item for each,
# named by the computer id after this prefix. Separate from the application's own waiting list:
# each tool reads only items it created, so neither makes the system ask the person for the other's.
REVOCATION_KEYCHAIN_ACCOUNT = "terminal-pending-revocation."

# What the managed client writes for the OS schedule of a terminal connection, and this
# client's own record. Removed with the connection; none of it holds a secret on a Mac.
TERMINAL_FILES = ("sync-client.sh", "sync-client.ps1", "sync-client.vbs", "sync-client.arguments.json",
                  # The guard a launcher starts before the client (sync_launch_guard.py of the
                  # managed client, which writes this copy with the launcher).
                  "sync-client-guard.py",
                  "scheduled-task.json", "terminal.json", "terminal.json.partial")


# ---------------------------------------------------------------- leaving


def disconnect_confirmed(status: int | None) -> bool:
    """The same rule as the desktop application: a success, or a credential the service no
    longer knows (401), ends the connection there. No answer or a server failure does not."""
    return status is not None and (200 <= status <= 299 or status == 401)


def pending_revocations(host: Host) -> list[dict]:
    """Connections this computer left while the service did not answer: [{"clientId",
    "controlPlaneUrl"}], from `terminal-revocations.json`. The credential of each waits apart:
    macOS keeps it in a Keychain item of its own per computer id, so the list never has to fit
    into one item and no credential is in a file; Windows seals it with DPAPI inside the list
    (the store its live credential is in too)."""
    items = read_object(revocations_path(host)).get("items")
    return [item for item in items if isinstance(item, dict)
            and isinstance(item.get("clientId"), str) and CLIENT_ID.fullmatch(item["clientId"])
            and isinstance(item.get("controlPlaneUrl"), str)] if isinstance(items, list) else []


def revocation_token(host: Host, item: dict) -> str | None:
    try:
        if host.platform == "macos":
            token = keychain_read(host, REVOCATION_KEYCHAIN_ACCOUNT + item["clientId"])
        else:
            token = reveal(host, item["credential"]) if isinstance(item.get("credential"), str) else None
    except (OSError, subprocess.SubprocessError):
        return None
    return token if valid_token(token) else None


def write_revocations(host: Host, items: list[dict]) -> None:
    if items:
        write_object(revocations_path(host), {"version": 1, "items": items})
    else:
        remove_file(revocations_path(host))


def keep_revocation(host: Host, client_id: str, origin: str, token: str) -> None:
    """Keeps one credential until the service confirms the end of its connection."""
    item = {"clientId": client_id, "controlPlaneUrl": origin}
    if host.platform == "macos":
        keychain_write(host, REVOCATION_KEYCHAIN_ACCOUNT + client_id, token)
    else:
        item["credential"] = protect(host, token)
    write_revocations(host, [*(other for other in pending_revocations(host) if other["clientId"] != client_id), item])


def drop_revocations(host: Host, client_ids: set[str]) -> None:
    for client_id in client_ids:
        if host.platform == "macos" and keychain_ours(host, REVOCATION_KEYCHAIN_ACCOUNT + client_id):
            keychain_delete(host, REVOCATION_KEYCHAIN_ACCOUNT + client_id)
    write_revocations(host, [item for item in pending_revocations(host) if item["clientId"] not in client_ids])


def deliver_revocations(host: Host, *, quiet: bool = False) -> list[dict]:
    """Ends, at the service, the connections this computer left while it did not answer."""
    waiting = pending_revocations(host)
    remaining, settled = [], set()
    for item in waiting:
        token = revocation_token(host, item)
        if token is None:
            # The credential is gone from this computer: nothing here can end that connection.
            settled.add(item["clientId"])
            if not quiet:
                host.out(text("remove_in_console", client=item["clientId"]))
            continue
        try:
            status, _ = host.post(checked_origin(item["controlPlaneUrl"]) + "/v1/client-session/disconnect",
                                  {"clientId": item["clientId"], "clientToken": token})
        except Refusal:
            status = None
        if disconnect_confirmed(status):
            settled.add(item["clientId"])
            if not quiet:
                host.out(text("revocation_delivered", client=item["clientId"]))
        else:
            remaining.append(item)
    if settled:
        drop_revocations(host, settled)
    return remaining


def record_disconnected(host: Host) -> None:
    # The marker the app bridges read (agent_identity.connection_path): no connection.
    write_object(client_root(host) / "connection.json", {"version": 1, "clientId": None})


def revoke(host: Host, client_id: str, origin: str, token: str | None) -> str:
    """Ends one connection at the service: "revoked", "pending" (kept until the service answers)
    or "unrevoked" (this computer cannot read the credential, so only a person can end it, in
    Artificial Memory). Raises when a credential that was read cannot be kept for later."""
    if token is None:
        return "unrevoked"
    status, _ = host.post(origin + "/v1/client-session/disconnect", {"clientId": client_id, "clientToken": token})
    if disconnect_confirmed(status):
        return "revoked"
    # Before anything is removed: a credential that cannot be kept must not be forgotten.
    try:
        keep_revocation(host, client_id, origin, token)
    except (OSError, subprocess.SubprocessError):
        raise Refusal("disconnect_not_recorded") from None
    return "pending"


def remove_local_access(host: Host, client_id: str, origin: str) -> list[str]:
    """The managed client takes this computer's access out of the AI apps. Returns the apps whose
    settings it could not clean (["*"] when it could not run)."""
    record_disconnected(host)
    script = managed_script(host)
    if not script.is_file():
        return []
    arguments = ["--control-plane-url", origin, "--client-id", client_id, "--disconnect"]
    deadline = time.monotonic() + DISCONNECT_WAIT_SECONDS
    while True:
        outcome = last_json_line(run_engine(host, script, arguments).stdout)
        if outcome.get("status") != "busy" or time.monotonic() >= deadline:
            break
        host.sleep(POLL_SECONDS)
    left = [item for item in outcome.get("left") or [] if isinstance(item, str)]
    return left if outcome.get("status") == "disconnected" else (left or ["*"])


def end_connection(host: Host, owned: dict) -> dict:
    """Ends a connection made here: the access at the service, then everything of it on this
    computer. Returns {"revocation": "revoked" | "pending" | "unrevoked", "left": [...]}.

    It always ends here. A credential that cannot be read (a damaged file, a lost Windows key)
    is no reason to keep the job, the files and the apps' access: the person is told that the
    service still knows the computer and where to remove it.
    The order survives an interruption: once the service is told (or the credential is kept
    for later), the job and the credential go first, so nothing can start a run or be mistaken
    for a live connection."""
    record = owned["record"]
    with SyncLock(host):
        try:
            token = load_credential(host, record["clientId"])
        except Refusal:
            token = None
        revocation = revoke(host, record["clientId"], record["controlPlaneUrl"], token)
        job = schedule_job(host, record["clientId"])
        if job is not None:
            delete_schedule(host, job)
        delete_credential(host)
    # Outside the lock: the managed client takes it for its own part.
    left: list[str] = []
    if owned.get("supersededBy") is None:
        left = remove_local_access(host, record["clientId"], record["controlPlaneUrl"])
    # Otherwise the desktop application has its own connection here: the apps' settings are its.
    for name in TERMINAL_FILES:
        remove_file(client_root(host) / name)
    return {"revocation": revocation, "left": left, "clientId": record["clientId"]}


def forget_app_connection(host: Host, owned: dict) -> dict:
    """`disconnect --without-app`: the way out when this computer is connected and the desktop
    application that holds the connection is no longer on it.

    Refused while the application is installed: its connection is live and is ended in the
    application. A credential that is not this client's own is never deleted without the
    service having been told to end it:
    * macOS: the application's Keychain item is asked for, which makes macOS ask the person;
      with the answer the connection is ended at the service and the item removed, without it
      nothing is changed;
    * Windows: the application's entry in Credential Manager cannot be read from here. It
      stays, and is no longer counted while the application is away."""
    if application_installed(host):
        raise Refusal("app_present", EXIT_REFUSED)
    client_id = owned["clientId"]
    record = read_object(terminal_path(host))
    origin = record.get("controlPlaneUrl") if valid_record(record) and record["clientId"] == client_id else host.api_origin
    token = None
    with SyncLock(host):
        if host.platform == "macos" and not keychain_ours(host, APP_KEYCHAIN_ACCOUNT):
            # The one place this client asks for a secret it did not write: said by the person,
            # with the application gone. macOS shows its own dialog; a refusal changes nothing.
            shown = host.keychain("find-generic-password", "-s", host.keychain_service, "-a", APP_KEYCHAIN_ACCOUNT, "-w")
            try:
                stored = json.loads(shown.stdout)["client"] if shown.returncode == 0 else None
                client_id, token = stored["id"], stored["clientToken"]
            except (ValueError, KeyError, TypeError):
                stored = None
            if stored is None or not CLIENT_ID.fullmatch(str(client_id)) or not valid_token(token):
                raise Refusal("keychain_not_allowed", EXIT_FAILED)
        elif client_id:
            try:
                token = load_credential(host, client_id)
            except Refusal:
                token = None
        revocation = revoke(host, client_id, origin, token) if client_id else "unrevoked"
        job = schedule_job(host, client_id) if client_id else None
        if job is not None:
            delete_schedule(host, job)
        if host.platform == "macos":
            # Its connection is ended at the service or waits on this client's own list.
            keychain_delete(host, APP_KEYCHAIN_ACCOUNT)
        else:
            remove_file(credential_path(host))
            if application_credentials(host):
                write_object(forgotten_path(host), {"version": 1, "at": iso_now()})
    left = remove_local_access(host, client_id, origin) if client_id else []
    if not client_id:
        record_disconnected(host)
    for name in TERMINAL_FILES:
        remove_file(client_root(host) / name)
    return {"revocation": revocation, "left": left, "clientId": client_id}


def revoke_new_connection(host: Host, client: dict) -> bool:
    status, _ = host.post(host.api_origin + "/v1/client-session/disconnect",
                          {"clientId": client["id"], "clientToken": client["clientToken"]})
    return disconnect_confirmed(status)


def abandon_new_connection(host: Host, client: dict, key: str, **values: object) -> Refusal:
    """A connection the service just made cannot be kept on this computer: it is ended there, and
    the person is told when even that did not work."""
    refusal = Refusal(key, **values)
    if not revoke_new_connection(host, client):
        refusal.args = (str(refusal) + "\n" + text("remove_in_console", client=client["id"]),)
    return refusal
