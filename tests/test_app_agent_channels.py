"""Each app's channels (tasks, Agent Memory, Project Context): the local bridge, never a token."""

from __future__ import annotations

import json
import io
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "tools/install_app_agent_channels.py"
sys.path.insert(0, str(ROOT / "adapters/client/scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import agent_identity  # noqa: E402
import control_plane_sync as execution  # noqa: E402
import mcp_bridge  # noqa: E402
import sync_client  # noqa: E402
from install_agent_memory_mcp import READ_TOOL_NAMES, TOOL_NAMES  # noqa: E402
from install_app_agent_channels import COLLABORATION_READ_TOOL_NAMES  # noqa: E402

TASKS = "https://app.artificial-memory.com/mcp"
MEMORY = "https://app.artificial-memory.com/agent-memory/mcp"
CONTEXT = "https://app.artificial-memory.com/project-context/mcp"
CHANNELS = {"collaboration": TASKS, "agentMemory": MEMORY, "projectContext": CONTEXT}
# The part of the client the app agents live in, reached through one of its functions: a name
# they read is replaced there.
APP_AGENTS = sync_client.configure_app_agent_channels.__globals__

# What an earlier client left on a computer: every channel as an HTTP entry with the owner's token.
LEGACY_CODEX = f"""model = "gpt-6"

[mcp_servers.agent-platform-collaboration]
url = "{TASKS}"
enabled = true

[mcp_servers.agent-platform-collaboration.http_headers]
Authorization = "Bearer apm_owner-personal-token"

[mcp_servers.codexMemory]
url = "{MEMORY}"

[mcp_servers.agentMemory]
url = "{MEMORY}"

[mcp_servers.agentMemory.http_headers]
Authorization = "Bearer apm_owner-personal-token"
X-Agent-Platform-Organization = "example-organization"

[mcp_servers.agentMemory.tools.search_memory]

[mcp_servers.projectContext]
url = "{CONTEXT}"

[mcp_servers.projectContext.http_headers]
Authorization = "Bearer apm_owner-personal-token"

[mcp_servers.yougile]
url = "https://yougile.example/mcp"
"""

LEGACY_CLAUDE = {"mcpServers": {
    "agent-platform-collaboration": {"type": "http", "url": TASKS,
                                     "headers": {"Authorization": "Bearer apm_owner-personal-token"}},
    "codexMemory": {"type": "http", "url": MEMORY},
    "agentMemory": {"type": "http", "url": MEMORY, "headers": {
        "Authorization": "Bearer apm_owner-personal-token", "X-Agent-Platform-Organization": "example-organization"}},
    "projectContext": {"type": "http", "url": CONTEXT,
                       "headers": {"Authorization": "Bearer apm_owner-personal-token"}},
    "figma": {"type": "http", "url": "https://figma.example/mcp"},
}}


class InstallerTests(unittest.TestCase):
    def install(self, root: Path, *providers: str, channels: dict[str, str] = CHANNELS) -> subprocess.CompletedProcess:
        command = [
            sys.executable, str(INSTALLER), "--python", sys.executable, "--bridge", str(root / "client/mcp_bridge.py"),
            "--codex-config", str(root / "codex/config.toml"), "--claude-config", str(root / "claude.json"),
            "--opencode-root", str(root / "opencode"), "--kimi-channels", str(root / "kimi-channels.json"),
            "--claude-desktop-config", str(root / "claude_desktop_config.json"),
        ]
        for service, endpoint in channels.items():
            command += ["--channel", f"{service}={endpoint}"]
        for provider in providers:
            command += ["--provider", provider]
        return subprocess.run(command, capture_output=True, text=True, cwd=ROOT / "tools", encoding="utf-8",
                              errors="replace", check=False)

    def legacy_home(self, root: Path) -> None:
        (root / "client").mkdir()
        (root / "client/mcp_bridge.py").write_text("# bridge\n", encoding="utf-8")
        (root / "codex").mkdir()
        (root / "codex/config.toml").write_text(LEGACY_CODEX, encoding="utf-8")
        (root / "claude.json").write_text(json.dumps(LEGACY_CLAUDE), encoding="utf-8")

    def test_every_owner_token_entry_becomes_the_local_bridge_and_other_servers_stay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_home(root)
            for _ in range(2):  # idempotent
                result = self.install(root, "codex", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
            codex_text = (root / "codex/config.toml").read_text(encoding="utf-8")
            claude_text = (root / "claude.json").read_text(encoding="utf-8")
            self.assertNotIn("apm_owner-personal-token", codex_text + claude_text)
            codex = tomllib.loads(codex_text)
            claude = json.loads(claude_text)["mcpServers"]
            for service, key in (("collaboration", "agent-platform-collaboration"), ("agentMemory", "agentMemory"),
                                 ("projectContext", "projectContext")):
                for provider, entry in (("codex", codex["mcp_servers"][key]), ("claude", claude[key])):
                    self.assertEqual(entry["command"], sys.executable)
                    self.assertEqual(entry["args"][-6:], ["--provider", provider, "--service", service,
                                                          "--endpoint", CHANNELS[service]])
                    self.assertFalse({"url", "headers", "http_headers"} & set(entry))
                self.assertEqual(codex_text.count(f"[mcp_servers.{key}]"), 1)
            self.assertEqual(claude["agentMemory"]["type"], "stdio")
            # Codex keeps one table per Agent Memory tool, as the token installer wrote them; the
            # reads run without asking, so a Codex started without a person still reaches memory.
            memory_tools = codex["mcp_servers"]["agentMemory"]["tools"]
            self.assertEqual(sorted(memory_tools), sorted({*TOOL_NAMES, *READ_TOOL_NAMES}))
            for name, table in memory_tools.items():
                self.assertEqual(table, {"approval_mode": "approve"} if name in READ_TOOL_NAMES else {}, name)
            # The task reads run without asking, every other task tool asks: in values every Codex
            # accepts, since "writes" stopped Codex 0.137 from starting at all.
            tasks = codex["mcp_servers"]["agent-platform-collaboration"]
            self.assertEqual(tasks["default_tools_approval_mode"], "prompt")
            self.assertEqual(tasks["tools"], {name: {"approval_mode": "approve"} for name in COLLABORATION_READ_TOOL_NAMES})
            self.assertNotIn("codexMemory", codex["mcp_servers"])
            self.assertNotIn("codexMemory", claude)
            self.assertEqual(codex["model"], "gpt-6")
            self.assertEqual(codex["mcp_servers"]["yougile"]["url"], "https://yougile.example/mcp")
            self.assertIn("figma", claude)

    def test_a_task_channel_written_with_writes_is_rewritten_and_other_entries_stay(self) -> None:
        # What the client wrote before 2026-10-15: Codex 0.137 refused the whole config with
        # "unknown variant `writes`" and did not start. The next sync replaces the entry, its
        # tool tables included; another server's own settings are not this client's to change.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_home(root)
            bridge = root / "client/mcp_bridge.py"
            arguments = json.dumps(["-E", "-s", str(bridge), "--provider", "codex", "--service", "collaboration",
                                    "--endpoint", TASKS])
            (root / "codex/config.toml").write_text(f"""model = "gpt-6"

[mcp_servers.agent-platform-collaboration]
command = {json.dumps(sys.executable)}
args = {arguments}
enabled = true
default_tools_approval_mode = "writes"

[mcp_servers.agent-platform-collaboration.tools.create_collaboration_work_item]
approval_mode = "approve"

[mcp_servers.someone-elses]
url = "https://other.example/mcp"
default_tools_approval_mode = "writes"

[mcp_servers.someone-elses.tools.search]
approval_mode = "approve"
""", encoding="utf-8")
            result = self.install(root, "codex", channels={"collaboration": TASKS})
            self.assertEqual(result.returncode, 0, result.stderr)
            text = (root / "codex/config.toml").read_text(encoding="utf-8")
            codex = tomllib.loads(text)
            tasks = codex["mcp_servers"]["agent-platform-collaboration"]
            self.assertEqual(tasks["default_tools_approval_mode"], "prompt")
            self.assertEqual(tasks["tools"], {name: {"approval_mode": "approve"} for name in COLLABORATION_READ_TOOL_NAMES})
            self.assertEqual(text.count("[mcp_servers.agent-platform-collaboration]"), 1)
            self.assertEqual(codex["mcp_servers"]["someone-elses"], {
                "url": "https://other.example/mcp", "default_tools_approval_mode": "writes",
                "tools": {"search": {"approval_mode": "approve"}},
            })
            self.assertEqual(codex["model"], "gpt-6")

    def test_opencode_and_kimi_get_their_own_agent_channels(self) -> None:
        # An OpenCode set up by an earlier client holds the owner token for Agent Memory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_home(root)
            (root / "opencode").mkdir()
            (root / "opencode/opencode.jsonc").write_text(
                '{\n  // retained\n  "theme": "system",\n  "mcp": {\n    "agentMemory": {"type": "remote", '
                f'"url": "{MEMORY}", "headers": {{"Authorization": "Bearer apm_owner-personal-token"}}}},\n'
                '    "figma": {"type": "remote", "url": "https://figma.example/mcp"}\n  }\n}\n', encoding="utf-8")
            result = self.install(root, "opencode", "kimi")
            self.assertEqual(result.returncode, 0, result.stderr)
            opencode = json.loads((root / "opencode/opencode.jsonc").read_text(encoding="utf-8"))
            self.assertEqual(opencode["theme"], "system")
            self.assertIn("figma", opencode["mcp"])
            self.assertNotIn("apm_owner-personal-token", json.dumps(opencode))
            entry = opencode["mcp"]["agentMemory"]
            self.assertEqual((entry["type"], entry["enabled"], entry["command"][0]), ("local", True, sys.executable))
            self.assertEqual(entry["command"][-6:], ["--provider", "opencode", "--service", "agentMemory",
                                                     "--endpoint", MEMORY])
            kimi = json.loads((root / "kimi-channels.json").read_text(encoding="utf-8"))["servers"]
            self.assertEqual(set(kimi), {"agent-platform-collaboration", "agentMemory", "projectContext"})
            self.assertEqual(kimi["projectContext"]["args"][-6:], ["--provider", "kimi", "--service",
                                                                   "projectContext", "--endpoint", CONTEXT])
            # Codex and Claude were not asked for: untouched.
            self.assertEqual((root / "codex/config.toml").read_text(encoding="utf-8"), LEGACY_CODEX)

    def test_the_claude_chat_app_gets_its_own_agent_channels(self) -> None:
        # The older adapter started mcp-remote with the owner's token in its environment.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_home(root)
            (root / "claude_desktop_config.json").write_text(json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {
                "agentMemory": {"command": "npx", "args": ["-y", "mcp-remote", MEMORY, "--header",
                                                           "Authorization:${AGENT_MEMORY_AUTH_HEADER}"],
                                "env": {"AGENT_MEMORY_AUTH_HEADER": "Bearer apm_owner-personal-token"}},
                "figma": {"command": "figma-mcp"}}}), encoding="utf-8")
            result = self.install(root, "claude-desktop")
            self.assertEqual(result.returncode, 0, result.stderr)
            text = (root / "claude_desktop_config.json").read_text(encoding="utf-8")
            self.assertNotIn("apm_owner-personal-token", text)
            config = json.loads(text)
            self.assertEqual(config["preferences"], {"theme": "dark"})
            self.assertIn("figma", config["mcpServers"])
            for service, key in (("collaboration", "agent-platform-collaboration"), ("agentMemory", "agentMemory"),
                                 ("projectContext", "projectContext")):
                entry = config["mcpServers"][key]
                self.assertEqual(set(entry), {"command", "args"})
                self.assertEqual(entry["args"][-6:], ["--provider", "claude-desktop", "--service", service,
                                                      "--endpoint", CHANNELS[service]])

    def test_a_missing_bridge_fails_instead_of_writing_a_dead_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.install(Path(directory), "codex")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "codex/config.toml").exists())

    def test_an_insecure_or_unknown_channel_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.legacy_home(root)
            for channels in ({"agentMemory": "http://app.example/agent-memory/mcp"},
                             {"filesystem": "https://app.example/fs"}):
                result = self.install(root, "codex", channels=channels)
                self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "codex/config.toml").read_text(encoding="utf-8"), LEGACY_CODEX)


