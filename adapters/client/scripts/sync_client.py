#!/usr/bin/env python3
"""The synchronization client of Artificial Memory: the entry point.

Started by the hourly job of a computer, by the desktop application or by the terminal client,
it brings the AI apps of the computer to the release approved for it: it asks the service for
the desired state, verifies the signed release, installs it into the chosen apps, keeps its
own files those of the release, and reports what it did. "Control Plane" in its options and
messages is the API of the service.

This file owns the arguments, the declaration of the client's files (`--runtime-files`), the
self-check, the installation of the client itself, the run lock and the receipt of a run. The
rest is in the parts next to it (`sync_*.py`, named in MANAGED_CLIENT_PARTS and bound into this
file's namespace while it is imported). The order of one synchronization is in
`control_plane_sync.py`, which is handed that namespace.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


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


def start_in_utf8() -> None:
    """This file's own streams, and the same word to what it starts.

    The installers of a release are started without `-E`: some write straight into the output
    of this process, the others are read here as UTF-8. The variable is the one way to tell an
    interpreter that honours its environment. A client started with `-E` (the self-check of a
    staged release) ignores it and sets its streams itself, as this file does."""
    utf8_streams()
    os.environ["PYTHONIOENCODING"] = "utf-8"


# Before any other file of the client is loaded: a part that does not load is reported in
# these streams, and the report names files under the person's home.
if __name__ == "__main__":
    start_in_utf8()

ADAPTERS_DIR = Path(__file__).resolve().parents[2]
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))
# Started as a script, this file already has its directory first on the path. A caller that
# loads it by path does not, and the client modules imported inside functions live there.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
# An installed client has the installer of managed servers next to this file; a release keeps
# it under tools/. The first run on a computer is the release's own copy of this file, and it
# imports that installer's helpers like any later run.
RELEASE_TOOLS_DIR = SCRIPT_DIR.parent.parent.parent / "tools"
if (not (SCRIPT_DIR / "install_managed_mcp_servers.py").is_file()
        and (RELEASE_TOOLS_DIR / "install_managed_mcp_servers.py").is_file()
        and str(RELEASE_TOOLS_DIR) not in sys.path):
    sys.path.append(str(RELEASE_TOOLS_DIR))

LOCAL_SAFE_TAR = Path(__file__).with_name("safe_tar.py")
if LOCAL_SAFE_TAR.is_file():
    from safe_tar import extract_data_archive
else:
    from common.safe_tar import extract_data_archive


ACTIVE_RUN_RECEIPT_VERSION = 1
RETIRED_CLIENT_FILES = ("migration_access.py", "install_collaboration_channel.py")
# The installed client is a flat directory, not a repository checkout. The installer,
# the start probe and the regression tests all derive the managed file set from here.
MANAGED_CLIENT_ENTRY = "sync_client.py"
MANAGED_CLIENT_MODULES = {
    "sync_guard.py": "adapters/client/scripts/sync_guard.py",
    "release_transport.py": "adapters/client/scripts/release_transport.py",
    "control_plane_sync.py": "adapters/client/scripts/control_plane_sync.py",
    "safe_tar.py": "adapters/common/safe_tar.py",
    # The app agents' identity and the local bridge each app starts for every channel.
    "agent_identity.py": "adapters/client/scripts/agent_identity.py",
    "mcp_bridge.py": "adapters/client/scripts/mcp_bridge.py",
}
MANAGED_CLIENT_TOOLS = {
    "install_project_context_mcp.py": "tools/install_project_context_mcp.py",
    "install_agent_memory_mcp.py": "tools/install_agent_memory_mcp.py",
    "install_managed_mcp_servers.py": "tools/install_managed_mcp_servers.py",
    "install_app_agent_channels.py": "tools/install_app_agent_channels.py",
}
# Files that hold functions of this entry point: part file -> the names taken from it, in the
# order the parts depend on each other. A part lives next to this file in a release and in an
# installed client, and is delivered because the release declares it (--runtime-files); it is
# never added to the release gate in control_plane_sync.py, so its name stays free to change.
# A part does not import this file, which runs as __main__: that would load a second copy.
# A part that is removed joins RETIRED_CLIENT_FILES.
MANAGED_CLIENT_PARTS: dict[str, tuple[str, ...]] = {
    "sync_messages.py": ("MESSAGES", "language", "text"),
    # What a launcher starts before the client. It imports nothing of the client, and the
    # parts after it take from it how a file another program holds is waited for.
    "sync_launch_guard.py": ("PREVIOUS_CLIENT_RUNTIME", "LAUNCHER_EVENTS_FILE", "LAUNCHER_EVENTS",
                             "LAUNCH_GUARD_FILE", "GUARD_NOTHING_TO_RESTORE", "GUARD_RUN_IN_PROGRESS",
                             "GUARD_RESTORE_FAILED", "GUARD_FILES_BUSY", "ClientFilesBusy", "replace_busy",
                             "copy_busy", "remove_busy", "read_busy", "copy_into_place",
                             "replace_previous_runtime", "retry_busy", "held_client_file"),
    "sync_base.py": ("iso_now", "client_platform", "required_client_token", "read_receipt", "skills_digest",
                     "app_chosen", "note_app_failure",
                     "state_digest", "_private_write_text", "_write_json_atomic", "_read_local_json",
                     "installer_accepts", "release_transport", "hold_release_transport"),
    "sync_rules.py": ("RULE_BLOCK_ID", "RULE_BLOCK", "MAX_RULE_BLOCKS_BYTES", "instruction_rule_blocks",
                      "with_instruction_rule_blocks", "instruction_files", "apply_instruction_rules",
                      "remove_instruction_rules"),
    "sync_consents.py": ("consent_root", "TEXT_SUFFIXES", "CONSENT_KEY", "seed_consents_of_a_new_computer",
                         "marked_executable", "skill_consent_key", "mcp_consent_key", "settle_consents",
                         "grant_consent", "skills_the_owner_agreed_to", "servers_the_owner_agreed_to",
                         "WITHDRAWN_POLICY", "RELEASE_ID", "known_withdrawn_releases", "learn_withdrawn_releases",
                         "refuse_withdrawn_release"),
    "sync_recovery.py": ("RECOVERY_STATE_FILE", "managed_client_root", "read_recovery_state",
                         "release_client_refused", "decide_client_recovery", "remove_undeclared_client_files",
                         "plan_client_recovery", "settle_client_recovery"),
    "sync_projects.py": ("CONTEXT_PASSPORT_FIELDS", "CONTEXT_PASSPORT_ITEM_FIELDS", "load_project_mappings",
                         "project_mapping_id", "project_assignment_digest", "project_receipt_path",
                         "read_project_receipt", "project_profiles_need_update", "project_profile_report",
                         "project_receipt_changed", "context_passport_root", "_context_passport_index",
                         "_context_passport_path", "_context_publications", "_validate_context_passport",
                         "read_context_passport_reports", "context_passports_need_update",
                         "install_context_passports"),
    # What a launcher of the hourly job is made of. The two limits are also this file's own:
    # a self-check may take as long for a probe made here as for a launcher's guard.
    "sync_launchers.py": ("CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS", "LAUNCHER_LOCK_WAIT_SECONDS", "SCHEDULE_LAYOUTS",
                          "KEYCHAIN_SERVICE", "KEYCHAIN_ACCOUNT", "KEYCHAIN_SERVICE_NAME", "schedule_layout",
                          "keychain_service", "runtime_installer", "reinstall_command", "macos_launcher",
                          "launch_guard_arguments", "write_launch_guard", "launch_guard_is_current",
                          "remove_launch_guard_without_launcher", "runtime_restore_shell",
                          "runtime_restore_powershell", "powershell_literal", "WINDOWS_ARGUMENTS_FILE",
                          "vbscript_literal", "_scheduled_command", "protect_windows_token"),
    # The hourly job itself. It reads the receipt of a run, which this file writes, and holds
    # what has to wait until that receipt is written.
    "sync_schedule.py": ("SCHEDULED_TASK_SCHEMA_VERSION", "WINDOWS_LAUNCHER_REVISION", "MACOS_LAUNCHER_REVISION",
                         "LOCAL_RUN_RECEIPT_VERSION", "scheduled_task_name", "scheduled_task_receipt_path",
                         "scheduled_task_fingerprint", "scheduled_task_is_current", "scheduled_task_report",
                         "launch_agent_label", "launch_agent_path", "_AT_EXIT_COMMANDS", "run_deferred_commands",
                         "retire_legacy_job", "record_schedule_owner", "retire_terminal_schedule",
                         "install_scheduled_task", "read_local_run_receipt", "LOCAL_OUTCOME_CODE",
                         "companion_scheduler_report", "manual_scheduler_report", "ensure_scheduled_task"),
    # What ending a connection removes, and the command that ends one: it also removes the
    # hourly job, so it is declared after the schedule.
    "sync_disconnect.py": ("ACCESS_CONFIG_KEYS", "ACCESS_DIRECTORIES", "ACCESS_CLIENT_FILES",
                           "CONNECTION_CLIENT_FILES", "connection_file_owner", "_managed_config_keys",
                           "_jsonc_tokens", "remove_jsonc_properties", "_without_jsonc_comments",
                           "remove_opencode_jsonc_access", "KIMI_MANAGED_KEYS_FILE", "remove_kimi_access",
                           "_claude_desktop_configs", "_is_bridge_entry", "_POWERSHELL_WORD", "is_cursor_platform_hook",
                           "remove_cursor_hooks", "remove_cursor_access", "remove_local_access",
                           "disconnect"),
    # Whether every Codex here accepts "writes"; the managed servers below are written by it.
    "sync_codex_versions.py": ("WRITES_APPROVAL_SINCE", "CODEX_APPROVAL_RECORD", "codex_binaries",
                               "check_codex_versions", "managed_servers_for_codex"),
    "sync_mcp.py": ("fetch_project_context_credential", "fetch_agent_memory_credential",
                    "fetch_managed_mcp_credential", "project_context_token_from_codex",
                    "project_context_token_from_claude", "project_context_provider_tokens",
                    "project_context_url_from_codex", "project_context_url_from_claude",
                    "project_context_provider_urls", "agent_memory_token_from_codex",
                    "agent_memory_token_from_claude", "agent_memory_provider_tokens",
                    "agent_memory_url_from_codex", "agent_memory_url_from_claude", "agent_memory_provider_urls",
                    "credential_revision", "resolve_project_context_installer", "resolve_agent_memory_installer",
                    "configure_project_context", "agent_memory_requires_update", "resolve_agent_memory_credential",
                    "configure_agent_memory", "kimi_is_installed", "configure_managed_mcp_servers", "note_restart_required",
                    "cursor_has_managed_entries",
                    "apply_mcp_inventory"),
    "sync_app_agents.py": ("APP_AGENT_SERVICE_REASON", "KIMI_APP_AGENT_CHANNELS", "claude_desktop_config",
                           "claude_desktop_running", "installed_local_apps",
                           "remove_app_agent_channels", "record_current_connection", "retire_stale_app_agents",
                           "configure_app_agent_channels"),
    "sync_skills.py": ("DEFAULT_SKILL_DELIVERY", "skill_delivery", "ORGANIZATION_SKILL_ID", "_safe_relative",
                       "SKILL_TRANSFER_BYTES", "fetch_organization_skills", "run_local_skills", "hand_over_skills",
                       "FOLLOWER_APPS", "cursor_root", "EMPTY_SKILLS_DIGEST", "follower_app_reports",
                       "follower_apps_need_update",
                       "_report_of", "_confirm_follower", "mirrored_skill_roots", "apply_local_skills",
                       "MIRRORED_PLATFORM_STATE", "mirrored_platform_commands", "apply_mirrored_platform",
                       "apply_mirrored_skills"),
    # One app of run_installers; the order around it is still defined below.
    "sync_release.py": ("needs_update", "RELEASE_PROVIDERS", "install_release_into_app"),
}
MAX_LAUNCHER_EVENTS = 20
# What every service accepts in a report. A launcher event or a reason that came later is sent
# to a service that names it in the desired state (`clientReportFields`); a service that does
# not would refuse the whole report, so it is told in the words it knows, or not at all.
LAUNCHER_EVENTS_OF_EVERY_SERVICE = frozenset({"self_check_failed", "runtime_restored", "restore_unavailable"})
EARLIER_LAUNCHER_EVENT = {"self_check_timed_out": "self_check_failed"}


def bind_client_parts(namespace: dict) -> None:
    """Load every declared part from this file's own directory and take its names.

    Strict on purpose, and done while this file is imported: a part that is absent, does not
    import, or lacks a name stops the start, so the self-check of a staged release fails and
    the client already on the computer stays. Nothing is left to fail later, in the middle of
    a run, with a name that was never bound."""
    import importlib.util
    entry_name = Path(MANAGED_CLIENT_ENTRY).stem
    imported_entry = sys.modules.get(entry_name)
    for file_name, names in MANAGED_CLIENT_PARTS.items():
        path = SCRIPT_DIR / file_name
        if not path.is_file():
            raise ImportError(f"Client runtime is incomplete: {file_name}")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        # Under its own name before it runs: a later part that imports this one gets the same
        # module, from this directory, whatever else the path offers.
        sys.modules[path.stem] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(path.stem, None)
            raise
        if sys.modules.get(entry_name) is not imported_entry:
            # This file runs as __main__, so the import made a second copy with its own state.
            raise ImportError(f"Client runtime part {file_name} imports the entry point")
        absent = [name for name in names if not hasattr(module, name)]
        if absent:
            raise ImportError(f"Client runtime part {file_name} does not define: {', '.join(absent)}")
        namespace.update((name, getattr(module, name)) for name in names)


# An installer asks a release for its file list before it stages anything. That answer names
# the parts and uses none of them, so it is given without them: a part that cannot be bound is
# then refused by the self-check of the staged copy, which says what is wrong with it.
if not (__name__ == "__main__" and "--runtime-files" in sys.argv[1:]):
    bind_client_parts(globals())


# How a launcher hands the client its token. It is taken out of this process's environment as
# soon as it is read: otherwise every program the client
# starts (tasklist, git, the installers of the apps) inherits it. The client reads it from its
# arguments from then on; the follow-up run of a replaced client is given it explicitly.
CLIENT_TOKEN_VARIABLE = "AGENT_PLATFORM_CLIENT_TOKEN"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id")
    parser.add_argument("--display-name")
    parser.add_argument("--provider", action="append", choices=("codex", "claude", "kimi", "opencode", "cursor"))
    parser.add_argument("--update-policy", choices=("automatic", "manual"), default="automatic")
    parser.add_argument(
        "--control-plane-url",
        default=os.environ.get(
            "AGENT_PLATFORM_CONTROL_PLANE_URL",
            "https://app.artificial-memory.com/agent-api",
        ),
    )
    parser.add_argument("--stage-only", action="store_true", help="Verify and cache a signed release without changing installed providers")
    parser.add_argument("--initial-install", action="store_true", help="Explicit first install; does not change the future update policy")
    # Given only by a person's own action: the repair in the desktop application, `sync --repair`
    # in the terminal. A release whose client was refused on this computer is then tried once
    # more (sync_recovery.py). No scheduled job is ever written with it.
    parser.add_argument("--repair-client", action="store_true", help="A person asked to repair the client")
    parser.add_argument("--release-trust-file", type=Path)
    parser.add_argument("--release-cache", type=Path, default=Path.home() / ".agent-platform/releases")
    parser.add_argument("--client-token", default=os.environ.pop(CLIENT_TOKEN_VARIABLE, None))
    parser.add_argument("--release-bundle", type=Path)
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--self-check", action="store_true",
        help="Import every managed client module from this directory and exit without contacting Control Plane",
    )
    parser.add_argument(
        "--runtime-files", action="store_true",
        help="Print the client runtime this release declares (installed name -> path in the release) and exit",
    )
    parser.add_argument("--install-scheduled-task", action="store_true")
    parser.add_argument(
        "--disconnect", action="store_true",
        help="End this computer's connection: tell Control Plane and remove its access from every app here",
    )
    parser.add_argument(
        "--grant-consent", metavar="KEY",
        help="Agree to a skill with programs or to a server with automatic approval that waits on this computer",
    )
    parser.add_argument("--pending-consents", action="store_true", help="List what waits for the owner's agreement")
    parser.add_argument(
        "--scheduler", choices=("os", "companion", "manual"), default="os",
        help="companion: Desktop Companion starts every run, so no launchd or Task Scheduler entry is kept; "
             "manual: the person starts every run from the terminal and turned the background schedule off",
    )
    # The layouts and the Keychain service are written out: the declaration of a release
    # (--runtime-files) reads the arguments before any part is bound, and both names live in
    # sync_launchers.py. A test keeps what is written here equal to what the part has.
    parser.add_argument(
        "--schedule-layout", choices=("legacy", "terminal"), default="legacy",
        help="terminal: a connection made by the command-line client. Its hourly job carries the product's "
             "name and, on macOS, reads the credential from the Keychain item Desktop Companion uses",
    )
    parser.add_argument("--keychain-service", default="com.artificial-memory.agent-platform",
                        help="macOS Keychain service of the terminal layout")
    parser.add_argument(
        "--project-context-url",
        default=os.environ.get(
            "PROJECT_CONTEXT_URL", "https://app.artificial-memory.com/project-context/mcp"
        ),
    )
    parser.add_argument("--project-context-token", default=os.environ.get("PROJECT_CONTEXT_ACCESS_TOKEN"))
    parser.add_argument(
        "--agent-memory-url",
        default=os.environ.get(
            "AGENT_MEMORY_URL", "https://app.artificial-memory.com/agent-memory/mcp"
        ),
    )
    parser.add_argument("--agent-memory-token", default=os.environ.get("AGENT_MEMORY_TOKEN"))
    parser.add_argument(
        "--agent-memory-organization",
        default=os.environ.get("AGENT_MEMORY_ORGANIZATION_ID"),
        help="Required tenant id when configuring Agent Memory without Control Plane metadata",
    )
    parser.add_argument(
        "--project-mappings",
        type=Path,
        default=Path.home() / ".agent-platform/project-mappings.json",
    )
    return parser.parse_args()


def default_client_id(hostname: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", hostname.lower()).strip("-.")
    return normalized or hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:16]


def desired_state(args: argparse.Namespace, client_id: str, display_name: str, providers: list[str]) -> dict:
    return release_transport().request_json(args.control_plane_url, "/v1/client-session/desired", {
        "clientId": client_id, "clientToken": required_client_token(args),
        "displayName": display_name, "hostname": platform.node(),
        "platform": client_platform(), "providers": providers, "updatePolicy": args.update_policy,
        # This installer writes Kimi, OpenCode and Cursor entries; older ones reject those targets.
        "capabilities": ["managedMcpMirror", "managedMcpCursor", "appAgents", "appAgentChannels", "organizationSkills", "personalSkills", "pluginSkills", "mcpInventory", "instructionRules"],
    })


def atomic_copy(source: Path, destination: Path) -> None:
    """Replace a file of the client in one step. A file another program holds for a moment (an
    antivirus reading it) is waited for; one that stays held raises ClientFilesBusy."""
    copy_into_place(source, destination)


# What this client can do, read by whoever starts it before relying on it: the desktop
# application (self-check of the installed client) and the installers (declaration of a
# release's client).
CLIENT_CAPABILITIES = ("schedulerCompanion", "disconnect", "appProviders", "consents", "schedulerManual",
                       "terminalSchedule", "releaseRecovery", "repairOnRequest",
                       # Cursor is an app this client can be started with (2026-10-15).
                       "cursorApp")


class ClientRuntimeProbeFailed(RuntimeError):
    code = "client_runtime_probe_failed"


def managed_client_parts() -> dict[str, str]:
    """Installed name -> path in the release, for the parts of this entry point."""
    return {name: f"adapters/client/scripts/{name}" for name in MANAGED_CLIENT_PARTS}


def managed_client_files() -> tuple[str, ...]:
    return (*MANAGED_CLIENT_MODULES, *MANAGED_CLIENT_TOOLS, *MANAGED_CLIENT_PARTS, MANAGED_CLIENT_ENTRY)


def runtime_files() -> dict[str, str]:
    """Installed name -> path in the release, as this release declares its own client runtime."""
    return {MANAGED_CLIENT_ENTRY: f"adapters/client/scripts/{MANAGED_CLIENT_ENTRY}",
            **MANAGED_CLIENT_MODULES, **MANAGED_CLIENT_TOOLS, **managed_client_parts()}


def missing_runtime_files(directory: Path) -> list[str]:
    return sorted(name for name in managed_client_files() if not (directory / name).is_file())


def runtime_self_check(directory: Path) -> dict:
    """Prove that a flat client directory can start a scheduled run.

    Every file the client declares has to be there, and every module has to import. Nothing
    completes a client during a run: the installer copies what the release declares, so a file
    that is absent was lost on the computer, and whoever starts the client (its launcher, the
    desktop application) answers a failed check by restoring the previous client. The parts
    of this file were bound when it was imported: without any one of them this function is
    never reached."""
    import importlib.util
    missing = missing_runtime_files(directory)
    if missing:
        raise ClientRuntimeProbeFailed(f"Client runtime is incomplete: {', '.join(missing)}")
    for name in MANAGED_CLIENT_MODULES:
        spec = importlib.util.spec_from_file_location(f"agent_platform_probe_{Path(name).stem}", directory / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    # The desktop application reads the capabilities before it takes the schedule over. An
    # installing client reads "complete" and refuses a staged client that answers false, so the
    # answer keeps both fields although a client that reaches this line lacks nothing.
    return {"status": "ok", "modules": sorted(MANAGED_CLIENT_MODULES), "complete": True,
            "missing": [], "capabilities": list(CLIENT_CAPABILITIES)}


def probe_client_runtime(directory: Path, *, require_complete: bool = False) -> None:
    """Run the self-check in a fresh interpreter that sees only the given directory.

    A check that fails while another program holds a file of that client says nothing about
    the client: the interpreter could not read the file. It is made again as any held file is
    waited for, and a file that stays held stops the caller with ClientFilesBusy."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}

    def check() -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [sys.executable, "-E", "-s", str(directory / MANAGED_CLIENT_ENTRY), "--self-check"],
            cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS, check=False,
        )
        if completed.returncode != 0:
            held = held_client_file(directory)
            if held is not None:
                # In the words every held file is waited for in.
                raise OSError(errno.EBUSY, "held by another program", str(held))
        return completed

    try:
        completed = retry_busy(check, directory)
    except subprocess.TimeoutExpired as error:
        raise ClientRuntimeProbeFailed("Client runtime self-check timed out") from error
    if completed.returncode != 0:
        detail = (completed.stderr.strip().splitlines() or ["no output"])[-1]
        raise ClientRuntimeProbeFailed(f"Client runtime self-check failed: {detail[:500]}")
    if require_complete:
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            raise ClientRuntimeProbeFailed("Client runtime self-check answered without a result") from None
        # An older release may start with files missing and say so here; one that reports no
        # "complete" at all decided in its own check.
        if result.get("complete") is False:
            raise ClientRuntimeProbeFailed(f"Client runtime is incomplete: {', '.join(result.get('missing', []))}")


