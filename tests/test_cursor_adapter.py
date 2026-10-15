"""Cursor as a follower app of the managed client (first slice, 2026-10-15).

What the platform writes into Cursor, and nothing else:
- `~/.cursor/mcp.json` `mcpServers`: the three channels as stdio entries of the local bridge
  (`mcp_bridge.py --provider cursor`) and the managed servers the service assigns to Cursor;
- `~/.cursor/hooks.json`: one entry per work-session event, recognised by its script under
  `~/.cursor/agent-platform/scripts/`.
A hook, a server or any other entry the person keeps there survives every run, a second run
changes nothing, and disconnecting takes out only what the client wrote.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/client/scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import agent_identity  # noqa: E402
import sync_client  # noqa: E402
from test_isolation import sandboxed_environment  # noqa: E402

ADAPTER = ROOT / "adapters/cursor/install.py"
CHANNELS_INSTALLER = ROOT / "tools/install_app_agent_channels.py"
MANAGED_INSTALLER = ROOT / "tools/install_managed_mcp_servers.py"
TASKS = "https://app.artificial-memory.com/mcp"
MEMORY = "https://app.artificial-memory.com/agent-memory/mcp"
CONTEXT = "https://app.artificial-memory.com/project-context/mcp"
CHANNELS = {"collaboration": TASKS, "agentMemory": MEMORY, "projectContext": CONTEXT}
# The hook a person on this Mac already had before Cursor joined the platform (dcg).
FOREIGN_HOOKS = {"version": 1, "hooks": {"beforeShellExecution": [{"command": "/opt/dcg/dcg-pre-shell.py"}],
                                         "stop": [{"command": "./hooks/notify.sh", "timeout": 5}]}}
PERSONS_SERVER = {"command": "npx", "args": ["-y", "@figma/mcp"], "env": {"FIGMA_TOKEN": "persons-own-token"}}


def run_adapter(cursor_root: Path, *extra: str, python: str = sys.executable) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ADAPTER), "--platform-root", str(ROOT), "--cursor-root", str(cursor_root),
                           "--python", python, *extra], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False)


class CursorAdapterHookTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cursor = Path(temporary.name) / ".cursor"
        self.cursor.mkdir()
        self.hooks = self.cursor / "hooks.json"
        self.hooks.write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
        self.script = self.cursor / "agent-platform" / "scripts" / "work_session_hook.py"

    def ours(self, config: dict) -> dict[str, list[dict]]:
        marker = str(self.cursor / "agent-platform" / "scripts")
        return {event: [entry for entry in entries if marker in entry.get("command", "")]
                for event, entries in config["hooks"].items()}

    def test_the_foreign_hook_survives_and_each_work_session_event_gets_one_entry_of_ours(self) -> None:
        result = run_adapter(self.cursor)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual((receipt["ok"], receipt["hooks"], receipt["restartRequired"]), (True, "changed", False))
        config = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["hooks"]["beforeShellExecution"], FOREIGN_HOOKS["hooks"]["beforeShellExecution"])
        self.assertEqual(config["hooks"]["stop"][0], FOREIGN_HOOKS["hooks"]["stop"][0])
        ours = self.ours(config)
        self.assertEqual({event for event, entries in ours.items() if entries},
                         {"sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd"})
        for event, entries in ours.items():
            if not entries:
                continue
            self.assertEqual(len(entries), 1, event)
            command = entries[0]["command"]
            # An absolute interpreter (the client's, which has the identity's dependencies) and
            # an absolute script: Cursor runs user hooks from ~/.cursor, with no PATH promised.
            self.assertIn(str(Path(sys.executable)), command)
            self.assertIn(str(self.script), command)
            self.assertTrue(command.endswith("--provider cursor"), command)
        # The script is the shared one, copied as it is.
        self.assertEqual(self.script.read_bytes(), (ROOT / "adapters/common/work_session_hook.py").read_bytes())

    def test_a_second_run_changes_nothing(self) -> None:
        run_adapter(self.cursor)
        before = self.hooks.read_bytes()
        result = run_adapter(self.cursor)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["hooks"], "current")
        self.assertEqual(self.hooks.read_bytes(), before)

    def test_an_entry_of_ours_with_another_interpreter_is_replaced_not_doubled(self) -> None:
        run_adapter(self.cursor, python="/old/python3")
        run_adapter(self.cursor)
        ours = self.ours(json.loads(self.hooks.read_text(encoding="utf-8")))
        self.assertEqual(sum(len(entries) for entries in ours.values()), 4)
        self.assertNotIn("/old/python3", self.hooks.read_text(encoding="utf-8"))

    def test_without_a_hooks_file_one_is_created_in_cursors_format(self) -> None:
        self.hooks.unlink()
        self.assertEqual(run_adapter(self.cursor).returncode, 0)
        config = json.loads(self.hooks.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], 1)
        self.assertEqual(sorted(config["hooks"]), ["beforeSubmitPrompt", "sessionEnd", "sessionStart", "stop"])

    def test_a_hooks_file_that_is_not_cursors_format_is_left_alone(self) -> None:
        for content in ("{not json", json.dumps({"version": 2, "hooks": {}}), json.dumps({"version": 1, "hooks": []}),
                        json.dumps({"version": 1, "hooks": {"stop": {"command": "x"}}})):
            self.hooks.write_text(content, encoding="utf-8")
            result = run_adapter(self.cursor)
            self.assertNotEqual(result.returncode, 0, content)
            self.assertEqual(self.hooks.read_text(encoding="utf-8"), content)

    def test_uninstall_takes_out_only_what_the_platform_added(self) -> None:
        run_adapter(self.cursor)
        result = run_adapter(self.cursor, "--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.hooks.read_text(encoding="utf-8")), FOREIGN_HOOKS)
        self.assertFalse((self.cursor / "agent-platform").exists())
        # Nothing of ours left: a second uninstall leaves the file as it is.
        before = self.hooks.read_bytes()
        self.assertEqual(run_adapter(self.cursor, "--uninstall").returncode, 0)
        self.assertEqual(self.hooks.read_bytes(), before)

    def test_the_adapter_records_the_release_it_installed(self) -> None:
        run_adapter(self.cursor, "--release-id", "20261015-test", "--agent-platform-sha", "a" * 40)
        receipt = json.loads((self.cursor / "agent-platform/install.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["release_id"], receipt["agent_platform_sha"]), ("20261015-test", "a" * 40))


class CursorChannelTests(unittest.TestCase):
    def install(self, root: Path) -> subprocess.CompletedProcess:
        command = [
            sys.executable, str(CHANNELS_INSTALLER), "--python", sys.executable, "--bridge", str(root / "client/mcp_bridge.py"),
            "--codex-config", str(root / "codex/config.toml"), "--claude-config", str(root / "claude.json"),
            "--cursor-config", str(root / ".cursor/mcp.json"), "--provider", "cursor",
        ]
        for service, endpoint in CHANNELS.items():
            command += ["--channel", f"{service}={endpoint}"]
        return subprocess.run(command, capture_output=True, text=True, cwd=ROOT / "tools", encoding="utf-8",
                              errors="replace", check=False)

    def test_cursor_gets_its_own_agent_channels_and_keeps_the_persons_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client").mkdir()
            (root / "client/mcp_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / ".cursor").mkdir()
            config_path = root / ".cursor/mcp.json"
            config_path.write_text(json.dumps({"mcpServers": {"figma": PERSONS_SERVER}}), encoding="utf-8")
            first = self.install(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["restartRequired"], ["cursor"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["figma"], PERSONS_SERVER)
            for service, key in (("collaboration", "agent-platform-collaboration"), ("agentMemory", "agentMemory"),
                                 ("projectContext", "projectContext")):
                entry = config["mcpServers"][key]
                self.assertEqual(set(entry), {"command", "args"})
                self.assertEqual(entry["command"], sys.executable)
                self.assertEqual(entry["args"][-6:], ["--provider", "cursor", "--service", service, "--endpoint", CHANNELS[service]])
            before = config_path.read_bytes()
            second = self.install(root)
            self.assertEqual(second.returncode, 0, second.stderr)
            # Nothing changed: Cursor is not asked to restart for nothing.
            self.assertEqual(json.loads(second.stdout)["restartRequired"], [])
            self.assertEqual(config_path.read_bytes(), before)


class CursorManagedServerTests(unittest.TestCase):
    @staticmethod
    def server(**overrides) -> dict:
        value = {
            "id": "mcp-one", "configKey": "timeweb-cloud", "displayName": "Timeweb Cloud",
            "url": "https://timeweb.cloud/api/v1/mcp", "authType": "bearer",
            "credentialRevision": hashlib.sha256(b"organization-token").hexdigest(),
            "targetProviders": ["codex", "cursor"], "enabledTools": [], "approvalMode": "writes",
            "enabled": True, "revision": 1,
        }
        value.update(overrides)
        return value

    def run_installer(self, root: Path, servers: list[dict], tokens: dict[str, str]) -> dict:
        (root / "manifest.json").write_text(json.dumps({"version": 1, "servers": servers}), encoding="utf-8")
        (root / "credentials.json").write_text(json.dumps({"version": 1, "tokens": tokens}), encoding="utf-8")
        completed = subprocess.run([
            sys.executable, str(MANAGED_INSTALLER), "--manifest", str(root / "manifest.json"),
            "--credentials", str(root / "credentials.json"), "--state", str(root / "state.json"),
            "--codex-config", str(root / "codex.toml"), "--claude-config", str(root / "claude.json"),
            "--provider", "codex", "--cursor-root", str(root / ".cursor"),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return json.loads(completed.stdout)

    def test_a_server_reaches_cursor_and_the_persons_entries_stay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cursor").mkdir()
            config_path = root / ".cursor/mcp.json"
            config_path.write_text(json.dumps({"mcpServers": {"figma": PERSONS_SERVER}}), encoding="utf-8")
            receipt = self.run_installer(root, [self.server()], {"mcp-one": "organization-token"})
            self.assertEqual(receipt["servers"][0]["providers"], ["codex", "cursor"])
            self.assertEqual(receipt["restartRequired"], ["cursor"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["figma"], PERSONS_SERVER)
            self.assertEqual(config["mcpServers"]["timeweb-cloud"], {
                "url": "https://timeweb.cloud/api/v1/mcp", "headers": {"Authorization": "Bearer organization-token"}})
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["providers"]["cursor"], ["timeweb-cloud"])
            before = config_path.read_bytes()
            self.assertEqual(self.run_installer(root, [self.server()], {"mcp-one": "organization-token"})["restartRequired"], [])
            self.assertEqual(config_path.read_bytes(), before)
            # No longer delivered: only the recorded entry goes.
            self.assertEqual(self.run_installer(root, [], {})["restartRequired"], ["cursor"])
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {"mcpServers": {"figma": PERSONS_SERVER}})

    def test_the_codex_approval_decision_leaves_cursors_entry_as_it_is(self) -> None:
        # The client may turn "writes" into "prompt" for an older Codex; Cursor has no approval
        # field, so its entry is the same either way.
        entries = []
        for mode in ("writes", "prompt"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".cursor").mkdir()
                self.run_installer(root, [self.server(approvalMode=mode)], {"mcp-one": "organization-token"})
                entries.append(json.loads((root / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"])
        self.assertEqual(entries[0], entries[1])
        self.assertEqual(set(entries[0]["timeweb-cloud"]), {"url", "headers"})

    def test_a_same_named_server_of_the_person_in_cursor_is_a_conflict_and_stays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cursor").mkdir()
            persons = {"url": "https://timeweb.cloud/api/v1/mcp", "headers": {"Authorization": "Bearer persons-own-token"}}
            (root / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"timeweb-cloud": persons}}), encoding="utf-8")
            receipt = self.run_installer(root, [self.server()], {"mcp-one": "organization-token"})
            self.assertEqual(receipt["conflicts"], {"cursor": ["timeweb-cloud"]})
            self.assertEqual(receipt["servers"][0]["providers"], ["codex"])
            self.assertEqual(json.loads((root / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["timeweb-cloud"], persons)
            self.run_installer(root, [], {})
            self.assertEqual(json.loads((root / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["timeweb-cloud"], persons)

    def test_what_an_older_installer_recorded_is_not_taken_for_cursors(self) -> None:
        # Its state named the keys alone; it wrote them into Codex, Claude Code and OpenCode, never Cursor.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cursor").mkdir()
            persons = {"url": "https://persons.example/mcp"}
            (root / ".cursor/mcp.json").write_text(json.dumps({"mcpServers": {"timeweb-cloud": persons}}), encoding="utf-8")
            (root / "state.json").write_text(json.dumps({"version": 1, "configKeys": ["timeweb-cloud"]}), encoding="utf-8")
            receipt = self.run_installer(root, [self.server()], {"mcp-one": "organization-token"})
            self.assertEqual(receipt["conflicts"], {"cursor": ["timeweb-cloud"]})
            self.assertEqual(json.loads((root / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["timeweb-cloud"], persons)

    def test_cursor_is_skipped_where_it_is_not_installed_and_oauth_is_left_to_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = self.run_installer(root, [self.server(), self.server(
                id="mcp-two", configKey="yougile", authType="oauth", credentialRevision=None)], {"mcp-one": "organization-token"})
            self.assertFalse((root / ".cursor").exists())
            self.assertEqual(receipt["restartRequired"], [])
            (root / ".cursor").mkdir()
            receipt = self.run_installer(root, [self.server(id="mcp-two", configKey="yougile", authType="oauth",
                                                            credentialRevision=None)], {})
            # Nothing to write: Cursor's config is not created for nothing.
            self.assertEqual(receipt["restartRequired"], [])
            self.assertFalse((root / ".cursor/mcp.json").exists())


def bridge_entry(provider: str) -> dict:
    return {"command": "/python", "args": ["-E", "-s", "/opt/agent-platform/client/mcp_bridge.py", "--provider", provider,
                                           "--service", "agentMemory", "--endpoint", MEMORY]}


class CursorDisconnectTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        client = self.home / ".agent-platform/client"
        client.mkdir(parents=True)
        (client / "managed-mcp-state.json").write_text(json.dumps({
            "version": 1, "configKeys": ["team-tracker", "crm"], "providers": {"codex": ["crm"], "cursor": ["team-tracker"]},
        }), encoding="utf-8")
        (self.home / ".cursor").mkdir()
        self.config = self.home / ".cursor/mcp.json"
        self.config.write_text(json.dumps({"mcpServers": {
            "agentMemory": bridge_entry("cursor"), "team-tracker": {"url": "https://tracker.example/mcp"},
            # The person's own: a server named like one the client wrote into Codex, and one of
            # the channel names that is not the bridge.
            "crm": {"url": "https://persons-crm.example/mcp"}, "projectContext": {"url": "https://mine.example/mcp"},
            "figma": PERSONS_SERVER}}), encoding="utf-8")
        self.hooks = self.home / ".cursor/hooks.json"
        self.hooks.write_text(json.dumps(FOREIGN_HOOKS), encoding="utf-8")
        self.args = SimpleNamespace(codex_root=self.home / ".codex", claude_config=self.home / ".claude.json",
                                    client_id="mac-person-12345678", control_plane_url="https://app.example/agent-api",
                                    client_token=None)

    def test_disconnect_takes_out_only_what_the_client_wrote_into_cursor(self) -> None:
        report = sync_client.remove_local_access(self.args, self.home)
        self.assertIn("cursor", report["cleaned"])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"mcpServers": {
            "crm": {"url": "https://persons-crm.example/mcp"}, "projectContext": {"url": "https://mine.example/mcp"},
            "figma": PERSONS_SERVER}})
        self.assertEqual(json.loads(self.hooks.read_text(encoding="utf-8")), FOREIGN_HOOKS)
        self.assertNotIn("cursor-hooks", report["removed"])
        again = sync_client.remove_local_access(self.args, self.home)
        self.assertNotIn("cursor", again["cleaned"])

    def test_an_app_no_longer_chosen_loses_only_its_bridge_entries(self) -> None:
        remove = sync_client.remove_app_agent_channels
        self.config.write_text(json.dumps({"mcpServers": {"agentMemory": bridge_entry("cursor"),
                                                          "projectContext": bridge_entry("claude"), "figma": PERSONS_SERVER}}),
                               encoding="utf-8")
        self.assertTrue(remove("cursor", self.args, self.home))
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"mcpServers": {"projectContext": bridge_entry("claude"),
                                                                              "figma": PERSONS_SERVER}})
        self.assertFalse(remove("cursor", self.args, self.home))


class CursorClientTests(unittest.TestCase):
    def test_cursor_is_an_app_of_every_client_list(self) -> None:
        import cli_host
        import client_status
        import command_line
        import companion_install
        self.assertIn("cursor", agent_identity.APP_AGENT_PROVIDERS)
        self.assertIn("cursor", cli_host.APPS)
        self.assertEqual(command_line.APP_NAMES["cursor"], "Cursor")
        self.assertIn("cursor", client_status.PROVIDERS)
        self.assertEqual(client_status.PROVIDER_RECEIPTS["cursor"], Path(".cursor/agent-platform/install.json"))
        self.assertIn("cursor", companion_install.PROVIDERS)
        self.assertIn("cursor", sync_client.FOLLOWER_APPS)
        self.assertIn("cursorApp", sync_client.CLIENT_CAPABILITIES)
        source = (ROOT / "adapters/client/scripts/sync_client.py").read_text(encoding="utf-8")
        self.assertIn('choices=("codex", "claude", "kimi", "opencode", "cursor")', source)
        self.assertIn('"managedMcpCursor"', source)
        from cli_messages import MESSAGES
        self.assertIn("cursor", MESSAGES["apps_unknown"][0])

    def test_cursor_is_found_on_the_computer_but_never_enrolled_by_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".cursor").mkdir()
            with patch.object(Path, "home", return_value=home), \
                    patch.dict(sync_client.configure_app_agent_channels.__globals__, {
                        "kimi_is_installed": Mock(return_value=False),
                        "claude_desktop_config": Mock(return_value=home / "none/claude_desktop_config.json")}):
                self.assertIn("cursor", sync_client.installed_local_apps(ROOT))

    def test_a_chosen_cursor_gets_its_receipt_and_its_adapter_with_the_clients_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".cursor").mkdir()
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
            with patch.object(Path, "home", return_value=home):
                reports = sync_client.follower_app_reports(ROOT, ["codex", "cursor"])
                self.assertEqual([(item["provider"], item["status"]) for item in reports], [("cursor", "current")])
                commands = dict(sync_client.mirrored_platform_commands(ROOT, args, home / "delivery.json"))
            command = commands["cursor"]
            self.assertEqual(command[:2], [sys.executable, str(ROOT / "adapters/cursor/install.py")])
            self.assertEqual(command[command.index("--cursor-root") + 1], str(home / ".cursor"))
            self.assertEqual(command[command.index("--python") + 1], sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(Path, "home", return_value=Path(directory)):
                missing = sync_client.follower_app_reports(ROOT, ["cursor"])
            self.assertEqual(missing[0]["status"], "failed")

    def test_cursor_is_confirmed_at_the_release_and_asked_to_restart_when_its_servers_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            (home / ".cursor").mkdir(parents=True)
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json",
                                   restart_required_apps={"cursor"})
            desired = {"release": {"releaseId": "release-1", "agentPlatformSha": "a" * 40}, "providers": ["cursor"],
                       "skillDelivery": {"version": 1, "audiences": ["everyone"], "disabled": []}}
            with patch.object(Path, "home", return_value=home):
                reports = sync_client.follower_app_reports(ROOT, ["cursor"])
                runs = []
                with patch.object(sync_client.subprocess, "run",
                                  side_effect=lambda command, **_: runs.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")):
                    sync_client.apply_mirrored_platform(ROOT, args, desired, base, reports, base / "mirrored-apps.json")
            self.assertEqual([Path(command[1]).parent.name for command in runs], ["cursor"])
            self.assertEqual((reports[0]["status"], reports[0]["releaseId"], reports[0]["restartRequired"]),
                             ("updated", "release-1", True))
            self.assertEqual(reports[0]["skillCount"], 0)


class CursorReleaseWithoutCursorTests(unittest.TestCase):
    """A client released before Cursor stops on its name, so it is never given it."""

    def test_the_terminal_client_never_gives_cursor_to_an_installed_client_that_does_not_know_it(self) -> None:
        # going through the installer whenever the installed client lacked
        # cursorApp reinstalled the approved release on every terminal sync. The installed
        # client serves the other apps; only Cursor alone needs the approved release's client.
        import cli_engine
        host = SimpleNamespace(keychain_service="service", python=sys.executable)
        for apps, capabilities, expected_script, expected_apps in (
                (["codex", "cursor"], ["terminalSchedule"], "sync_client.py", ["codex"]),
                (["codex", "cursor"], ["terminalSchedule", "cursorApp"], "sync_client.py", ["codex", "cursor"]),
                (["cursor"], ["terminalSchedule"], "companion_install.py", ["cursor"])):
            record = {"controlPlaneUrl": "https://app.example/agent-api", "clientId": "mac", "displayName": "Mac",
                      "updatePolicy": "automatic", "schedule": "on", "providers": apps}
            started: list[tuple[str, list[str]]] = []

            def run_engine(_host, script, arguments, token=None, started=started):
                started.append((Path(script).name, [arguments[i + 1] for i, word in enumerate(arguments) if word == "--provider"]))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "installed"}), stderr="")

            with patch.object(cli_engine, "engine_capabilities", return_value=capabilities), \
                    patch.object(cli_engine, "run_engine", side_effect=run_engine), \
                    patch.object(cli_engine, "client_root", return_value=Path("/client")), \
                    patch.object(cli_engine, "managed_script", return_value=Path("/client/sync_client.py")):
                cli_engine.run_sync(host, record, "token")
            self.assertEqual(started[0], (expected_script, expected_apps), (apps, capabilities))

    def test_the_installer_gives_an_older_release_every_app_but_cursor(self) -> None:
        source = (ROOT / "adapters/client/scripts/companion_install.py").read_text(encoding="utf-8")
        self.assertIn('if "cursor" in providers and CURSOR_CAPABILITY not in release_capabilities(script, environment):', source)
        self.assertIn('"detail": "release_without_cursor"', source)


class CursorChannelStageTests(unittest.TestCase):
    def test_cursors_channels_are_written_only_by_an_installer_that_knows_cursor(self) -> None:
        import mcp_bridge
        args = SimpleNamespace(control_plane_url="https://app.example/agent-api", client_id="client-1",
                               codex_root=Path("/computer/.codex"), claude_config=Path("/computer/.claude.json"))
        desired = {"release": {"agentPlatformSha": "c" * 40}, "appAgentProviders": ["codex", "cursor"]}

        def configure(knows_cursor: bool):
            with patch.object(agent_identity.AppAgentIdentity, "register",
                              lambda identity, *rest: {"participantId": f"app-{identity.provider}-0123456789abcdef"}), \
                    patch.object(mcp_bridge.Bridge, "check", lambda bridge: None), \
                    patch.dict(sync_client.configure_app_agent_channels.__globals__, {
                        "claude_desktop_config": Mock(return_value=Path("/computer/Claude/claude_desktop_config.json")),
                        "claude_desktop_running": Mock(return_value=False),
                        "required_client_token": Mock(return_value="client-token"),
                        "installer_accepts": Mock(return_value=knows_cursor),
                        "retire_stale_app_agents": Mock(return_value={})}), \
                    patch.object(sync_client.subprocess, "run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "restartRequired": ["cursor"]}), stderr="")
                receipts = sync_client.configure_app_agent_channels(Path("/release"), args, ["codex"], desired, CHANNELS)
            return receipts, run.call_args.args[0]

        receipts, command = configure(True)
        self.assertEqual(receipts["cursor"], {"status": "configured", "participantId": "app-cursor-0123456789abcdef"})
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--provider"], ["codex", "cursor"])
        self.assertEqual(command[command.index("--cursor-config") + 1], str(Path.home() / ".cursor" / "mcp.json"))
        self.assertEqual(args.restart_required_apps, {"cursor"})
        receipts, command = configure(False)
        self.assertEqual(receipts["cursor"], {"status": "failed", "reason": "app_without_installer"})
        self.assertNotIn("--cursor-config", command)
        self.assertNotIn("cursor", command)



def load_adapter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cursor_adapter_under_test", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CursorBrokenConfigTests(unittest.TestCase):
    """a broken ~/.cursor/mcp.json is Cursor's problem alone. It is left as it is,
    Cursor is reported failed, and Codex and Claude Code are configured as on any other run."""

    BROKEN = '{"mcpServers": {"figma": '

    def test_the_managed_installer_configures_codex_and_keeps_its_state_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".cursor").mkdir()
            (root / ".cursor/mcp.json").write_text(self.BROKEN, encoding="utf-8")
            (root / "state.json").write_text(json.dumps({"version": 1, "configKeys": ["old"],
                                                         "providers": {"codex": ["old"], "cursor": ["old"]}}), encoding="utf-8")
            server = CursorManagedServerTests.server(authType="none", credentialRevision=None)
            receipt = CursorManagedServerTests().run_installer(root, [server], {})
            self.assertEqual(receipt["failed"], {"cursor": "config_unreadable"})
            self.assertEqual(receipt["servers"][0]["providers"], ["codex"])
            self.assertEqual((root / ".cursor/mcp.json").read_text(encoding="utf-8"), self.BROKEN)
            import tomllib
            self.assertIn("timeweb-cloud", tomllib.loads((root / "codex.toml").read_text(encoding="utf-8"))["mcp_servers"])
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            # Codex is what this run wrote; Cursor keeps its record until its file can be read.
            self.assertEqual(state["providers"], {"codex": ["timeweb-cloud"], "cursor": ["old"]})

    def test_the_channel_installer_configures_codex_and_leaves_cursor_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client").mkdir()
            (root / "client/mcp_bridge.py").write_text("# bridge\n", encoding="utf-8")
            (root / ".cursor").mkdir()
            (root / ".cursor/mcp.json").write_text(self.BROKEN, encoding="utf-8")
            command = [
                sys.executable, str(CHANNELS_INSTALLER), "--python", sys.executable, "--bridge", str(root / "client/mcp_bridge.py"),
                "--codex-config", str(root / "codex/config.toml"), "--claude-config", str(root / "claude.json"),
                "--cursor-config", str(root / ".cursor/mcp.json"), "--provider", "codex", "--provider", "cursor",
                *[part for service, endpoint in CHANNELS.items() for part in ("--channel", f"{service}={endpoint}")],
            ]
            done = subprocess.run(command, capture_output=True, text=True, cwd=ROOT / "tools", encoding="utf-8",
                                  errors="replace", check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertEqual(result["failed"], {"cursor": "config_unreadable"})
            self.assertEqual(result["providers"], ["codex"])
            self.assertTrue((root / "codex/config.toml").is_file())
            self.assertEqual((root / ".cursor/mcp.json").read_text(encoding="utf-8"), self.BROKEN)

    def test_a_sync_with_a_broken_cursor_config_completes_for_every_other_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            (home / ".cursor").mkdir(parents=True)
            (home / ".cursor/mcp.json").write_text(self.BROKEN, encoding="utf-8")
            (home / ".agent-platform/client").mkdir(parents=True)
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json",
                                   control_plane_url="https://app.example/agent-api", client_token="t")
            server = CursorManagedServerTests.server(authType="none", credentialRevision=None)
            desired = {"release": {"releaseId": "release-1", "agentPlatformSha": "a" * 40}, "providers": ["codex", "cursor"],
                       "mcpServers": [server], "skillDelivery": {"version": 1, "audiences": ["everyone"], "disabled": []}}
            temporary = base / "run"
            temporary.mkdir()
            with patch.object(Path, "home", return_value=home):
                servers = sync_client.configure_managed_mcp_servers(ROOT, args, ["codex"], desired, "mac", temporary)
                self.assertEqual(servers[0]["providers"], ["codex"])
                self.assertEqual(args.app_failures, {"cursor": "config_unreadable"})
                reports = sync_client.follower_app_reports(ROOT, ["cursor"])
                with patch.object(sync_client.subprocess, "run",
                                  side_effect=lambda command, **_: SimpleNamespace(returncode=0, stdout="", stderr="")):
                    sync_client.apply_mirrored_platform(ROOT, args, desired, base, reports, base / "mirrored-apps.json")
            self.assertEqual(reports[0]["status"], "failed")
            self.assertIn("mcp.json", reports[0]["error"])
            self.assertEqual((home / ".cursor/mcp.json").read_text(encoding="utf-8"), self.BROKEN)


class CursorNotChosenTests(unittest.TestCase):
    """Cursor on a computer is no consent. Nothing is written into ~/.cursor
    unless Cursor is chosen, and what an earlier choice wrote goes."""

    def test_the_managed_installer_is_pointed_at_cursor_only_when_it_is_chosen_or_has_entries_to_take_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".cursor").mkdir()
            (home / ".agent-platform/client").mkdir(parents=True)
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
            desired = {"providers": ["codex"], "mcpServers": []}

            def command(state: dict | None, chosen: list[str]) -> list[str]:
                path = home / ".agent-platform/client/managed-mcp-state.json"
                if state is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(json.dumps(state), encoding="utf-8")
                with patch.object(Path, "home", return_value=home), \
                        patch.object(sync_client.subprocess, "run") as run:
                    run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "servers": []}), stderr="")
                    sync_client.configure_managed_mcp_servers(ROOT, args, ["codex"], {**desired, "providers": chosen},
                                                              "mac", home)
                return run.call_args.args[0]

            self.assertNotIn("--cursor-root", command(None, ["codex"]))
            self.assertIn("--cursor-root", command(None, ["codex", "cursor"]))
            # Cursor was chosen before and has a server recorded there: the installer takes it back.
            self.assertIn("--cursor-root", command({"version": 1, "configKeys": ["crm"], "providers": {"cursor": ["crm"]}},
                                                   ["codex"]))

    def test_hooks_are_installed_only_for_a_chosen_cursor_and_taken_out_when_it_is_no_longer_chosen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            cursor = home / ".cursor"
            cursor.mkdir(parents=True)
            (cursor / "hooks.json").write_text(json.dumps(FOREIGN_HOOKS), encoding="utf-8")
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json")
            desired = {"release": {"releaseId": "release-1", "agentPlatformSha": "a" * 40}, "providers": ["codex"],
                       "skillDelivery": {"version": 1, "audiences": ["everyone"], "disabled": []}}
            report = {"provider": "codex", "status": "current"}
            runs = []
            with patch.object(Path, "home", return_value=home), \
                    patch.object(sync_client.subprocess, "run",
                                 side_effect=lambda command, **_: runs.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")):
                sync_client.apply_mirrored_platform(ROOT, args, desired, base, [report], base / "mirrored-apps.json")
            self.assertEqual([command for command in runs if "cursor" in str(command[1])], [])
            # Installed while it was chosen, then no longer chosen: the platform's hooks go.
            installed = run_adapter(cursor)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            with patch.object(Path, "home", return_value=home), \
                    patch.object(sync_client.subprocess, "run",
                                 side_effect=lambda command, **_: SimpleNamespace(returncode=0, stdout="", stderr="")):
                sync_client.apply_mirrored_platform(ROOT, args, desired, base, [report], base / "mirrored-apps.json")
            self.assertEqual(json.loads((cursor / "hooks.json").read_text(encoding="utf-8")), FOREIGN_HOOKS)
            self.assertFalse((cursor / "agent-platform").exists())


class CursorHookCommandTests(unittest.TestCase):
    """Cursor 3.24 runs a hook command with the system shell on macOS and Linux and with
    PowerShell on Windows (pwsh, else Windows PowerShell; -NoProfile -NonInteractive
    -ExecutionPolicy Bypass), piping the input from a file: `Get-Content -LiteralPath <file> -Raw
    | & { $input | <command> }`, with `& ` put before a command that starts with a quote. Read
    from the extension host of Cursor 3.24.18 (ExtHostShellExec.$executeHookDirect); the
    documentation says only that a command is "a shell string"."""

    def test_the_windows_command_is_a_powershell_call_of_quoted_paths(self) -> None:
        adapter = load_adapter()
        command = adapter.hook_command(Path("D:/Profiles/Alex Doe's/.cursor"), r"D:\Tools\Python 3.12\python.exe",
                                       windows=True)
        self.assertEqual(command, "& 'D:\\Tools\\Python 3.12\\python.exe' "
                                  "'D:\\Profiles\\Alex Doe''s\\.cursor\\agent-platform\\scripts\\work_session_hook.py' "
                                  "--provider cursor")
        self.assertTrue(adapter.is_ours({"command": command}, Path("D:/Profiles/Alex Doe's/.cursor"), windows=True))

    @unittest.skipUnless(__import__("shutil").which("pwsh"), "PowerShell runs the hooks of Cursor on Windows")
    def test_powershell_runs_the_command_the_way_cursor_does_with_spaces_and_an_apostrophe(self) -> None:
        import shutil
        adapter = load_adapter()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "Alex Doe's home"
            cursor = home / ".cursor"
            (cursor / "agent-platform/scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "adapters/common/work_session_hook.py", cursor / "agent-platform/scripts/work_session_hook.py")
            # Use a real interpreter layout on both hosts. An extensionless
            # Windows symlink is opened through file associations by PowerShell,
            # rather than executed as Python, and can return no stdout at all.
            import venv
            interpreter_home = Path(directory) / "Python 3's"
            venv.EnvBuilder(with_pip=False).create(interpreter_home)
            interpreter = (interpreter_home / "Scripts/python.exe" if sys.platform == "win32"
                           else interpreter_home / "bin/python")
            probe = subprocess.run([str(interpreter), "-c", "print('interpreter-ok')"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   check=False, timeout=30)
            self.assertEqual((probe.returncode, probe.stdout.strip()), (0, "interpreter-ok"), probe.stderr)
            # The same call hook_command builds on Windows, of this computer's paths.
            command = adapter.powershell_call([str(interpreter), str(cursor / "agent-platform/scripts/work_session_hook.py"),
                                               "--provider", "cursor"])
            self.assertEqual(command.split(" ")[0], "&")
            payload = Path(directory) / "payload.json"
            payload.write_text(json.dumps({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c",
                                           "cursor_version": "3.24.18", "workspace_roots": []}), encoding="utf-8")
            literal = str(payload).replace("'", "''")
            script = (f"$OutputEncoding = [System.Text.Encoding]::UTF8; Get-Content -LiteralPath '{literal}' -Raw | "
                      f"& {{ $input | {command} }}")
            done = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env=sandboxed_environment(home), check=False, timeout=120)
            self.assertEqual((done.returncode, done.stdout.strip()), (0, '{"continue": true}'), done.stderr)

    def test_a_home_with_an_apostrophe_keeps_one_entry_of_ours_per_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cursor = Path(directory) / "O'Brien" / ".cursor"
            cursor.mkdir(parents=True)
            for _ in range(3):
                self.assertEqual(run_adapter(cursor).returncode, 0)
            hooks = json.loads((cursor / "hooks.json").read_text(encoding="utf-8"))["hooks"]
            self.assertEqual({event: len(entries) for event, entries in hooks.items()},
                             {"sessionStart": 1, "beforeSubmitPrompt": 1, "stop": 1, "sessionEnd": 1})

    def test_the_script_is_copied_byte_for_byte_and_not_rewritten_when_it_is_the_same(self) -> None:
        adapter = load_adapter()
        with tempfile.TemporaryDirectory() as directory:
            platform = Path(directory) / "release"
            (platform / "adapters/common").mkdir(parents=True)
            source = platform / "adapters/common/work_session_hook.py"
            source.write_bytes(b"#!/usr/bin/env python3\r\nprint('crlf and lf')\nprint('\xc3\xa9')\r\n")
            cursor = Path(directory) / ".cursor"
            adapter.install_scripts(platform, cursor)
            target = cursor / "agent-platform/scripts/work_session_hook.py"
            self.assertEqual(target.read_bytes(), source.read_bytes())
            written = target.stat().st_mtime_ns
            os_utime = __import__("os").utime
            os_utime(target, ns=(written - 10_000_000_000, written - 10_000_000_000))
            adapter.install_scripts(platform, cursor)
            self.assertEqual(target.stat().st_mtime_ns, written - 10_000_000_000)


class CursorUninstallTests(unittest.TestCase):
    """disconnecting (and uninstalling, which disconnects first) takes the
    platform's hooks out of Cursor: they name the client's interpreter, which uninstall deletes."""

    def test_disconnect_takes_the_platforms_hooks_and_directory_out_of_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cursor = home / ".cursor"
            cursor.mkdir()
            (cursor / "hooks.json").write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8")
            installed = run_adapter(cursor)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            (home / ".agent-platform/client").mkdir(parents=True)
            args = SimpleNamespace(codex_root=home / ".codex", claude_config=home / ".claude.json",
                                   client_id="mac-person-12345678", control_plane_url="https://app.example/agent-api",
                                   client_token=None)
            report = sync_client.remove_local_access(args, home)
            self.assertIn("cursor-hooks", report["removed"])
            self.assertEqual(json.loads((cursor / "hooks.json").read_text(encoding="utf-8")), FOREIGN_HOOKS)
            self.assertFalse((cursor / "agent-platform").exists())

    def test_the_client_and_the_adapter_recognise_the_same_entries(self) -> None:
        # Two copies of one rule (the client is delivered apart from the adapters).
        import sync_disconnect
        adapter = load_adapter()
        # This case explicitly models POSIX even when the suite runs on Windows.
        root = PurePosixPath("/opt/me/.cursor")
        script = root / "agent-platform/scripts/work_session_hook.py"
        for command, windows in (
                (adapter.hook_command(root, "/opt/py/bin/python3", windows=False), False),
                (f"python3 {script} --provider cursor", False),
                ("python3 /opt/me/.cursor/hooks/notify.py", False),
                ("python3 /opt/me/.cursor/agent-platform/scriptsX/work_session_hook.py", False),
                (adapter.hook_command(Path("D:/me/.cursor"), r"D:\py\python.exe", windows=True), True),
                ("& 'D:\\me\\.cursor\\hooks\\notify.ps1'", True)):
            entry = {"command": command}
            cursor_root = Path("D:/me/.cursor") if windows else root
            self.assertEqual(sync_disconnect.is_cursor_platform_hook(entry, cursor_root, windows=windows),
                             adapter.is_ours(entry, cursor_root, windows=windows), command)


if __name__ == "__main__":
    unittest.main()
