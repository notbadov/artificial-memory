#!/usr/bin/env python3
"""Local stdio MCP server that forwards an app's messages to an Agent Platform MCP endpoint.

Each AI app (Codex, Claude Code, Kimi, OpenCode) launches one bridge per Agent Platform service
from its own MCP config: the task channel, Agent Memory and Project Context. The bridge
authenticates as that app's agent with a short-lived ``aas_`` session from agent_identity.py and
names the agent's organization, so no token is ever written into an app config. Messages are
newline-delimited JSON-RPC on stdin/stdout; responses may be JSON or server-sent events.
Diagnostics on stderr are stable reasons only: never tokens, headers or message bodies.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_identity import APP_AGENT_PROVIDERS, AgentIdentityError, AppAgentIdentity  # noqa: E402
from sync_messages import text  # noqa: E402

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 120
# The services a bridge forwards to; `service_name` says what a person calls each.
SERVICES = ("collaboration", "agentMemory", "projectContext")


class BridgeError(RuntimeError):
    pass


def configure_stdio_utf8() -> None:
    """Keep MCP's stdio transport UTF-8 on every host, including legacy Windows locales."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def service_name(service: str) -> str:
    """The name an app shows when the service is unavailable: the product's name and what the
    service is for, because a person never sees the internal service names."""
    if service == "agentMemory":
        return text("bridge_service_memory")
    if service == "projectContext":
        return text("bridge_service_context")
    return text("bridge_service_tasks")


def refusal_message(service: str, reason: str) -> str:
    """What the app shows, in the language of the computer. A pause or a revocation is a decision
    of the owner of the space, not an outage to fix by synchronizing, and the message says which
    it is."""
    name = service_name(service)
    if reason == "app_agent_paused":
        return text("bridge_agent_paused", service=name)
    if reason == "app_agent_revoked":
        return text("bridge_agent_revoked", service=name)
    if reason == "app_agent_other_connection":
        return text("bridge_agent_other_connection", service=name)
    return text("bridge_not_connected", service=name, reason=reason)


class Bridge:
    def __init__(self, identity: AppAgentIdentity, endpoint: str, output=None, *,
                 service: str = "collaboration") -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not (parsed.scheme == "https" or local) or not parsed.netloc or parsed.username or parsed.password:
            raise BridgeError("mcp_endpoint_insecure")
        if service not in SERVICES:
            raise BridgeError("mcp_service_unsupported")
        self.identity = identity
        self.endpoint = endpoint
        self.service = service
        self.output = output or sys.stdout
        self.session_id: str | None = None
        self._write_lock = threading.Lock()
        self._session_lock = threading.Lock()

    def emit(self, message: Any) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.output.write(line + "\n")
            self.output.flush()

    def _post(self, body: bytes, token: str) -> tuple[int, str, bytes, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            # Agent Memory and Project Context serve several organizations behind one endpoint.
            "X-Agent-Platform-Organization": self.identity.identity()["organizationId"],
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context(),
            ) as response:
                return (response.status, response.headers.get("Content-Type", ""),
                        response.read(MAX_MESSAGE_BYTES + 1), response.headers.get("Mcp-Session-Id"))
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read(MAX_MESSAGE_BYTES + 1), None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError("mcp_endpoint_unreachable") from None

    def forward(self, body: bytes) -> list[Any]:
        """Send one message and return the JSON-RPC messages the endpoint answered with."""
        status, content_type, raw, session_id = self._post(body, self.identity.access_token())
        if status in {401, 403}:
            # The session expired or was revoked: one fresh session, one retry.
            status, content_type, raw, session_id = self._post(body, self.identity.access_token(force_refresh=True))
        if len(raw) > MAX_MESSAGE_BYTES:
            raise BridgeError("mcp_response_too_large")
        if session_id:
            with self._session_lock:
                self.session_id = session_id
        if status == 202 or not raw.strip():
            return []
        if "text/event-stream" in content_type.lower():
            messages = []
            for line in raw.decode("utf-8", errors="replace").splitlines():
                data = line[5:].strip() if line.startswith("data:") else ""
                if data:
                    messages.append(json.loads(data))
            return messages
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise BridgeError(f"mcp_endpoint_http_{status}") from None
        messages = value if isinstance(value, list) else [value]
        if not all(isinstance(item, dict) and item.get("jsonrpc") == "2.0" for item in messages):
            # A service-level refusal ({"ok": false, ...}) is not a JSON-RPC answer: the app gets
            # an error for its request instead of a body it cannot interpret.
            raise BridgeError(f"mcp_endpoint_http_{status}")
        return messages

    def check(self) -> None:
        """Initialize the service as this agent; the sync runs it before an app config changes."""
        answers = self.forward(json.dumps({
            "jsonrpc": "2.0", "id": "agent-platform-check", "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "agent-platform-sync-check", "version": "1"},
            },
        }).encode("utf-8"))
        if not any(isinstance(answer, dict) and answer.get("id") == "agent-platform-check"
                   and isinstance(answer.get("result"), dict) for answer in answers):
            raise BridgeError("mcp_initialize_refused")

    def handle(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return
        requests = message if isinstance(message, list) else [message]
        ids = [item.get("id") for item in requests if isinstance(item, dict) and "id" in item and "method" in item]
        try:
            answers = self.forward(line.encode("utf-8"))
            for answer in answers:
                self.emit(answer)
            # Only an actual app request is evidence of contact. check() deliberately
            # bypasses handle(), so sync probes and session issuance never count.
            initialized = {item.get("id") for item in requests if isinstance(item, dict) and item.get("method") == "initialize"}
            if any(answer.get("id") in initialized and "result" in answer for answer in answers):
                try:
                    self.identity.report_contact()
                except Exception as error:
                    # The MCP response was already delivered: telemetry failure must not
                    # emit a second response for the same request id.
                    reason = str(error) if isinstance(error, AgentIdentityError) else "contact_report_failed"
                    print(f"agent-platform contact report: {reason}", file=sys.stderr, flush=True)
        except Exception as error:  # every request gets an answer, never silence
            reason = str(error) if isinstance(error, (BridgeError, AgentIdentityError)) else (
                f"bridge_internal_error_{type(error).__name__}")
            print(f"agent-platform bridge: {reason}", file=sys.stderr, flush=True)
            for request_id in ids:
                self.emit({"jsonrpc": "2.0", "id": request_id, "error": {
                    "code": -32000, "message": refusal_message(self.service, reason),
                }})

    def run(self, source=None) -> None:
        source = source or sys.stdin
        with ThreadPoolExecutor(max_workers=4) as pool:
            for line in source:
                if line.strip():
                    pool.submit(self.handle, line.strip())


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=APP_AGENT_PROVIDERS)
    parser.add_argument("--service", required=True, choices=sorted(SERVICES))
    parser.add_argument("--endpoint", required=True, help="Agent Platform MCP endpoint, e.g. https://app.artificial-memory.com/mcp")
    args = parser.parse_args(argv)
    try:
        bridge = Bridge(AppAgentIdentity(args.provider), args.endpoint, service=args.service)
    except BridgeError as error:
        print(f"agent-platform bridge: {error}", file=sys.stderr)
        return 2
    bridge.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
