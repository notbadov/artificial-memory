"""What the parts of the synchronization client share and none of them owns.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the launch
guard, which imports nothing of the client itself, and every part declared after it may import it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import platform
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sync_launch_guard import replace_busy


def iso_now() -> str:
    """The current moment the way every record of this client writes it: UTC, ending in Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_private_json(path: Path, value: dict) -> None:
    """Replace a record of this client in one step, readable by its owner alone.

    Written next to the file and moved over it, so a reader never finds half a record. A
    record another program is reading at that moment is waited for (ClientFilesBusy after that)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    replace_busy(temporary, path)


def client_platform() -> str:
    value = platform.system().lower()
    return "windows" if value == "windows" else "macos" if value == "darwin" else "linux"


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


def required_client_token(args: argparse.Namespace) -> str:
    if not isinstance(args.client_token, str) or not args.client_token.strip():
        raise SystemExit("Agent Platform HTTPS sync requires a client credential")
    return args.client_token.strip()


def read_receipt(root: Path) -> dict:
    path = root / "agent-platform-install.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def skills_digest(receipt: dict) -> str:
    skills = receipt.get("skills")
    normalized = sorted(item for item in skills if isinstance(item, str)) if isinstance(skills, list) else []
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode("utf-8")).hexdigest()


def state_digest(state: dict) -> str:
    stable_state = {
        "version": state.get("version"),
        "registryDigest": state.get("registryDigest"),
        "skills": state.get("skills", []),
    }
    return hashlib.sha256(
        json.dumps(stable_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _private_write_text(path: Path, value: str, *, encoding: str = "utf-8", mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        replace_busy(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        replace_busy(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_local_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def app_chosen(desired: dict, app: str) -> bool:
    """Whether the computer's owner chose `app`: when it was connected, or later in the web
    application. An app found on the computer is no choice: nothing is written into it."""
    chosen = [*(desired.get("providers") or []), *(desired.get("appAgentProviders") or [])]
    return app in chosen


def note_app_failure(args: argparse.Namespace, app: str, reason: str) -> None:
    """An app whose config an installer left as it was; its own receipt says so."""
    failures = dict(getattr(args, "app_failures", None) or {})
    failures.setdefault(app, reason)
    args.app_failures = failures


def installer_accepts(installer: Path, option: str) -> bool:
    """Whether a program of a release knows an option.

    The client that installs a release is the one already on the computer, and it is older or
    newer than the release: newer when an organization goes back to an earlier release, and
    when a computer is connected again while a later release is on trial. So nothing a release
    is started with is taken for granted; an option it may not have is offered only when its
    own text names it."""

    try:
        return option in installer.read_text(encoding="utf-8")
    except OSError:
        return False


# The transport this run goes on with once it has replaced the client's own files.
_HELD_TRANSPORT = None


def release_transport():
    # Loaded by path from next to this file: the same file in a release and in an installed
    # client, whatever the path of the process offers.
    if _HELD_TRANSPORT is not None:
        return _HELD_TRANSPORT
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_platform_release_transport", Path(__file__).with_name("release_transport.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hold_release_transport() -> None:
    """From now on every request of this run uses the transport as it is loaded now.

    Called before a run replaces the client's own files. The file next to this one is then
    another release's, and the run still has requests to make: the skills, the report."""
    global _HELD_TRANSPORT
    if _HELD_TRANSPORT is None:
        _HELD_TRANSPORT = release_transport()