class ChannelContractTests(unittest.TestCase):
    def test_control_plane_channels_are_read_strictly(self) -> None:
        listed = {"appAgentChannels": [
            {"service": "collaboration", "configKey": "agent-platform-collaboration", "url": TASKS},
            {"service": "agentMemory", "configKey": "agentMemory", "url": MEMORY},
            {"service": "projectContext", "configKey": "projectContext", "url": CONTEXT},
        ]}
        self.assertEqual(execution.app_agent_channels(listed), CHANNELS)
        # Control Plane 1.49-1.50 sent the task channel alone.
        self.assertEqual(execution.app_agent_channels(
            {"collaborationMcp": {"configKey": "agent-platform-collaboration", "url": TASKS}}),
            {"collaboration": TASKS})
        self.assertEqual(execution.app_agent_channels({}), {})
        for broken in (
            [{"service": "agentMemory", "configKey": "agentMemory", "url": "http://app.example/agent-memory/mcp"}],
            [{"service": "agentMemory", "configKey": "projectContext", "url": MEMORY}],
            [{"service": "agentMemory", "configKey": "agentMemory", "url": MEMORY}] * 2,
            [{"service": "filesystem", "configKey": "filesystem", "url": MEMORY}],
        ):
            with self.assertRaises(SystemExit):
                execution.app_agent_channels({"appAgentChannels": broken})


