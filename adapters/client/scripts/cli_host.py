"""The computer as the terminal client sees it, and the files it keeps there.

`Host` is everything outside the process: the terminal, the network, other programs, the OS.
Tests replace it; the product uses it as it is. A command that cannot go on stops with a
`Refusal`, which carries a message for the person and nothing sensitive. Below them: where
the client's files are under the person's home, and how one is written so that nobody else
can read it and no reader ever finds half of it.

This file is a part of `command_line.py` and lies next to it in the runtime. Of the client it
imports the messages.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from cli_messages import text

DEFAULT_API_ORIGIN = "https://app.artificial-memory.com/agent-api"
DEFAULT_CONSOLE_ORIGIN = "https://app.artificial-memory.com"

APPS = ("codex", "claude", "kimi", "opencode", "cursor")

TOKEN_VARIABLE = "AGENT_PLATFORM_CLIENT_TOKEN"
CLIENT_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")

# The Keychain item of the desktop application's enrollment, shared with it on a Mac.
APP_KEYCHAIN_SERVICE = "com.artificial-memory.agent-platform"
APP_KEYCHAIN_ACCOUNT = "managed-client"

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


class Refusal(Exception):
    """A command that stops with a message for the person; nothing sensitive is in it."""

    def __init__(self, key: str, exit_code: int = EXIT_FAILED, **values: object):
        super().__init__(text(key, **values))
        self.key, self.exit_code = key, exit_code


def client_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    raise Refusal("unsupported_platform")


def windows_system_tool(*parts: str) -> str:
    """The full path of a program of Windows itself, under System32 of this installation.

    Windows looks for a program named without a path in the directory of the process that
    starts it before it looks in PATH, so a `schtasks.exe` left there would run instead of the
    real one, in places with a secret in its environment. Without a SystemRoot that names a
    directory on a drive the system directory is unknown, and nothing is started. Every set of
    files delivered on its own carries this function word for word
    (tests/test_client_without_posix.py)."""
    root = os.environ.get("SystemRoot") or ""
    if not ntpath.isabs(root) or not ntpath.splitdrive(root)[0]:
        raise OSError("SystemRoot does not name the Windows directory; a system tool is not started")
    return ntpath.join(root, "System32", *parts)


def checked_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise Refusal("bad_origin")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Host:
    """Everything outside this process: the network, the terminal, other programs, the OS.

    Tests replace it; the product uses it as it is."""

    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()
        self.platform = client_platform()
        self.python = sys.executable
        self.keychain_service = APP_KEYCHAIN_SERVICE
        self.api_origin = checked_origin(os.environ.get("AGENT_PLATFORM_API_ORIGIN", DEFAULT_API_ORIGIN))
        self.console_origin = checked_origin(os.environ.get("AGENT_PLATFORM_CONSOLE_ORIGIN", DEFAULT_CONSOLE_ORIGIN))

    # -- terminal
    def out(self, message: str) -> None:
        print(message, flush=True)

    def interactive(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # -- network
    def post(self, url: str, payload: dict, timeout: float = 20) -> tuple[int | None, dict | None]:
        """(HTTP status, JSON object) of a POST; (None, None) when the service did not answer.
        A response body is returned only for a success: an error body is never shown."""
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json", "accept": "application/json"}, method="POST")
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
                raw = response.read(256 * 1024 + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return None, None
        try:
            value = json.loads(raw) if len(raw) <= 256 * 1024 else None
        except ValueError:
            value = None
        return status, value if isinstance(value, dict) else None

    # -- other programs
    def environment(self, token: str | None = None) -> dict:
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("PYTHON") and key != TOKEN_VARIABLE}
        if token:
            environment[TOKEN_VARIABLE] = token
        return environment

    def run(self, command: list[str], *, token: str | None = None, extra: dict | None = None,
            timeout: float | None = None, stdin: str | None = None,
            without: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        options: dict = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        if stdin is None:
            options["stdin"] = subprocess.DEVNULL
        environment = {key: value for key, value in self.environment(token).items()
                       if key.upper() not in {name.upper() for name in without}}
        return subprocess.run(command, env={**environment, **(extra or {})}, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", cwd=self.home, check=False,
                              timeout=timeout, input=stdin, **options)

    def keychain(self, *arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        """`/usr/bin/security` on the login Keychain of the person who runs this."""
        return self.run(["/usr/bin/security", *arguments], timeout=30, stdin=stdin)

    def open_browser(self, url: str) -> bool:
        try:
            if self.platform == "macos":
                return self.run(["/usr/bin/open", url], timeout=20).returncode == 0
            # rundll32 takes the address as one argument; `start` would pass it through cmd.exe.
            return self.run([windows_system_tool("rundll32.exe"), "url.dll,FileProtocolHandler", url],
                            timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def user_id(self) -> int:
        return os.getuid()

    def application_bundles(self) -> list[Path]:
        """Where the desktop application is installed when it is on this computer."""
        if self.platform == "macos":
            return [Path("/Applications/Artificial Memory.app"), self.home / "Applications" / "Artificial Memory.app"]
        local = os.environ.get("LOCALAPPDATA")
        return [Path(local) / name / "Artificial Memory" for name in ("", "Programs")] if local else []


# ---------------------------------------------------------------- local files


def platform_root(host: Host) -> Path:
    return host.home / ".agent-platform"


def client_root(host: Host) -> Path:
    return platform_root(host) / "client"


def read_object(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        value = json.loads(raw) if len(raw) <= 1024 * 1024 else None
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_object(path: Path, value: dict) -> None:
    private_write(path, json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def application_installed(host: Host) -> bool:
    """Whether the desktop application is on this computer: its bundle is where it installs."""
    return any(path.exists() for path in host.application_bundles())


def application_was_here(host: Host) -> bool:
    """The application is, or was, on this computer: its bundle, or the state it keeps under
    `~/.agent-platform/companion`. The runtime, the synchronization client and the release
    cache are its files as much as this client's."""
    return application_installed(host) or (platform_root(host) / "companion").exists()


def credential_path(host: Host) -> Path:
    """Windows only: the DPAPI file the managed client's launcher reads."""
    return client_root(host) / "credential.dpapi"


def terminal_path(host: Host) -> Path:
    return client_root(host) / "terminal.json"


def unfinished_path(host: Host) -> Path:
    """Windows: the record of a connection whose credential is being saved; see `connect`."""
    return client_root(host) / "terminal.json.partial"


def revocations_path(host: Host) -> Path:
    return client_root(host) / "terminal-revocations.json"


def managed_script(host: Host) -> Path:
    return client_root(host) / "sync_client.py"


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
