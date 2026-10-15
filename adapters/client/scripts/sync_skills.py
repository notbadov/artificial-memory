"""Skills on this computer beyond the ones a release installs into Codex and Claude Code.

The organization's skills and the owner's own are fetched from the service and installed by
the release's tool on every sync, because they change without a release. A skill the owner
asked for in the web application is handed over from the app's own folder. Kimi Code and
OpenCode follow the computer's release through their adapters: the platform's part of them and
the same skills are brought to them here, and each says what it has.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the consents and the app agents (where Kimi's channels are kept).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from sync_app_agents import KIMI_APP_AGENT_CHANNELS
from sync_disconnect import remove_cursor_hooks
from sync_base import (_read_local_json, _write_json_atomic, app_chosen, installer_accepts, release_transport,
                       required_client_token)
from sync_consents import skills_the_owner_agreed_to
from sync_messages import text

DEFAULT_SKILL_DELIVERY = {"version": 1, "audiences": ["everyone"], "disabled": []}


def skill_delivery(desired: dict) -> dict:
    """Which platform skills this organization's computers receive; installers validate it.

    An older service sends none: then no platform skill is treated as switched off.
    """

    value = desired.get("skillDelivery")
    if isinstance(value, dict) and set(value) == set(DEFAULT_SKILL_DELIVERY):
        return value
    return dict(DEFAULT_SKILL_DELIVERY)


# One path segment, as the service names skills: no separators, no dots at the start.
ORGANIZATION_SKILL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")


def _safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts or path.startswith(("/", "\\")):
        raise RuntimeError(f"organization skill file path is not allowed: {path}")
    return candidate


# A skill is up to 4 MiB; base64 and JSON escaping add to it. Under the general limit of a
# response (256 KiB) no larger skill would arrive.
SKILL_TRANSFER_BYTES = 9 * 1024 * 1024


def fetch_organization_skills(args: argparse.Namespace, client_id: str, delivered: list[dict], staging: Path) -> None:
    """Download each delivered skill (the organization's or the owner's) into staging/<id>; the installer checks digests."""

    staging_root = staging.resolve()
    for item in delivered:
        # The skill id comes from the service and is joined to the staging path. An absolute id
        # replaces the path it is joined to, so unchecked it would let a compromised or
        # impersonated service write any file the user can write.
        skill_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(skill_id, str) or not ORGANIZATION_SKILL_ID.fullmatch(skill_id):
            raise RuntimeError("organization skill id is not allowed")
        response = release_transport().request_json(args.control_plane_url, "/v1/client-session/organization-skill", {
            "clientId": client_id, "clientToken": required_client_token(args),
            "skillId": skill_id, "version": item["version"],
        }, limit=SKILL_TRANSFER_BYTES)
        skill = response.get("skill") if isinstance(response, dict) else None
        files = skill.get("files") if isinstance(skill, dict) else None
        if not isinstance(files, list):
            continue
        root = staging / skill_id
        for entry in files:
            target = root / _safe_relative(str(entry.get("path", "")))
            if not target.resolve().is_relative_to(staging_root / skill_id):
                raise RuntimeError("organization skill file path is not allowed")
            target.parent.mkdir(parents=True, exist_ok=True)
            content = entry.get("content", "")
            data = base64.b64decode(content) if entry.get("encoding") == "base64" else str(content).encode("utf-8")
            target.write_bytes(data)


def run_local_skills(platform_root: Path, arguments: list[str]) -> dict | None:
    """Organization skills and the skill inventory, from the release's own tool (None if absent)."""

    tool = platform_root / "tools" / "local_skills.py"
    if not tool.is_file():
        return None
    completed = subprocess.run([sys.executable, str(tool), *arguments], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return json.loads(completed.stdout)


def hand_over_skills(platform_root: Path, args: argparse.Namespace, desired: dict, client_id: str) -> None:
    """Send the skills the owner asked this computer for in the web application.

    Only a skill named in the desired state is read, and only from the app's own skills
    directory. A failure leaves the request waiting for the next sync.
    """

    requests = desired.get("skillImports")
    for item in requests if isinstance(requests, list) else []:
        provider, skill_id, request_id = item.get("provider"), item.get("skillId"), item.get("requestId")
        if provider not in {"codex", "claude"} or not isinstance(skill_id, str) or not isinstance(request_id, str):
            continue
        root = args.codex_root if provider == "codex" else args.claude_root
        plugin_id = item.get("pluginId")
        try:
            exported = run_local_skills(platform_root, [
                "export", "--skills-root", str(root / "skills"), "--skill-id", skill_id,
                # From one of the app's plugins: the owner asked for a personal copy of its skill.
                *(["--provider", provider, "--plugin-id", plugin_id] if isinstance(plugin_id, str) else []),
            ])
            if exported is None:
                return
            response = release_transport().request_json(args.control_plane_url, "/v1/client-session/skill-import", {
                "clientId": client_id, "clientToken": required_client_token(args), "requestId": request_id,
                "files": exported.get("files"), "error": exported.get("error"),
                **({"pluginVersion": exported["pluginVersion"]} if exported.get("pluginVersion") else {}),
            })
        except (subprocess.CalledProcessError, ValueError, OSError):
            continue
        # The skill now belongs to the owner at the service: install it in this same sync, so the
        # folder is adopted at once instead of staying "only on this computer" until the next one.
        accepted = response.get("import", {}).get("skill") if isinstance(response.get("import"), dict) else None
        delivered = desired.get("organizationSkills")
        if isinstance(accepted, dict) and isinstance(delivered, list) and all(item.get("id") != accepted.get("id") for item in delivered):
            delivered.append(accepted)


# Apps that follow the computer's release through their adapter: Kimi Code, OpenCode and Cursor.
FOLLOWER_APPS = ("kimi", "opencode", "cursor")


def cursor_root() -> Path | None:
    """Cursor's directory when Cursor is on this computer (~/.cursor on every system)."""
    root = Path.home() / ".cursor"
    return root if root.is_dir() else None
EMPTY_SKILLS_DIGEST = hashlib.sha256(b"[]").hexdigest()


def follower_app_reports(platform_root: Path, apps: list[str]) -> list[dict]:
    """A receipt for each chosen app that follows the release, before its installer runs.

    Kimi Code and OpenCode send a receipt like Codex and Claude Code do: without one a computer
    with only one of them chosen could not synchronize at all, and with all four chosen a
    release could never be confirmed on it. A chosen app that is not on the computer is
    reported as failed with that reason; apply_mirrored_platform fills in the rest.
    """

    present = {provider for provider, _ in mirrored_skill_roots(platform_root)}
    if cursor_root() is not None:
        present.add("cursor")
    return [{
        "provider": provider, "releaseId": None, "agentPlatformSha": None, "skillCount": 0,
        "skillsDigest": EMPTY_SKILLS_DIGEST, "restartRequired": False, "durationMs": None,
        "status": "current" if provider in present else "failed",
        "error": None if provider in present else text("app_not_on_computer"),
    } for provider in apps if provider in FOLLOWER_APPS]


def follower_apps_need_update(desired: dict, apps: list[str], state_path: Path | None = None) -> bool:
    """Whether a chosen follower app is not at the desired release and skill delivery yet."""

    wanted = {"releaseId": desired.get("release", {}).get("releaseId"), "skillDelivery": skill_delivery(desired)}
    state = _read_local_json(state_path or MIRRORED_PLATFORM_STATE)
    return any(state.get(provider) != wanted for provider in apps if provider in FOLLOWER_APPS)


def _report_of(reports: list[dict], provider: str) -> dict:
    """The app's own receipt when it has one; otherwise the computer's first receipt."""

    return next((item for item in reports if item.get("provider") == provider), reports[0])


def _confirm_follower(report: dict, desired: dict, skills_root: Path | None, *, updated: bool) -> None:
    """The app is at the desired release: record it and what the platform installed there
    (Cursor gets no skills of its own yet: `skills_root` None)."""

    release = desired.get("release", {})
    receipt = _read_local_json(skills_root.parent / "agent-platform-skills.json") if skills_root is not None else {}
    managed = receipt.get("managed_skill_digests") if isinstance(receipt.get("managed_skill_digests"), dict) else {}
    names = sorted(name for name in managed if isinstance(name, str))
    report.update({
        "releaseId": release.get("releaseId"), "agentPlatformSha": release.get("agentPlatformSha"),
        "skillCount": len(names),
        "skillsDigest": hashlib.sha256(json.dumps(names, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "skillConflicts": sorted(item for item in receipt.get("skill_conflicts") or [] if isinstance(item, str)),
    })
    if report.get("status") != "failed":
        report["status"] = "updated" if updated else "current"


def mirrored_skill_roots(platform_root: Path) -> list[tuple[str, Path]]:
    """Skills directories of Kimi Code and OpenCode on this computer, when those apps are here.

    A release has no installer of its own for them as it has for Codex and Claude Code, but the
    organization's and the owner's skills belong in them too.
    """

    roots: list[tuple[str, Path]] = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_platform_kimi_install", platform_root / "adapters" / "kimi" / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        home = module.default_kimi_homes(False)[0]
        if home.is_dir():
            roots.append(("kimi", home / "skills"))
    except Exception:  # an absent or older adapter: Kimi simply gets nothing here
        pass
    opencode = Path.home() / ".config" / "opencode"
    if opencode.is_dir():
        roots.append(("opencode", opencode / "skills"))
    return roots


def apply_local_skills(
    platform_root: Path,
    args: argparse.Namespace,
    desired: dict,
    projects: list[dict],
    temporary: Path,
    reports: list[dict],
    client_id: str,
) -> None:
    """On every sync: bring the organization's skills to the desired state and list what each app has.

    Not only with a release update: organization skills change without a release, and one
    created in the web application must not wait for the next release to reach a computer.
    """

    hand_over_skills(platform_root, args, desired, client_id)
    organization_skills = desired.get("organizationSkills")
    staging = temporary / "organization-skills"
    fetch_error = None
    if isinstance(organization_skills, list):
        try:
            fetch_organization_skills(args, client_id, organization_skills, staging)
        except Exception as error:  # the installer reports each missing skill as not fetched
            fetch_error = str(error)[:300]
        # A skill with programs in it is installed only after the owner of this computer agreed.
        organization_skills = skills_the_owner_agreed_to(organization_skills, staging)
        (temporary / "organization-skills.json").write_text(json.dumps(organization_skills), encoding="utf-8")
        (temporary / "organization-projects.json").write_text(json.dumps(
            [{"projectId": item["projectId"], "projectRoot": str(item["projectRoot"])} for item in projects]), encoding="utf-8")
    for report in reports:
        provider = report["provider"]
        root = args.codex_root if provider == "codex" else args.claude_root
        skills_root = root / "skills"
        if isinstance(organization_skills, list):
            try:
                summary = run_local_skills(platform_root, [
                    "install-organization", "--provider", provider, "--skills-root", str(skills_root),
                    "--staging", str(staging), "--delivered", str(temporary / "organization-skills.json"),
                    "--projects", str(temporary / "organization-projects.json"),
                ])
            except (subprocess.CalledProcessError, ValueError) as error:
                summary = {"conflicts": [], "failed": [{"id": "all", "reason": str(error)[:200]}], "installed": []}
            if summary is not None:
                report["skillConflicts"] = sorted(set(report.get("skillConflicts") or []) | set(summary["conflicts"]))
                failed = [f"{item['id']} ({item['reason']})" for item in summary["failed"]]
                if failed or fetch_error:
                    report["status"] = "failed"
                    report["error"] = ("organization skills: " + ", ".join(failed or [fetch_error]))[:1000]
                elif summary.get("installed") and report.get("status") == "current":
                    report["status"] = "updated"
        try:
            # Names only, never content: what this app really has, for the web application.
            inventory = run_local_skills(platform_root, [
                "inventory", "--provider", provider, "--skills-root", str(skills_root),
                "--platform-receipt", str(root / "agent-platform-install.json"),
            ])
        except (subprocess.CalledProcessError, ValueError):
            inventory = None
        if inventory is not None:
            report["skillInventory"] = inventory
        try:
            # Hooks and mods of this app: events and owners only (a release without the command fails here).
            report["hookInventory"] = run_local_skills(platform_root, [
                "hooks", "--provider", provider, "--skills-root", str(skills_root),
                "--platform-receipt", str(root / "agent-platform-install.json"),
            ])
        except (subprocess.CalledProcessError, ValueError):
            report.pop("hookInventory", None)


MIRRORED_PLATFORM_STATE = Path.home() / ".agent-platform/client/mirrored-apps.json"
# In that record, next to the apps: app -> the digest of the connections its adapter last
# installed (follower_connections). Not inside the record of an app: a client of an earlier
# release compares that record as a whole with the release and the skill delivery, and would
# start the adapter again for every word it does not know. It passes over a name that is no app.
MIRRORED_CONNECTIONS = "connections"
# The servers the service assigns to Kimi, written by the installer of managed servers for
# Kimi's adapter to install.
KIMI_MANAGED_MIRROR = Path.home() / ".agent-platform/client/managed-mcp-mirror.json"


def mirrored_platform_commands(platform_root: Path, args: argparse.Namespace, delivery_path: Path,
                               release: dict | None = None) -> list[tuple[str, list[str]]]:
    """The platform installers of Kimi Code and OpenCode for the apps present on this computer.

    Kimi's command-line home only: its desktop app overwrites its embedded runtime while it
    runs, so that one stays with its own bootstrap. OpenCode's connections are the client's
    job (app agent channels), so its installer leaves them and local models alone.
    """

    commands: list[tuple[str, list[str]]] = []
    for provider, skills_root in mirrored_skill_roots(platform_root):
        installer = platform_root / "adapters" / provider / "install.py"
        if not installer.is_file():
            continue
        # Without these the adapter would install somewhere else or write what is not its to
        # write (the desktop app of Kimi, OpenCode's connections and local models). A release
        # whose adapter does not know one of them has no adapter this client can start.
        if provider == "kimi":
            required = [("--platform-root", str(platform_root)), ("--no-desktop", None),
                        ("--kimi-home", str(skills_root.parent))]
            offered = [("--app-agent-channels", str(KIMI_APP_AGENT_CHANNELS)),
                       ("--managed-mcp-mirror", str(KIMI_MANAGED_MIRROR))]
            if memory_is_a_channel(args):
                # No owner's token is anywhere on this computer. Without the word the adapter
                # looks for one in the configs of Codex and Claude Code whenever Kimi has no
                # channel of its own (a new connection, an app that was not chosen, an agent
                # the owner paused) and stops there.
                offered.append(("--skip-memory", None))
        else:
            required = [("--platform-root", str(platform_root)), ("--config-root", str(skills_root.parent)),
                        ("--skip-memory", None), ("--skip-ollama", None)]
            offered = []
        offered += [("--codex-config", str(args.codex_root / "config.toml")), ("--claude-config", str(args.claude_config)),
                    ("--skill-delivery-manifest", str(delivery_path))]
        if not all(installer_accepts(installer, option) for option, _ in required):
            continue
        command = [sys.executable, str(installer)]
        for option, value in (*required, *(item for item in offered if installer_accepts(installer, item[0]))):
            command += [option] if value is None else [option, value]
        commands.append((provider, command))
    cursor = cursor_root()
    installer = platform_root / "adapters" / "cursor" / "install.py"
    if cursor is not None and installer.is_file() and installer_accepts(installer, "--cursor-root"):
        # Cursor's hooks start the work-session script with this client's interpreter, which
        # has the dependencies of the agent's identity.
        command = [sys.executable, str(installer), "--platform-root", str(platform_root), "--cursor-root", str(cursor),
                   "--python", sys.executable]
        release = release or {}
        for option, key in (("--release-id", "releaseId"), ("--agent-platform-sha", "agentPlatformSha")):
            if isinstance(release.get(key), str):
                command += [option, release[key]]
        commands.append(("cursor", command))
    return commands


def memory_is_a_channel(args: argparse.Namespace) -> bool:
    """Whether the apps of this computer reach Agent Memory as agents of their own, through the
    local bridge. Then the owner's token is fetched for no app and written into no config."""

    return "agentMemory" in getattr(args, "app_agent_services", ())


def follower_connections(provider: str) -> str | None:
    """A digest of the files an app's adapter writes the app's connections from; None for an
    app whose connections the client writes itself.

    Kimi's config is its adapter's: the channels of Kimi's own agent and the servers the
    service assigns reach it only when the adapter runs, from two files the client keeps. So
    the adapter runs again when either file is not what it last read, not only with a release:
    an app chosen after the release was installed would otherwise wait for the next one."""

    if provider == "cursor":
        # Cursor's hooks name this client's interpreter: a new one installs them again.
        return hashlib.sha256(b"python\0" + sys.executable.encode("utf-8")).hexdigest()
    if provider != "kimi":
        return None
    digest = hashlib.sha256()
    for path in (KIMI_APP_AGENT_CHANNELS, KIMI_MANAGED_MIRROR):
        try:
            digest.update(b"file\0" + hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"absent\0")
    return digest.hexdigest()


def apply_mirrored_platform(platform_root: Path, args: argparse.Namespace, desired: dict, temporary: Path,
                            reports: list[dict], state_path: Path | None = None) -> None:
    """Keep the platform's own part of Kimi Code and OpenCode at the computer's release.

    A release is installed into Codex and Claude Code only; without this step Kimi and OpenCode
    would keep whatever was last installed by hand and fall behind with every release. Each
    app's installer runs when the release or the organization's skill delivery changed, when
    the connections it installs changed (follower_connections), and again at the next sync if
    it failed. The orchestrator calls this after the servers and the channels of the run are
    written: those are the connections.
    """

    if not reports:
        return
    state_path = state_path or MIRRORED_PLATFORM_STATE
    wanted = {"releaseId": desired.get("release", {}).get("releaseId"), "skillDelivery": skill_delivery(desired)}
    state = _read_local_json(state_path)
    delivery_path = temporary / "mirrored-skill-delivery.json"
    delivery_path.write_text(json.dumps(wanted["skillDelivery"], ensure_ascii=False), encoding="utf-8")
    roots = dict(mirrored_skill_roots(platform_root))
    installed_connections = state.get(MIRRORED_CONNECTIONS)
    installed_connections = dict(installed_connections) if isinstance(installed_connections, dict) else {}
    own = {item.get("provider"): item for item in reports if item.get("provider") in FOLLOWER_APPS}
    installed = set()
    changed = False
    cursor = cursor_root()
    if cursor is not None and not app_chosen(desired, "cursor"):
        # Cursor on this computer is no choice: nothing of the platform's is left in it.
        try:
            remove_cursor_hooks(cursor)
        except (OSError, ValueError):
            pass
    for provider, command in mirrored_platform_commands(platform_root, args, delivery_path, desired.get("release")):
        if provider == "cursor" and not app_chosen(desired, "cursor"):
            continue
        installed.add(provider)
        updated = False
        # Read before the adapter runs: this is what it is about to install.
        connections = follower_connections(provider)
        if state.get(provider) != wanted or (connections is not None
                                             and installed_connections.get(provider) != connections):
            started_at = time.monotonic()
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
            except subprocess.CalledProcessError as error:
                reason = (error.stderr or error.stdout or "").strip().splitlines()[-1:] or ["installer failed"]
                # On the app's own receipt when it has one; otherwise on the computer's first.
                target = _report_of(reports, provider)
                target["status"] = "failed"
                target["error"] = f"{provider} adapter: {reason[0]}"[:1000]
                continue
            state[provider] = wanted
            if connections is not None:
                installed_connections[provider] = connections
                state[MIRRORED_CONNECTIONS] = installed_connections
            changed = updated = True
            if provider in own:
                own[provider]["durationMs"] = int((time.monotonic() - started_at) * 1000)
        if provider in own:
            failure = (getattr(args, "app_failures", None) or {}).get(provider)
            if failure:
                # An installer left the app's config as it was (it could not read it).
                own[provider]["status"] = "failed"
                own[provider]["error"] = text("app_config_unreadable", reason=failure)[:1000]
            _confirm_follower(own[provider], desired, roots.get(provider), updated=updated)
            if provider in (getattr(args, "restart_required_apps", None) or ()):
                # Its servers or channels changed in this run, and it reads them only when it starts.
                own[provider]["restartRequired"] = True
    for provider, report in own.items():
        # The app is here, but this release has no adapter for it: nothing confirmed its state.
        if provider not in installed and report.get("status") != "failed":
            report["status"] = "failed"
            report["error"] = text("app_without_installer")
    if changed:
        _write_json_atomic(state_path, state)


def apply_mirrored_skills(platform_root: Path, desired: dict, temporary: Path, reports: list[dict]) -> None:
    """Organization and personal skills for Kimi Code and OpenCode, from the files already fetched.

    Runs after apply_local_skills (which fetched and verified the same skills for Codex and
    Claude). A failure is reported on the app's own receipt when it has one, otherwise on the
    computer's first.
    """

    delivered = desired.get("organizationSkills")
    if not isinstance(delivered, list) or not reports:
        return
    empty = temporary / "no-projects.json"
    empty.write_text("[]", encoding="utf-8")
    mirrored: list[dict] = []
    for provider, skills_root in mirrored_skill_roots(platform_root):
        try:
            summary = run_local_skills(platform_root, [
                "install-organization", "--provider", provider, "--skills-root", str(skills_root),
                "--staging", str(temporary / "organization-skills"), "--delivered", str(temporary / "organization-skills.json"),
                "--projects", str(empty),
            ])
        except (subprocess.CalledProcessError, ValueError) as error:
            summary = {"failed": [{"id": "all", "reason": str(error)[:200]}]}
        failed = [f"{item['id']} ({item['reason']})" for item in (summary or {}).get("failed", [])]
        if failed:
            target = _report_of(reports, provider)
            target["status"] = "failed"
            target["error"] = (f"organization skills ({provider}): " + ", ".join(failed))[:1000]
        try:
            # Names only, after the install: what this app has now, for the web application.
            inventory = run_local_skills(platform_root, [
                "inventory", "--provider", provider, "--skills-root", str(skills_root),
                "--platform-receipt", str(skills_root.parent / "agent-platform-skills.json"),
            ])
        except (subprocess.CalledProcessError, ValueError):
            inventory = None
        if inventory is not None:
            entry = {"app": provider, "skillInventory": inventory}
            try:
                # Kimi's [[hooks]] and OpenCode's code plugins: events and names, never commands.
                entry["hookInventory"] = run_local_skills(platform_root, [
                    "hooks", "--provider", provider, "--skills-root", str(skills_root),
                    "--platform-receipt", str(skills_root.parent / "agent-platform-skills.json"),
                ])
            except (subprocess.CalledProcessError, ValueError):
                pass
            mirrored.append(entry)
    if mirrored:
        reports[0]["mirroredApps"] = mirrored
