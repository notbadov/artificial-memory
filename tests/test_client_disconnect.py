"""A computer ends its own connection: Control Plane is told, and its access leaves every app."""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/client/scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import sync_client  # noqa: E402

CODEX = """model = "gpt-6"

[mcp_servers.agent-platform-collaboration]
command = "/python"
args = ["-E", "bridge.py"]
enabled = true

[mcp_servers.agentMemory]
command = "/python"

[mcp_servers.agentMemory.tools.search_memory]

[mcp_servers.team-tracker]
url = "https://tracker.example/mcp"

[mcp_servers.team-tracker.http_headers]
Authorization = "Bearer organization-secret"

[mcp_servers.my-own-server]
url = "https://mine.example/mcp"
"""


class ClientDisconnectTests(unittest.TestCase):
    # A connected computer can leave, so its owner can connect it to another account, and an
    # organization's access is removed from the machine.
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.client = self.home / ".agent-platform/client"
        self.client.mkdir(parents=True)
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text(CODEX, encoding="utf-8")
        (self.client / "managed-mcp-state.json").write_text(json.dumps({"configKeys": ["team-tracker"]}), encoding="utf-8")
        servers = {"agent-platform-collaboration": {"command": "/python"}, "projectContext": {"command": "/python"},
                   "codexMemory": {"command": "old"}, "team-tracker": {"url": "https://tracker.example/mcp"},
                   "my-own-server": {"url": "https://mine.example/mcp"}}
        (self.home / ".claude.json").write_text(json.dumps({"theme": "dark", "mcpServers": servers}), encoding="utf-8")
        desktop = self.home / "Library/Application Support/Claude/claude_desktop_config.json"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({"mcpServers": {"agentMemory": {"command": "/python"}}}), encoding="utf-8")
        opencode = self.home / ".config/opencode"
        opencode.mkdir(parents=True)
        (opencode / "opencode.json").write_text(json.dumps({"mcp": {"agentMemory": {"type": "local"}, "mine": {}}}), encoding="utf-8")
        for name in ("agents/claude", "organization-skills/release", "context-passports", "releases/r1"):
            (self.home / ".agent-platform" / name).mkdir(parents=True)
        (self.home / ".agent-platform/agents/claude/identity.json").write_text("{}", encoding="utf-8")
        (self.client / "credential").write_text("client-secret", encoding="utf-8")
        (self.client / "sync_client.py").write_text("# the client itself stays", encoding="utf-8")
        self.args = SimpleNamespace(
            codex_root=self.home / ".codex", claude_config=self.home / ".claude.json", client_id="mac-person-12345678",
            control_plane_url="https://app.example/agent-api", client_token="client-token",
        )

    def test_access_leaves_every_app_and_nothing_else_is_touched(self) -> None:
        report = sync_client.remove_local_access(self.args, self.home)
        self.assertEqual(report["cleaned"], ["claude", "claude-desktop", "codex", "opencode"])
        codex = tomllib.loads((self.home / ".codex/config.toml").read_text(encoding="utf-8"))
        self.assertEqual((codex["model"], sorted(codex["mcp_servers"])), ("gpt-6", ["my-own-server"]))
        self.assertNotIn("organization-secret", (self.home / ".codex/config.toml").read_text(encoding="utf-8"))
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual((claude["theme"], sorted(claude["mcpServers"])), ("dark", ["my-own-server"]))
        self.assertEqual(json.loads((self.home / ".config/opencode/opencode.json").read_text(encoding="utf-8")), {"mcp": {"mine": {}}})
        desktop = self.home / "Library/Application Support/Claude/claude_desktop_config.json"
        self.assertEqual(json.loads(desktop.read_text(encoding="utf-8")), {"mcpServers": {}})
        # Whatever could not be removed must not speak for the connection that ended.
        marker = json.loads((self.home / ".agent-platform/client/connection.json").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"version": 1, "clientId": None})
        # Keys, credentials and copies of organization data are gone; the platform itself stays.
        for gone in ("agents", "organization-skills", "context-passports", "client/credential", "client/managed-mcp-state.json"):
            self.assertFalse((self.home / ".agent-platform" / gone).exists(), gone)
        for kept in ("releases/r1", "client/sync_client.py"):
            self.assertTrue((self.home / ".agent-platform" / kept).exists(), kept)
        # A second run finds nothing left to do.
        again = sync_client.remove_local_access(self.args, self.home)
        self.assertEqual((again["cleaned"], again["removed"]), ([], []))

    def test_a_name_the_client_wrote_into_one_app_is_left_in_an_app_where_it_is_the_persons(self) -> None:
        # The installer of managed servers records, per app,
        # what it wrote; a server of the same name the person keeps in another app is theirs.
        (self.client / "managed-mcp-state.json").write_text(json.dumps({
            "version": 1, "configKeys": ["team-tracker"], "providers": {"codex": ["team-tracker"], "claude": []},
        }), encoding="utf-8")
        sync_client.remove_local_access(self.args, self.home)
        codex = tomllib.loads((self.home / ".codex/config.toml").read_text(encoding="utf-8"))
        self.assertEqual(sorted(codex["mcp_servers"]), ["my-own-server"])
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(claude["mcpServers"]), ["my-own-server", "team-tracker"])

    def test_a_server_of_the_persons_own_in_kimi_stays_whatever_another_app_was_given(self) -> None:
        # The same in Kimi: Kimi's adapter records what it wrote into
        # Kimi's mcp.json; a name the installer of managed servers wrote into Codex or Claude Code
        # (here team-tracker) is not a reason to take the person's entry of that name out of Kimi.
        kimi = self.home / ".kimi-code"
        (kimi / "agent-platform").mkdir(parents=True)
        persons_own = {"url": "https://tracker.example/mcp", "headers": {"Authorization": "Bearer persons-own-token"}}
        (kimi / "mcp.json").write_text(json.dumps({"mcpServers": {
            "org-crm": {"url": "https://crm.example/mcp"}, "team-tracker": persons_own}}), encoding="utf-8")
        (kimi / "agent-platform/managed-mcp-keys.json").write_text(json.dumps({"keys": ["org-crm"]}), encoding="utf-8")

        report = sync_client.remove_local_access(self.args, self.home)

        self.assertIn("kimi", report["cleaned"])
        self.assertEqual(json.loads((kimi / "mcp.json").read_text(encoding="utf-8")),
                         {"mcpServers": {"team-tracker": persons_own}})

    def test_a_config_that_cannot_be_read_is_reported_and_left_alone(self) -> None:
        (self.home / ".claude.json").write_text("{ not json", encoding="utf-8")
        (self.home / ".config/opencode/opencode.json").unlink()
        (self.home / ".config/opencode/opencode.jsonc").write_text("// broken\n{ \"mcp\": ", encoding="utf-8")
        kimi = self.home / ".kimi-code"
        kimi.mkdir()
        (kimi / "mcp.json").write_text("{ not json", encoding="utf-8")
        report = sync_client.remove_local_access(self.args, self.home)
        self.assertEqual(report["left"], ["claude", "kimi", "opencode"])
        self.assertEqual((self.home / ".claude.json").read_text(encoding="utf-8"), "{ not json")
        self.assertEqual((kimi / "mcp.json").read_text(encoding="utf-8"), "{ not json")

    def test_kimi_and_a_commented_opencode_config_lose_the_access_and_keep_the_rest(self) -> None:
        # Neither may be reported as "left" while it keeps the access.
        (self.home / ".config/opencode/opencode.json").unlink()
        jsonc = self.home / ".config/opencode/opencode.jsonc"
        jsonc.write_text("""{
  // my theme
  "theme": "dark", /* keep */
  "mcp": {
    "agentMemory": { "type": "local", "command": ["/python", "bridge.py"] }, // platform
    "mine": { "type": "remote", "url": "https://mine.example/mcp // not a comment" },
    "team-tracker": { "type": "remote", "url": "https://tracker.example/mcp" }
  }
}
""", encoding="utf-8")
        kimi = self.home / ".kimi-code"
        (kimi / "agent-platform").mkdir(parents=True)
        (kimi / "mcp.json").write_text(json.dumps({"mcpServers": {
            "agentMemory": {"command": "./bin/bridge"}, "org-crm": {"url": "https://crm.example/mcp"},
            "mine": {"url": "https://mine.example/mcp"}}, "other": 1}), encoding="utf-8")
        (kimi / "agent-platform/managed-mcp-keys.json").write_text(json.dumps({"keys": ["agentMemory", "org-crm"]}), encoding="utf-8")

        report = sync_client.remove_local_access(self.args, self.home)
        self.assertEqual(report["left"], [])
        self.assertIn("kimi", report["cleaned"])
        self.assertIn("opencode", report["cleaned"])
        text = jsonc.read_text(encoding="utf-8")
        self.assertIn("// my theme", text)
        self.assertIn("/* keep */", text)
        self.assertNotIn("agentMemory", text)
        self.assertNotIn("team-tracker", text)
        self.assertEqual(json.loads(sync_client._without_jsonc_comments(text)),
                         {"theme": "dark", "mcp": {"mine": {"type": "remote", "url": "https://mine.example/mcp // not a comment"}}})
        self.assertEqual(json.loads((kimi / "mcp.json").read_text(encoding="utf-8")),
                         {"mcpServers": {"mine": {"url": "https://mine.example/mcp"}}, "other": 1})
        self.assertFalse((kimi / "agent-platform/managed-mcp-keys.json").exists())
        again = sync_client.remove_local_access(self.args, self.home)
        self.assertEqual((again["cleaned"], again["left"]), ([], []))

    def test_the_last_and_the_only_property_are_cut_cleanly(self) -> None:
        cut = sync_client.remove_jsonc_properties
        self.assertEqual(json.loads(cut('{"mcp": {"a": 1, "b": [1, {"c": 2}]}}', "mcp", {"b"})), {"mcp": {"a": 1}})
        self.assertEqual(json.loads(cut('{"mcp": {"a": {"x": "}"}}, "t": 1}', "mcp", {"a"})), {"mcp": {}, "t": 1})
        self.assertEqual(cut('{"other": {"a": 1}}', "mcp", {"a"}), '{"other": {"a": 1}}')
        with self.assertRaises(ValueError):
            cut('{"mcp": {"a": ', "mcp", {"a"})

    def test_control_plane_is_told_first_and_the_access_leaves_even_when_it_cannot_be(self) -> None:
        sent = []
        transport = SimpleNamespace(request_json=lambda url, path, body: sent.append((url, path, body)))
        # The transport is replaced where `disconnect` reads it: in the file the function is in.
        leaving = sync_client.disconnect.__globals__
        with patch.dict(leaving, {"release_transport": lambda: transport}), patch("builtins.print") as printed:
            self.assertEqual(sync_client.disconnect(self.args, self.home), 0)
        self.assertEqual(sent, [("https://app.example/agent-api", "/v1/client-session/disconnect",
                                 {"clientId": "mac-person-12345678", "clientToken": "client-token"})])
        result = json.loads(printed.call_args.args[0])
        self.assertEqual((result["status"], result["serverNotified"]), ("disconnected", True))
        self.assertNotIn("client-token", printed.call_args.args[0])

        self.setUp()
        failing = SimpleNamespace(request_json=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
        with patch.dict(leaving, {"release_transport": lambda: failing}), patch("builtins.print") as printed:
            self.assertEqual(sync_client.disconnect(self.args, self.home), 0)
        self.assertFalse(json.loads(printed.call_args.args[0])["serverNotified"])
        self.assertFalse((self.home / ".agent-platform/agents").exists())

    def test_the_client_says_it_can_disconnect(self) -> None:
        # Desktop Companion reads the capability before it disconnects through the installed client.
        source = (ROOT / "adapters/client/scripts/sync_client.py").read_text(encoding="utf-8")
        self.assertIn('CLIENT_CAPABILITIES = ("schedulerCompanion", "disconnect", "appProviders", "consents", "schedulerManual",', source)
        self.assertIn('if getattr(args, "disconnect", False):', source)


if __name__ == "__main__":
    unittest.main()
