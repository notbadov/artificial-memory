from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "adapters/common/work_session_hook.py"
SPEC = importlib.util.spec_from_file_location("work_session_hook", SOURCE)
HOOK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(HOOK)

URL = "https://app.example.test/api/agent/work-sessions"


class Identity:
    def __init__(self, url: str = URL) -> None:
        self.url, self.refreshed = url, 0

    def identity(self) -> dict:
        return {"workSessionUrl": self.url} if self.url else {}

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.refreshed += int(force_refresh)
        return "aas_fresh" if force_refresh else "aas_cached"


class WorkSessionHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.repository = self.home / "repo"
        self.repository.mkdir()
        for arguments in (
            ("init", "-q", "-b", "main"), ("config", "user.email", "t@example.test"), ("config", "user.name", "t"),
            ("commit", "-q", "--allow-empty", "-m", "first"),
            ("remote", "add", "origin", "https://person:secret-token@git.example.test/org/repo.git"),
        ):
            subprocess.run(["git", "-C", str(self.repository), *arguments], check=True, capture_output=True)
        self.calls: list[tuple[str, str, dict]] = []
        self.answers: list[tuple[int, dict]] = []
        self.identity = Identity()
        self.reporter = HOOK.Reporter("claude", self.home, self.identity)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def post(self, url: str, token: str, payload: dict) -> tuple[int, dict]:
        self.calls.append((url, token, payload))
        return self.answers.pop(0) if self.answers else (200, {"session": {"id": "ws-1", "state": "active"}})

    def report(self, event: str, now: float, thread: str = "thread-1") -> None:
        with mock.patch.object(HOOK, "post", self.post):
            self.reporter.report(event, thread, str(self.repository), now)

    def test_a_thread_starts_once_beats_at_most_once_a_minute_and_ends(self) -> None:
        self.report("SessionStart", 1_000)
        self.report("UserPromptSubmit", 1_010)
        self.report("Stop", 1_030)
        self.report("UserPromptSubmit", 1_070)
        self.report("SessionEnd", 1_080)
        self.assertEqual([call[2]["action"] for call in self.calls], ["start", "heartbeat", "end"])
        start = self.calls[0][2]
        self.assertEqual(start["threadId"], "thread-1")
        self.assertEqual(start["branch"], "main")
        self.assertRegex(start["headSha"], r"^[0-9a-f]{40}$")
        self.assertEqual(self.calls[1][2]["sessionId"], "ws-1")
        self.assertEqual(self.calls[0][:2], (URL, "aas_cached"))
        # After the end the thread is forgotten here.
        self.assertEqual(list((self.home / ".agent-platform/work-sessions/claude").glob("*.json")), [])

    def test_only_the_repository_position_leaves_the_computer(self) -> None:
        self.report("UserPromptSubmit", 1_000)
        sent = json.dumps(self.calls[0][2])
        self.assertEqual(self.calls[0][2]["repositoryUrl"], "https://git.example.test/org/repo.git")
        self.assertNotIn("secret-token", sent)
        self.assertNotIn(str(self.home), sent)
        self.assertEqual(set(self.calls[0][2]), {"action", "threadId", "repositoryUrl", "branch", "headSha"})

    def test_a_new_commit_is_reported_at_once(self) -> None:
        self.report("SessionStart", 1_000)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-q", "--allow-empty", "-m", "second"],
                       check=True, capture_output=True)
        self.report("Stop", 1_005)
        self.assertEqual([call[2]["action"] for call in self.calls], ["start", "heartbeat"])
        self.assertNotEqual(self.calls[1][2]["headSha"], self.calls[0][2]["headSha"])

    def test_a_session_the_server_closed_is_started_again(self) -> None:
        self.report("SessionStart", 1_000)
        self.answers = [(409, {}), (200, {"session": {"id": "ws-2"}})]
        self.report("UserPromptSubmit", 2_000)
        self.assertEqual([call[2]["action"] for call in self.calls], ["start", "heartbeat", "start"])
        self.report("UserPromptSubmit", 2_100)
        self.assertEqual(self.calls[-1][2], {**self.calls[-1][2], "action": "heartbeat", "sessionId": "ws-2"})

    def test_an_expired_agent_session_is_refreshed_once(self) -> None:
        self.answers = [(401, {}), (200, {"session": {"id": "ws-1"}})]
        self.report("SessionStart", 1_000)
        self.assertEqual([call[1] for call in self.calls], ["aas_cached", "aas_fresh"])

    def test_nothing_is_sent_without_an_https_endpoint_or_outside_a_repository(self) -> None:
        for url in ("", "http://insecure.example.test/api/agent/work-sessions"):
            self.reporter = HOOK.Reporter("claude", self.home, Identity(url))
            self.report("SessionStart", 1_000)
        self.assertEqual(self.calls, [])
        self.reporter = HOOK.Reporter("claude", self.home, self.identity)
        outside = self.home / "plain"
        outside.mkdir()
        with mock.patch.object(HOOK, "post", self.post):
            self.reporter.report("SessionStart", "thread-9", str(outside), 1_000)
        self.assertEqual(self.calls[0][2], {"action": "start", "threadId": "thread-9"})

    def run_main(self, provider: str, stdin: str, identity=None) -> str:
        output = io.StringIO()
        loader = (lambda *_: identity) if identity is not None else mock.Mock(side_effect=ImportError("no client"))
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(output), \
                mock.patch.object(HOOK, "load_identity", loader), \
                mock.patch.object(HOOK.Path, "home", return_value=self.home):
            self.assertEqual(HOOK.main(["--provider", provider]), 0)
        return output.getvalue().strip()

    def test_the_hook_never_fails_the_persons_work(self) -> None:
        event = json.dumps({"session_id": "t", "hook_event_name": "SessionStart", "cwd": str(self.repository)})
        # A computer that is not connected, a broken event, and a platform that is down.
        self.assertEqual(self.run_main("claude", event), "{}")
        self.assertEqual(self.run_main("codex", event), '{"continue": true}')
        self.assertEqual(self.run_main("claude", "not json"), "{}")
        with mock.patch.object(HOOK, "post", side_effect=TimeoutError("down")):
            self.assertEqual(self.run_main("codex", event, self.identity), '{"continue": true}')

    def test_the_script_is_kept_once_and_each_installer_copies_it_from_there(self) -> None:
        # Two tracked copies drift apart; each app's installer takes adapters/common's at install.
        self.assertFalse((ROOT / "adapters/claude/scripts" / SOURCE.name).exists(),
                         "the Claude installer copies adapters/common/work_session_hook.py; keep no second copy")
        for installer in ("adapters/claude/install.py", "adapters/codex/scripts/install_codex_release.py"):
            self.assertIn('"adapters" / "common" / "work_session_hook.py"',
                          (ROOT / installer).read_text(encoding="utf-8"), installer)

    def test_the_claude_scripts_are_replaced_at_once_with_the_shared_hook_among_them(self) -> None:
        installer = load("claude_install", "adapters/claude/install.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / ".claude" / "agent-platform" / "scripts"
            destination.mkdir(parents=True)
            (destination / "work_session_hook.py").write_text("old\n", encoding="utf-8")
            installer.install_directory(ROOT / "adapters/claude/scripts", destination, root / ".claude",
                                        extra={"work_session_hook.py": SOURCE})
            self.assertEqual((destination / "work_session_hook.py").read_bytes(), SOURCE.read_bytes())
            self.assertTrue((destination / "learning_gate_hook.py").is_file())
            self.assertEqual([path.name for path in destination.parent.iterdir()], ["scripts"], "no staging is left")


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str((ROOT / relative).parent))
    spec.loader.exec_module(module)
    return module