class ConfigureChannelsTests(unittest.TestCase):
    def setUp(self):
        self.desired = {"release": {"agentPlatformSha": "b" * 40}}

    args = SimpleNamespace(control_plane_url="https://app.example/agent-api", client_id="client-1",
                           codex_root=Path("/home/.codex"), claude_config=Path("/home/.claude.json"))
    desired = {"release": {"agentPlatformSha": "c" * 40}}
    local_apps: list[str] = []
    claude_running = False

    def configure(self, refuse: dict[tuple[str, str], str], unregistered: set[str]):
        def register(identity, url, client_id, token, sha):
            self.order.append("register:" + identity.provider)
            if identity.provider in unregistered:
                raise agent_identity.AgentIdentityError("app_agent_owner_required")
            return {"participantId": f"app-{identity.provider}-0123456789abcdef"}

        checked: list[tuple[str, str]] = []
        self.order: list[str] = []

        def retire(args, apps):
            # Never the real home: these tests name a fixture connection.
            self.order.append("retire:" + ",".join(apps))
            return {}

        def check(bridge):
            checked.append((bridge.identity.provider, bridge.service))
            if (bridge.identity.provider, bridge.service) in refuse:
                raise mcp_bridge.BridgeError(refuse[(bridge.identity.provider, bridge.service)])

        # The names are replaced where the function that reads them lives: the entry point has
        # them from a part, and replacing them there would leave the function with its own.
        with patch.object(agent_identity.AppAgentIdentity, "register", register), \
                patch.object(mcp_bridge.Bridge, "check", check), \
                patch.dict(APP_AGENTS, {
                    "installed_local_apps": Mock(return_value=self.local_apps),
                    "claude_desktop_config": Mock(return_value=Path("/home/Claude/claude_desktop_config.json")),
                    "claude_desktop_running": Mock(return_value=self.claude_running),
                    "required_client_token": Mock(return_value="client-token"),
                    "retire_stale_app_agents": Mock(side_effect=retire)}), \
                patch.object(sync_client.subprocess, "run") as run:
            receipts = sync_client.configure_app_agent_channels(
                Path("/release"), self.args, ["codex", "claude"], self.desired, CHANNELS)
        return receipts, run, checked

    def test_each_app_is_registered_and_checked_as_its_agent_before_its_config_changes(self) -> None:
        receipts, run, checked = self.configure({}, {"claude"})
        self.assertEqual(receipts, {
            "codex": {"status": "configured", "participantId": "app-codex-0123456789abcdef"},
            "claude": {"status": "failed", "reason": "app_agent_owner_required"},
        })
        self.assertEqual(checked, [("codex", "collaboration"), ("codex", "agentMemory"), ("codex", "projectContext")])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--provider") + 1], "codex")
        self.assertEqual(command.count("--provider"), 1)
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--channel"],
                         [f"{service}={url}" for service, url in CHANNELS.items()])
        self.assertNotIn("client-token", json.dumps(command))

    def test_an_app_that_cannot_reach_a_channel_keeps_its_config(self) -> None:
        receipts, run, _ = self.configure({("claude", "agentMemory"): "mcp_endpoint_http_401"}, set())
        self.assertEqual(receipts["claude"], {"status": "failed", "reason": "agent_memory_mcp_endpoint_http_401"})
        command = run.call_args.args[0]
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--provider"], ["codex"])

    def test_discovery_never_enrolls_unselected_apps(self) -> None:
        self.local_apps = ["opencode", "kimi", "claude-desktop"]
        receipts, run, checked = self.configure({}, set())
        self.assertEqual(list(receipts), ["codex", "claude"])
        self.assertEqual({provider for provider, _ in checked}, {"codex", "claude"})

    def test_explicitly_selected_additional_apps_get_channels(self) -> None:
        self.desired["appAgentProviders"] = ["codex", "claude", "opencode", "kimi", "claude-desktop"]
        receipts, run, checked = self.configure({}, set())
        self.assertEqual(list(receipts), ["codex", "claude", "opencode", "kimi", "claude-desktop"])
        command = run.call_args.args[0]
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--provider"],
                         ["codex", "claude", "opencode", "kimi", "claude-desktop"])
        # A path of this computer, written the way this computer writes paths (backslashes on Windows).
        self.assertEqual(command[command.index("--claude-desktop-config") + 1],
                         str(Path("/home/Claude/claude_desktop_config.json")))
        self.assertIn("--opencode-root", command)
        self.assertEqual(command[command.index("--kimi-channels") + 1], str(sync_client.KIMI_APP_AGENT_CHANNELS))

    def test_identities_of_another_connection_are_retired_before_any_app_registers(self) -> None:
        self.configure({}, set())
        self.assertEqual(self.order, ["retire:codex,claude", "register:codex", "register:claude"])

    def test_the_claude_chat_app_waits_until_it_is_closed(self) -> None:
        # A running app saves its config over the sync's a minute later.
        self.local_apps, self.claude_running = ["claude-desktop"], True
        self.desired["appAgentProviders"] = ["codex", "claude", "claude-desktop"]
        receipts, run, _ = self.configure({}, set())
        self.assertEqual(receipts["claude-desktop"], {"status": "deferred", "reason": "claude_desktop_running"})
        command = run.call_args.args[0]
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--provider"], ["codex", "claude"])

    def test_no_installer_runs_when_no_app_is_ready(self) -> None:
        receipts, run, _ = self.configure({}, {"codex", "claude"})
        self.assertEqual({receipt["status"] for receipt in receipts.values()}, {"failed"})
        run.assert_not_called()


