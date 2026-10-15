#!/usr/bin/env python3
"""Tell Artificial Memory that this AI app thread is alive.

Run by the app's own hooks (Claude Code, Codex, Cursor). It reports the thread's start, a heartbeat when the person
sends a prompt or the app finishes a turn, and the end. It sends the repository's remote
address, branch and commit; never a prompt, an answer, a file, or a local path.

A hook must never get in the way of the person's work, so this script:
- waits a few seconds at most and gives up silently;
- prints only what the app expects from a hook and always exits 0;
- does nothing on a computer that is not connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TIMEOUT_SECONDS = 4
GIT_TIMEOUT_SECONDS = 2
# A heartbeat extends a 15-minute lease; one a minute is more than enough.
HEARTBEAT_SECONDS = 60
STATE_KEEP_SECONDS = 7 * 86_400
END_EVENTS = {"SessionEnd"}
LIVE_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop"}
# Cursor's hooks name the same moments in camelCase (https://cursor.com/docs/agent/hooks).
CURSOR_EVENTS = {"sessionStart": "SessionStart", "beforeSubmitPrompt": "UserPromptSubmit",
                 "stop": "Stop", "sessionEnd": "SessionEnd"}
def runs_inside_cursor(event: object) -> bool:
    """Whether Cursor started this hook. Cursor loads Claude Code's hooks too
    (https://cursor.com/docs/reference/third-party-hooks), and a Claude hook it runs must not
    count Cursor's conversation as a Claude Code session.

    Cursor names itself in the input of every hook it runs, `cursor_version` ("Common input
    fields", https://cursor.com/docs/agent/hooks). `conversation_id` and `generation_id` come
    with it, but those are generic names another tool may send, so they decide nothing alone."""
    version = event.get("cursor_version") if isinstance(event, dict) else None
    return isinstance(version, str) and bool(version.strip())


def cursor_position(event: dict) -> str | None:
    """The project of a Cursor conversation: its first workspace root. A user hook runs from
    ~/.cursor, so the directory the hook was started in says nothing about the project."""
    roots = event.get("workspace_roots")
    first = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else None
    return first or os.environ.get("CURSOR_PROJECT_DIR") or None


def state_root(home: Path) -> Path:
    return home / ".agent-platform" / "work-sessions"


def git(cwd: str, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def without_credentials(remote: str | None) -> str | None:
    """A remote may carry a user name or a token (https://user:token@host/…); neither is sent."""
    if not remote:
        return None
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]+@", r"\1", remote, flags=re.IGNORECASE)


def position(cwd: str) -> dict[str, str]:
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    facts = {
        "repositoryUrl": without_credentials(git(cwd, "remote", "get-url", "origin")),
        # A detached HEAD has no branch name.
        "branch": None if branch == "HEAD" else branch,
        "headSha": git(cwd, "rev-parse", "HEAD"),
    }
    return {key: value for key, value in facts.items() if value}


def post(url: str, token: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - https only, checked by the caller
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        return error.code, {}


class Reporter:
    def __init__(self, provider: str, home: Path, identity: Any) -> None:
        self.provider, self.identity = provider, identity
        self.root = state_root(home) / provider
        self.url = str(identity.identity().get("workSessionUrl", ""))

    def path(self, thread_id: str) -> Path:
        return self.root / (hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32] + ".json")

    def load(self, thread_id: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path(thread_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) and isinstance(value.get("sessionId"), str) else None

    def save(self, thread_id: str, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.path(thread_id)
        staged = target.with_suffix(".tmp")
        staged.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(staged, 0o600)
        staged.replace(target)

    def forget(self, thread_id: str) -> None:
        self.path(thread_id).unlink(missing_ok=True)

    def prune(self, now: float) -> None:
        """Threads that ended without telling leave a file behind; Runtime closes them itself."""
        try:
            for path in self.root.glob("*.json"):
                if now - path.stat().st_mtime > STATE_KEEP_SECONDS:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def send(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, body = post(self.url, self.identity.access_token(), payload)
        if status == 401:
            status, body = post(self.url, self.identity.access_token(force_refresh=True), payload)
        return status, body

    def report(self, event_name: str, thread_id: str, cwd: str, now: float) -> None:
        if not self.url.startswith("https://"):
            return
        state = self.load(thread_id)
        if event_name in END_EVENTS:
            if state is not None:
                self.send({"action": "end", "sessionId": state["sessionId"], "reason": "completed"})
                self.forget(thread_id)
            return
        if event_name not in LIVE_EVENTS:
            return
        facts = position(cwd)
        if state is not None:
            moved = (state.get("branch"), state.get("headSha")) != (facts.get("branch"), facts.get("headSha"))
            if not moved and now - float(state.get("beatAt", 0)) < HEARTBEAT_SECONDS:
                return
            status, _ = self.send({"action": "heartbeat", "sessionId": state["sessionId"], **{
                key: facts[key] for key in ("branch", "headSha") if key in facts
            }})
            if status == 200:
                self.save(thread_id, {**state, "beatAt": now, "branch": facts.get("branch"), "headSha": facts.get("headSha")})
                return
            if status != 409:
                return
            # The session ended on the server (it was silent too long): start again below.
            self.forget(thread_id)
        status, body = self.send({"action": "start", "threadId": thread_id, **facts})
        session = body.get("session") if status == 200 else None
        if isinstance(session, dict) and isinstance(session.get("id"), str):
            self.save(thread_id, {
                "sessionId": session["id"], "beatAt": now, "branch": facts.get("branch"), "headSha": facts.get("headSha"),
            })
            self.prune(now)


def load_identity(provider: str, home: Path) -> Any:
    """The app's agent identity, kept by the managed client. Missing on an unconnected computer."""
    sys.path.insert(0, str(home / ".agent-platform" / "client"))
    from agent_identity import AppAgentIdentity  # noqa: PLC0415 - optional at run time

    return AppAgentIdentity(provider, home=home)


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


def main(argv: list[str] | None = None) -> int:
    utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("claude", "codex", "cursor"))
    args = parser.parse_args(argv)
    # What the app expects from a hook that has nothing to say.
    answer: dict[str, Any] = {"continue": True} if args.provider == "codex" else {}
    try:
        # PowerShell, which runs Cursor's hooks on Windows, may put a byte order mark before the input.
        event = json.loads(sys.stdin.read().lstrip("\ufeff"))
        if args.provider != "cursor" and runs_inside_cursor(event):
            # Another app's hook that Cursor loaded: Cursor's own hook reports this conversation.
            print(f"Artificial Memory: this {args.provider} work-session hook runs inside Cursor and records "
                  "nothing; Cursor's own hook reports Cursor's sessions.", file=sys.stderr)
        elif args.provider == "cursor":
            name = str(event.get("hook_event_name", ""))
            if name == "beforeSubmitPrompt":
                # Cursor may hold a prompt back on this hook's word; it is told to send it.
                answer = {"continue": True}
            thread_id = str(event.get("conversation_id") or "")
            if thread_id and name in CURSOR_EVENTS:
                home = Path.home()
                Reporter(args.provider, home, load_identity(args.provider, home)).report(
                    CURSOR_EVENTS[name], thread_id, cursor_position(event) or os.getcwd(), time.time(),
                )
        else:
            thread_id = str(event.get("session_id") or event.get("thread_id") or "")
            if thread_id and isinstance(event, dict):
                home = Path.home()
                Reporter(args.provider, home, load_identity(args.provider, home)).report(
                    str(event.get("hook_event_name", "")), thread_id, str(event.get("cwd") or os.getcwd()), time.time(),
                )
    except BaseException:  # noqa: BLE001 - a hook never fails the person's work, whatever happened
        pass
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
