"""Kimi Code and OpenCode are synchronized in their own right.

A computer connected with Codex, Claude Code, Kimi Code and OpenCode chosen must send a receipt
for each of them, or a release can never be confirmed on it; and a computer with only Kimi Code
or OpenCode chosen must synchronize too, not only one where they sit next to Codex or Claude
Code.
"""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "adapters/client/scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load("managed_client_followers", "sync_client.py")
EXECUTION = load("control_plane_sync_followers", "control_plane_sync.py")
DESIRED = {
    "release": {"releaseId": "release-1", "agentPlatformSha": "a" * 40},
    "skillDelivery": {"version": 1, "audiences": ["everyone"], "disabled": []},
}


class FollowerAppTests(unittest.TestCase):
    def home(self, *apps: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        home = base / "home"
        home.mkdir()
        if "kimi" in apps:
            (home / ".kimi-code").mkdir()
        if "opencode" in apps:
            (home / ".config/opencode").mkdir(parents=True)
        environment = dict(os.environ)
        os.environ.pop("KIMI_CODE_HOME", None)
        original_home = Path.home
        Path.home = classmethod(lambda cls: home)

        def restore():
            Path.home = original_home
            os.environ.clear()
            os.environ.update(environment)

        self.addCleanup(restore)
        return base, home

    def test_a_chosen_app_that_is_on_the_computer_gets_its_own_receipt(self):
        base, home = self.home("kimi", "opencode")
        (home / ".kimi-code/agent-platform-skills.json").write_text(json.dumps({
            "version": 1, "managed_skill_digests": {"archify": "d" * 64, "humanizer": "e" * 64}, "skill_conflicts": ["mine"],
        }), encoding="utf-8")
        args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
        state = base / "mirrored-apps.json"
        runs = []
        reports = MODULE.follower_app_reports(ROOT, ["kimi", "opencode"])
        self.assertEqual([(item["provider"], item["status"], item["releaseId"]) for item in reports],
                         [("kimi", "current", None), ("opencode", "current", None)])
        self.assertTrue(MODULE.follower_apps_need_update(DESIRED, ["kimi"], state))
        with patch.object(MODULE.subprocess, "run", side_effect=lambda command, **_: runs.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")):
            MODULE.apply_mirrored_platform(ROOT, args, DESIRED, base, reports, state)
        kimi, opencode = reports
        self.assertEqual((kimi["status"], kimi["releaseId"], kimi["agentPlatformSha"]), ("updated", "release-1", "a" * 40))
        self.assertEqual((kimi["skillCount"], kimi["skillConflicts"]), (2, ["mine"]))
        self.assertEqual(kimi["skillsDigest"], hashlib.sha256(b'["archify","humanizer"]').hexdigest())
        self.assertEqual((opencode["status"], opencode["releaseId"], opencode["skillCount"]), ("updated", "release-1", 0))
        self.assertFalse(MODULE.follower_apps_need_update(DESIRED, ["kimi", "opencode"], state))
        # The next sync runs no installer and still confirms the release.
        again = MODULE.follower_app_reports(ROOT, ["kimi"])
        with patch.object(MODULE.subprocess, "run", side_effect=AssertionError("no installer should run")):
            MODULE.apply_mirrored_platform(ROOT, args, DESIRED, base, again, state)
        self.assertEqual((again[0]["status"], again[0]["releaseId"]), ("current", "release-1"))
        self.assertEqual(len(runs), 2)

    def test_a_failing_adapter_fails_the_apps_own_receipt_and_nobody_elses(self):
        base, home = self.home("kimi", "opencode")
        args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")

        def run(command, **_):
            if "opencode" in command[1]:
                raise MODULE.subprocess.CalledProcessError(1, command, "", "OpenCode config must be valid JSON: x")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        reports = [{"provider": "claude", "status": "current"}, *MODULE.follower_app_reports(ROOT, ["kimi", "opencode"])]
        with patch.object(MODULE.subprocess, "run", side_effect=run):
            MODULE.apply_mirrored_platform(ROOT, args, DESIRED, base, reports, base / "mirrored-apps.json")
        self.assertEqual([item["status"] for item in reports], ["current", "updated", "failed"])
        self.assertEqual(reports[2]["error"], "opencode adapter: OpenCode config must be valid JSON: x")
        self.assertIsNone(reports[2]["releaseId"])

    def commands(self, home: Path, platform_root: Path = ROOT, services: tuple[str, ...] | None = None) -> dict:
        args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
        if services is not None:
            # As the orchestrator leaves it: the services the apps reach as agents of their own.
            args.app_agent_services = set(services)
        return dict(MODULE.mirrored_platform_commands(platform_root, args, home / "delivery.json"))

    def release_with_adapters(self, base: Path, kimi: str, opencode: str) -> Path:
        """A release whose adapters name the given options and no other."""
        platform_root = base / "release"
        for provider, options in (("kimi", kimi), ("opencode", opencode)):
            adapter = platform_root / "adapters" / provider / "install.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text(
                "from pathlib import Path\n\n\ndef default_kimi_homes(include_desktop):\n"
                "    return [Path.home() / '.kimi-code']\n\n\n"
                f"OPTIONS = {options.split()!r}\n", encoding="utf-8")
        return platform_root

    def test_kimi_is_not_sent_for_the_owners_token_where_apps_are_agents_of_their_own(self):
        """A computer connected again with Codex and Claude Code chosen and Kimi Code on it.
        Kimi had no channel of its own, the adapter looked for the
        owner's token in the configs of the other two, where there is none any more, and
        stopped; the run failed at provider_installation with both chosen apps as failed."""
        base, home = self.home("kimi", "opencode")
        every = ("collaboration", "agentMemory", "projectContext")
        self.assertIn("--skip-memory", self.commands(home, services=every)["kimi"])
        # A service from before the apps' own agents: the token is in the configs, as it was.
        self.assertNotIn("--skip-memory", self.commands(home)["kimi"])
        self.assertNotIn("--skip-memory", self.commands(home, services=("collaboration",))["kimi"])
        # OpenCode's connections were never its adapter's.
        for services in (None, every):
            self.assertTrue({"--skip-memory", "--skip-ollama"} <= set(self.commands(home, services=services)["opencode"]))

    def test_an_adapter_is_started_with_the_options_its_release_names_and_no_other(self):
        base, home = self.home("kimi", "opencode")
        platform_root = self.release_with_adapters(
            base, kimi="--platform-root --no-desktop --kimi-home",
            opencode="--platform-root --config-root --skip-memory --skip-ollama --skill-delivery-manifest")
        commands = self.commands(home, platform_root, ("collaboration", "agentMemory", "projectContext"))
        self.assertEqual(commands["kimi"][2:], ["--platform-root", str(platform_root), "--no-desktop",
                                                "--kimi-home", str(home / ".kimi-code")])
        self.assertEqual(commands["opencode"][2:], [
            "--platform-root", str(platform_root), "--config-root", str(home / ".config/opencode"),
            "--skip-memory", "--skip-ollama", "--skill-delivery-manifest", str(home / "delivery.json")])
        # The adapters of this tree name everything this client offers.
        for provider, command in self.commands(home, services=("agentMemory",)).items():
            adapter = (ROOT / "adapters" / provider / "install.py").read_text(encoding="utf-8")
            for word in command[2:]:
                if word.startswith("--"):
                    self.assertIn(f'"{word}"', adapter, provider)

    def test_an_adapter_that_does_not_know_what_keeps_it_in_its_place_is_not_started(self):
        base, home = self.home("kimi", "opencode")
        # Without --no-desktop Kimi's adapter would write into the desktop app; without
        # --skip-ollama OpenCode's would write the local models.
        platform_root = self.release_with_adapters(
            base, kimi="--platform-root --kimi-home --skip-memory",
            opencode="--platform-root --config-root --skip-memory")
        self.assertEqual(self.commands(home, platform_root), {})
        args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
        reports = MODULE.follower_app_reports(platform_root, ["kimi", "opencode"])
        with patch.object(MODULE.subprocess, "run", side_effect=AssertionError("no adapter should run")), \
                patch.dict(os.environ, {"ARTIFICIAL_MEMORY_LANG": "ru"}):
            MODULE.apply_mirrored_platform(platform_root, args, DESIRED, base, reports, base / "mirrored-apps.json")
        self.assertEqual([(item["status"], item["error"]) for item in reports],
                         [("failed", "В этой версии нет установщика для приложения")] * 2)

    def test_kimis_adapter_runs_again_when_the_connections_it_installs_change(self):
        """Kimi's config is written by its adapter from two files the client keeps. An app
        chosen after the release was installed would otherwise wait for the next release."""
        base, home = self.home("kimi", "opencode")
        args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
        state, channels, mirror = base / "mirrored-apps.json", base / "channels.json", base / "mirror.json"
        runs = []

        def synchronize() -> list[str]:
            runs.clear()
            reports = [{"provider": "claude", "status": "current"}]
            with patch.object(MODULE.subprocess, "run", side_effect=lambda command, **_: runs.append(
                    Path(command[1]).parent.name) or SimpleNamespace(returncode=0, stdout="", stderr="")):
                MODULE.apply_mirrored_platform(ROOT, args, DESIRED, base, reports, state)
            return list(runs)

        with patch.dict(MODULE.apply_mirrored_platform.__globals__,
                        {"KIMI_APP_AGENT_CHANNELS": channels, "KIMI_MANAGED_MIRROR": mirror}):
            self.assertEqual(synchronize(), ["kimi", "opencode"])
            self.assertEqual(synchronize(), [])
            channels.write_text('{"version": 1, "servers": {}}', encoding="utf-8")
            self.assertEqual(synchronize(), ["kimi"])
            self.assertEqual(synchronize(), [])
            mirror.write_text('{"servers": {"one": {}}}', encoding="utf-8")
            self.assertEqual(synchronize(), ["kimi"])
            channels.unlink()
            self.assertEqual(synchronize(), ["kimi"])
            self.assertEqual(synchronize(), [])
        # The record of an app keeps the shape a client of an earlier release compares as a
        # whole; one more word in it and that client would start the adapter on every sync.
        recorded = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(recorded["kimi"], {"releaseId": "release-1", "skillDelivery": DESIRED["skillDelivery"]})
        self.assertEqual(recorded["opencode"], recorded["kimi"])
        self.assertFalse(MODULE.follower_apps_need_update(DESIRED, ["kimi", "opencode"], state))

    def test_a_chosen_app_that_is_not_on_the_computer_says_so(self):
        base, home = self.home("opencode")
        for language, said in (("ru", "Приложение не найдено на этом компьютере"),
                               ("en", "The app is not found on this computer")):
            with patch.dict(os.environ, {"ARTIFICIAL_MEMORY_LANG": language}):
                reports = MODULE.follower_app_reports(ROOT, ["kimi", "opencode", "codex"])
            self.assertEqual([(item["provider"], item["status"]) for item in reports],
                             [("kimi", "failed"), ("opencode", "current")])
            self.assertEqual(reports[0]["error"], said)

    def test_the_client_accepts_every_app_and_says_so(self):
        source = (ROOT / "adapters/client/scripts/sync_client.py").read_text(encoding="utf-8")
        self.assertIn('choices=("codex", "claude", "kimi", "opencode", "cursor")', source)
        self.assertIn('"appProviders"', source)
        bootstrap = (ROOT / "adapters/client/scripts/companion_install.py").read_text(encoding="utf-8")
        self.assertIn('PROVIDERS = ("codex", "claude", "kimi", "opencode", "cursor")', bootstrap)

    def test_a_computer_with_only_kimi_chosen_synchronizes(self):
        args = SimpleNamespace(
            provider=["kimi"], check_only=False, client_id="fixture", display_name="Mac",
            project_mappings=Path("mappings.json"), update_policy="automatic",
            codex_root=Path("codex"), claude_root=Path("claude"),
            control_plane_url="https://app.example/agent-api", release_trust_file=Path("trust.json"),
            release_cache=Path("cache"), client_token="private-test-token",
        )
        desired = {
            "clientProtocol": 2, "release": {"releaseId": "release-1", "agentPlatformSha": "a" * 40},
            "projectProfileAssignments": [],
            "appAgentChannels": [
                {"service": "collaboration", "configKey": "agent-platform-collaboration", "url": "https://app.example/mcp"},
                {"service": "agentMemory", "configKey": "agentMemory", "url": "https://app.example/agent-memory/mcp"},
                {"service": "projectContext", "configKey": "projectContext", "url": "https://app.example/project-context/mcp"},
            ],
        }
        receipt = {"provider": "kimi", "releaseId": None, "agentPlatformSha": None, "skillCount": 0,
                   "skillsDigest": "0" * 64, "restartRequired": False, "durationMs": None, "status": "current", "error": None}
        helper = Mock()
        helper.read_launcher_events.return_value = []
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        helper.load_project_mappings.return_value = []
        helper.project_profiles_need_update.return_value = False
        helper.context_passports_need_update.return_value = False
        helper.follower_apps_need_update.return_value = True
        helper.plan_client_recovery.return_value = {"action": "none", "root": Path("client"), "report": {"recovery": "none"},
                                                    "releaseId": "fixture", "requested": False, "requests": 0}
        helper.settle_client_recovery.return_value = {"recovery": "none"}
        helper.run_installers.return_value = ([], MagicPath())
        helper.follower_app_reports.return_value = [receipt]
        helper.apply_mirrored_platform.side_effect = lambda *call: call[4][0].update(releaseId="release-1", status="updated")
        helper.configure_managed_mcp_servers.return_value = []
        helper.configure_app_agent_channels.return_value = {"kimi": {"status": "configured"}}
        helper.ensure_scheduled_task.return_value = {"status": "installed"}
        helper.client_platform.return_value = "macos"
        with patch.object(EXECUTION, "extract_verified", return_value=(Path("a.tar.gz"), Path("bundle"), Path("platform"))), \
                patch.object(EXECUTION, "call", return_value={"command": None}), patch("builtins.print"):
            self.assertEqual(EXECUTION.run_locked(args, desired, ["kimi"], helper), 0)
        # Nothing is installed into Codex or Claude Code, the client itself is still refreshed.
        self.assertEqual(helper.run_installers.call_args.args[2], [])
        helper.read_receipt.assert_not_called()
        # The schedule and the report carry the app that was chosen.
        self.assertEqual(helper.ensure_scheduled_task.call_args.args[4], ["kimi"])
        payload = helper.report_state.call_args.args[1]
        self.assertEqual([(item["provider"], item["status"], item["releaseId"]) for item in payload["providers"]],
                         [("kimi", "updated", "release-1")])
        self.assertEqual(payload["syncOutcome"], {"status": "succeeded", "stage": None})


class OrganizationSkillPathTests(unittest.TestCase):
    def test_a_skill_id_from_the_server_cannot_leave_the_staging_directory(self):
        # An absolute skill id would replace the staging path, so the server could make the
        # client write any file the user can write (a shell profile, a login item) before any
        # digest was checked.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            staging, outside = base / "staging", base / "outside"
            staging.mkdir()
            outside.mkdir()
            args = SimpleNamespace(control_plane_url="https://app.example/agent-api", client_token="private-test-token")
            transport = Mock()
            transport.request_json.return_value = {"skill": {"files": [{"path": ".zshenv", "content": "echo owned"}]}}
            # Replaced in the part the skills live in, which is where they read the name.
            with patch.dict(MODULE.fetch_organization_skills.__globals__, {"release_transport": Mock(return_value=transport)}):
                for skill_id in (str(outside), "../outside", "a/b", ".hidden", "", "A", None, 7):
                    with self.assertRaises(RuntimeError, msg=repr(skill_id)):
                        MODULE.fetch_organization_skills(args, "client-1", [{"id": skill_id, "version": 1}], staging)
                self.assertEqual(list(outside.iterdir()), [])
                transport.request_json.assert_not_called()
                # A well-formed id is written inside staging and nowhere else.
                MODULE.fetch_organization_skills(args, "client-1", [{"id": "release-notes", "version": 1}], staging)
            self.assertEqual((staging / "release-notes/.zshenv").read_text(encoding="utf-8"), "echo owned")
            self.assertEqual(list(outside.iterdir()), [])


class MagicPath:
    """A managed script path that exists, for the scheduler step."""

    def is_file(self) -> bool:
        return True


if __name__ == "__main__":
    unittest.main()
