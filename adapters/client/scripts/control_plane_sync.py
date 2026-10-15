"""The order of one synchronization, stage by stage.

The entry point (`sync_client.py`) takes the run lock, asks the service for the desired state
and calls `run` with its own namespace as `sync`: every function of the client is reached as
`sync.X`, and this file imports nothing of the client but the safe extraction of an archive.
A stage is named before it starts, so a failure is reported with the stage it happened in and
a reason code, never with a free-form error. There is one path: the service over HTTPS and a
signed release, with no fallback to anything else.

"Control Plane" in the names and messages here is the API of the service the client talks to.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
import socket
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import UTC, datetime
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parents[2]
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))

LOCAL_SAFE_TAR = Path(__file__).with_name("safe_tar.py")
if LOCAL_SAFE_TAR.is_file():
    from safe_tar import extract_data_archive
else:
    from common.safe_tar import extract_data_archive


def call(args, sync, action, **payload):
    return sync.release_transport().request_json(args.control_plane_url, "/v1/client-session/" + action, {
        "clientId": args.client_id, "clientToken": sync.required_client_token(args), **payload,
    })


class PreflightFailure(RuntimeError):
    """A mandatory MCP service could not be reached; `reason` is a stable, secret-free code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def preflight_reason(service, step, error):
    if isinstance(error, urllib.error.HTTPError):
        kind = f"http_{error.code}"
    elif isinstance(error, (TimeoutError, socket.timeout)) or (
            isinstance(error, urllib.error.URLError) and isinstance(error.reason, (TimeoutError, socket.timeout))):
        kind = "timeout"
    elif isinstance(error, (urllib.error.URLError, ConnectionError)):
        kind = "network"
    else:
        kind = "invalid"
    return f"{service}_{step}_{kind}"


TRANSIENT_PREFLIGHT = re.compile(r"_(http_5\d\d|http_429|timeout|network)$")


def initialize_service(opener, request, service, attempts=2, delay_seconds=2.0):
    """Initialize one MCP endpoint; a transient failure gets one delayed retry."""
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise PreflightFailure(f"{service}_initialize_http_{response.status}")
            return
        except PreflightFailure as failure:
            reason = failure.reason
            error = failure
        except Exception as caught:
            reason = preflight_reason(service, "initialize", caught)
            error = caught
        if attempt == attempts or not TRANSIENT_PREFLIGHT.search(reason):
            raise PreflightFailure(reason) from error
        time.sleep(delay_seconds)


OWNER_DISABLED_APP_AGENT = frozenset({"app_agent_paused", "app_agent_revoked"})


def app_agent_channels(desired: dict) -> dict[str, str]:
    """Service -> endpoint each app reaches as its own agent. The service sends every channel in
    appAgentChannels; an earlier one sent the task channel alone as collaborationMcp."""
    channels: dict[str, str] = {}
    if isinstance(desired.get("appAgentChannels"), list):
        items = desired["appAgentChannels"]
    elif isinstance(desired.get("collaborationMcp"), dict):
        items = [{**desired["collaborationMcp"], "service": "collaboration"}]
    else:
        return channels
    keys = {"collaboration": "agent-platform-collaboration", "agentMemory": "agentMemory",
            "projectContext": "projectContext"}
    for item in items:
        service = item.get("service") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        if (service not in keys or item.get("configKey") != keys[service] or service in channels
                or not isinstance(url, str) or not url.startswith("https://")):
            raise SystemExit("Control Plane returned an invalid app agent channel")
        channels[service] = url
    return channels


