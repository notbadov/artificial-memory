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


def session_id(event: dict[str, Any]) -> str | None:
    raw = event.get("session_id")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw)) if raw else None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    event_name = str(event.get("hook_event_name", ""))
    if event_name == "SessionStart":
        mode = os.environ.get("AGENT_PLATFORM_MODE")
        if mode:
            print(f"Semantic mode: {mode}. Keep all quality gates defined by agent-platform/shared/modes.")
        return
    if event_name == "UserPromptSubmit":
        prompt = str(event.get("prompt", ""))
        if defect_learning_installed() and any(pattern.search(prompt) for pattern in STRONG_DEFECT_PATTERNS):
            print(
                "This appears to be a confirmed escaped or repeated defect. If the task includes fixing it, "
                "use the learn-from-defect skill and establish its completion artifacts before implementation."
            )
        return
    if event_name == "Stop" and not event.get("stop_hook_active"):
        session = session_id(event)
        if not session:
            return
        try:
            state = json.loads((state_root() / f"{session}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if state.get("status") == "pending":
            print(
                "The defect-learning gate is still pending. Add root-cause evidence, regression evidence, "
                "the preventive control, and a reviewable Agent Memory proposal before finishing.",
                file=sys.stderr,
            )
            raise SystemExit(2)


if __name__ == "__main__":
    main()