def declared_runtime_files(platform_root: Path) -> dict[str, str]:
    """The client runtime the release being installed declares, so an older installer never
    decides which files a newer client needs. A release from before declared runtimes gets
    every file this installer knows that it contains; its own self-check decides."""
    entry = platform_root / "adapters" / "client" / "scripts" / MANAGED_CLIENT_ENTRY
    if not entry.is_file():
        raise ClientRuntimeProbeFailed(text("release_without_sync_client"))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    try:
        completed = subprocess.run(
            [sys.executable, "-E", "-s", str(entry), "--runtime-files"], cwd=entry.parent, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ClientRuntimeProbeFailed("Client runtime declaration timed out") from error
    if completed.returncode != 0:
        return {name: relative for name, relative in runtime_files().items() if (platform_root / relative).is_file()}
    try:
        declared = json.loads(completed.stdout.strip().splitlines()[-1])["files"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        raise ClientRuntimeProbeFailed("Client runtime declaration is invalid") from None
    flat = re.compile(r"[A-Za-z0-9_]+\.py")
    relative = re.compile(r"[A-Za-z0-9_]+(/[A-Za-z0-9_.-]+)*\.py")
    if (not isinstance(declared, dict) or MANAGED_CLIENT_ENTRY not in declared
            or not all(isinstance(name, str) and flat.fullmatch(name) and isinstance(path, str)
                       and relative.fullmatch(path) and ".." not in path.split("/")
                       for name, path in declared.items())):
        raise ClientRuntimeProbeFailed("Client runtime declaration is invalid")
    return declared


def preserve_previous_runtime(managed_dir: Path) -> None:
    """Keep the currently installed runtime as the rollback target, but only if it works.

    The copy that is the rollback target now stays until the new one has taken its place
    (replace_previous_runtime): a file held by another program stops the renewal with
    ClientFilesBusy and leaves the computer with the copy it had."""
    if not (managed_dir / MANAGED_CLIENT_ENTRY).is_file():
        return
    try:
        probe_client_runtime(managed_dir)
    except ClientRuntimeProbeFailed:
        return
    staged = Path(tempfile.mkdtemp(prefix=".previous-", dir=managed_dir))
    try:
        for name in managed_client_files():
            if (managed_dir / name).is_file():
                copy_busy(managed_dir / name, staged / name)
        replace_previous_runtime(managed_dir, staged)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


# Modules of the client a run imports only when it reaches the step that needs them: the
# identity of the apps' agents and the bridge (the channels), and the installer of managed
# servers, whose way of writing a config the rules and the channels use.
MODULES_A_RUN_IMPORTS_LATE = ("agent_identity", "mcp_bridge", "install_managed_mcp_servers")


def keep_what_this_run_still_needs() -> None:
    """Load what the rest of this run needs of the client, before the client's files are replaced.

    A run goes on after it has installed a release: servers, channels, the apps that follow
    the release, rules, the report. The files next to this one are then the release's, written
    for its client, which is older than this one when an organization goes back to a release
    and newer on every update. What this run still uses it therefore takes from memory. A
    module that cannot be loaded now is left for the step that asks for it: this client is
    then damaged, and the release being installed is what repairs it."""
    import importlib
    for name in MODULES_A_RUN_IMPORTS_LATE:
        try:
            importlib.import_module(name)
        except Exception:
            # A damaged module can raise anything when imported (ImportError, SyntaxError or an
            # error of its own top-level code), and none of it may stop the run before it installs
            # the release that repairs the module. The step that needs the module imports it
            # again and reports there what is wrong.
            continue
    hold_release_transport()


def install_managed_runtime(platform_root: Path, managed_dir: Path) -> None:
    """Publish a new client runtime only after it passes a self-check in its installed layout.

    A file that another program holds longer than this client waits stops the installation
    with ClientFilesBusy. Nothing is undone: what was copied is the release's, the entry point
    goes last, and the next run copies again."""
    files = declared_runtime_files(platform_root)
    sources = {name: platform_root / relative for name, relative in files.items()}
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise ClientRuntimeProbeFailed(text("release_without_client_modules", modules=", ".join(missing)))
    managed_dir.mkdir(parents=True, exist_ok=True)
    # Left by an installation whose own directories were held when it ended. Only one run
    # installs at a time (the run lock), so none of them is in use.
    for stale in (*managed_dir.glob(".staging-*"), *managed_dir.glob(".previous-*")):
        shutil.rmtree(stale, ignore_errors=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=managed_dir))
    try:
        for name, path in sources.items():
            copy_busy(path, staging / name)
        probe_client_runtime(staging, require_complete=True)
        preserve_previous_runtime(managed_dir)
        if managed_dir.resolve() == SCRIPT_DIR:
            # The client being replaced is the one that runs.
            keep_what_this_run_still_needs()
        # Dependencies first, entry point last: a scheduled run never starts a new entry
        # point against modules from the previous release.
        for name in sorted(sources, key=lambda name: name == MANAGED_CLIENT_ENTRY):
            atomic_copy(staging / name, managed_dir / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    prune_retired_client_files(managed_dir)


LAUNCHER_EVENT_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# How many lines the events file had when this run read it, by path.
_LAUNCHER_EVENT_LINES_READ: dict[str, int] = {}


def read_launcher_events(client_root: Path | None = None) -> list[dict]:
    """Return the bounded, validated launcher events that have not been reported yet."""
    path = (client_root or Path.home() / ".agent-platform" / "client") / LAUNCHER_EVENTS_FILE
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-MAX_LAUNCHER_EVENTS:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict) and set(value) == {"event", "at"} and value["event"] in LAUNCHER_EVENTS
                and isinstance(value["at"], str) and LAUNCHER_EVENT_TIME.fullmatch(value["at"])):
            events.append(value)
    _LAUNCHER_EVENT_LINES_READ[str(path)] = len(lines)
    return events


