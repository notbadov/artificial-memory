"""Whose the connection of this computer is, and how a new one is authorized.

One computer has one connection, whichever tool made it. `ownership` says whether it was made
here and is still run from here, or belongs to the desktop application, which made it or took
it over; every command that would change a connection of the application says so and stops.
A new connection is authorized in the browser, with a confirmation code shown here and typed
on the approval page.

This file is a part of `command_line.py` and lies next to it in the runtime. Of the client it
imports the messages, the host and the credential.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import platform
import re
import secrets
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from cli_credentials import application_enrollment, stored_enrollment, valid_token
from cli_host import (APPS, CLIENT_ID, EXIT_REFUSED, Host, Refusal, application_installed, client_root,
                      credential_path, iso_now, platform_root, read_object, remove_file, terminal_path,
                      unfinished_path, write_object)
from cli_messages import text

CALLBACK_URI = "agent-platform://auth/callback"

URL_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")

POLL_SECONDS = 2


# ---------------------------------------------------------------- who owns the connection


def connection_marker(host: Host) -> str | None:
    value = read_object(client_root(host) / "connection.json").get("clientId")
    return value if isinstance(value, str) and CLIENT_ID.fullmatch(value) else None


def valid_record(record: dict) -> bool:
    return (record.get("version") == 1 and isinstance(record.get("clientId"), str)
            and bool(CLIENT_ID.fullmatch(record["clientId"]))
            and isinstance(record.get("displayName"), str) and bool(record["displayName"].strip())
            and isinstance(record.get("controlPlaneUrl"), str)
            and record.get("updatePolicy") in ("automatic", "manual")
            and record.get("schedule") in ("on", "off")
            and isinstance(record.get("providers"), list) and bool(record["providers"])
            and all(provider in APPS for provider in record["providers"]))


def launch_agent_label(client_id: str) -> str:
    """The launchd label of a terminal connection's job (launch_agent_label in sync_schedule.py, terminal layout)."""
    return "com.artificial-memory.sync." + hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def terminal_record(host: Host) -> dict | None:
    """The record of a connection made here, repaired from what survives an interruption.

    Windows: a record that waited for its credential (`connect` stopped between the two) becomes
    the record once the credential is there. macOS: the Keychain item carries the whole
    enrollment, so a lost or older record is written again from it."""
    record = read_object(terminal_path(host))
    if host.platform == "windows":
        unfinished = read_object(unfinished_path(host))
        if not valid_record(record) and valid_record(unfinished) and credential_path(host).is_file():
            write_object(terminal_path(host), unfinished)
            remove_file(unfinished_path(host))
            record = unfinished
        return record if valid_record(record) else None
    stored = stored_enrollment(host)
    if stored is None:
        return record if valid_record(record) else None
    client = stored["client"]
    if valid_record(record) and record["clientId"] == client["id"]:
        return record
    # Whether the person had the schedule on is not in the Keychain: the job itself says. A
    # record rebuilt as "on" would bring back a schedule that was turned off.
    scheduled = (host.home / "Library" / "LaunchAgents" / (launch_agent_label(client["id"]) + ".plist")).is_file()
    rebuilt = {"version": 1, "clientId": client["id"], "displayName": client.get("displayName"),
               "providers": client.get("providers"), "updatePolicy": client.get("updatePolicy"),
               "controlPlaneUrl": stored.get("controlPlaneUrl"), "schedule": "on" if scheduled else "off",
               "connectedAt": iso_now()}
    if not valid_record(rebuilt):
        return None
    write_object(terminal_path(host), rebuilt)
    return rebuilt


def run_by_app(host: Host, client_id: str) -> bool:
    """The desktop application runs this connection. Two records say so: the managed client names
    who started its last run for the computer (`schedule-owner.json`, written on every run), and
    the application keeps the state of taking a client with an OS schedule over
    (`companion/migration.json`)."""
    owner = read_object(client_root(host) / "schedule-owner.json")
    if owner.get("clientId") == client_id and owner.get("scheduler") == "companion":
        return True
    state = read_object(platform_root(host) / "companion" / "migration.json")
    return (state.get("version") == 1 and state.get("legacyClientId") == client_id
            and state.get("phase") in ("adopted", "disabled", "finalized"))


