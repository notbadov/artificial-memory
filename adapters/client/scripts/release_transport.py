"""Fetching and verifying a signed release; nothing here changes an AI app.

A release is used only when its envelope is signed with a key of the trust file
(`verify_release`) and its archive is the one the signed manifest names, with a safe layout
and a checksum for every file (`inspect_bundle`). "Control Plane" in the messages is the API
of the service: requests go to it over HTTPS and a redirect is refused. It imports nothing of
the client, so every tool that installs a release can load it by itself.

What it says to a person comes from its own table, `MESSAGES`, in English or in Russian: it is
delivered alone in the bootstrap that connects a computer, without the client's table
(`sync_messages.py`), and decides the language the same way the two clients do.
"""
from __future__ import annotations

import base64
import hashlib
import json
import locale
import os
import re
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
DOMAIN = b"agent-platform-release-v1\x00"
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$")
WINDOWS_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)

# key -> (English, Russian). The key is the stable reason of a refusal; a text takes its values
# by name, the same names in both languages.
MESSAGES: dict[str, tuple[str, str]] = {
    "verifier_missing": (
        "Checking a release needs cryptography; install the client's dependencies before starting it.",
        "Для проверки релиза нужен cryptography; установите зависимости клиента до запуска."),
    "signature_rejected": (
        "The release did not pass the check of its signature or trusted key. Installation was not started.",
        "Релиз не прошёл проверку подписи или доверенного ключа. Установка не начата."),
    "redirect_refused": (
        "A Control Plane redirect is not allowed: the address must be given explicitly.",
        "Перенаправление Control Plane запрещено: адрес должен быть задан явно."),
    "address_invalid": (
        "Control Plane needs an HTTPS address without credentials or parameters.",
        "Control Plane требует HTTPS-адрес без учётных данных и параметров."),
    "request_rejected": (
        "Control Plane rejected the request (HTTP {status}). Check the connection and access to the release.",
        "Control Plane отклонил запрос (HTTP {status}). Проверьте подключение и доступ к релизу."),
    "service_unreachable": (
        "Could not reach Control Plane. The current installation is kept.",
        "Не удалось связаться с Control Plane. Текущая установка сохранена."),
    "answer_too_large": ("The Control Plane answer is too large.", "Ответ Control Plane слишком большой."),
    "answer_invalid": ("The Control Plane answer is invalid.", "Некорректный ответ Control Plane."),
    "archive_unsafe": ("The layout of the release archive is unsafe.", "Небезопасная структура архива релиза."),
    "archive_paths_repeated": ("The archive contains repeated paths.", "Архив содержит повторяющиеся пути."),
    "archive_too_large": (
        "The unpacked release exceeds the allowed size.", "Распакованный релиз превышает допустимый размер."),
    "archive_file_as_directory": (
        "A file path is used as a directory in the archive.", "Путь файла используется как каталог в архиве."),
    "archive_root_mismatch": (
        "The root of the archive does not match the release identifier.",
        "Корень архива не совпадает с идентификатором релиза."),
    "manifest_missing": (
        "The release manifest is missing or damaged.", "Отсутствует или повреждён манифест релиза."),
    "content_mismatch": (
        "The content of the release does not match the signed manifest.",
        "Содержимое релиза не совпадает с подписанным манифестом."),
    "checksums_invalid": (
        "The release's list of checksums is invalid.", "Некорректный список контрольных сумм релиза."),
    "checksum_mismatch": (
        "The checksum of a release file does not match.", "Контрольная сумма файла релиза не совпадает."),
    "checksums_incomplete": (
        "Not every file of the release is covered by a checksum.", "Не все файлы релиза покрыты контрольными суммами."),
    "assigned_version_mismatch": (
        "The assigned version does not match the signed release.",
        "Назначенная версия не совпадает с подписанным релизом."),
    "download_size_mismatch": (
        "The size of the answer does not match the signed release.",
        "Размер ответа не совпадает с подписанным релизом."),
    "download_interrupted": (
        "The download was interrupted. The current installation is kept.",
        "Загрузка прервана. Текущая установка сохранена."),
    "bundle_damaged": (
        "The bundle is damaged: its checksum does not match.", "Сборка повреждена: контрольная сумма не совпадает."),
}