def reportable_launcher_events(events: list[dict], desired: dict) -> list[dict]:
    """The launcher events as this service can be told them.

    A service validates each name, and one that is new to it would cost the whole report. Such
    an event is sent under the earlier name that is still true of it (a self-check that was
    ended did fail), or left out when there is none; the events on the computer are untouched."""
    accepted = desired.get("clientReportFields")
    accepted = accepted if isinstance(accepted, list) else []
    reported = []
    for event in events:
        name = event["event"]
        if name not in LAUNCHER_EVENTS_OF_EVERY_SERVICE and f"launcherEvents.{name}" not in accepted:
            name = EARLIER_LAUNCHER_EVENT.get(name)
        if name is not None:
            reported.append({**event, "event": name})
    return reported


def clear_launcher_events(client_root: Path | None = None) -> None:
    """Forget launcher events once the service has accepted a report that carried them.

    What was written down after this run read the file stays for the next report: a job that
    starts during a run finds the lock held and says so in this very file. A file that cannot
    be changed now (a launcher is writing to it) is left as it is; its events are reported
    again, and the service keeps each one once."""
    path = (client_root or Path.home() / ".agent-platform" / "client") / LAUNCHER_EVENTS_FILE
    seen = _LAUNCHER_EVENT_LINES_READ.pop(str(path), None)
    try:
        later = [] if seen is None else path.read_text(encoding="utf-8-sig").splitlines()[seen:]
        if later:
            _private_write_text(path, "\n".join(later) + "\n")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prune_retired_client_files(managed_dir: Path) -> None:
    for name in RETIRED_CLIENT_FILES:
        remove_busy(managed_dir / name)


