"""The one-time command that connects a computer with the system's own Python.

Given an enrollment code of the service, it registers the computer, verifies the approved
signed release and starts that release's `sync_client.py` for the first installation and the
hourly job. It is delivered apart from a release and continues an interrupted connection from
the state it keeps next to itself. "Control Plane" in its name is the API of the service.

Long-lived client credentials never enter process arguments, output or release bundles.
Windows protects them with DPAPI; macOS keeps them in current-user mode-0600 files. A computer
that is already connected is refused: this installer is for a fresh profile only.

What it says to a person comes from its own table, `MESSAGES`, in the language the transport
next to it decides: the service delivers the two files alone, without the client.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parents[2]
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))

from common.safe_tar import extract_data_archive
import release_transport as transport

# key -> (English, Russian). The key is the stable reason of a refusal or the name of a step.
MESSAGES: dict[str, tuple[str, str]] = {
    "windows_protection_failed": (
        "Could not protect the connection for the current Windows user",
        "Не удалось защитить подключение для текущего пользователя Windows"),
    "platform_unsupported": (
        "This installer needs Windows or macOS and Python 3.11 or newer",
        "Для этого установщика нужны Windows или macOS и Python 3.11 или новее"),
    "saved_connection_damaged": (
        "The saved connection is damaged; the repeated installation is stopped",
        "Сохранённое подключение повреждено; повторная установка остановлена"),
    "saved_connection_other_system": (
        "The saved connection was made for another operating system",
        "Сохранённое подключение создано для другой операционной системы"),
    "other_connection_started": (
        "Another connection was already started here. Continue the earlier one or revoke it in the control panel",
        "Здесь уже начато другое подключение. Продолжите прежнее или отзовите его в панели управления"),
    "already_connected": (
        "This user is already connected to Agent Platform. The existing client needs to be moved, not installed again",
        "Этот пользователь уже подключён к Agent Platform. Нужен перенос существующего клиента, а не повторная установка"),
    "connection_in_progress": (
        "The connection is already running in another window", "Подключение уже выполняется в другом окне"),
    "trust_key_changed": (
        "The trusted key changed. Replacing the key automatically is not allowed",
        "Доверенный ключ изменился. Автоматическая замена ключа запрещена"),
    "step_connecting": ("1/4 Connecting the current user…", "1/4 Подключаю текущего пользователя…"),
    "reconnect_unsupported": (
        "Control Plane does not support a protected repeated connection",
        "Control Plane не поддерживает защищённое повторное подключение"),
    "step_continuing": ("1/4 Continuing the saved connection…", "1/4 Продолжаю сохранённое подключение…"),
    "step_verifying": (
        "2/4 Checking the signature and content of the approved bundle…",
        "2/4 Проверяю подпись и содержимое одобренной сборки…"),
    "bootstrap_mismatch": (
        "The trusted installer and the approved version do not agree; update the bootstrap on the server",
        "Доверенный установщик и одобренная версия не согласованы; обновите bootstrap на сервере"),
    "bundle_changed": (
        "The bundle changed after it was downloaded; installation stopped",
        "Сборка изменилась после загрузки; установка остановлена"),
    "client_installer_missing": (
        "The verified bundle has no installer of the client", "В проверенной сборке отсутствует установщик клиента"),
    "step_configuring": (
        "3/4 Setting up skills, memory, context and background updates…",
        "3/4 Настраиваю навыки, память, контекст и фоновое обновление…"),
    "installation_unfinished": (
        "The connection is saved, but the installation did not finish. Run the same command again; no new card will appear",
        "Подключение сохранено, но установка не завершена. Повторите ту же команду; новая карточка не появится"),
    "step_done": (
        "4/4 Installation finished. Restart the connected apps. The result of the check is in the control panel.",
        "4/4 Установка завершена. Перезапустите подключённые приложения. Результат проверки доступен в панели управления."),
    "connection_unfinished": (
        "The connection did not finish. Check the network, the validity of the command and access to the approved "
        "bundle. Saved settings were not removed.",
        "Подключение не завершено. Проверьте сеть, срок действия команды и доступ к одобренной сборке. Сохранённые "
        "настройки не удалены."),
}


def text(key: str, **values: object) -> str:
    """A message of this installer with its values filled in, in the language of the computer."""
    english, russian = MESSAGES[key]
    return (russian if transport.language() == "ru" else english).format(**values)


class Refusal(ValueError):
    """What this installer stops on and says to the person as it is; `code` is its stable
    reason, the key of its sentence. Any other error is the one general sentence (`main`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def private_write(path: Path, value: dict):
    descriptor, temporary = tempfile.mkstemp(prefix=".onboarding-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def windows_system_tool(*parts: str) -> str:
    """The full path of a program of Windows itself, under System32 of this installation.

    Windows looks for a program named without a path in the directory of the process that
    starts it before it looks in PATH, so a `schtasks.exe` left there would run instead of the
    real one, in places with a secret in its environment. Without a SystemRoot that names a
    directory on a drive the system directory is unknown, and nothing is started. Every set of
    files delivered on its own carries this function word for word
    (tests/test_client_without_posix.py)."""
    root = os.environ.get("SystemRoot") or ""
    if not ntpath.isabs(root) or not ntpath.splitdrive(root)[0]:
        raise OSError("SystemRoot does not name the Windows directory; a system tool is not started")
    return ntpath.join(root, "System32", *parts)


def dpapi(value: str, *, decrypt=False) -> str:
    # The subprocess receives the secret only in its environment; stdout is captured.
    environment = os.environ.copy()
    environment.pop("PSModulePath", None)
    environment["AGENT_PLATFORM_BOOTSTRAP_SECRET"] = value
    prefix = (
        '$module=Join-Path $PSHOME "Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1"; '
        'Import-Module $module -Force; '
    )
    code = prefix + (
        "$s=ConvertTo-SecureString $env:AGENT_PLATFORM_BOOTSTRAP_SECRET; "
        "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
        "try {[Console]::Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} "
        "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
        if decrypt else
        "$s=ConvertTo-SecureString $env:AGENT_PLATFORM_BOOTSTRAP_SECRET -AsPlainText -Force; "
        "[Console]::Write(($s | ConvertFrom-SecureString))"
    )
    executable = windows_system_tool("WindowsPowerShell", "v1.0", "powershell.exe")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", code], check=False,
                            env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode or not result.stdout.strip():
        raise Refusal("windows_protection_failed", text("windows_protection_failed"))
    return result.stdout.strip()


def client_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise Refusal("platform_unsupported", text("platform_unsupported"))


def protect_secret(value: str, platform_name: str) -> dict:
    if platform_name == "windows":
        return {"protection": "dpapi", "value": dpapi(value)}
    return {"protection": "mode-0600", "value": value}


def reveal_secret(value: object, platform_name: str) -> str:
    if not isinstance(value, dict) or set(value) != {"protection", "value"} \
            or not isinstance(value["value"], str) or not value["value"]:
        raise Refusal("saved_connection_damaged", text("saved_connection_damaged"))
    expected = "dpapi" if platform_name == "windows" else "mode-0600"
    if value["protection"] != expected:
        raise Refusal("saved_connection_other_system", text("saved_connection_other_system"))
    return dpapi(value["value"], decrypt=True) if platform_name == "windows" else value["value"]


def state_secret(state: dict, name: str, legacy_name: str, platform_name: str) -> str:
    if name in state:
        return reveal_secret(state[name], platform_name)
    if platform_name == "windows" and isinstance(state.get(legacy_name), str):
        return dpapi(state[legacy_name], decrypt=True)
    raise Refusal("saved_connection_damaged", text("saved_connection_damaged"))


def enrollment_fingerprint(config: dict) -> str:
    return hashlib.sha256((config["url"] + "\0" + config["code"]).encode()).hexdigest()


def check_owner(home: Path, config: dict, state: dict | None):
    if state is not None:
        if state.get("version") not in {1, 2} or state.get("enrollmentFingerprint") != enrollment_fingerprint(config):
            raise Refusal("other_connection_started", text("other_connection_started"))
    markers = [home / ".agent-platform/client/sync_client.py", home / ".agent-platform/client/credential.dpapi",
               home / ".agent-platform/client/credential"]
    markers += [home / name / "agent-platform-install.json" for name in (".codex", ".claude")]
    if any(path.exists() for path in markers):
        ownership = home / ".agent-platform/onboarding/owner.json"
        if state is not None and state.get("client") and ownership.is_file():
            owner = json.loads(ownership.read_text(encoding="utf-8"))
            if owner == {"protocol": "control-plane-v1", "clientId": state["client"]["id"], "url": config["url"]}:
                return
        raise Refusal("already_connected", text("already_connected"))


@contextmanager
def bootstrap_lock(root: Path):
    with (root / "onboarding.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Refusal("connection_in_progress", text("connection_in_progress")) from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refusal("connection_in_progress", text("connection_in_progress")) from None
        yield


def install(config: dict, root: Path, home: Path) -> int:
    platform_name = client_platform()
    state_path = root / "session.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    check_owner(home, config, state)
    trust_path = root / "release-trust.json"
    if trust_path.exists() and json.loads(trust_path.read_text(encoding="utf-8")) != config["trust"]:
        raise Refusal("trust_key_changed", text("trust_key_changed"))
    if not trust_path.exists():
        private_write(trust_path, config["trust"])
    if state is None:
        import secrets
        token = secrets.token_urlsafe(48)
        state = {"version": 2, "enrollmentFingerprint": enrollment_fingerprint(config),
                 "url": config["url"], "client": None,
                 "credential": protect_secret(token, platform_name),
                 "bootstrapAgentPlatformSha": config.get("bootstrapAgentPlatformSha"),
                 "enrollmentCode": protect_secret(config["code"], platform_name), "phase": "prepared"}
        private_write(state_path, state)
    else:
        token = state_secret(state, "credential", "credentialDpapi", platform_name)
    if state["client"] is None:
        print(text("step_connecting"), flush=True)
        client = transport.request_json(config["url"], "/v1/client-enrollments/redeem", {
            "code": config["code"], "clientToken": token, "hostname": platform.node(),
            "username": os.environ.get("USERNAME") or os.environ.get("USER", ""), "platform": platform_name,
        })["client"]
        if client.pop("clientToken") != token:
            raise ValueError(text("reconnect_unsupported"))
        state.update(client=client, phase="enrolled")
        private_write(state_path, state)
    else:
        client = state["client"]
        print(text("step_continuing"), flush=True)
    desired = transport.request_json(config["url"], "/v1/client-session/desired", {
        "clientId": client["id"], "clientToken": token, "hostname": platform.node(),
        "displayName": client["displayName"], "platform": platform_name, "providers": client["providers"],
        "updatePolicy": client["updatePolicy"],
    })
    print(text("step_verifying"), flush=True)
    cache = home / ".agent-platform/releases"
    manifest = transport.verify_release(desired["release"]["envelope"], trust_path)
    if manifest["agentPlatformSha"] != config.get("bootstrapAgentPlatformSha"):
        raise Refusal("bootstrap_mismatch", text("bootstrap_mismatch"))
    state["bootstrapAgentPlatformSha"] = config["bootstrapAgentPlatformSha"]
    private_write(state_path, state)
    staged = transport.stage_release(config["url"], client["id"], token, desired, trust_path, cache)
    with tempfile.TemporaryDirectory(prefix="verified-", dir=root) as temporary:
        extracted = Path(temporary)
        # Copy before final verification to prevent a cache replacement between check and extraction.
        archive = extracted / "release.tar.gz"
        import shutil
        shutil.copyfile(staged["bundlePath"], archive)
        with archive.open("rb") as stream:
            if archive.stat().st_size != manifest["bundleSize"] or hashlib.file_digest(stream, "sha256").hexdigest() != manifest["bundleSha256"]:
                raise ValueError(text("bundle_changed"))
        transport.inspect_bundle(archive, manifest)
        with tarfile.open(archive, "r:gz") as handle:
            extract_data_archive(handle, extracted)
        script = extracted / manifest["releaseId"] / "agent-platform/adapters/client/scripts/sync_client.py"
        if not script.is_file():
            raise ValueError(text("client_installer_missing"))
        command = [sys.executable, str(script), "--control-plane-url", config["url"],
                   "--client-id", client["id"], "--display-name", client["displayName"], "--update-policy", client["updatePolicy"],
                   "--release-trust-file", str(trust_path), "--release-cache", str(cache), "--initial-install", "--install-scheduled-task"]
        for provider in client["providers"]:
            command += ["--provider", provider]
        environment = os.environ.copy()
        environment["AGENT_PLATFORM_CLIENT_TOKEN"] = token
        check_owner(home, config, state)
        private_write(root / "owner.json", {"protocol": "control-plane-v1", "clientId": client["id"], "url": config["url"]})
        print(text("step_configuring"), flush=True)
        result = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False)
        if result.returncode:
            state["phase"] = "installation_failed"
            private_write(state_path, state)
            raise Refusal("installation_unfinished", text("installation_unfinished"))
    state["phase"] = "installed"
    private_write(state_path, state)
    print(text("step_done"), flush=True)
    return 0


def utf8_streams() -> None:
    """Read and say everything in UTF-8, whatever the code page of this computer.

    Whoever starts this file reads what it prints as UTF-8. An interpreter decides otherwise:
    on Windows its streams are in the code page of the system, and started with `-E` it ignores
    PYTHONUTF8 and PYTHONIOENCODING, so every sentence that is not ASCII arrives as other
    letters. Every file that is started carries this function word for word: the files are
    delivered in separate sets that do not import each other
    (tests/test_client_output_encoding.py)."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)


def main() -> int:
    utf8_streams()
    try:
        platform_name = client_platform()
        if sys.version_info < (3, 11):
            raise Refusal("platform_unsupported", text("platform_unsupported"))
        root = Path(__file__).resolve().parent
        with bootstrap_lock(root):
            supplied = os.environ.pop("AGENT_PLATFORM_ONBOARDING", None)
            if supplied:
                config = json.loads(supplied)
            else:
                state = json.loads((root / "session.json").read_text(encoding="utf-8"))
                config = {"url": state["url"],
                          "code": state_secret(state, "enrollmentCode", "enrollmentCodeDpapi", platform_name),
                          "bootstrapAgentPlatformSha": state["bootstrapAgentPlatformSha"],
                          "trust": json.loads((root / "release-trust.json").read_text(encoding="utf-8"))}
            return install(config, root, Path.home())
    except Exception as error:
        # Only this installer's own refusals are suitable for a terminal.
        message = str(error) if isinstance(error, Refusal) else text("connection_unfinished")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