def language() -> str:
    """"ru" on a computer whose language is Russian, "en" on any other.

    Word for word the `language` of both clients (sync_messages.py, cli_messages.py), held so by
    tests/test_client_module_boundaries.py: everything a person reads on one computer is in one
    language, also what the first installer says before any client is there."""
    for name in ("ARTIFICIAL_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "ru" if value.lower().startswith("ru") else "en"
    try:
        value = locale.getlocale()[0] or ""
    except ValueError:
        value = ""
    return "ru" if value.lower().startswith(("ru", "russian")) else "en"


def text(key: str, **values: object) -> str:
    """A message of this file with its values filled in, in the language of the computer."""
    english, russian = MESSAGES[key]
    return (russian if language() == "ru" else english).format(**values)


def require_release_verifier():
    """Load the signature primitive, and say so in plain words when `cryptography` is missing."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:
        raise ValueError(text("verifier_missing")) from error
    return InvalidSignature, Ed25519PublicKey


def signing_bytes(envelope: dict) -> bytes:
    return DOMAIN + json.dumps(
        {key: envelope[key] for key in ("schemaVersion", "keyId", "manifest")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")


def verify_release(envelope: dict, trust_file: Path) -> dict:
    InvalidSignature, Ed25519PublicKey = require_release_verifier()
    try:
        if set(envelope) != {"schemaVersion", "keyId", "manifest", "signature"} or type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1:
            raise ValueError()
        manifest = envelope["manifest"]
        if set(manifest) != {"releaseId", "agentPlatformSha", "homeServerSha", "bundleSha256", "bundleSize", "createdAt", "minClientProtocol"}:
            raise ValueError()
        for field in ("releaseId", "keyId"):
            value = manifest[field] if field == "releaseId" else envelope[field]
            if not isinstance(value, str) or not ID.fullmatch(value):
                raise ValueError()
        for field, length in (("agentPlatformSha", 40), ("homeServerSha", 40), ("bundleSha256", 64)):
            if not isinstance(manifest[field], str) or not re.fullmatch("[0-9a-f]{%d}" % length, manifest[field]):
                raise ValueError()
        if type(manifest["bundleSize"]) is not int or not 1 <= manifest["bundleSize"] <= MAX_BUNDLE_BYTES:
            raise ValueError()
        if type(manifest["minClientProtocol"]) is not int or manifest["minClientProtocol"] != 1:
            raise ValueError()
        if not isinstance(manifest["createdAt"], str) or not manifest["createdAt"].endswith("Z"):
            raise ValueError()
        datetime.fromisoformat(manifest["createdAt"].replace("Z", "+00:00"))
        trust = json.loads(trust_file.read_text(encoding="utf-8"))
        if trust["version"] != 1:
            raise ValueError()
        key = trust["keys"][envelope["keyId"]]
        if key["status"] != "active":
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key["publicKey"], validate=True)).verify(
            base64.b64decode(envelope["signature"], validate=True), signing_bytes(envelope),
        )
    except (ValueError, TypeError, KeyError, OSError, AttributeError, InvalidSignature) as error:
        raise ValueError(text("signature_rejected")) from error
    return manifest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(text("redirect_refused"))


def open_request(base_url: str, path: str, payload: dict):
    url = urllib.parse.urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError(text("address_invalid"))
    request = urllib.request.Request(
        base_url.rstrip("/") + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json, application/gzip"}, method="POST",
    )
    try:
        return urllib.request.build_opener(NoRedirect).open(request, timeout=120)
    except urllib.error.HTTPError as error:
        # Never echo untrusted response bodies (they can include a credential).
        raise ValueError(text("request_rejected", status=error.code)) from error
    except urllib.error.URLError as error:
        raise ValueError(text("service_unreachable")) from error


def request_json(base_url: str, path: str, payload: dict, limit: int = 256 * 1024) -> dict:
    with open_request(base_url, path, payload) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(text("answer_too_large"))
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(text("answer_invalid"))
    return value


def inspect_bundle(archive: Path, manifest: dict) -> dict:
    """Verify bounded portable regular-file archive, internal identity and all checksums.

    No downloaded code is imported, executed or extracted during this operation.
    """
    with tarfile.open(archive, "r:gz") as handle:
        files = {}
        seen = set()
        roots = set()
        total = 0
        for index, member in enumerate(handle):
            path = PurePosixPath(member.name)
            if (index >= 100_000 or not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or ":" in member.name
                    or str(path) != member.name.rstrip("/")
                    or any(WINDOWS_DEVICE.fullmatch(part) for part in path.parts)
                    or any(any(ord(c) < 32 for c in part) or part.endswith((".", " ")) for part in path.parts)
                    or not (member.isdir() or member.isfile())):
                raise ValueError(text("archive_unsafe"))
            normalized = str(path).casefold()
            if normalized in seen:
                raise ValueError(text("archive_paths_repeated"))
            seen.add(normalized)
            roots.add(path.parts[0])
            total += member.size
            if member.size < 0 or total > 2 * 1024 * 1024 * 1024:
                raise ValueError(text("archive_too_large"))
            if member.isfile():
                files[str(path)] = member
        for name in files:
            if any(str(parent) in files for parent in PurePosixPath(name).parents):
                raise ValueError(text("archive_file_as_directory"))
        if roots != {manifest["releaseId"]}:
            raise ValueError(text("archive_root_mismatch"))
        root = manifest["releaseId"] + "/"

        def small_text(name: str, limit: int) -> str:
            item = files.get(root + name)
            if item is None or item.size > limit:
                raise ValueError(text("manifest_missing"))
            with handle.extractfile(item) as stream:
                return stream.read(limit + 1).decode("utf-8")

        inner = json.loads(small_text("manifest.json", 256 * 1024))
        if (inner.get("release_id") != manifest["releaseId"]
                or inner.get("sources", {}).get("agent-platform", {}).get("sha") != manifest["agentPlatformSha"]
                or inner.get("sources", {}).get("home-server-monorepo", {}).get("sha") != manifest["homeServerSha"]
                or inner.get("layout", {}).get("agent-platform") != "agent-platform"):
            raise ValueError(text("content_mismatch"))
        checked = set()
        for line in small_text("checksums.sha256", 16 * 1024 * 1024).splitlines():
            digest, separator, relative = line.partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or relative in checked or root + relative not in files:
                raise ValueError(text("checksums_invalid"))
            with handle.extractfile(files[root + relative]) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(text("checksum_mismatch"))
            checked.add(relative)
        if {root + item for item in checked} != set(files) - {root + "checksums.sha256"}:
            raise ValueError(text("checksums_incomplete"))
    return inner


def _stage_release(base_url: str, desired: dict, trust_file: Path, cache: Path, *, path: str, payload: dict) -> dict:
    manifest = verify_release(desired["release"]["envelope"], trust_file)
    if any(desired["release"].get(key) != manifest[key] for key in ("releaseId", "agentPlatformSha", "bundleSha256")):
        raise ValueError(text("assigned_version_mismatch"))
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (manifest["bundleSha256"] + ".tar.gz")
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        with os.fdopen(fd, "wb") as output, open_request(base_url, path, {
            **payload, "releaseId": manifest["releaseId"],
        }) as response:
            if response.headers.get("Content-Length") != str(manifest["bundleSize"]):
                raise ValueError(text("download_size_mismatch"))
            remaining = manifest["bundleSize"]
            digest = hashlib.sha256()
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(text("download_interrupted"))
                remaining -= len(chunk)
                digest.update(chunk)
                output.write(chunk)
            if response.read(1) or digest.hexdigest() != manifest["bundleSha256"]:
                raise ValueError(text("bundle_damaged"))
            output.flush()
            os.fsync(output.fileno())
        inspect_bundle(Path(temporary), manifest)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"status": "staged", "releaseId": manifest["releaseId"], "bundleSha256": manifest["bundleSha256"],
            "bundlePath": str(target), "installed": False}


def stage_release(base_url: str, client_id: str, client_token: str, desired: dict, trust_file: Path, cache: Path) -> dict:
    return _stage_release(base_url, desired, trust_file, cache, path='/v1/client-session/bundle',
                          payload={'clientId': client_id, 'clientToken': client_token})