# Failing stage and reason codes for the local run receipt, read by the desktop app.
# Only identifier-like codes are kept; free-form errors never reach the receipt.
_LOCAL_OUTCOME: dict[str, str] = {}


def record_local_outcome(stage: object, reason: object = None) -> None:
    _LOCAL_OUTCOME.clear()
    for key, value in (("stage", stage), ("reason", reason)):
        if isinstance(value, str) and LOCAL_OUTCOME_CODE.fullmatch(value):
            _LOCAL_OUTCOME[key] = value


def _diagnostic_for_exit(exit_code: int, detail: object = None) -> tuple[str, str | None]:
    if exit_code == 0:
        return "ok", None
    return "sync_failed", text("run_failed_action")


def run_main_with_receipt() -> int:
    """Run the hidden client while retaining only a bounded, redacted outcome."""
    if {"--self-check", "--runtime-files"} & set(sys.argv[1:]):
        # A runtime probe is not a sync run and must not overwrite the client's run receipt.
        # Nor can it replace the client, so nothing looks for a follow-up run: the declaration
        # of a release is answered with no part bound, and that look reads through one.
        return main_once()
    client_id = None
    for index, value in enumerate(sys.argv[:-1]):
        if value == "--client-id":
            candidate = sys.argv[index + 1]
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", candidate):
                client_id = candidate
            break
    client_id = client_id or default_client_id(platform.node())
    root = Path.home() / ".agent-platform" / "client"
    detail: object = None
    try:
        exit_code = main()
    except SystemExit as error:
        detail = error.code
        exit_code = error.code if isinstance(error.code, int) else 1
    except Exception:
        exit_code = 1
    exit_code = exit_code if isinstance(exit_code, int) and 0 <= exit_code <= 255 else 1
    code, action = _diagnostic_for_exit(exit_code, detail)
    receipt = {
        "version": LOCAL_RUN_RECEIPT_VERSION,
        "clientId": client_id,
        "status": "succeeded" if exit_code == 0 else "failed",
        "code": code,
        "exitCode": exit_code,
        "completedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "action": action,
    }
    if _LOCAL_OUTCOME.get("skipped") == "busy":
        return exit_code
    if exit_code != 0:
        receipt.update({key: _LOCAL_OUTCOME[key] for key in ("stage", "reason") if key in _LOCAL_OUTCOME})
    try:
        root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(root / "last-run.json", receipt)
    except OSError:
        pass
    run_deferred_commands()
    return exit_code