def forgotten_path(host: Host) -> Path:
    return client_root(host) / "app-connection-forgotten.json"


def app_connection_forgotten(host: Host) -> bool:
    """Windows: the person removed the application and then said so here (`disconnect
    --without-app`). The application's entry in Credential Manager cannot be read or ended from
    here, so it is left where it is and no longer counted, for as long as the application stays
    away: reinstalled, it finds its entry and is the owner again."""
    return host.platform == "windows" and forgotten_path(host).is_file() and not application_installed(host)


def ownership(host: Host) -> dict:
    """Who connected this computer: {"owner": "terminal" | "app" | "none", "clientId", ...}.

    "terminal": the connection was made here and this client still runs it. `supersededBy`
    (Windows) says the application holds another connection of its own: the id when known,
    otherwise "".
    "app": the computer is connected and the connection is not this client's to change:
    the application made it, or took over one made here.
    """
    record = terminal_record(host)
    marker = connection_marker(host)
    app_holds = application_enrollment(host) and not app_connection_forgotten(host)
    if host.platform == "macos":
        # One Keychain item for both tools: it is either the application's or this client's.
        if app_holds:
            return {"owner": "app", "clientId": marker}
        if record is None:
            return {"owner": "none", "clientId": None}
        # A record whose Keychain item is gone or unreadable is still a terminal connection:
        # its job and files are here, and `disconnect` is how the person removes them.
        if run_by_app(host, record["clientId"]):
            return {"owner": "app", "clientId": record["clientId"]}
        return {"owner": "terminal", "clientId": record["clientId"], "record": record, "supersededBy": None}
    held = credential_path(host).is_file()
    if record is not None:
        # The record says whose the connection is. With its credential file lost it is still a
        # terminal connection (one that cannot synchronize; `disconnect` removes it), not the
        # application's.
        if run_by_app(host, record["clientId"]):
            return {"owner": "app", "clientId": record["clientId"]}
        other = marker if marker and marker != record["clientId"] else ("" if app_holds else None)
        return {"owner": "terminal", "clientId": record["clientId"], "record": record, "supersededBy": other}
    if app_holds or marker or held:
        return {"owner": "app", "clientId": marker}
    return {"owner": "none", "clientId": None}


def refuse_for_app(owned: dict, action: str) -> None:
    if owned["owner"] == "app":
        client = f" ({owned['clientId']})" if owned["clientId"] else ""
        raise Refusal("owned_by_app", EXIT_REFUSED, client=client, action=text("action_" + action))


def refuse_superseded(owned: dict) -> None:
    if owned.get("supersededBy") is not None:
        raise Refusal("superseded", EXIT_REFUSED, client=owned["clientId"],
                      other=owned["supersededBy"] or text("superseded_unknown"))


def require_terminal(host: Host, action: str, *, allow_superseded: bool = False) -> dict:
    owned = ownership(host)
    refuse_for_app(owned, action)
    if owned["owner"] == "none":
        raise Refusal("not_connected", EXIT_REFUSED)
    if not allow_superseded:
        refuse_superseded(owned)
    return owned


def parse_callback(value: object, expected_state: str) -> str | None:
    """The one-time code of an approved request; None for a denial or anything unexpected."""
    if not isinstance(value, str):
        return None
    url = urllib.parse.urlsplit(value)
    if (url.scheme, url.netloc, url.path, url.fragment) != ("agent-platform", "auth", "/callback", ""):
        return None
    pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
    values = dict(pairs)
    if len(pairs) != 2 or len(values) != 2 or values.get("state") != expected_state:
        return None
    code = values.get("code")
    return code if isinstance(code, str) and URL_TOKEN.fullmatch(code) else None


def user_name() -> str:
    """The account name shown to the person who approves this computer. `getpass` reads it from
    the environment and then from the password database, which Windows does not have: without
    USERNAME there it raises instead of answering."""
    try:
        return getpass.getuser() or "unknown-user"
    except (ImportError, KeyError, OSError):
        return "unknown-user"