def preflight_credentials(args, desired, sync):
    credentials = {}
    for service, fetch in (("agentMemory", sync.fetch_agent_memory_credential), ("projectContext", sync.fetch_project_context_credential)):
        metadata = desired.get(service, {})
        revision = metadata.get("credentialRevision")
        if (metadata.get("status") != "available" or not isinstance(revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", revision)):
            raise RuntimeError(sync.text("mcp_connections_missing"))
        try:
            credentials[service] = fetch(args, args.client_id, revision, metadata.get("url"))
        except (Exception, SystemExit) as error:
            raise PreflightFailure(preflight_reason(service, "credential", error)) from error
    for service, credential in credentials.items():
        url = urlsplit(credential["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise RuntimeError(sync.text("mcp_address_invalid"))
        # Endpoint reachability/auth is checked before any provider installer can write.
        organization_id = credential.get("organizationId")
        if service == "agentMemory" and not isinstance(organization_id, str):
            raise RuntimeError(sync.text("agent_memory_organization_missing"))
        headers = {
            "Authorization": "Bearer " + credential["token"], "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if isinstance(organization_id, str):
            headers["X-Agent-Platform-Organization"] = organization_id
        request = urllib.request.Request(credential["url"], data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "agent-platform-preflight", "version": "2"},
            }}).encode(), method="POST", headers=headers)
        opener = urllib.request.build_opener(sync.release_transport().NoRedirect())
        initialize_service(opener, request, service)
    args.agent_memory_token = credentials["agentMemory"]["token"]
    args.agent_memory_url = credentials["agentMemory"]["url"]
    args.project_context_token = credentials["projectContext"]["token"]
    args.project_context_url = credentials["projectContext"]["url"]
    return credentials


# Apps a release is installed into with a receipt of their own; the rest follow it.
RELEASE_PROVIDERS = ("codex", "claude")


def receipts(args, providers, sync, *, failed=False):
    result = []
    for provider in providers:
        receipt = sync.read_receipt(args.codex_root if provider == "codex" else args.claude_root)
        missing = not receipt.get("release_id")
        result.append({"provider": provider, "releaseId": receipt.get("release_id"),
                       "agentPlatformSha": receipt.get("agent_platform_sha"), "skillCount": len(receipt.get("skills", [])),
                       "skillsDigest": sync.skills_digest(receipt), "status": "failed" if failed or missing else "current",
                       "restartRequired": False, "durationMs": None,
                       "error": sync.text("app_install_unconfirmed") if failed or missing else None,
                       # Platform skills the installer left out for the user's own (kept between releases).
                       "skillConflicts": sorted(set(receipt.get("skill_conflicts") or []))})
    return result


def extract_verified(args, desired, staged, temporary, sync):
    # Copy to a private staging directory and verify that exact copy before extraction.
    archive = temporary / "verified.tar.gz"
    sync.shutil.copy2(staged["bundlePath"], archive)
    manifest = sync.release_transport().verify_release(desired["release"]["envelope"], args.release_trust_file)
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["bundleSha256"]:
            raise RuntimeError(sync.text("bundle_changed_after_download"))
    sync.release_transport().inspect_bundle(archive, manifest)
    with tarfile.open(archive, "r:gz") as handle:
        extract_data_archive(handle, temporary / "verified")
    bundle_root = temporary / "verified" / manifest["releaseId"]
    platform_root = bundle_root / "agent-platform"
    required = ["adapters/client/scripts/sync_client.py", "adapters/client/scripts/sync_guard.py", "adapters/client/scripts/release_transport.py",
                "adapters/client/scripts/control_plane_sync.py", "adapters/common/safe_tar.py",
                "adapters/client/scripts/agent_identity.py", "adapters/client/scripts/mcp_bridge.py",
                "tools/install_agent_memory_mcp.py",
                "tools/install_project_context_mcp.py", "tools/install_managed_mcp_servers.py",
                "tools/install_app_agent_channels.py", "tools/project_profiles.py"]
    if not all((platform_root / p).is_file() for p in required):
        raise RuntimeError(sync.text("release_without_full_installation"))
    return archive, bundle_root, platform_root


def run(args, desired, sync):
    providers = args.provider or ["codex", "claude"]
    if args.check_only:
        print(json.dumps({"clientId": args.client_id, "status": "checked", "installed": False,
                          "releaseId": desired["release"]["releaseId"]}))
        return 0
    if desired.get("clientProtocol") != 2:
        raise SystemExit(sync.text("protocol_unsupported"))
    # sync_client.main holds the per-user execution lock before fetching desired.
    return run_locked(args, desired, providers, sync)


def service_accepts(desired, field):
    """Whether the service names a field it accepts in a report beyond what every client sends
    (`clientReportFields` of the desired state). It validates a report field by field, so what
    it does not name is kept from it."""
    accepted = desired.get("clientReportFields")
    return isinstance(accepted, list) and field in accepted


def reportable_events(launcher_events, desired, sync):
    """The launcher events in the names this service knows (absent in older client runtimes,
    whose launchers write nothing a service does not know)."""
    reportable = getattr(sync, "reportable_launcher_events", None)
    return reportable(launcher_events, desired) if callable(reportable) else launcher_events


def should_install(args, command, update):
    return bool(command) or bool(getattr(args, "initial_install", False)) or (args.update_policy == "automatic" and update)


def require_scheduler(platform_name, task, sync):
    if platform_name in ("windows", "macos") and (not isinstance(task, dict) or task.get("status") != "installed"):
        raise RuntimeError(sync.text("scheduled_task_unconfirmed"))


def run_locked(args, desired, chosen, sync):
    # `chosen` is every app picked for this computer. A release is installed into Codex and
    # Claude Code; Kimi Code and OpenCode follow it through their adapters and send their own
    # receipt, so a computer works with any set of them, including none of the first two.
    providers = [provider for provider in chosen if provider in RELEASE_PROVIDERS]
    followers = [provider for provider in chosen if provider not in RELEASE_PROVIDERS]
    command, task, reports, recovery = None, None, None, None
    app_reports = []
    mcp_state = {name: {"status": "failed", "credentialRevision": None} for name in ("agentMemory", "projectContext")}
    launcher_events = sync.read_launcher_events()
    # Before any stage can fail: an app agent of another connection stops being answered now,
    # not only after the channels stage (absent in older client runtimes).
    record_current_connection = getattr(sync, "record_current_connection", None)
    if callable(record_current_connection):
        record_current_connection(args)
    try:
        stage = "mcp_preflight"
        channels = app_agent_channels(desired)
        # Apps that reach Agent Memory and Project Context as their own agents need no owner token.
        bridged = {"agentMemory", "projectContext"} <= set(channels)
        args.app_agent_services = set(channels)
        # Preflight all mandatory services before provider or scheduler mutation. With app agent
        # channels each service is initialized per app, as that app's agent, before its config changes.
        credentials = {} if bridged else preflight_credentials(args, desired, sync)
        projects = sync.load_project_mappings(
            args.project_mappings,
            desired.get("projectProfileAssignments", []),
        )
        stage = "signed_release"
        # A release the platform withdrew is refused before it is fetched (absent in older runtimes).
        refuse_withdrawn = getattr(sync, "refuse_withdrawn_release", None)
        if callable(refuse_withdrawn):
            refuse_withdrawn(desired["release"]["releaseId"])
        staged = sync.release_transport().stage_release(args.control_plane_url, args.client_id,
            sync.required_client_token(args), desired, args.release_trust_file, args.release_cache)
        with tempfile.TemporaryDirectory(prefix="control-plane-install-") as directory:
            temporary = Path(directory)
            archive, bundle_root, platform_root = extract_verified(args, desired, staged, temporary, sync)
            # What this signed release says was withdrawn is remembered from now on.
            learn_withdrawn = getattr(sync, "learn_withdrawn_releases", None)
            if callable(learn_withdrawn):
                refuse_withdrawn(desired["release"]["releaseId"], learn_withdrawn(platform_root))
            stage = "command_claim"
            command = call(args, sync, "claim-command", releaseId=desired["release"]["releaseId"])["command"]
            update = any(sync.needs_update(sync.read_receipt(args.codex_root if p == "codex" else args.claude_root), desired) for p in providers)
            update = update or sync.project_profiles_need_update(projects, providers)
            update = update or sync.context_passports_need_update(desired, providers)
            # Without Codex or Claude Code the client itself is refreshed when a follower lags.
            update = update or (not providers and sync.follower_apps_need_update(desired, followers))
            apply = should_install(args, command, update)
            # The client's own files are compared with the release whatever the apps' receipts
            # say: they name a release from the moment it was installed into the apps. The
            # release counts as taken on this computer when every app already has it; then a
            # client that is not the release's is a repair, and no update a person still decides on.
            stage = "client_runtime"
            release_id = desired["release"]["releaseId"]
            accepted = (all(sync.read_receipt(args.codex_root if p == "codex" else args.claude_root).get("release_id")
                            == release_id for p in providers)
                        if providers else not sync.follower_apps_need_update(desired, followers))
            recovery = sync.plan_client_recovery(sync, desired, platform_root, accepted=accepted,
                                                 events=launcher_events,
                                                 requested=bool(getattr(args, "repair_client", False)))
            if recovery["action"] == "repair" and not apply:
                # With an installation the client is copied there, together with the apps.
                sync.install_managed_runtime(platform_root, recovery["root"])
            if apply:
                stage = "provider_installation"
                args.release_bundle = archive  # verified private copy, never a CLI override
                reports, managed_script = sync.run_installers(args, desired, providers, temporary, projects, args.client_id)
            else:
                stage = "mcp_configuration"
                reports = receipts(args, providers, sync)
                for report in reports:
                    report["projectProfiles"] = [sync.project_profile_report(sync.read_project_receipt(p, report["provider"]))
                                                 for p in projects if sync.read_project_receipt(p, report["provider"])]
                if not bridged:
                    sync.configure_agent_memory(platform_root, args, providers, desired, args.client_id,
                                                credentials["agentMemory"])
                    sync.configure_project_context(platform_root, args, providers, desired, args.client_id)
                managed_script = Path.home() / ".agent-platform/client/sync_client.py"
            for report in reports:
                if "contextPassports" not in report:
                    report["contextPassports"] = sync.read_context_passport_reports(report["provider"])
            # From here `reports` also carries the receipts of the chosen follower apps.
            app_reports = sync.follower_app_reports(platform_root, followers) if followers else []
            release_reports, reports = reports, [*reports, *app_reports]
            # Organization skills and the skill inventory follow the desired state on every sync,
            # with or without a release update (absent in older client runtimes).
            apply_local_skills = getattr(sync, "apply_local_skills", None)
            if callable(apply_local_skills):
                apply_local_skills(platform_root, args, desired, projects, temporary, release_reports, args.client_id)
            stage = "managed_mcp_configuration"
            mcp_state["managed"] = sync.configure_managed_mcp_servers(
                platform_root, args, providers, desired, args.client_id, temporary,
            )
            # What each app really has, after the platform's own servers are in place (absent in
            # older client runtimes).
            apply_mcp_inventory = getattr(sync, "apply_mcp_inventory", None)
            if callable(apply_mcp_inventory):
                apply_mcp_inventory(platform_root, args, desired, release_reports, args.client_id)
            if channels:
                # After managed servers: a hand-made record with the same key is removed there.
                stage = "app_agent_channels"
                # Per app: its agent and whether its channels were written ("collaboration" is the
                # report key the service accepts for these receipts).
                mcp_state["collaboration"] = sync.configure_app_agent_channels(
                    platform_root, args, providers, desired, channels,
                )
            for name, credential in credentials.items():
                mcp_state[name] = {"status": "configured", "credentialRevision": credential["credentialRevision"]}
            if bridged:
                # A deferred app (the Claude chat app while it runs) gets its channels later, and an app
                # the owner paused or revoked is meant to be without them: neither is a failure.
                every_app = all(receipt["status"] != "failed" or receipt.get("reason") in OWNER_DISABLED_APP_AGENT
                                for receipt in mcp_state["collaboration"].values())
                for name in ("agentMemory", "projectContext"):
                    mcp_state[name] = {"status": "configured" if every_app else "failed", "credentialRevision": None}
            # The apps that follow the release come after the servers and the channels, and not
            # before: Kimi's adapter writes Kimi's connections from two files those stages have
            # just written. Started before them it installed the files of the run before, and on
            # the first run of a connection it found none and no token either, and stopped.
            # What goes wrong in an adapter is the report of an app and no exception; anything
            # else that is raised here is a failed installation and no failed channel.
            stage = "provider_installation"
            # The platform's own part of Kimi Code and OpenCode follows the release (absent in older runtimes).
            apply_mirrored_platform = getattr(sync, "apply_mirrored_platform", None)
            if callable(apply_mirrored_platform):
                apply_mirrored_platform(platform_root, args, desired, temporary, reports)
            # The same skills for Kimi Code and OpenCode on this computer (absent in older runtimes).
            apply_mirrored_skills = getattr(sync, "apply_mirrored_skills", None)
            if callable(apply_mirrored_skills):
                apply_mirrored_skills(platform_root, desired, temporary, reports)
            # Standing instructions of the organization, its projects and the owner (absent in
            # older client runtimes); they follow the desired state on every sync. After the
            # adapters, which write the instruction files of Kimi Code and OpenCode.
            apply_instruction_rules = getattr(sync, "apply_instruction_rules", None)
            if callable(apply_instruction_rules):
                apply_instruction_rules(args, desired, providers, reports)
            if any(item["status"] == "failed" for item in reports):
                # Named here and not left at the last stage set above (app_agent_channels): that
                # would send the diagnosis to the channels when an installer refused the release.
                stage = "provider_installation"
                raise RuntimeError(sync.text("providers_not_installed"))
            if managed_script.is_file():
                stage = "scheduled_task"
                task = sync.ensure_scheduled_task(managed_script, args, args.client_id, args.display_name or sync.platform.node(), chosen)
            require_scheduler(sync.client_platform(), task, sync)
            stage = "client_runtime"
            client_runtime = sync.settle_client_recovery(sync, recovery)
            # Everything else of the run is done and is reported as it is. A release whose
            # client was refused here is the one thing that is not: the run says so and fails.
            refused = client_runtime["recovery"] == "refused"
            reason = sync.ClientRuntimeProbeFailed.code if refused else None
            payload = make_report(args, reports, command, mcp_state, task, sync, failed=refused,
                                  failed_stage="client_runtime" if refused else None, failure_reason=reason,
                                  launcher_events=reportable_events(launcher_events, desired, sync),
                                  client_runtime=client_runtime)
            if refused:
                sync.record_local_outcome("client_runtime", reason)
            stage = "report"
            sync.report_state(args, payload)
            sync.clear_launcher_events()
            if refused:
                # A person who asked for a repair of this release before is not told that
                # nothing was tried.
                message = (sync.text("client_release_refused_after_request", release_id=release_id)
                           if recovery["requests"] else sync.text("client_release_refused", release_id=release_id))
                print(json.dumps({"clientId": args.client_id, "status": "failed", "phase": "client_runtime",
                                  "message": message,
                                  "messageLanguage": sync.language(),
                                  "receiptDelivered": True, "reason": reason}, ensure_ascii=False))
                return 1
            print(json.dumps({"clientId": args.client_id, "status": "update_available" if update and not apply else "current",
                              "providers": reports, "command": payload.get("commandResult")}, ensure_ascii=False))
            return 0
    except (Exception, SystemExit) as error:
        # Never echo credentials, response bodies, subprocess environment or opaque errors.
        if not reports:
            # Nothing was confirmed: every chosen app is reported as not synchronized.
            reports = receipts(args, providers, sync, failed=True) + [{
                "provider": provider, "releaseId": None, "agentPlatformSha": None, "skillCount": 0,
                "skillsDigest": hashlib.sha256(b"[]").hexdigest(), "restartRequired": False,
                "durationMs": None, "status": "failed", "error": None,
            } for provider in followers]
        for item in reports:
            item["status"] = "failed"
            item["error"] = sync.text("app_sync_unfinished")
        report_delivered = True
        failure_reason = error.reason if isinstance(error, PreflightFailure) else None
        probe_failure = getattr(sync, "ClientRuntimeProbeFailed", None)
        if (stage in {"client_runtime", "provider_installation"} and isinstance(probe_failure, type)
                and isinstance(error, probe_failure)):
            # The new client did not start in its installed layout; the previous one stays active.
            failure_reason = probe_failure.code
        # A file of the client that another program held while the client was being copied
        # (an antivirus reading it) is no client that did not start, and is said apart from it.
        held_files = getattr(sync, "ClientFilesBusy", None)
        files_busy = (stage in {"client_runtime", "provider_installation"} and isinstance(held_files, type)
                      and isinstance(error, held_files))
        if files_busy:
            failure_reason = held_files.code
        record_outcome = getattr(sync, "record_local_outcome", None)
        if callable(record_outcome):
            record_outcome(stage, failure_reason)
        # The service is told a reason it validates; one that came after it is kept from it.
        reported_reason = failure_reason
        if files_busy and not service_accepts(desired, f"syncOutcome.reason.{held_files.code}"):
            reported_reason = None
        client_runtime = None
        if recovery is not None:
            try:
                # Where the client stands now; a run that failed puts nothing more in order.
                # What a held file stopped is not counted against the release.
                client_runtime = (sync.settle_client_recovery(sync, recovery, finished=False, files_busy=True)
                                  if files_busy else sync.settle_client_recovery(sync, recovery, finished=False))
            except OSError:
                client_runtime = recovery["report"]
        try:
            sync.report_state(args, make_report(args, reports, command, mcp_state, task, sync, failed=True,
                                                failed_stage=stage, failure_reason=reported_reason,
                                                launcher_events=reportable_events(launcher_events, desired, sync),
                                                client_runtime=client_runtime))
        except (Exception, SystemExit):
            report_delivered = False
        if report_delivered:
            sync.clear_launcher_events()
        messages = {
            "mcp_preflight": sync.text("stage_mcp_preflight"),
            "signed_release": sync.text("stage_signed_release"),
            "command_claim": sync.text("stage_command_claim"),
            "client_runtime": sync.text("client_repair_failed"),
            "provider_installation": sync.text("stage_provider_installation"),
            "mcp_configuration": sync.text("stage_mcp_configuration"),
            "managed_mcp_configuration": sync.text("stage_managed_mcp_configuration"),
            "app_agent_channels": sync.text("stage_app_agent_channels"),
            "scheduled_task": sync.text("stage_scheduled_task"),
            "report": sync.text("stage_report"),
        }
        # Every sentence is from the client's table, in the person's language, and says which
        # language it is: a terminal client from before shows a sentence only in its own.
        message = messages[stage]
        if files_busy:
            message = sync.text("client_files_busy")
        elif recovery is not None and recovery["requested"] and (
                stage == "client_runtime" or (failure_reason is not None
                                              and failure_reason == getattr(probe_failure, "code", None))):
            # The attempt was the person's own: they read that it was made and what is left to do.
            message = sync.text("client_repair_on_request_failed", release_id=recovery["releaseId"])
        answer = {"clientId": args.client_id, "status": "failed",
                  "message": message, "messageLanguage": sync.language(), "phase": stage,
                  "receiptDelivered": report_delivered, "errorType": type(error).__name__, "reason": failure_reason}
        print(json.dumps(answer, ensure_ascii=False))
        return 1


def make_report(args, reports, command, mcp_state, task, sync, *, failed, failed_stage=None, failure_reason=None,
                launcher_events=(), client_runtime=None):
    for provider in reports:
        for profile in provider.get("projectProfiles", []):
            if profile.get("error") is not None:
                profile["error"] = "Установка проектного профиля не завершена"
    payload = {"version": 1, "clientId": args.client_id, "reportedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
               "providers": reports, "mcpState": mcp_state,
               "commandId": command["id"] if command else None,
               # The failing stage is a stable, secret-free code; free-form errors never leave the client.
               "syncOutcome": {"status": "failed" if failed else "succeeded", "stage": failed_stage if failed else None}}
    if failed and failure_reason:
        payload["syncOutcome"]["reason"] = failure_reason
    if launcher_events:
        payload["launcherEvents"] = list(launcher_events)
    if client_runtime is not None:
        # Which client did this run and whether the installed one is the release's: the apps'
        # receipts above say what is in the apps, and nothing about the client's own files.
        payload["clientRuntime"] = client_runtime
    if task is not None:
        payload["scheduledTask"] = task
    if command:
        payload["commandLeaseToken"] = command["leaseToken"]
        payload["commandResult"] = {"status": "failed" if failed else "completed",
                                    "error": "Синхронизация не завершена" if failed else None}
    return payload