def run_installers(
    args: argparse.Namespace,
    desired: dict,
    providers: list[str],
    temporary: Path,
    projects: list[dict],
    client_id: str,
) -> tuple[list[dict], Path]:
    archive = temporary / "release.tar.gz"
    if not args.release_bundle:
        raise RuntimeError("Control Plane did not provide a verified release bundle")
    shutil.copy2(args.release_bundle, archive)
    transport = release_transport()
    manifest = transport.verify_release(desired["release"]["envelope"], args.release_trust_file)
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["bundleSha256"]:
            raise RuntimeError(text("verified_bundle_changed"))
    transport.inspect_bundle(archive, manifest)
    import tarfile
    unpacked = temporary / "unpacked"
    with tarfile.open(archive, "r:gz") as handle:
        extract_data_archive(handle, unpacked)
    roots = [path for path in unpacked.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise SystemExit("release archive has an unexpected layout")
    platform_root = roots[0] / "agent-platform"
    # Apps reach Agent Memory and Project Context as their own agents: no owner token is fetched.
    bridged = {"agentMemory", "projectContext"} <= set(getattr(args, "app_agent_services", ()))
    memory_credential = None
    if not bridged:
        memory_credential = resolve_agent_memory_credential(args, providers, desired, client_id)
        if memory_credential is None:
            raise SystemExit("Agent Memory is unavailable in Control Plane; client configuration was not changed")
    state_path = temporary / "external-skill-state.json"
    state_path.write_text(json.dumps(desired["externalSkillState"], ensure_ascii=False), encoding="utf-8")
    delivery_path = temporary / "skill-delivery.json"
    delivery_path.write_text(json.dumps(skill_delivery(desired), ensure_ascii=False), encoding="utf-8")
    reports: list[dict] = []
    managed_script: Path | None = None
    for provider in providers:
        install_release_into_app(provider, args, desired, temporary, projects, platform_root, archive, state_path,
                                 delivery_path, reports)
    source = platform_root / "adapters" / "client" / "scripts" / "sync_client.py"
    if source.is_file():
        managed_script = Path.home() / ".agent-platform" / "client" / MANAGED_CLIENT_ENTRY
        # A release whose client failed here twice is installed into the apps as usual; its
        # client is not copied a third time (sync_recovery.py), and the run reports that.
        if not release_client_refused(desired["release"]["releaseId"], managed_script.parent):
            install_managed_runtime(platform_root, managed_script.parent)
    if not bridged:
        configure_agent_memory(platform_root, args, providers, desired, client_id, memory_credential)
        configure_project_context(platform_root, args, providers, desired, client_id)
    return reports, managed_script or Path(__file__).resolve()


def report_state(args: argparse.Namespace, payload: dict) -> None:
    release_transport().request_json(args.control_plane_url, "/v1/client-session/report", {
        "clientId": payload["clientId"], "clientToken": required_client_token(args), "report": payload,
    })


FOLLOW_UP_ENVIRONMENT = "AGENT_PLATFORM_SYNC_FOLLOW_UP"


def runtime_fingerprint(directory: Path) -> str:
    """Digest of the installed client files in ``directory`` (the ones a release replaces)."""

    digest = hashlib.sha256()
    for name in sorted(runtime_files()):
        try:
            # A file another program holds for a moment is waited for. Read as missing, it made
            # the client look replaced when it was read again at the end of the run, and the run
            # was made once more for nothing. One that stays held is missing at both looks.
            digest.update(name.encode("utf-8") + b"\0" + read_busy(directory / name) + b"\0")
        except OSError:
            digest.update(name.encode("utf-8") + b"\0missing\0")
    return digest.hexdigest()


def follow_up_needed(before: str, after: str, result: int, environment: dict) -> bool:
    """A sync that replaced the client's own files is finished by the new client.

    The run that installs a release is still the previous client. Without a follow-up every
    step the new client adds (skills for Kimi and OpenCode, a new inventory) would happen only
    at the next sync, and a person would have to synchronize twice. One follow-up, never a chain.
    """

    return result == 0 and before != after and not environment.get(FOLLOW_UP_ENVIRONMENT)


def main() -> int:
    script = Path(__file__).resolve()
    fingerprint = runtime_fingerprint(script.parent)
    # Kept for the follow-up run alone: main_once takes it out of this process's environment.
    token = os.environ.get(CLIENT_TOKEN_VARIABLE)
    result = main_once()
    if follow_up_needed(fingerprint, runtime_fingerprint(script.parent), result, os.environ):
        environment = {**os.environ, FOLLOW_UP_ENVIRONMENT: "1"}
        if token:
            environment[CLIENT_TOKEN_VARIABLE] = token
        return subprocess.run([sys.executable, str(script), *sys.argv[1:]], env=environment, check=False).returncode
    return result


def main_once() -> int:
    started_at = time.monotonic()
    args = parse_args()
    if getattr(args, "self_check", False):
        print(json.dumps(runtime_self_check(Path(__file__).resolve().parent)))
        return 0
    if getattr(args, "runtime_files", False):
        print(json.dumps({"files": runtime_files(), "capabilities": list(CLIENT_CAPABILITIES)}))
        return 0
    if getattr(args, "pending_consents", False):
        try:
            print((consent_root() / "pending-consents.json").read_text(encoding="utf-8"))
        except OSError:
            print(json.dumps({"version": 1, "items": []}))
        return 0
    if getattr(args, "grant_consent", None):
        granted = grant_consent(args.grant_consent)
        print(json.dumps({"granted": granted}))
        return 0 if granted else 1
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_platform_sync_guard", Path(__file__).with_name("sync_guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    root = Path.home() / ".agent-platform/client"
    try:
        with guard.sync_guard(root):
            root.mkdir(parents=True, exist_ok=True)
            active_run_path = root / "current-run.json"
            active_run = {
                "version": ACTIVE_RUN_RECEIPT_VERSION,
                "clientId": args.client_id or default_client_id(platform.node()),
                "pid": os.getpid(),
                "startedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "action": "sync",
            }
            try:
                _write_json_atomic(active_run_path, active_run)
            except OSError:
                pass
            try:
                return main_locked(args, started_at)
            finally:
                try:
                    active_run_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except guard.ClientBusy:
        # Another run holds the lock; its own receipt is the outcome, so this one records nothing.
        _LOCAL_OUTCOME["skipped"] = "busy"
        print(json.dumps({"clientId": args.client_id, "status": "busy",
                          "message": text("sync_already_running")}, ensure_ascii=False))
        return 0


def main_locked(args: argparse.Namespace, started_at: float) -> int:
    # The per-user lock precedes authentication, command claim, scheduler changes,
    # provider writes, credential delivery and report_state.
    if getattr(args, "disconnect", False):
        # Under the same lock as a sync, so no run is writing the configs it cleans.
        return disconnect(args)
    if (not args.release_trust_file or not args.control_plane_url or not args.client_id or args.release_bundle
            or (args.stage_only and args.install_scheduled_task)):
        raise SystemExit(text("control_plane_arguments_required"))
    if args.install_scheduled_task and getattr(args, "scheduler", "os") == "companion":
        raise SystemExit(text("companion_owns_schedule"))
    if args.install_scheduled_task and getattr(args, "scheduler", "os") == "manual":
        raise SystemExit(text("manual_schedule_off"))
    if getattr(args, "initial_install", False):
        seed_consents_of_a_new_computer()
    providers = args.provider or ["codex", "claude"]
    hostname = platform.node()
    client_id = args.client_id or default_client_id(hostname)
    display_name = args.display_name or hostname
    try:
        desired = desired_state(args, client_id, display_name, providers)
    except (Exception, SystemExit):
        print(json.dumps({"clientId": client_id, "status": "failed", "phase": "desired_state",
                          "message": text("desired_state_failed"), "messageLanguage": language()}, ensure_ascii=False))
        return 1
    if not args.stage_only:
        import importlib.util
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("control_plane_sync", Path(__file__).with_name("control_plane_sync.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run(args, desired, SimpleNamespace(**globals()))
    result = release_transport().stage_release(
        args.control_plane_url, client_id, required_client_token(args), desired,
        args.release_trust_file, args.release_cache,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(run_main_with_receipt())