class WorkSessionHookInstallTests(unittest.TestCase):
    def commands(self, settings: dict, event: str) -> list[str]:
        return [hook["command"] for entry in settings["hooks"].get(event, []) for hook in entry["hooks"]]

    def test_claude_gets_every_platform_hook_once_and_keeps_the_persons_own(self) -> None:
        installer = load("claude_install", "adapters/claude/install.py")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".claude" / "settings.json"
            target.parent.mkdir()
            own = {"hooks": [{"type": "command", "command": "say done"}]}
            target.write_text(json.dumps({"hooks": {"Stop": [own]}}), encoding="utf-8")
            # Twice: a sync runs the installer on every release.
            installer.install_settings(ROOT, target)
            installer.install_settings(ROOT, target)
            settings = json.loads(target.read_text(encoding="utf-8"))
        stop = self.commands(settings, "Stop")
        self.assertEqual(stop[0], "say done")
        self.assertEqual(len(stop), len(set(stop)), stop)
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            reported = [item for item in self.commands(settings, event) if "work_session_hook.py" in item]
            self.assertEqual(
                reported, ['python3 "$HOME/.claude/agent-platform/scripts/work_session_hook.py" --provider claude'],
                event,
            )
        # The hooks that were there before this one are still installed.
        self.assertTrue(any("learning_gate_hook.py" in item for item in self.commands(settings, "Stop")))

    def test_codex_hooks_name_a_script_its_installer_puts_in_place(self) -> None:
        fragment = json.loads((ROOT / "adapters/codex/hooks/work-sessions.json").read_text(encoding="utf-8"))
        commands = {hook["command"] for entries in fragment["hooks"].values() for entry in entries for hook in entry["hooks"]}
        self.assertEqual(commands, {'python3 "$HOME/.codex/agent-platform/scripts/work_session_hook.py" --provider codex'})
        self.assertEqual(set(fragment["hooks"]), {"UserPromptSubmit", "Stop"})
        installer = (ROOT / "adapters/codex/scripts/install_codex_release.py").read_text(encoding="utf-8")
        copy = installer.index('scripts / "work_session_hook.py"')
        self.assertLess(copy, installer.index('install_hooks(hook_file, codex_root / "hooks.json")'))
        self.assertIn('"adapters" / "common" / "work_session_hook.py"', installer)

    def test_codex_hooks_are_not_duplicated_by_a_second_install(self) -> None:
        installer = load("_codex_install", "adapters/codex/scripts/_codex_install.py")
        with tempfile.TemporaryDirectory() as directory:
            hooks = Path(directory) / "hooks.json"
            for _ in range(2):
                installer.install_hooks(ROOT / "adapters/codex/hooks/work-sessions.json", hooks)
            installed = json.loads(hooks.read_text(encoding="utf-8"))
        self.assertEqual([len(entries) for entries in installed["hooks"].values()], [1, 1])


if __name__ == "__main__":
    unittest.main()
