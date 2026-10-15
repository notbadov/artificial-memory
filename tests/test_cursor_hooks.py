"""Work-session hooks in Cursor, and Claude Code's hooks when Cursor runs them.

Cursor's own hooks (`~/.cursor/hooks.json`) start the shared work-session script with
`--provider cursor`: events in camelCase, the thread is `conversation_id`, and the repository is
the first workspace root (a user hook runs from ~/.cursor, not from the project).

Cursor can also load Claude Code's hooks (https://cursor.com/docs/reference/third-party-hooks).
Every hook input Cursor sends carries `cursor_version`, `conversation_id` and `generation_id`
(https://cursor.com/docs/agent/hooks, "Common input fields"); Claude Code sends none of them.
A Claude hook that receives them runs inside Cursor: it records nothing, so Cursor's session is
never counted as a Claude Code session, and says so on stderr only. The learning gate does not
block Cursor's stop with Claude's `decision: block`, which Cursor would turn into an endless
follow-up (Claude hooks have no loop limit in Cursor).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HOOK = load("work_session_hook_cursor", "adapters/common/work_session_hook.py")
GATE = ROOT / "adapters/claude/scripts/learning_gate_hook.py"
URL = "https://app.example.test/api/agent/work-sessions"
# What Cursor sends to every hook, as its documentation lists the common fields.
CURSOR_COMMON = {"conversation_id": "conv-1", "generation_id": "gen-1", "model": "auto", "cursor_version": "2.4.21",
                 "user_email": None, "transcript_path": None}


class Identity:
    def identity(self) -> dict:
        return {"workSessionUrl": URL}

    def access_token(self, *, force_refresh: bool = False) -> str:
        return "aas_cached"


class CursorWorkSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.repository = self.home / "repo"
        self.repository.mkdir()
        for arguments in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.test"),
                          ("config", "user.name", "t"), ("commit", "-q", "--allow-empty", "-m", "first")):
            subprocess.run(["git", "-C", str(self.repository), *arguments], check=True, capture_output=True)
        self.calls: list[dict] = []
        self.loaded: list[str] = []

    def post(self, url: str, token: str, payload: dict) -> tuple[int, dict]:
        self.calls.append(payload)
        return 200, {"session": {"id": "ws-1"}}

    def run_main(self, provider: str, event: dict) -> tuple[str, str]:
        output, errors = io.StringIO(), io.StringIO()

        def loader(name: str, home: Path):
            self.loaded.append(name)
            return Identity()

        # A user hook runs from ~/.cursor, never from the project.
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), redirect_stdout(output), \
                redirect_stderr(errors), mock.patch.object(HOOK, "load_identity", loader), \
                mock.patch.object(HOOK, "post", self.post), mock.patch.object(HOOK.Path, "home", return_value=self.home), \
                mock.patch.object(HOOK.os, "getcwd", return_value=str(self.home)):
            self.assertEqual(HOOK.main(["--provider", provider]), 0)
        return output.getvalue().strip(), errors.getvalue()

    def cursor_event(self, name: str, **extra) -> dict:
        return {**CURSOR_COMMON, "hook_event_name": name, "workspace_roots": [str(self.repository)], **extra}

    def test_a_cursor_conversation_starts_beats_and_ends_as_cursors_session(self) -> None:
        answers = [self.run_main("cursor", self.cursor_event("sessionStart", session_id="conv-1", composer_mode="agent")),
                   self.run_main("cursor", self.cursor_event("beforeSubmitPrompt", prompt="never sent")),
                   self.run_main("cursor", self.cursor_event("stop", status="completed", loop_count=0)),
                   self.run_main("cursor", self.cursor_event("sessionEnd", session_id="conv-1", reason="completed"))]
        # What Cursor expects from each hook: beforeSubmitPrompt may stop a prompt and is told not to.
        self.assertEqual([answer for answer, _ in answers], ["{}", '{"continue": true}', "{}", "{}"])
        self.assertEqual([call["action"] for call in self.calls], ["start", "end"])
        self.assertEqual(self.calls[0]["threadId"], "conv-1")
        # The repository is the workspace root, not the directory Cursor ran the hook from.
        self.assertEqual(self.calls[0]["branch"], "main")
        self.assertNotIn("never sent", json.dumps(self.calls))
        self.assertEqual(set(self.loaded), {"cursor"})
        self.assertTrue((self.home / ".agent-platform/work-sessions/cursor").is_dir())

    def test_a_claude_hook_that_cursor_runs_records_nothing_and_says_so_on_stderr(self) -> None:
        for provider, quiet in (("claude", "{}"), ("codex", '{"continue": true}')):
            for name in ("SessionStart", "sessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
                with self.subTest(provider=provider, event=name):
                    answer, said = self.run_main(provider, self.cursor_event(
                        name, session_id="conv-1", cwd=str(self.repository)))
                    self.assertEqual(answer, quiet)
                    self.assertIn("Cursor", said)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.loaded, [])
        self.assertFalse((self.home / ".agent-platform/work-sessions/claude").exists())

    def test_the_input_cursor_gave_the_claude_hooks_on_a_real_start_is_recognised(self) -> None:
        # Cursor 3.24.18 on macOS, 2026-10-15: at its start it ran both Claude Code hooks of the
        # platform from ~/.claude/settings.json ("Loaded Claude user hooks" in its hooks log)
        # with this input; the e-mail address is left out here.
        real = {"conversation_id": "empty-state-draft", "generation_id": "", "model": "unknown", "model_id": "default",
                "is_background_agent": False, "composer_mode": "agent", "session_id": "empty-state-draft",
                "hook_event_name": "sessionStart", "cursor_version": "3.24.18", "workspace_roots": [],
                "user_email": None, "transcript_path": None}
        self.assertTrue(HOOK.runs_inside_cursor(real))
        answer, said = self.run_main("claude", real)
        self.assertEqual((answer, self.calls, self.loaded), ("{}", [], []))
        self.assertIn("Cursor", said)

    def test_cursor_is_recognised_by_cursor_version_and_a_generic_conversation_id_is_not_enough(self) -> None:
        # conversation_id is a generic name another tool may send; Cursor is
        # known by cursor_version, the other two fields only support it.
        self.assertTrue(HOOK.runs_inside_cursor({"session_id": "s", "cursor_version": "3.24.18"}))
        for fields in ({"conversation_id": "x"}, {"generation_id": "x"}, {"conversation_id": "x", "generation_id": "y"},
                       {"cursor_version": ""}, {"cursor_version": None}):
            self.assertFalse(HOOK.runs_inside_cursor({"session_id": "s", **fields}), fields)
        claude = {"session_id": "s", "transcript_path": "/t.jsonl", "cwd": "/repo", "hook_event_name": "Stop",
                  "permission_mode": "default", "stop_hook_active": False}
        self.assertFalse(HOOK.runs_inside_cursor(claude))
        self.assertFalse(HOOK.runs_inside_cursor("not an object"))
        answer, said = self.run_main("claude", {**claude, "hook_event_name": "SessionStart", "cwd": str(self.repository)})
        self.assertEqual((answer, said), ("{}", ""))
        self.assertEqual([call["action"] for call in self.calls], ["start"])


class LearningGateInCursorTests(unittest.TestCase):
    def run_gate(self, event: dict, environment: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(GATE)], input=json.dumps(event), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env={**os.environ, **environment}, check=False)

    def test_a_pending_gate_does_not_block_cursor_the_way_it_blocks_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "conv-1.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")
            environment = {"AGENT_LEARNING_GATE_ROOT": str(state), "CLAUDE_ENV_FILE": str(root / "env")}
            claude = self.run_gate({"session_id": "conv-1", "hook_event_name": "Stop"}, environment)
            self.assertEqual(json.loads(claude.stdout)["decision"], "block")
            # A conversation_id alone is not Cursor: the gate still speaks Claude Code.
            other = self.run_gate({"session_id": "conv-1", "conversation_id": "conv-1", "hook_event_name": "Stop"}, environment)
            self.assertEqual(json.loads(other.stdout)["decision"], "block")
            for name in ("Stop", "stop", "SessionStart", "UserPromptSubmit"):
                cursor = self.run_gate({**CURSOR_COMMON, "conversation_id": "conv-1", "session_id": "conv-1",
                                        "hook_event_name": name, "prompt": "клиент нашёл баг опять"}, environment)
                self.assertEqual((cursor.returncode, cursor.stdout.strip()), (0, "{}"), name)
                self.assertIn("Cursor", cursor.stderr)
            # Claude Code's session start writes its environment file; Cursor's does not.
            self.assertFalse((root / "env").exists())


if __name__ == "__main__":
    unittest.main()
