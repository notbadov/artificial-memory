#!/usr/bin/env python3
"""The local status of this computer's connection, as the desktop application shows it.

Read from the records the client keeps under ``~/.agent-platform`` and the receipts in the homes
of the apps. Every value is bounded and none is a credential. It imports nothing of the client.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


STATUS_VERSION = 1
ACTIVE_RUN_VERSION = 1
MAX_JSON_BYTES = 64 * 1024
PROVIDERS = ("codex", "claude", "kimi", "opencode", "cursor")
PROVIDER_RECEIPTS = {
    "codex": Path(".codex/agent-platform-install.json"),
    "claude": Path(".claude/agent-platform-install.json"),
    "kimi": Path(".kimi/agent-platform-install.json"),
    "opencode": Path(".config/opencode/agent-platform-install.json"),
    # Written by adapters/cursor/install.py inside the platform's own directory in ~/.cursor.
    "cursor": Path(".cursor/agent-platform/install.json"),
}
CLIENT_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
PHASES = {"prepared", "enrolled", "installed", "installation_failed"}


def _read_object(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        value = json.loads(raw) if len(raw) <= MAX_JSON_BYTES else None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _platform_name(value: str | None = None) -> str:
    system = (value or platform.system()).lower()
    if system in {"windows", "macos", "linux"}:
        return system
    if system == "darwin":
        return "macos"
    return "linux"


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum or "\n" in value or "\r" in value:
        return None
    return value


def _safe_control_plane_url(value: object) -> str | None:
    text = _bounded_text(value, 512)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9T:.+Z-]{20,40}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _connection(root: Path) -> tuple[dict, dict]:
    owner = _read_object(root / "onboarding/owner.json")
    session = _read_object(root / "onboarding/session.json")
    client = session.get("client") if isinstance(session.get("client"), dict) else {}
    owner_client_id = _bounded_text(owner.get("clientId"), 64)
    session_client_id = _bounded_text(client.get("id"), 64)
    client_id = owner_client_id or session_client_id
    if client_id is not None and CLIENT_ID_PATTERN.fullmatch(client_id) is None:
        client_id = None
    url = _safe_control_plane_url(owner.get("url") or session.get("url"))
    phase = session.get("phase") if session.get("phase") in PHASES else None
    client_matches = not owner_client_id or not session_client_id or owner_client_id == session_client_id
    owned = (
        owner.get("protocol") == "control-plane-v1"
        and isinstance(client_id, str)
        and isinstance(url, str)
        and client_matches
    )
    if owned and phase == "installed":
        status = "connected"
    elif owned or session or owner:
        status = "action_required" if phase == "installation_failed" or not client_matches else "connecting"
    else:
        status = "not_connected"
    connection = {
        "status": status,
        "clientId": client_id if isinstance(client_id, str) else None,
        "displayName": _bounded_text(client.get("displayName"), 160) if client_matches else None,
        "controlPlaneUrl": url if isinstance(url, str) else None,
        "phase": phase,
    }
    if owner or session:
        return connection, client if client_matches else {}

    # Clients installed before browser-based onboarding have no owner/session
    # files. Their successful authenticated run and scheduler receipts are the
    # credential-free migration evidence that the machine is already linked.
    last = _read_object(root / "client/last-run.json")
    task = _read_object(root / "client/scheduled-task.json")
    last_id = _bounded_text(last.get("clientId"), 64)
    task_id = _bounded_text(task.get("clientId"), 64)
    last_valid = (
        last.get("version") == 1
        and isinstance(last_id, str)
        and CLIENT_ID_PATTERN.fullmatch(last_id) is not None
        and last.get("status") in {"succeeded", "failed"}
        and _iso(last.get("completedAt")) is not None
    )
    task_valid = (
        task.get("version") == 1
        and isinstance(task_id, str)
        and CLIENT_ID_PATTERN.fullmatch(task_id) is not None
        and _bounded_text(task.get("launcher"), 80) is not None
        and _iso(task.get("updatedAt")) is not None
    )
    if last_valid and task_valid and last_id != task_id:
        return {
            "status": "action_required",
            "clientId": None,
            "displayName": None,
            "controlPlaneUrl": None,
            "phase": None,
        }, {}
    legacy_client_id = last_id if last_valid else task_id if task_valid else None
    if legacy_client_id is None:
        return connection, {}
    providers = task.get("providers") if isinstance(task.get("providers"), list) else []
    legacy_client = {
        "id": legacy_client_id,
        "displayName": _bounded_text(task.get("displayName"), 160),
        "providers": [provider for provider in providers if provider in PROVIDERS],
    }
    legacy_status = (
        "connected" if last_valid and last.get("status") == "succeeded"
        else "action_required" if last_valid
        else "connecting"
    )
    return {
        "status": legacy_status,
        "clientId": legacy_client_id,
        "displayName": legacy_client["displayName"],
        "controlPlaneUrl": _safe_control_plane_url(task.get("controlPlaneUrl")),
        "phase": "installed" if legacy_status == "connected" else None,
    }, legacy_client


def _sync(root: Path, client_id: str | None, now: datetime, stale_after_seconds: int) -> dict:
    active = _read_object(root / "client/current-run.json")
    started = _iso(active.get("startedAt"))
    active_matches = (
        active.get("version") == ACTIVE_RUN_VERSION
        and isinstance(active.get("pid"), int)
        and active.get("pid") > 0
        and active.get("clientId") == client_id
        and started is not None
    )
    if active_matches:
        age = (now - started).total_seconds()
        clock_invalid = age < -300
        interrupted = clock_invalid or age > stale_after_seconds
        return {
            "status": "interrupted" if interrupted else "running",
            "startedAt": active["startedAt"],
            "completedAt": None,
            "code": "invalid_run_marker_time" if clock_invalid else "stale_run_marker" if interrupted else "syncing",
            "action": "retry_sync" if interrupted else None,
        }
    last = _read_object(root / "client/last-run.json")
    completed = _iso(last.get("completedAt"))
    valid = (
        last.get("version") == 1
        and last.get("clientId") == client_id
        and last.get("status") in {"succeeded", "failed"}
        and completed is not None
    )
    if not valid:
        return {"status": "never", "startedAt": None, "completedAt": None, "code": None, "action": None}
    code_value = _bounded_text(last.get("code"), 64)
    code = code_value if code_value and re.fullmatch(r"[a-z0-9_]+", code_value) else None
    return {
        "status": last["status"],
        "startedAt": None,
        "completedAt": last["completedAt"],
        "code": code,
        "action": None if last["status"] == "succeeded" else "open_control_plane",
    }


def _background(root: Path, client_id: str | None, system: str, connected: bool) -> dict:
    receipt = _read_object(root / "client/scheduled-task.json")
    valid = (
        receipt.get("version") == 1
        and receipt.get("clientId") == client_id
        and _bounded_text(receipt.get("launcher"), 80) is not None
        and _iso(receipt.get("updatedAt")) is not None
    )
    if valid:
        status = "enabled"
    elif not connected:
        status = "not_configured"
    elif system == "linux":
        status = "unsupported"
    else:
        status = "action_required"
    return {
        "status": status,
        "launcher": _bounded_text(receipt.get("launcher"), 80) if valid else None,
        "updatedAt": receipt.get("updatedAt") if valid else None,
    }


def _provider_status(home: Path, client: dict) -> list[dict]:
    selected = client.get("providers") if isinstance(client.get("providers"), list) else []
    infer_from_receipts = not selected
    candidates = PROVIDERS if infer_from_receipts else selected
    result = []
    for provider in candidates:
        if provider not in PROVIDERS or any(item["id"] == provider for item in result):
            continue
        receipt = _read_object(home / PROVIDER_RECEIPTS[provider])
        release_value = _bounded_text(receipt.get("release_id"), 128)
        release_id = release_value if release_value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_value) else None
        sha_value = _bounded_text(receipt.get("agent_platform_sha"), 64)
        sha = sha_value if sha_value and re.fullmatch(r"[a-f0-9]{40,64}", sha_value) else None
        skills = receipt.get("skills") if isinstance(receipt.get("skills"), list) else None
        installed = release_id is not None and sha is not None
        if infer_from_receipts and not installed:
            continue
        result.append({
            "id": provider,
            "status": "installed" if installed else "not_installed",
            "releaseId": release_id,
            "agentPlatformSha": sha,
            "skillCount": len(skills) if skills is not None else None,
        })
    return result


def collect_status(
    root: Path | None = None,
    *,
    home: Path | None = None,
    system: str | None = None,
    now: datetime | None = None,
    stale_after_seconds: int = 7200,
) -> dict:
    home = (home or Path.home()).resolve()
    root = (root or (home / ".agent-platform")).resolve()
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    platform_name = _platform_name(system)
    connection, client = _connection(root)
    client_id = connection["clientId"]
    return {
        "version": STATUS_VERSION,
        "generatedAt": timestamp.isoformat().replace("+00:00", "Z"),
        "platform": platform_name,
        "connection": connection,
        "sync": _sync(root, client_id, timestamp, stale_after_seconds),
        "background": _background(root, client_id, platform_name, connection["status"] == "connected"),
        "providers": _provider_status(home, client),
    }


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


def main() -> int:
    utf8_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--platform", choices=("windows", "macos", "linux"))
    args = parser.parse_args()
    print(json.dumps(collect_status(args.root, home=args.home, system=args.platform), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
