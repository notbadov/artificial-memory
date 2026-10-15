"""The credential of a connection made in the terminal, and the stores it is kept in.

On a Mac it is the Keychain item the desktop application keeps its own enrollment in, so either
tool finds the connection the other made; on Windows it is a file sealed with DPAPI for the person
who is signed in, in the format the launcher of the hourly job reads. The secret of an item
this client did not create is never asked for here: such a request opens a system dialog.

The credential is never printed, logged or passed in arguments.

This file is a part of `command_line.py` and lies next to it in the runtime. Of the client it
imports the host.
"""

from __future__ import annotations

import json
import re
import subprocess

from cli_host import (APP_KEYCHAIN_ACCOUNT, APP_KEYCHAIN_SERVICE, CLIENT_ID, Host, Refusal, client_root,
                      credential_path, private_write, remove_file, windows_system_tool)

# The kind attribute of every Keychain item this client creates. It is how the client tells its
# own items from the application's without asking for a secret.
KEYCHAIN_KIND = "artificial-memory-terminal"
# `security -i` reads commands in lines of 4096 bytes.
SECURITY_LINE_LIMIT = 4000


# ---------------------------------------------------------------- the credential


def dpapi(host: Host, value: str, *, decrypt: bool) -> str:
    """Windows protection of the current user, in the format the managed client's launcher reads.
    The secret reaches PowerShell only in its environment."""
    module = ('$module=Join-Path $PSHOME "Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1"; '
              "Import-Module $module -Force; ")
    code = '$ErrorActionPreference="Stop"; ' + module + (
        "$s=ConvertTo-SecureString $env:AGENT_PLATFORM_CLIENT_SECRET; "
        "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
        "try {[Console]::Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} "
        "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
        if decrypt else
        "$s=ConvertTo-SecureString $env:AGENT_PLATFORM_CLIENT_SECRET -AsPlainText -Force; "
        "[Console]::Write(($s | ConvertFrom-SecureString))")
    executable = windows_system_tool("WindowsPowerShell", "v1.0", "powershell.exe")
    # Without an inherited module path, as the managed client seals the same file: a module of
    # that name somewhere else on the path would otherwise be the one that sees the secret.
    result = host.run([executable, "-NoProfile", "-NonInteractive", "-Command", code],
                      extra={"AGENT_PLATFORM_CLIENT_SECRET": value}, without=("PSModulePath",), timeout=30)
    if result.returncode or not result.stdout.strip():
        raise OSError("DPAPI")
    return result.stdout.strip()


def valid_token(value: object) -> bool:
    # The same bounds the desktop application applies when it takes this connection over.
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(character.isspace() for character in value)


def keychain_attributes(host: Host, account: str) -> str | None:
    """What the Keychain shows of an item without its secret; None when there is no item.
    The secret is not requested, so no system dialog opens, whoever made the item."""
    try:
        result = host.keychain("find-generic-password", "-s", host.keychain_service, "-a", account)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def keychain_exists(host: Host, account: str) -> bool:
    return keychain_attributes(host, account) is not None


def keychain_ours(host: Host, account: str) -> bool:
    """Whether this client created the item. Only then may its secret be asked for: an item the
    application created answers such a request with a system dialog."""
    attributes = keychain_attributes(host, account)
    return attributes is not None and f'"desc"<blob>="{KEYCHAIN_KIND}"' in attributes


def keychain_write(host: Host, account: str, value: str) -> None:
    """Creates an item, or replaces one this client created. The value travels on standard input
    as hexadecimal, never in arguments; an item `security` created is read back by it, and by
    the hourly launcher, without a dialog."""
    if keychain_exists(host, account) and not keychain_ours(host, account):
        raise OSError("Keychain item of another program")
    command = (f"add-generic-password -U -s {host.keychain_service} -a {account} -D {KEYCHAIN_KIND} "
               f"-X {value.encode('ascii').hex()}\n")
    # `security -i` reads lines of at most 4096 bytes and runs the rest as another command.
    if len(command) > SECURITY_LINE_LIMIT:
        raise OSError("Keychain value too long")
    result = host.keychain("-i", stdin=command)
    if result.returncode != 0 or keychain_read(host, account) != value:
        raise OSError("Keychain")