class ClaudeDesktopRunningTests(unittest.TestCase):
    def test_macos_matches_the_app_itself_including_an_ancestor_process(self) -> None:
        # Without -a, pgrep skips Claude.app when the sync runs inside it.
        with patch.dict(APP_AGENTS, {"client_platform": Mock(return_value="macos")}), \
                patch.object(sync_client.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(sync_client.claude_desktop_running())
        self.assertEqual(run.call_args.args[0], ["pgrep", "-a", "-f", "/Claude.app/Contents/MacOS/Claude$"])


class ContactEvidenceTests(unittest.TestCase):
    def test_sync_probe_never_reports_app_contact_but_real_initialize_does(self):
        identity = Mock()
        bridge = mcp_bridge.Bridge(identity, TASKS, output=io.StringIO())
        with patch.object(bridge, "forward", return_value=[{"jsonrpc": "2.0", "id": "agent-platform-check", "result": {}}]):
            bridge.check()
        identity.report_contact.assert_not_called()
        with patch.object(bridge, "forward", return_value=[{"jsonrpc": "2.0", "id": 1, "result": {}}]):
            bridge.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        identity.report_contact.assert_called_once()

    def test_refused_initialize_is_not_contact(self):
        identity = Mock()
        bridge = mcp_bridge.Bridge(identity, TASKS, output=io.StringIO())
        with patch.object(bridge, "forward", return_value=[{"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}]):
            bridge.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        identity.report_contact.assert_not_called()

    def test_contact_reporting_failure_never_sends_a_second_mcp_response(self):
        identity = Mock()
        identity.report_contact.side_effect = OSError("fixture")
        output = io.StringIO()
        bridge = mcp_bridge.Bridge(identity, TASKS, output=output)
        with patch.object(bridge, "forward", return_value=[{"jsonrpc": "2.0", "id": 1, "result": {}}]):
            bridge.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("result", json.loads(output.getvalue()))

class ClaudeDesktopProcessTests(unittest.TestCase):
    def test_windows_counts_only_the_desktop_app_not_claude_codes_cli(self) -> None:
        with patch.dict(APP_AGENTS, {"client_platform": Mock(return_value="windows")}), \
                patch.dict("os.environ", {"SystemRoot": "C:\\Windows"}), \
                patch.object(sync_client.subprocess, "run", return_value=SimpleNamespace(stdout="0\n")) as run:
            self.assertFalse(sync_client.claude_desktop_running())
        self.assertIn("AnthropicClaude", run.call_args.args[0][-1])
        self.assertEqual(run.call_args.args[0][0], "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")

    def test_a_failed_check_counts_as_running(self) -> None:
        with patch.dict(APP_AGENTS, {"client_platform": Mock(return_value="macos")}), \
                patch.object(sync_client.subprocess, "run", side_effect=OSError("no pgrep")):
            self.assertTrue(sync_client.claude_desktop_running())


class StaleAppAgentTests(unittest.TestCase):
    """A computer that goes from a second account back to the first must not leave the Claude chat
    app, not selected there, working for the second account's organization."""

    URL = "https://app.example/agent-api"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.args = SimpleNamespace(client_id="mac-current", control_plane_url=self.URL,
                                    codex_root=self.home / ".codex", claude_config=self.home / ".claude.json")
        self.desktop = self.home / "Library/Application Support/Claude/claude_desktop_config.json"
        self.desktop.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def identity(self, provider: str, **recorded) -> agent_identity.AppAgentIdentity:
        identity = agent_identity.AppAgentIdentity(provider, home=self.home, windows=False)
        key = identity.keys.load_or_create()
        identity.identity_path.write_text(json.dumps({
            "participantId": f"app-{provider}-0000000000000000", "connectionId": "acn_1", "organizationId": "second",
            "keyId": identity.keys.key_id(key), "audience": "agent-platform", "controlPlaneUrl": self.URL,
            "provider": provider, **recorded}), encoding="utf-8")
        return identity

    def bridge_entry(self, provider: str, service: str = "agentMemory") -> dict:
        return {"command": "/runtime/python3", "args": [
            "-E", "-s", str(self.home / ".agent-platform/client/mcp_bridge.py"),
            "--provider", provider, "--service", service, "--endpoint", MEMORY]}

    def retire(self, *apps: str, claude_running: bool = False) -> dict:
        with patch.dict(APP_AGENTS, {"claude_desktop_running": Mock(return_value=claude_running)}):
            return sync_client.retire_stale_app_agents(self.args, list(apps), self.home)

    def test_an_app_left_out_of_the_new_connection_stops_speaking_for_the_previous_organization(self) -> None:
        left_out = self.identity("claude-desktop", clientId="mac-second")
        self.desktop.write_text(json.dumps({"mcpServers": {
            "agentMemory": self.bridge_entry("claude-desktop"), "figma": {"command": "figma"}}, "theme": "dark"}), encoding="utf-8")
        report = self.retire("codex")
        self.assertEqual(report, {"forgotten": ["claude-desktop"], "cleaned": ["claude-desktop"], "deferred": [], "left": []})
        self.assertFalse(left_out.root.exists())
        with self.assertRaisesRegex(agent_identity.AgentIdentityError, "app_agent_not_registered"):
            agent_identity.AppAgentIdentity("claude-desktop", home=self.home, windows=False).identity()
        self.assertEqual(json.loads(self.desktop.read_text(encoding="utf-8")), {"mcpServers": {"figma": {"command": "figma"}}, "theme": "dark"})
        self.assertEqual(self.retire("codex"), {"forgotten": [], "cleaned": [], "deferred": [], "left": []})

    def test_a_selected_app_does_not_carry_the_previous_connections_key_into_the_new_one(self) -> None:
        carried = self.identity("codex", clientId="mac-second")
        key_id = carried.keys.key_id(carried.keys.load_or_create())
        self.assertEqual(self.retire("codex")["forgotten"], ["codex"])
        fresh = agent_identity.AppAgentIdentity("codex", home=self.home, windows=False)
        self.assertNotEqual(fresh.keys.key_id(fresh.keys.load_or_create()), key_id)

    def test_the_current_connections_agents_stay_with_their_keys(self) -> None:
        current = self.identity("codex", clientId="mac-current")
        # Recorded before the connection was kept: recognised by the participant Control Plane derives.
        derived = __import__("hashlib").sha256(b"mac-current").hexdigest()[:16]
        older = self.identity("claude", participantId=f"app-claude-{derived}")
        only_a_key = agent_identity.AppAgentIdentity("opencode", home=self.home, windows=False)
        only_a_key.keys.load_or_create()
        self.assertEqual(self.retire("codex", "claude", "opencode")["forgotten"], [])
        self.assertTrue(current.identity_path.is_file() and older.identity_path.is_file())
        self.assertTrue(only_a_key.root.is_dir())

    def test_another_server_or_an_unrecognised_older_identity_is_not_this_connection(self) -> None:
        self.identity("codex", clientId="mac-current", controlPlaneUrl="https://other.example/agent-api")
        self.identity("claude")
        self.assertEqual(self.retire("codex", "claude")["forgotten"], ["claude", "codex"])

    def test_the_running_claude_chat_app_loses_its_agent_at_once_and_its_entries_when_closed(self) -> None:
        self.identity("claude-desktop", clientId="mac-second")
        config = {"mcpServers": {"agentMemory": self.bridge_entry("claude-desktop")}}
        self.desktop.write_text(json.dumps(config), encoding="utf-8")
        report = self.retire("codex", claude_running=True)
        self.assertEqual((report["forgotten"], report["cleaned"], report["deferred"]),
                         (["claude-desktop"], [], ["claude-desktop"]))
        self.assertEqual(json.loads(self.desktop.read_text(encoding="utf-8")), config)
        self.assertEqual(self.retire("codex")["cleaned"], ["claude-desktop"])

    def test_only_this_apps_bridge_entries_leave_its_config(self) -> None:
        self.args.claude_config.write_text(json.dumps({"mcpServers": {
            "agentMemory": {"type": "http", "url": MEMORY},
            "projectContext": {"type": "stdio", **self.bridge_entry("claude", "projectContext")},
            "agent-platform-collaboration": self.bridge_entry("codex", "collaboration")}}), encoding="utf-8")
        self.args.codex_root.mkdir()
        (self.args.codex_root / "config.toml").write_text(
            'model = "gpt-6"\n\n[mcp_servers.agentMemory]\ncommand = "/runtime/python3"\nargs = '
            + json.dumps(self.bridge_entry("codex")["args"]) + '\n\n[mcp_servers.agentMemory.tools.search_memory]\n\n'
            '[mcp_servers.yougile]\nurl = "https://yougile.example/mcp"\n', encoding="utf-8")
        opencode = self.home / ".config/opencode"
        opencode.mkdir(parents=True)
        entry = self.bridge_entry("opencode")
        (opencode / "opencode.jsonc").write_text(
            '{\n  // mine\n  "mcp": {"agentMemory": ' + json.dumps({"type": "local", "command": [entry["command"], *entry["args"]]})
            + ', "own": {"type": "remote", "url": "https://own.example"}}\n}\n', encoding="utf-8")
        kimi = self.home / ".agent-platform/client/kimi-app-agent-channels.json"
        kimi.parent.mkdir(parents=True)
        kimi.write_text("{}", encoding="utf-8")
        report = self.retire()
        self.assertEqual(sorted(report["cleaned"]), ["claude", "codex", "kimi", "opencode"])
        self.assertEqual(set(json.loads(self.args.claude_config.read_text(encoding="utf-8"))["mcpServers"]),
                         {"agentMemory", "agent-platform-collaboration"})
        self.assertEqual(tomllib.loads((self.args.codex_root / "config.toml").read_text(encoding="utf-8")),
                         {"model": "gpt-6", "mcp_servers": {"yougile": {"url": "https://yougile.example/mcp"}}})
        text = (opencode / "opencode.jsonc").read_text(encoding="utf-8")
        self.assertIn("// mine", text)
        self.assertNotIn("agentMemory", text)
        self.assertIn("own.example", text)
        self.assertFalse(kimi.exists())

    def test_a_config_that_cannot_be_read_is_reported_and_left(self) -> None:
        self.args.claude_config.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.retire("codex")["left"], ["claude"])
        self.assertEqual(self.args.claude_config.read_text(encoding="utf-8"), "{not json")


class SyncStageTests(unittest.TestCase):
    def run_sync(self, desired_extra: dict, channel_receipts=None, channel_error: Exception | None = None):
        args = SimpleNamespace(
            provider=["codex"], check_only=False, client_id="fixture", project_mappings=Path("mappings.json"),
            update_policy="manual", codex_root=Path("codex"), claude_root=Path("claude"),
            control_plane_url="https://app.example", release_trust_file=Path("trust.json"), release_cache=Path("cache"),
            display_name="Mac",
        )
        desired = {"clientProtocol": 2, "release": {"releaseId": "fixture"}, "projectProfileAssignments": [],
                   **desired_extra}
        # It says its sentences from the client's table.
        helper = Mock(text=sync_client.text, language=sync_client.language)
        helper.read_receipt.return_value = {}
        helper.read_launcher_events.return_value = []
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        helper.needs_update.return_value = False
        helper.project_profiles_need_update.return_value = False
        helper.context_passports_need_update.return_value = False
        helper.load_project_mappings.return_value = []
        # The installed client is the release's: the comparison finds nothing to do.
        helper.plan_client_recovery.return_value = {"action": "none", "root": Path("client"), "report": {"recovery": "none"},
                                                    "releaseId": "fixture", "requested": False, "requests": 0}
        helper.settle_client_recovery.return_value = {"recovery": "none"}
        order: list[str] = []
        helper.configure_managed_mcp_servers.side_effect = lambda *a, **k: order.append("managed") or []

        def channels(*a, **k):
            order.append("channels")
            if channel_error:
                raise channel_error
            return channel_receipts

        helper.configure_app_agent_channels.side_effect = channels
        credentials = {name: {"credentialRevision": "a" * 64} for name in ("agentMemory", "projectContext")}
        with patch.object(execution, "preflight_credentials", return_value=credentials) as preflight, \
                patch.object(execution, "extract_verified", return_value=(Path("a"), Path("b"), Path("c"))), \
                patch.object(execution, "call", return_value={"command": None}), \
                patch.object(execution, "receipts", return_value=[{"provider": "codex", "status": "current"}]), \
                patch.object(execution, "require_scheduler"), \
                patch("builtins.print"):
            code = execution.run_locked(args, desired, ["codex"], helper)
        return code, helper, order, preflight, args

    bridged = {"appAgentChannels": [
        {"service": "collaboration", "configKey": "agent-platform-collaboration", "url": TASKS},
        {"service": "agentMemory", "configKey": "agentMemory", "url": MEMORY},
        {"service": "projectContext", "configKey": "projectContext", "url": CONTEXT},
    ]}

    def test_with_app_agent_channels_no_owner_token_is_fetched_or_written(self) -> None:
        receipts = {"codex": {"status": "configured", "participantId": "app-codex-0123456789abcdef"}}
        _, helper, order, preflight, args = self.run_sync(self.bridged, receipts)
        report = helper.report_state.call_args_list[0].args[1]
        self.assertEqual(report["syncOutcome"], {"status": "succeeded", "stage": None})
        preflight.assert_not_called()
        helper.configure_agent_memory.assert_not_called()
        helper.configure_project_context.assert_not_called()
        self.assertEqual(order, ["managed", "channels"])
        self.assertEqual(args.app_agent_services, set(CHANNELS))
        self.assertEqual(helper.configure_app_agent_channels.call_args.args[4], CHANNELS)
        mcp = report["mcpState"]
        self.assertEqual(mcp["agentMemory"], {"status": "configured", "credentialRevision": None})
        self.assertEqual(mcp["projectContext"], {"status": "configured", "credentialRevision": None})
        self.assertEqual(mcp["collaboration"], receipts)

    def test_the_connection_is_recorded_before_any_stage_can_fail(self) -> None:
        # A sync that fails early must not leave another connection's agents answering.
        code, helper, _, _, args = self.run_sync(self.bridged, channel_error=RuntimeError("installer failed"))
        self.assertEqual(code, 1)
        helper.record_current_connection.assert_called_once_with(args)
        names = [call[0] for call in helper.mock_calls]
        self.assertLess(names.index("record_current_connection"), names.index("configure_managed_mcp_servers"))

    def test_a_deferred_app_does_not_mark_memory_and_context_failed(self) -> None:
        receipts = {"codex": {"status": "configured", "participantId": "app-codex-0123456789abcdef"},
                    "claude-desktop": {"status": "deferred", "reason": "claude_desktop_running"}}
        _, helper, _, _, _ = self.run_sync(self.bridged, receipts)
        mcp = helper.report_state.call_args_list[0].args[1]["mcpState"]
        self.assertEqual(mcp["agentMemory"]["status"], "configured")

    def test_an_app_the_owner_paused_or_revoked_is_not_a_failure(self) -> None:
        receipts = {"codex": {"status": "configured", "participantId": "app-codex-0123456789abcdef"},
                    "kimi": {"status": "failed", "reason": "app_agent_paused"},
                    "opencode": {"status": "failed", "reason": "app_agent_revoked"}}
        _, helper, _, _, _ = self.run_sync(self.bridged, receipts)
        mcp = helper.report_state.call_args_list[0].args[1]["mcpState"]
        self.assertEqual(mcp["agentMemory"]["status"], "configured")

    def test_an_app_without_its_channels_marks_memory_and_context_failed(self) -> None:
        receipts = {"codex": {"status": "failed", "reason": "agent_memory_mcp_endpoint_http_401"}}
        _, helper, _, _, _ = self.run_sync(self.bridged, receipts)
        mcp = helper.report_state.call_args_list[0].args[1]["mcpState"]
        self.assertEqual(mcp["agentMemory"]["status"], "failed")

    def test_the_task_channel_alone_keeps_the_owner_token_path(self) -> None:
        receipts = {"codex": {"status": "configured", "participantId": "app-codex-0123456789abcdef"}}
        _, helper, _, preflight, _ = self.run_sync(
            {"collaborationMcp": {"configKey": "agent-platform-collaboration", "url": TASKS}}, receipts)
        preflight.assert_called_once()
        helper.configure_agent_memory.assert_called_once()
        mcp = helper.report_state.call_args_list[0].args[1]["mcpState"]
        self.assertEqual(mcp["agentMemory"]["credentialRevision"], "a" * 64)

    def test_a_channel_failure_is_reported_as_its_own_stage(self) -> None:
        code, helper, order, _, _ = self.run_sync(self.bridged, channel_error=RuntimeError("installer failed"))
        self.assertEqual(code, 1)
        self.assertEqual(order, ["managed", "channels"])
        outcome = helper.report_state.call_args.args[1]["syncOutcome"]
        self.assertEqual(outcome, {"status": "failed", "stage": "app_agent_channels"})


if __name__ == "__main__":
    unittest.main()
