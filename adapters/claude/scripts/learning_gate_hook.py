#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


STRONG_DEFECT_PATTERNS = (
    re.compile(r"(заказчик|клиент|пользователь).{0,100}(наш[её]л|сообщил|проверил|ошиб|баг|слом)", re.I | re.S),
    re.compile(r"(опять|снова|повтор).{0,100}(ошиб|баг|слом|не работает)", re.I | re.S),
    re.compile(r"(недопровер|не проверил).{0,100}(прод|production|релиз|выклад)", re.I | re.S),
    re.compile(r"(customer|client|user).{0,100}(found|reported|verified).{0,60}(bug|error|broken|failure)", re.I | re.S),
    re.compile(r"(escaped defect|regression|recurred after release)", re.I),
)


def runs_inside_cursor(event: object) -> bool:
    """Cursor loads Claude Code's hooks (https://cursor.com/docs/reference/third-party-hooks).
    This gate speaks Claude Code: its `decision: block` becomes a follow-up message in Cursor,
    without the loop limit Cursor's own hooks have, and its session start writes Claude Code's
    environment file. In Cursor it stays silent. Cursor is known by `cursor_version` in the
    input ("Common input fields", https://cursor.com/docs/agent/hooks), the same test as
    adapters/common/work_session_hook.py, which is delivered apart from this file."""
    version = event.get("cursor_version") if isinstance(event, dict) else None
    return isinstance(version, str) and bool(version.strip())


def defect_learning_installed() -> bool:
    """Whether this app has the learn-from-defect skill.

    The skill is optional. Without it the hook stays silent about defects: a person who did not
    choose the skill is not told to use it, and nothing can arm its completion gate.
    """

    here = Path(__file__).resolve()
    roots = [here.parents[2] / "skills", here.parents[1] / "skills"]
    override = os.environ.get("AGENT_SKILLS_ROOT")
    if override:
        roots.insert(0, Path(override).expanduser())
    return any((root / "learn-from-defect" / "SKILL.md").is_file() for root in roots)


def state_root() -> Path:
    override = os.environ.get("AGENT_LEARNING_GATE_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".agent-platform" / "learning-loop" / "state"


def session_id(event: dict[str, Any]) -> str:
    raw = event.get("session_id")
    if not raw:
        raise ValueError("session_id is missing")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw))


def read_state(event: dict[str, Any]) -> dict[str, Any] | None:
    path = state_root() / f"{session_id(event)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def session_start_context(event: dict[str, Any]) -> dict[str, Any]:
    session = session_id(event)
    claude_root = Path.home() / ".claude"
    environment_file = os.environ.get("CLAUDE_ENV_FILE")
    if environment_file:
        with Path(environment_file).open("a", encoding="utf-8") as handle:
            handle.write(f"export AGENT_SESSION_ID={session}\n")
            handle.write(f"export AGENT_SKILLS_ROOT={claude_root / 'skills'}\n")
            handle.write(f"export AGENT_LEARNING_GATE_ROOT={state_root()}\n")
    mode_name = os.environ.get("AGENT_PLATFORM_MODE")
    mode_context = "No semantic mode was selected; use normal Claude Code defaults and all required quality gates."
    if mode_name:
        mode_path = claude_root / "agent-platform" / "modes" / f"{mode_name}.json"
        try:
            mode = json.loads(mode_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            mode_context = f"Semantic mode {mode_name!r} was requested but its installed contract is unavailable."
        else:
            mode_context = (
                f"Semantic mode: {mode.get('name')}. Quality gates: "
                f"{json.dumps(mode.get('quality_gates', {}), ensure_ascii=False)}."
            )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": mode_context,
        }
    }


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("{}")
        return
    if runs_inside_cursor(event):
        print("Artificial Memory: the Claude Code learning gate runs inside Cursor and does nothing there.",
              file=sys.stderr)
        print("{}")
        return
    event_name = str(event.get("hook_event_name", ""))
    if event_name == "SessionStart":
        try:
            print(json.dumps(session_start_context(event), ensure_ascii=False))
        except ValueError:
            print("{}")
        return
    if event_name == "UserPromptSubmit":
        prompt = str(event.get("prompt", ""))
        if defect_learning_installed() and any(pattern.search(prompt) for pattern in STRONG_DEFECT_PATTERNS):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "This appears to be a confirmed escaped or repeated defect. If the task includes fixing it, "
                        "invoke /learn-from-defect and establish its completion artifacts before implementation."
                    ),
                }
            }, ensure_ascii=False))
            return
    if event_name == "Stop" and not event.get("stop_hook_active"):
        try:
            state = read_state(event)
        except ValueError:
            state = None
        if state and state.get("status") == "pending":
            print(json.dumps({
                "decision": "block",
                "reason": (
                    "The defect-learning gate is still pending. Add root-cause evidence, regression evidence, "
                    "the preventive control, and a reviewable Agent Memory proposal before finishing."
                ),
            }, ensure_ascii=False))
            return
    print("{}")


if __name__ == "__main__":
    main()
