"""The local MCP bridge: stdio JSON-RPC forwarded with the app agent's short-lived session."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "adapters/client/scripts"
for directory in (ROOT / "tools", SCRIPTS):
    sys.path.insert(0, str(directory))

from agent_identity import AgentKeyStore, AppAgentIdentity  # noqa: E402
from mcp_bridge import Bridge  # noqa: E402
from test_isolation import sandboxed_environment  # noqa: E402


class FakePlatform(BaseHTTPRequestHandler):
    sessions = 0
    reject_first = False
    seen_tokens: list[str] = []
    organizations: list[str | None] = []
    bodies: list[dict] = []

    def log_message(self, *args) -> None:
        return

    def _json(self, status: int, value=None, content_type="application/json") -> None:
        body = b"" if value is None else (value if isinstance(value, bytes) else json.dumps(value).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/agent-sessions":
            type(self).sessions += 1
            expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
            self._json(200, {"accessToken": f"aas_session{type(self).sessions}", "expiresAt": expires})
            return
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        type(self).seen_tokens.append(token)
        type(self).organizations.append(self.headers.get("X-Agent-Platform-Organization"))
        type(self).bodies.append(payload)
        if self.path == "/project-context/refusing":
            # Project Context answers a refused credential with its own body, not JSON-RPC.
            self._json(403, {"ok": False, "error": "project_access_denied"})
            return
        if type(self).reject_first and token == "aas_session1":
            self._json(401, {"error": "expired"})
            return
        if "id" not in payload:
            self._json(202)
        elif payload["method"] == "stream/test":
            frame = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"streamed": True}})
            self._json(200, f"event: message\ndata: {frame}\n\n".encode(), "text/event-stream")
        elif payload["method"] == "unicode/test":
            self._json(200, {
                "jsonrpc": "2.0", "id": payload["id"], "result": {"message": "Проект готов"},
            })
        else:
            self._json(200, {"jsonrpc": "2.0", "id": payload["id"], "result": {"echo": payload["method"]}})


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        FakePlatform.sessions = 0
        FakePlatform.reject_first = False
        FakePlatform.seen_tokens = []
        FakePlatform.organizations = []
        FakePlatform.bodies = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlatform)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        identity = AppAgentIdentity("claude", home=self.home, windows=False)
        key = identity.keys.load_or_create()
        identity.root.mkdir(parents=True, exist_ok=True)
        identity.identity_path.write_text(json.dumps({
            "participantId": "app-claude-1", "displayName": "Claude Code · Mac", "connectionId": "acn_1",
            "organizationId": "example-organization", "keyId": AgentKeyStore.key_id(key), "audience": "agent-platform",
            "provider": "claude", "controlPlaneUrl": self.base,
        }), encoding="utf-8")
        self.identity = identity

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.temporary.cleanup()

    def bridge_environment(self) -> dict:
        """The environment of a bridge an app starts in this test's home.

        The whole home and not HOME alone: on Windows a process finds its home through
        USERPROFILE, so a bridge given only HOME looked for the agent's identity in the home
        of whoever ran the tests and answered app_agent_not_registered."""
        return {key: value for key, value in sandboxed_environment(self.home).items() if not key.startswith("PYTHON")}

    def run_bridge(self, *messages: dict, path: str = "/mcp", service: str = "collaboration",
                   language: str = "en") -> list[dict]:
        """The answers of a bridge on a computer whose language is `language`."""
        from unittest.mock import patch
        output = io.StringIO()
        bridge = Bridge(self.identity, self.base + path, output=output, service=service)
        with patch.dict(os.environ, {"ARTIFICIAL_MEMORY_LANG": language}):
            bridge.run(io.StringIO("".join(json.dumps(item) + "\n" for item in messages)))
        return sorted((json.loads(line) for line in output.getvalue().splitlines()), key=lambda item: str(item.get("id")))

    def test_requests_are_answered_notifications_are_not_and_streams_are_unwrapped(self) -> None:
        answers = self.run_bridge(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "stream/test"},
        )
        self.assertEqual(answers, [
            {"jsonrpc": "2.0", "id": 1, "result": {"echo": "initialize"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"streamed": True}},
        ])
        self.assertEqual(set(FakePlatform.seen_tokens), {"aas_session1"})
        self.assertEqual(FakePlatform.sessions, 1)

    def test_an_expired_session_is_renewed_once_and_the_request_retried(self) -> None:
        FakePlatform.reject_first = True
        answers = self.run_bridge({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        self.assertEqual(answers, [{"jsonrpc": "2.0", "id": 7, "result": {"echo": "tools/list"}}])
        self.assertEqual(FakePlatform.seen_tokens, ["aas_session1", "aas_session2"])

    def test_an_unreachable_channel_answers_each_request_with_a_clear_error(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        answers = self.run_bridge({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(answers[0]["id"], 3)
        self.assertEqual(answers[0]["error"]["code"], -32000)
        self.assertIn("Artificial Memory tasks is not connected for this app", answers[0]["error"]["message"])

    def test_an_unexpected_failure_still_answers_the_request(self) -> None:
        # A failure inside a worker thread must not vanish and leave the app waiting forever.
        from unittest.mock import patch
        with patch.object(Bridge, "forward", side_effect=ValueError("boom")):
            answers = self.run_bridge({"jsonrpc": "2.0", "id": 9, "method": "tools/call"})
        self.assertEqual(answers[0]["id"], 9)
        self.assertIn("bridge_internal_error_ValueError", answers[0]["error"]["message"])
        self.assertNotIn("boom", answers[0]["error"]["message"])

    def test_the_bridge_starts_isolated_and_never_prints_the_session(self) -> None:
        # Read as the bridge writes: UTF-8, whatever the code page of this computer.
        result = subprocess.run(
            [sys.executable, "-E", "-s", str(SCRIPTS / "mcp_bridge.py"), "--provider", "claude",
             "--service", "collaboration", "--endpoint", self.base + "/mcp"],
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.bridge_environment(),
            timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"jsonrpc": "2.0", "id": 1, "result": {"echo": "ping"}})
        self.assertNotIn("aas_", result.stdout + result.stderr)

    def test_stdio_is_utf8_even_when_the_host_uses_a_legacy_windows_code_page(self) -> None:
        # Windows Codex initializes ASCII-only services, but hides tools as soon as an MCP
        # response contains Cyrillic when the bridge inherits cp1251 for stdout.
        environment = self.bridge_environment()
        bridge_path = str(SCRIPTS / "mcp_bridge.py")
        bootstrap = (
            "import runpy,sys;"
            "sys.stdin.reconfigure(encoding='cp1251');"
            "sys.stdout.reconfigure(encoding='cp1251');"
            f"sys.argv=[{bridge_path!r},'--provider','claude','--service','collaboration',"
            f"'--endpoint',{(self.base + '/mcp')!r}];"
            f"runpy.run_path({bridge_path!r},run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-E", "-s", "-c", bootstrap],
            input=(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "unicode/test"}) + "\n").encode(),
            capture_output=True, env=environment, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), {
            "jsonrpc": "2.0", "id": 8, "result": {"message": "Проект готов"},
        })

    def test_every_request_names_the_agents_organization(self) -> None:
        # Agent Memory refuses a request without it before looking at the session.
        self.run_bridge({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, path="/agent-memory/mcp",
                        service="agentMemory")
        self.assertEqual(FakePlatform.organizations, ["example-organization"])

    def test_a_service_refusal_becomes_an_error_for_the_request_not_a_foreign_body(self) -> None:
        answers = self.run_bridge({"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                  path="/project-context/refusing", service="projectContext")
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["id"], 4)
        self.assertIn("Artificial Memory project context is not connected for this app", answers[0]["error"]["message"])
        self.assertIn("Code: mcp_endpoint_http_403.", answers[0]["error"]["message"])
        self.assertNotIn("Agent Platform", answers[0]["error"]["message"])
        self.assertNotIn("project_access_denied", json.dumps(answers))

    def test_the_sync_check_initializes_as_the_agent_and_reports_a_refusal(self) -> None:
        Bridge(self.identity, self.base + "/agent-memory/mcp", service="agentMemory").check()
        self.assertEqual(FakePlatform.bodies[-1]["method"], "initialize")
        from mcp_bridge import BridgeError
        with self.assertRaisesRegex(BridgeError, "mcp_endpoint_http_403"):
            Bridge(self.identity, self.base + "/project-context/refusing", service="projectContext").check()

    def test_an_agent_the_owner_paused_says_so_instead_of_asking_to_synchronize(self) -> None:
        from unittest.mock import patch
        from agent_identity import AgentIdentityError
        with patch.object(Bridge, "forward", side_effect=AgentIdentityError("app_agent_paused")):
            answers = self.run_bridge({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, service="agentMemory")
        message = answers[0]["error"]["message"]
        self.assertIn("paused for this app by the space owner", message)
        self.assertNotIn("synchronize", message)

    def test_an_agent_of_another_connection_is_named_as_such(self) -> None:
        from unittest.mock import patch
        from agent_identity import AgentIdentityError
        with patch.object(Bridge, "forward", side_effect=AgentIdentityError("app_agent_other_connection")):
            answers = self.run_bridge({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, service="agentMemory")
        self.assertIn("connected under a different account", answers[0]["error"]["message"])

    def test_a_refusal_is_said_in_the_language_of_the_computer(self) -> None:
        # The desktop application names its button in Russian only: a Russian sentence names it,
        # an English one says what to do and holds no Russian word.
        from unittest.mock import patch
        from agent_identity import AgentIdentityError
        for reason in ("app_agent_other_connection", "app_agent_not_registered"):
            with self.subTest(reason=reason), patch.object(Bridge, "forward", side_effect=AgentIdentityError(reason)):
                english = self.run_bridge({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})[0]["error"]["message"]
                russian = self.run_bridge({"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
                                          language="ru")[0]["error"]["message"]
            self.assertNotRegex(english, "[\u0400-\u04ff]")
            self.assertTrue(russian.startswith("Задачи Artificial Memory: "), russian)
            self.assertIn("«Синхронизировать сейчас»", russian)

    def test_an_unknown_service_is_refused(self) -> None:
        with self.assertRaises(Exception):
            Bridge(self.identity, self.base + "/mcp", service="filesystem")

    def test_plain_http_endpoints_are_refused_except_localhost(self) -> None:
        with self.assertRaises(Exception):
            Bridge(self.identity, "http://app.example/mcp")


if __name__ == "__main__":
    unittest.main()
