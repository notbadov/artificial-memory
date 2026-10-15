#!/usr/bin/env python3
"""Install the managed sync client for Desktop Companion from the approved signed release.

Desktop Companion 0.3.0 runs this from its signed client runtime (bundled CPython) on first
install and when a person asks it to restore synchronization. It is the install half of the one-time
onboarding command without enrollment and without an OS scheduler: Companion already holds
the client identity in the system credential store and starts every run itself.

The client token arrives only in AGENT_PLATFORM_CLIENT_TOKEN; nothing here writes it to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
for directory in (SCRIPT_DIR, SCRIPT_DIR.parents[1]):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import release_transport as transport  # noqa: E402
# The terminal client's table: it lies next to this file in the same runtime, and both clients
# decide the language the same way.
from cli_messages import text  # noqa: E402

# Flat inside the client runtime archive; repository layout in development and tests.
if (SCRIPT_DIR / "safe_tar.py").is_file():
    from safe_tar import extract_data_archive  # noqa: E402
else:
    from common.safe_tar import extract_data_archive  # noqa: E402

# Every app a computer can be connected for; the release's sync client decides how each is served.
PROVIDERS = ("codex", "claude", "kimi", "opencode", "cursor")
# What a release's client declares when it knows Cursor (sync_client.CLIENT_CAPABILITIES).
CURSOR_CAPABILITY = "cursorApp"


def client_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    raise SystemExit(text("install_platform_unsupported"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane-url", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--update-policy", choices=("automatic", "manual"), default="automatic")
    parser.add_argument("--provider", action="append", choices=PROVIDERS, required=True)
    parser.add_argument("--release-trust-file", type=Path, required=True)
    parser.add_argument("--release-cache", type=Path, default=Path.home() / ".agent-platform/releases")
    # The desktop application starts every run itself. The command-line client has no process of
    # its own between runs, so it asks for the OS schedule, or for none when the person turned it off.
    parser.add_argument("--scheduler", choices=("companion", "os", "manual"), default="companion")
    # Given by the command-line client only; see SCHEDULE_LAYOUTS in sync_launchers.py.
    parser.add_argument("--schedule-layout", choices=("legacy", "terminal"), default="legacy")
    parser.add_argument("--keychain-service")
    # A person asked for a repair (in the desktop application, or `sync --repair` in the terminal)
    # and the installed client could not be given the request itself. Never part of the
    # command a launcher of an hourly job starts.
    parser.add_argument("--repair-client", action="store_true")
    return parser.parse_args(argv)


def release_capabilities(script: Path, environment: dict) -> list[str]:
    """What the release's own synchronization client says it can do. Asked through its
    declaration, which answers from the layout of a release; the self-check is for an installed client."""
    completed = subprocess.run([sys.executable, "-E", "-s", str(script), "--runtime-files"], env=environment,
                               cwd=script.parent, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        capabilities = json.loads(completed.stdout.strip().splitlines()[-1]).get("capabilities", [])
    except (IndexError, ValueError, AttributeError):
        return []
    return [item for item in capabilities if isinstance(item, str)] if completed.returncode == 0 else []


def install(args: argparse.Namespace, token: str) -> dict:
    platform_name = client_platform()
    desired = transport.request_json(args.control_plane_url, "/v1/client-session/desired", {
        "clientId": args.client_id, "clientToken": token, "hostname": platform.node(),
        "displayName": args.display_name, "platform": platform_name, "providers": args.provider,
        "updatePolicy": args.update_policy, "capabilities": ["managedMcpMirror"],
    })
    manifest = transport.verify_release(desired["release"]["envelope"], args.release_trust_file)
    staged = transport.stage_release(
        args.control_plane_url, args.client_id, token, desired, args.release_trust_file, args.release_cache,
    )
    with tempfile.TemporaryDirectory(prefix="agent-platform-companion-install-") as temporary:
        extracted = Path(temporary)
        # Copy before the final check so a cache replacement cannot slip in between.
        archive = extracted / "release.tar.gz"
        shutil.copyfile(staged["bundlePath"], archive)
        with archive.open("rb") as stream:
            if (archive.stat().st_size != manifest["bundleSize"]
                    or hashlib.file_digest(stream, "sha256").hexdigest() != manifest["bundleSha256"]):
                raise SystemExit(text("install_bundle_changed"))
        transport.inspect_bundle(archive, manifest)
        with tarfile.open(archive, "r:gz") as handle:
            extract_data_archive(handle, extracted)
        script = extracted / manifest["releaseId"] / "agent-platform/adapters/client/scripts/sync_client.py"
        if not script.is_file():
            raise SystemExit(text("install_bundle_without_client"))
        command = [
            sys.executable, "-E", "-s", str(script),
            "--control-plane-url", args.control_plane_url, "--client-id", args.client_id,
            "--display-name", args.display_name, "--update-policy", args.update_policy,
            "--release-trust-file", str(args.release_trust_file), "--release-cache", str(args.release_cache),
            "--initial-install", "--scheduler", args.scheduler,
        ]
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        providers = list(args.provider)
        if "cursor" in providers and CURSOR_CAPABILITY not in release_capabilities(script, environment):
            # The approved release's client stops on a name it does not know: it is given the
            # other apps, and Cursor follows with the release that knows it.
            providers = [provider for provider in providers if provider != "cursor"]
            if not providers:
                return {"status": "failed", "releaseId": manifest["releaseId"], "detail": "release_without_cursor"}
        for provider in providers:
            command += ["--provider", provider]
        if args.schedule_layout == "terminal":
            # An approved release from before the command-line client would stop on the option
            # below with nothing a person can read.
            if "terminalSchedule" not in release_capabilities(script, environment):
                return {"status": "failed", "releaseId": manifest["releaseId"], "detail": "release_without_terminal_schedule"}
            command += ["--schedule-layout", "terminal"]
            if args.keychain_service:
                command += ["--keychain-service", args.keychain_service]
        if args.repair_client and "repairOnRequest" in release_capabilities(script, environment):
            # Handed on to the release's own client, which decides what a request changes. A
            # release from before requests would stop on the option, and runs as it always did.
            command.append("--repair-client")
        environment["AGENT_PLATFORM_CLIENT_TOKEN"] = token
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        detail = (completed.stdout.strip().splitlines() or completed.stderr.strip().splitlines() or ["no output"])[-1]
        return {"status": "failed", "releaseId": manifest["releaseId"], "detail": detail[:1000]}
    return {"status": "installed", "releaseId": manifest["releaseId"]}


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


def main(argv: list[str] | None = None) -> int:
    utf8_streams()
    args = parse_args(argv)
    token = os.environ.pop("AGENT_PLATFORM_CLIENT_TOKEN", "")
    if not token:
        print(json.dumps({"status": "failed", "detail": "client token is missing"}), flush=True)
        return 2
    try:
        result = install(args, token)
    except (Exception, SystemExit) as error:
        result = {"status": "failed", "detail": str(error)[:1000] or error.__class__.__name__}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