def redemption_fault(status: int | None, redemption: dict | None) -> str | None:
    """Why the answer to a redemption cannot be used, as one of a few fixed words; None when it
    can. The word names the check that failed and never repeats anything the service sent: it
    is shown to the person, so that a failure can be told apart without a credential in sight."""
    if status is None:
        return "redeem-no-answer"
    if status != 200:
        # `status` is a number the HTTP library read: the only part of an error that is shown.
        return f"redeem-http-{int(status)}"
    client = redemption.get("client") if redemption else None
    if not isinstance(client, dict):
        return "redeem-answer"
    if not isinstance(client.get("id"), str) or not CLIENT_ID.fullmatch(client["id"]):
        return "client-id"
    if not valid_token(client.get("clientToken")):
        return "client-token"
    if client.get("updatePolicy") not in ("automatic", "manual"):
        return "client-update-policy"
    providers = client.get("providers")
    if not isinstance(providers, list) or not providers or any(provider not in APPS for provider in providers):
        return "client-providers"
    return None


def authorize(host: Host, name: str, apps: list[str], open_browser: bool) -> dict:
    """The browser authorization the desktop application uses, with the confirmation code shown
    here and typed on the approval page. Returns the redemption."""
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    base = host.api_origin + "/v1/browser-authorizations"
    status, created = host.post(base, {
        "displayName": name, "hostname": platform.node() or "unknown-device",
        "username": user_name(), "platform": host.platform, "providers": apps,
        "state": state, "codeChallenge": challenge, "codeChallengeMethod": "S256",
        "redirectUri": CALLBACK_URI, "confirmation": "code",
    })
    if status == 404:
        raise Refusal("authorization_unavailable")
    request = created.get("request") if status == 200 and created else None
    if not isinstance(request, dict) or not isinstance(request.get("id"), str) or not URL_TOKEN.fullmatch(request["id"]):
        raise Refusal("service_unavailable")
    code = request.get("confirmationCode")
    try:
        expires = datetime.fromisoformat(str(request.get("expiresAt")).replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("service_unavailable") from None
    if expires.tzinfo is None:
        # A time without a zone cannot be compared with the clock; nothing was shown or opened yet.
        host.post(f"{base}/{request['id']}/cancel", {"codeVerifier": verifier})
        raise Refusal("service_unavailable")
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        # The code is what ties the approval to this terminal; without it nothing is connected.
        host.post(f"{base}/{request['id']}/cancel", {"codeVerifier": verifier})
        raise Refusal("authorization_without_code")
    url = f"{host.console_origin}/connect/{request['id']}"
    host.out(text("confirm_in_browser", code=code, name=name, url=url))
    host.out(text("browser_opened" if open_browser and host.open_browser(url) else "browser_not_opened"))
    try:
        while True:
            host.sleep(POLL_SECONDS)
            if datetime.now(UTC) >= expires:
                raise Refusal("authorization_expired")
            status, answer = host.post(f"{base}/{request['id']}/status", {"codeVerifier": verifier})
            decision = answer.get("status") if status == 200 and answer else "pending"
            if decision == "pending":
                continue
            if decision == "approved":
                one_time = parse_callback(answer.get("callbackUri"), state)
                if one_time is None:
                    raise Refusal("authorization_lost", reason="callback")
                break
            known = {"denied": "authorization_denied", "expired": "authorization_expired",
                     "cancelled": "authorization_cancelled"}
            if decision not in known:
                raise Refusal("authorization_lost", reason="status")
            raise Refusal(known[decision])
    except KeyboardInterrupt:
        host.post(f"{base}/{request['id']}/cancel", {"codeVerifier": verifier})
        raise Refusal("authorization_cancelled") from None
    status, redemption = host.post(f"{base}/redeem", {"code": one_time, "codeVerifier": verifier})
    reason = redemption_fault(status, redemption)
    if reason:
        # The service may have made the computer and the answer was lost or unusable: this
        # computer holds no key for it, so only the person can remove it.
        raise Refusal("authorization_lost", reason=reason)
    return redemption
