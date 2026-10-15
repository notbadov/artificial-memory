"""Agent identity of one AI app on this computer.

Each app (Codex, Claude Code, Kimi, OpenCode, Cursor) has its own Ed25519 key under
``~/.agent-platform/agents/<provider>/``. The private key never leaves the computer: the public
key is registered with the service by the synchronization client, and the app's local MCP bridge
exchanges signed assertions for short-lived ``aas_`` sessions. On Windows the key is protected
with DPAPI for the current user; elsewhere it is a 0600 PEM file.

"Control Plane" in the names here is the API of the service. Whatever else speaks as an app's
agent on this computer uses this file too (the work-session hook of the adapters among them),
so there is one implementation. It imports nothing of the client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


# "claude-desktop" is the Claude chat app, a separate app from Claude Code.
APP_AGENT_PROVIDERS = ("codex", "claude", "kimi", "opencode", "claude-desktop", "cursor")
MAX_RESPONSE_BYTES = 1024 * 1024
SESSION_REFRESH_MARGIN = timedelta(seconds=60)
ASSERTION_LIFETIME = timedelta(seconds=60)


class AgentIdentityError(RuntimeError):
    """A stable, secret-free reason; callers show it without request bodies or tokens."""


def agents_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".agent-platform" / "agents"


def bound_to_client(identity: dict[str, Any], provider: str, client_id: str) -> bool:
    """Whether a recorded identity was issued to the computer connection named `client_id`.

    An identity recorded by an earlier client, which did not keep ``clientId``, is recognised by
    the participant id the service derives from the computer's id.
    """
    recorded = identity.get("clientId")
    if isinstance(recorded, str):
        return recorded == client_id
    derived = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
    return identity.get("participantId") == f"app-{provider}-{derived}"


def bound_to_connection(identity: dict[str, Any], provider: str, client_id: str, control_plane_url: str) -> bool:
    """Whether a recorded identity was issued to this computer's current connection.

    Without this check a computer moved to another account would keep speaking for the previous
    organization through an app that is not selected in the new one.
    """
    return (identity.get("controlPlaneUrl") == control_plane_url.rstrip("/")
            and bound_to_client(identity, provider, client_id))


def connection_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".agent-platform" / "client" / "connection.json"


def record_connection(client_id: str | None, home: Path | None = None) -> None:
    """Name the connection this computer has now; ``None`` after it left one.

    Written when a sync starts and when the computer disconnects, before anything else can fail,
    so an identity of another connection is refused at once and not only after a finished sync.
    """
    _write_json(connection_path(home), {"version": 1, "clientId": client_id})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _private_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --- Windows DPAPI (current user), via ctypes so the client needs no extra package ----------

def _dpapi(data: bytes, *, protect: bool) -> bytes:  # pragma: no cover - exercised on Windows
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN: never prompt; the bridge runs without a user interface.
    if not function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise AgentIdentityError("app_agent_key_unreadable")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


class AgentKeyStore:
    """The app's private key: created once, never sent anywhere."""

    def __init__(self, root: Path, *, windows: bool | None = None) -> None:
        self.root = root
        self.windows = sys.platform == "win32" if windows is None else windows
        self.pem_path = root / "private-key.pem"
        self.dpapi_path = root / "private-key.dpapi"

    def _load(self) -> Ed25519PrivateKey | None:
        pem: bytes | None = None
        if self.windows and self.dpapi_path.is_file():
            pem = _dpapi(self.dpapi_path.read_bytes(), protect=False)
        elif self.pem_path.is_file():
            pem = self.pem_path.read_bytes()
        if pem is None:
            return None
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise AgentIdentityError("app_agent_key_invalid")
        if self.windows and self.pem_path.is_file():
            # An older unprotected key file moves under DPAPI.
            self._save(key)
        return key

    def _save(self, key: Ed25519PrivateKey) -> None:
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
        if self.windows:
            _private_write(self.dpapi_path, _dpapi(pem, protect=True))
            if self.pem_path.exists():
                self.pem_path.unlink()
        else:
            _private_write(self.pem_path, pem)

    def load_or_create(self) -> Ed25519PrivateKey:
        key = self._load()
        if key is None:
            key = Ed25519PrivateKey.generate()
            self._save(key)
        return key

    @staticmethod
    def public_key(key: Ed25519PrivateKey) -> str:
        return _b64url(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    @classmethod
    def key_id(cls, key: Ed25519PrivateKey) -> str:
        """Derived from the public key, so a regenerated key never reuses an id."""
        return "key-" + hashlib.sha256(cls.public_key(key).encode("ascii")).hexdigest()[:16]


def signed_assertion(key: Ed25519PrivateKey, identity: dict[str, Any], key_id: str) -> tuple[dict[str, Any], str]:
    current = _now()
    assertion = {
        "organizationId": identity["organizationId"],
        "connectionId": identity["connectionId"],
        "keyId": key_id,
        "audience": identity["audience"],
        "nonce": secrets.token_urlsafe(32),
        "issuedAt": _iso(current),
        "expiresAt": _iso(current + ASSERTION_LIFETIME),
    }
    message = json.dumps(assertion, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return assertion, _b64url(key.sign(message))


def _refusal_reason(error: urllib.error.HTTPError) -> str:
    """The service names app-agent refusals (app_agent_owner_required, app_agent_revoked, ...);
    every other failure is reported by status only, never with the response body."""
    try:
        message = json.loads(error.read(4096)).get("message", "")
    except (OSError, ValueError, AttributeError):
        message = ""
    code = message.split(":", 1)[0] if isinstance(message, str) else ""
    if code.startswith("app_agent_") and code.replace("_", "").isalnum() and len(code) <= 64:
        return code
    return f"control_plane_http_{error.code}"


def post_json(url: str, payload: dict[str, Any], *, token: str = "", timeout: float = 30) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not (parsed.scheme == "https" or local) or not parsed.netloc or parsed.username or parsed.password:
        raise AgentIdentityError("control_plane_url_insecure")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise AgentIdentityError(_refusal_reason(error)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AgentIdentityError("control_plane_unreachable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AgentIdentityError("control_plane_response_too_large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise AgentIdentityError("control_plane_response_invalid") from None
    if not isinstance(value, dict):
        raise AgentIdentityError("control_plane_response_invalid")
    return value


class AppAgentIdentity:
    """Registration and sessions of one app's agent on this computer."""

    def report_contact(self) -> None:
        identity = self.identity()
        post_json(identity["controlPlaneUrl"] + "/v1/agent-session/app-contact", {},
                  token=self.access_token(), timeout=5)

    def __init__(self, provider: str, *, home: Path | None = None, windows: bool | None = None) -> None:
        if provider not in APP_AGENT_PROVIDERS:
            raise AgentIdentityError("app_agent_provider_unsupported")
        self.provider = provider
        self.home = home
        self.root = agents_root(home) / provider
        self.keys = AgentKeyStore(self.root, windows=windows)
        self.identity_path = self.root / "identity.json"
        self.session_path = self.root / "session.json"
        # Concurrent requests of one bridge share one refresh instead of racing for sessions.
        self._session_lock = threading.Lock()

    def register(self, control_plane_url: str, client_id: str, client_token: str, agent_platform_sha: str) -> dict[str, Any]:
        """Run by the managed client on every sync: creates the agent once, then renews it."""
        key = self.keys.load_or_create()
        response = post_json(control_plane_url.rstrip("/") + "/v1/client-session/app-agents", {
            "clientId": client_id, "clientToken": client_token, "provider": self.provider,
            "keyId": self.keys.key_id(key), "publicKey": self.keys.public_key(key),
            "agentPlatformSha": agent_platform_sha,
        })
        agent = response.get("appAgent")
        required = {"participantId", "displayName", "connectionId", "organizationId", "keyId", "audience"}
        if not isinstance(agent, dict) or not required.issubset(agent) or agent["keyId"] != self.keys.key_id(key):
            raise AgentIdentityError("app_agent_registration_invalid")
        identity = {key_name: agent[key_name] for key_name in sorted(required)} | {
            "provider": self.provider, "controlPlaneUrl": control_plane_url.rstrip("/"),
            # The connection this identity belongs to; another connection must not reuse it.
            "clientId": client_id,
        }
        # Where this app's hooks report a live thread; a service that shows none sends no address.
        if isinstance(agent.get("workSessionUrl"), str) and agent["workSessionUrl"].startswith("https://"):
            identity["workSessionUrl"] = agent["workSessionUrl"]
        if _read_json(self.identity_path) != identity:
            _write_json(self.identity_path, identity)
        return identity

    def recorded(self) -> dict[str, Any] | None:
        """What is on disk, complete or not; None when this app has nothing recorded."""
        return _read_json(self.identity_path)

    def forget(self) -> bool:
        """Remove the key, the identity and the session, so this app speaks for no one. True when removed."""
        if not self.root.exists():
            return False
        shutil.rmtree(self.root)
        return True

    def identity(self) -> dict[str, Any]:
        value = _read_json(self.identity_path)
        required = {"participantId", "connectionId", "organizationId", "keyId", "audience", "controlPlaneUrl"}
        if value is None or not required.issubset(value):
            raise AgentIdentityError("app_agent_not_registered")
        # A computer that never recorded its connection (an earlier client did not) is not
        # refused; once the connection is recorded, only its own identities speak.
        marker = connection_path(self.home)
        if marker.exists():
            connection = _read_json(marker)
            current = connection.get("clientId") if connection else None
            if not isinstance(current, str) or not bound_to_client(value, self.provider, current):
                raise AgentIdentityError("app_agent_other_connection")
        return value

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid ``aas_`` session, shared by every bridge of this app through session.json."""
        with self._session_lock:
            return self._access_token(force_refresh)

    def _access_token(self, force_refresh: bool) -> str:
        identity = self.identity()
        if not force_refresh:
            cached = _read_json(self.session_path)
            if cached and cached.get("connectionId") == identity["connectionId"]:
                try:
                    expires = datetime.fromisoformat(str(cached["expiresAt"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    expires = _now()
                if expires - SESSION_REFRESH_MARGIN > _now() and str(cached.get("accessToken", "")).startswith("aas_"):
                    return str(cached["accessToken"])
        key = self.keys.load_or_create()
        if self.keys.key_id(key) != identity["keyId"]:
            raise AgentIdentityError("app_agent_key_changed")
        assertion, signature = signed_assertion(key, identity, identity["keyId"])
        session = post_json(identity["controlPlaneUrl"] + "/v1/agent-sessions",
                            {"assertion": assertion, "signature": signature})
        token = session.get("accessToken")
        if not isinstance(token, str) or not token.startswith("aas_") or not isinstance(session.get("expiresAt"), str):
            raise AgentIdentityError("agent_session_invalid")
        _write_json(self.session_path, {
            "accessToken": token, "expiresAt": session["expiresAt"], "connectionId": identity["connectionId"],
        })
        return token