def keychain_read(host: Host, account: str) -> str | None:
    """The secret of an item this client created; None for any other item or none."""
    if not keychain_ours(host, account):
        return None
    result = host.keychain("find-generic-password", "-s", host.keychain_service, "-a", account, "-w")
    return result.stdout.strip() if result.returncode == 0 else None


def keychain_delete(host: Host, account: str) -> None:
    host.keychain("delete-generic-password", "-s", host.keychain_service, "-a", account)


def protect(host: Host, token: str) -> str:
    return dpapi(host, token, decrypt=False) if host.platform == "windows" else token


def reveal(host: Host, stored: str) -> str:
    return dpapi(host, stored, decrypt=True) if host.platform == "windows" else stored


def stored_enrollment(host: Host) -> dict | None:
    """macOS: the enrollment this client wrote into the Keychain, when it is still the one there."""
    try:
        stored = json.loads(keychain_read(host, APP_KEYCHAIN_ACCOUNT) or "")
        client = stored["client"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    if (stored.get("source") != "terminal" or not isinstance(client, dict)
            or not isinstance(client.get("id"), str) or not CLIENT_ID.fullmatch(client["id"])):
        return None
    return stored


def application_enrollment(host: Host) -> bool:
    """Whether the desktop application holds an enrollment of its own in the system credential store.
    It is there from the moment the application connects, before its first synchronization
    writes anything into the shared directory."""
    if host.platform == "macos":
        return keychain_exists(host, APP_KEYCHAIN_ACCOUNT) and not keychain_ours(host, APP_KEYCHAIN_ACCOUNT)
    # Windows Credential Manager names the item by the service and the account together.
    return any(APP_KEYCHAIN_ACCOUNT in target for target in application_credentials(host))


def application_credentials(host: Host) -> list[str]:
    """Windows: the Credential Manager targets of the desktop application's own items, as `cmdkey` names them."""
    try:
        listed = host.run([windows_system_tool("cmdkey.exe"), "/list"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    targets = []
    for line in listed.stdout.splitlines():
        found = re.search(r"target=(\S+)", line)
        if found and APP_KEYCHAIN_SERVICE in found.group(1):
            targets.append(found.group(1))
    return targets


def save_credential(host: Host, redemption: dict, trust: dict) -> None:
    client = redemption["client"]
    if host.platform == "windows":
        client_root(host).mkdir(parents=True, exist_ok=True)
        private_write(credential_path(host), protect(host, client["clientToken"]) + "\n")
        return
    # The record the desktop application stores after its own sign-in (it reads client.id,
    # clientToken, displayName, providers, updatePolicy and releaseTrust), so it can take this
    # connection over by reading it. Only that is kept, with the release keys already narrowed
    # to the ones this client was built with. ASCII, which `security -w` prints as it is.
    record = {"client": {name: client[name] for name in ("id", "clientToken", "displayName", "providers", "updatePolicy")},
              "credentials": {}, "releaseTrust": trust, "source": "terminal", "controlPlaneUrl": host.api_origin}
    keychain_write(host, APP_KEYCHAIN_ACCOUNT, json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def load_credential(host: Host, client_id: str) -> str:
    try:
        if host.platform == "windows":
            token = reveal(host, credential_path(host).read_text(encoding="utf-8").strip())
        else:
            stored = stored_enrollment(host)
            token = stored["client"]["clientToken"] if stored and stored["client"]["id"] == client_id else None
    except (OSError, subprocess.SubprocessError, UnicodeError, KeyError, TypeError):
        raise Refusal("credential_unreadable", client=client_id) from None
    if not valid_token(token):
        raise Refusal("credential_unreadable", client=client_id)
    return token


def delete_credential(host: Host) -> None:
    """Removes the credential this client wrote. The application's own Keychain item is never
    removed here: it is not this client's to end."""
    if host.platform == "windows":
        remove_file(credential_path(host))
    elif keychain_ours(host, APP_KEYCHAIN_ACCOUNT):
        keychain_delete(host, APP_KEYCHAIN_ACCOUNT)
