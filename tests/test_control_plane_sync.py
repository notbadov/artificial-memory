import importlib.util
import hashlib
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch


ROOT = Path(__file__).parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "adapters/client/scripts" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


execution = load("execution", "control_plane_sync.py")
sync = load("sync_v1", "sync_client.py")
# The names the schedule reads are those of the file its functions are in; a test replaces
# them there, reached through one of those functions.
SCHEDULE = sync.install_scheduled_task.__globals__


def speaking_client() -> Mock:
    """A stand-in for the client's namespace that says its sentences from the client's table."""
    return Mock(text=sync.text, language=sync.language)


def scheduler_args(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        control_plane_url="https://app.artificial-memory.com/agent-api",
        release_trust_file=root / "trust.json",
        release_cache=root / "cache",
        client_token="private-test-token",
        update_policy="automatic",
        codex_root=root / "codex",
        claude_root=root / "claude",
        claude_config=root / "claude.json",
        bin_dir=root / "bin",
        project_mappings=root / "projects.json",
    )


class ControlPlaneSyncTests(unittest.TestCase):
    def test_verified_release_extraction_checks_the_downloaded_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_id = "release-1"
            source = root / "source" / release_id / "agent-platform"
            required = [
                "adapters/client/scripts/sync_client.py",
                "adapters/client/scripts/sync_guard.py",
                "adapters/client/scripts/release_transport.py",
                "adapters/client/scripts/control_plane_sync.py",
                "adapters/common/safe_tar.py",
                "tools/install_agent_memory_mcp.py",
                "tools/install_project_context_mcp.py",
                "tools/install_managed_mcp_servers.py",
                "tools/install_app_agent_channels.py",
                "adapters/client/scripts/agent_identity.py",
                "adapters/client/scripts/mcp_bridge.py",
                "tools/project_profiles.py",
            ]
            for relative in required:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# fixture\n", encoding="utf-8")
            bundle = root / "bundle.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                archive.add(source.parent, arcname=release_id)
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            transport = Mock()
            transport.verify_release.return_value = {
                "releaseId": release_id,
                "bundleSha256": digest,
            }
            helper = SimpleNamespace(
                shutil=shutil,
                release_transport=lambda: transport,
            )
            temporary = root / "extract"
            temporary.mkdir()

            _, _, platform_root = execution.extract_verified(
                SimpleNamespace(release_trust_file=root / "trust.json"),
                {"release": {"envelope": {}}},
                {"bundlePath": str(bundle)},
                temporary,
                helper,
            )

            self.assertTrue((platform_root / required[0]).is_file())
            transport.inspect_bundle.assert_called_once()

    def test_client_parser_has_one_production_transport(self):
        with patch("sys.argv", ["sync_client.py"]):
            args = sync.parse_args()
        self.assertEqual(
            args.control_plane_url,
            "https://app.artificial-memory.com/agent-api",
        )
        self.assertFalse(hasattr(args, "transport"))
        self.assertFalse(hasattr(args, "remote_host"))
        self.assertFalse(hasattr(args, "remote_database"))

    def test_initial_install_respects_manual_policy_on_following_runs(self):
        args = SimpleNamespace(update_policy="manual", initial_install=True)
        self.assertTrue(execution.should_install(args, None, True))
        args.initial_install = False
        self.assertFalse(execution.should_install(args, None, True))
        self.assertTrue(execution.should_install(args, {"action": "sync"}, True))

    def test_windows_and_macos_require_a_verified_scheduler(self):
        for platform_name in ("windows", "macos"):
            for task in (None, {"status": "failed"}):
                with self.subTest(platform_name=platform_name, task=task):
                    with self.assertRaises(RuntimeError):
                        execution.require_scheduler(platform_name, task, sync)
            execution.require_scheduler(platform_name, {"status": "installed"}, sync)
        execution.require_scheduler("linux", None, sync)

    def test_preflight_fails_before_any_installation_when_mcp_is_incomplete(self):
        args = SimpleNamespace(client_id="fixture")
        helper = speaking_client()
        helper.fetch_agent_memory_credential.return_value = {
            "token": "a" * 48,
            "url": "https://fixture.invalid",
        }
        desired = {
            "agentMemory": {
                "status": "available",
                "credentialRevision": "a" * 64,
                "url": "https://fixture.invalid",
            }
        }
        with self.assertRaisesRegex(RuntimeError, "MCP"):
            execution.preflight_credentials(args, desired, helper)
        helper.run_installers.assert_not_called()
        helper.configure_agent_memory.assert_not_called()

    def test_mcp_credentials_use_only_the_control_plane_session(self):
        args = SimpleNamespace(
            client_token="test-client",
            control_plane_url="https://app.artificial-memory.com/agent-api",
        )
        token = "memory-test-credential"
        revision = sync.credential_revision(token)
        transport = Mock()
        transport.request_json.return_value = {
            "credential": {
                "token": token,
                "url": "https://memory.invalid/mcp",
                "credentialRevision": revision,
                "organizationId": "example-organization",
            }
        }
        # Replaced in the part the two functions live in, which is where they read the name.
        with patch.dict(sync.fetch_agent_memory_credential.__globals__, {"release_transport": Mock(return_value=transport)}):
            sync.fetch_agent_memory_credential(
                args, "fixture", revision, "https://memory.invalid/mcp"
            )
            sync.fetch_project_context_credential(
                args, "fixture", revision, "https://memory.invalid/mcp"
            )
        self.assertTrue(
            all(
                call.args[1] == "/v1/client-session/credential"
                for call in transport.request_json.call_args_list
            )
        )

    def test_check_only_does_not_mutate_or_report(self):
        args = SimpleNamespace(provider=["codex"], check_only=True, client_id="fixture")
        helper = speaking_client()
        with patch("builtins.print"):
            self.assertEqual(
                execution.run(args, {"release": {"releaseId": "fixture"}}, helper),
                0,
            )
        helper.report_state.assert_not_called()
        helper.run_installers.assert_not_called()

    def test_control_plane_project_assignments_are_required_input(self):
        args = SimpleNamespace(
            provider=["codex"],
            check_only=False,
            client_id="fixture",
            project_mappings=Path("mappings.json"),
            update_policy="manual",
            codex_root=Path("codex"),
            claude_root=Path("claude"),
        )
        desired = {
            "clientProtocol": 2,
            "release": {"releaseId": "fixture"},
            "projectProfileAssignments": [
                {"version": 1, "projectId": "example", "profiles": []}
            ],
        }
        helper = speaking_client()
        helper.read_receipt.return_value = {}
        helper.load_project_mappings.side_effect = RuntimeError("stop after mapping")
        with patch.object(execution, "preflight_credentials", return_value={}), patch(
            "builtins.print"
        ):
            self.assertEqual(execution.run_locked(args, desired, ["codex"], helper), 1)
        helper.load_project_mappings.assert_called_once_with(
            args.project_mappings, desired["projectProfileAssignments"]
        )

    def test_macos_scheduler_keeps_token_out_of_launcher_and_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = scheduler_args(root)
            query = SimpleNamespace(returncode=0, stdout="")
            with patch.dict(SCHEDULE, {"client_platform": Mock(return_value="macos")}), patch.object(
                sync.Path, "home", return_value=root
            ), patch.object(
                sync.os, "getuid", return_value=501, create=True
            ), patch.object(
                sync.os, "chmod"
            ) as chmod, patch.object(sync.subprocess, "run", return_value=query) as run:
                receipt = sync.install_scheduled_task(
                    root / "sync_client.py",
                    args,
                    "mac-primary",
                    "Mac Марии",
                    ["codex"],
                )
                plist = sync.launch_agent_path("mac-primary")

            credential = root / ".agent-platform/client/credential"
            launcher = root / ".agent-platform/client/sync-client.sh"
            self.assertEqual(credential.read_text(encoding="utf-8").strip(), args.client_token)
            chmod.assert_any_call(ANY, 0o600)
            launcher_text = launcher.read_text(encoding="utf-8")
            plist_text = plist.read_text(encoding="utf-8")
            self.assertNotIn(args.client_token, launcher_text)
            self.assertNotIn(args.client_token, plist_text)
            self.assertNotIn("--transport", launcher_text)
            self.assertEqual(receipt["launcher"], "launchd")
            self.assertEqual(receipt["displayName"], "Mac Марии")
            self.assertEqual(receipt["providers"], ["codex"])
            self.assertEqual(receipt["controlPlaneUrl"], args.control_plane_url)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in commands))

    def test_macos_launcher_revision_does_not_reload_its_own_loaded_job(self):
        # A launchd run that rewrites its launcher and calls `launchctl bootout` on its own
        # job is terminated and leaves no schedule.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = scheduler_args(root)
            loaded = SimpleNamespace(returncode=0, stdout="")
            with patch.dict(SCHEDULE, {"client_platform": Mock(return_value="macos")}), patch.object(
                sync.Path, "home", return_value=root
            ), patch.object(sync.os, "getuid", return_value=501, create=True), patch.object(
                sync.os, "chmod"
            ), patch.object(sync.subprocess, "run", return_value=loaded) as run:
                sync.install_scheduled_task(root / "sync_client.py", args, "mac-primary", "Mac Марии", ["codex"])
                launcher = root / ".agent-platform/client/sync-client.sh"
                launcher.write_text("#!/bin/sh\n# previous launcher revision\n", encoding="utf-8")
                run.reset_mock()

                sync.install_scheduled_task(root / "sync_client.py", args, "mac-primary", "Mac Марии", ["codex"])

            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any(command[:2] == ["launchctl", "bootout"] for command in commands))
            self.assertFalse(any(command[:2] == ["launchctl", "kickstart"] for command in commands))
            self.assertIn("--self-check", launcher.read_text(encoding="utf-8"))

    def test_macos_scheduler_reloads_a_missing_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = scheduler_args(root)
            with patch.dict(SCHEDULE, {"client_platform": Mock(return_value="macos")}), patch.object(
                sync.Path, "home", return_value=root
            ), patch.object(sync.os, "getuid", return_value=501, create=True), patch.object(
                sync.os, "chmod"
            ), patch.object(sync.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
                sync.install_scheduled_task(root / "sync_client.py", args, "mac-primary", "Mac Марии", ["codex"])
            missing = lambda command, **_: SimpleNamespace(returncode=113 if command[:2] == ["launchctl", "print"] else 0, stdout="")
            with patch.dict(SCHEDULE, {"client_platform": Mock(return_value="macos")}), patch.object(
                sync.Path, "home", return_value=root
            ), patch.object(sync.os, "getuid", return_value=501, create=True), patch.object(
                sync.os, "chmod"
            ), patch.object(sync.subprocess, "run", side_effect=missing) as run:
                sync.install_scheduled_task(root / "sync_client.py", args, "mac-primary", "Mac Марии", ["codex"])

            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any(command[:2] == ["launchctl", "bootstrap"] for command in commands))

    def test_windows_scheduler_uses_dpapi_and_no_transport_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = scheduler_args(root)
            protect = Mock(return_value="sealed-token")
            with patch.dict(SCHEDULE, {"client_platform": Mock(return_value="windows"),
                                       "protect_windows_token": protect}), \
                    patch.dict("os.environ", {"SystemRoot": "C:\\Windows"}), patch.object(
                sync.Path, "home", return_value=root
            ), patch.object(sync.subprocess, "run") as run:
                receipt = sync.install_scheduled_task(
                    root / "sync_client.py",
                    args,
                    "fixture",
                    "Fixture $(must-not-run)",
                    ["codex"],
                )
            protect.assert_called_once_with(args.client_token)
            launcher = (
                root / ".agent-platform/client/sync-client.ps1"
            ).read_text(encoding="utf-8-sig")
            # The command is data next to the script, never part of its source.
            self.assertNotIn("Fixture", launcher)
            launch = json.loads((root / ".agent-platform/client/sync-client.arguments.json").read_text(encoding="utf-8"))
            self.assertEqual(set(launch), {"version", "python", "commandLine"})
            self.assertIn('"Fixture $(must-not-run)"', launch["commandLine"])
            self.assertIn("--release-trust-file", launch["commandLine"])
            self.assertNotIn("--transport", launch["commandLine"])
            self.assertNotIn(args.client_token, json.dumps(launch))
            self.assertNotIn(args.client_token, launcher)
            self.assertEqual(receipt["displayName"], "Fixture $(must-not-run)")
            self.assertEqual(receipt["providers"], ["codex"])
            self.assertEqual(receipt["controlPlaneUrl"], args.control_plane_url)
            hidden = (
                root / ".agent-platform/client/sync-client.vbs"
            ).read_text(encoding="utf-16")
            self.assertIn(", 0, True)", hidden)
            task = run.call_args_list[-1].args[0]
            self.assertIn("wscript.exe //B //Nologo", task[task.index("/TR") + 1])

    def test_report_contract_contains_no_ssh_or_migration_state(self):
        args = SimpleNamespace(client_id="fixture")
        payload = execution.make_report(
            args,
            [{"provider": "codex", "projectProfiles": []}],
            None,
            {
                "agentMemory": {"status": "configured", "credentialRevision": "a" * 64},
                "projectContext": {"status": "configured", "credentialRevision": "b" * 64},
            },
            None,
            sync,
            failed=False,
        )
        serialized = json.dumps(payload)
        self.assertNotIn("ssh", serialized.lower())
        self.assertNotIn("migration", serialized.lower())

    def test_failed_run_reports_its_stage_and_launcher_events(self):
        args = SimpleNamespace(
            provider=["codex"], check_only=False, client_id="fixture", project_mappings=Path("mappings.json"),
            update_policy="manual", codex_root=Path("codex"), claude_root=Path("claude"),
        )
        desired = {"clientProtocol": 2, "release": {"releaseId": "fixture"}, "projectProfileAssignments": []}
        events = [{"event": "runtime_restored", "at": "2026-09-29T17:27:10Z"}]
        helper = speaking_client()
        helper.read_receipt.return_value = {}
        helper.read_launcher_events.return_value = events
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        helper.release_transport.return_value.stage_release.side_effect = RuntimeError("bundle unavailable")
        with patch.object(execution, "preflight_credentials", return_value={}), patch("builtins.print"):
            self.assertEqual(execution.run_locked(args, desired, ["codex"], helper), 1)

        payload = helper.report_state.call_args.args[1]
        self.assertEqual(payload["syncOutcome"], {"status": "failed", "stage": "signed_release"})
        self.assertEqual(payload["launcherEvents"], events)
        self.assertNotIn("bundle unavailable", json.dumps(payload, default=str))
        helper.clear_launcher_events.assert_called_once_with()

    def test_mcp_preflight_failure_reports_service_step_and_status(self):
        # A run that fails at mcp_preflight says more than "HTTPError".
        import io
        import urllib.error
        args = SimpleNamespace(
            provider=["codex"], check_only=False, client_id="fixture", project_mappings=Path("mappings.json"),
            update_policy="manual", codex_root=Path("codex"), claude_root=Path("claude"),
        )
        desired = {"clientProtocol": 2, "release": {"releaseId": "fixture"}, "projectProfileAssignments": []}
        helper = speaking_client()
        helper.read_receipt.return_value = {}
        helper.read_launcher_events.return_value = []
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        failure = urllib.error.HTTPError(
            "https://app.example/agent-memory/mcp", 503, "Service Unavailable", {}, io.BytesIO(b"secret body"),
        )
        with patch.object(execution, "preflight_credentials",
                          side_effect=execution.PreflightFailure(
                              execution.preflight_reason("agentMemory", "initialize", failure))), \
                patch("builtins.print"):
            self.assertEqual(execution.run_locked(args, desired, ["codex"], helper), 1)

        outcome = helper.report_state.call_args.args[1]["syncOutcome"]
        self.assertEqual(outcome, {"status": "failed", "stage": "mcp_preflight", "reason": "agentMemory_initialize_http_503"})
        self.assertNotIn("secret body", json.dumps(helper.report_state.call_args.args[1], default=str))
        # The same codes reach the local run receipt that the desktop app reads.
        helper.record_local_outcome.assert_called_once_with("mcp_preflight", "agentMemory_initialize_http_503")

    def test_a_client_runtime_that_does_not_start_reports_its_own_reason(self):
        # A release whose client cannot start must not surface only as a generic
        # provider_installation failure.
        args = SimpleNamespace(
            provider=["codex"], check_only=False, client_id="fixture", project_mappings=Path("mappings.json"),
            update_policy="automatic", codex_root=Path("codex"), claude_root=Path("claude"),
            control_plane_url="https://app.example", release_trust_file=Path("trust.json"), release_cache=Path("cache"),
        )
        desired = {"clientProtocol": 2, "release": {"releaseId": "fixture"}, "projectProfileAssignments": []}
        helper = speaking_client()
        helper.read_receipt.return_value = {}
        helper.read_launcher_events.return_value = []
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        helper.ClientRuntimeProbeFailed = type("ClientRuntimeProbeFailed", (RuntimeError,), {"code": "client_runtime_probe_failed"})
        helper.plan_client_recovery.return_value = {"action": "none", "root": Path("client"), "report": {"recovery": "none"},
                                                    "releaseId": "fixture", "requested": False, "requests": 0}
        helper.settle_client_recovery.return_value = {"recovery": "none"}
        helper.run_installers.side_effect = helper.ClientRuntimeProbeFailed("self-check failed")
        with patch.object(execution, "preflight_credentials", return_value={}), \
                patch.object(execution, "extract_verified", return_value=(Path("a"), Path("b"), Path("c"))), \
                patch.object(execution, "call", return_value={"command": {"id": "cmd", "leaseToken": "lease"}}), \
                patch("builtins.print"):
            self.assertEqual(execution.run_locked(args, desired, ["codex"], helper), 1)

        outcome = helper.report_state.call_args.args[1]["syncOutcome"]
        self.assertEqual(outcome, {"status": "failed", "stage": "provider_installation", "reason": "client_runtime_probe_failed"})
        helper.record_local_outcome.assert_called_once_with("provider_installation", "client_runtime_probe_failed")

    def test_preflight_reason_classifies_timeouts_and_network_errors(self):
        import socket
        import urllib.error
        self.assertEqual(execution.preflight_reason("projectContext", "credential", socket.timeout()), "projectContext_credential_timeout")
        self.assertEqual(execution.preflight_reason("projectContext", "initialize", urllib.error.URLError(TimeoutError())),
                         "projectContext_initialize_timeout")
        self.assertEqual(execution.preflight_reason("agentMemory", "initialize", urllib.error.URLError("refused")),
                         "agentMemory_initialize_network")
        self.assertEqual(execution.preflight_reason("agentMemory", "credential", SystemExit("bad")), "agentMemory_credential_invalid")

    def test_mcp_initialize_retries_one_transient_failure_but_not_a_rejection(self):
        # One identity hiccup must not fail the whole hourly sync.
        import urllib.error

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def http_error(code):
            return urllib.error.HTTPError("https://context.invalid/mcp", code, "fixture", {}, None)

        with patch.object(execution.time, "sleep") as sleep:
            opener = Mock()
            opener.open.side_effect = [http_error(503), Response()]
            execution.initialize_service(opener, "request", "projectContext")
            self.assertEqual(opener.open.call_count, 2)
            sleep.assert_called_once()

            for transient in (http_error(502), http_error(429), urllib.error.URLError(TimeoutError()), urllib.error.URLError("reset")):
                opener = Mock()
                opener.open.side_effect = [transient, Response()]
                execution.initialize_service(opener, "request", "agentMemory")
                self.assertEqual(opener.open.call_count, 2)

            opener = Mock()
            opener.open.side_effect = [http_error(503), http_error(503)]
            with self.assertRaises(execution.PreflightFailure) as failure:
                execution.initialize_service(opener, "request", "projectContext")
            self.assertEqual(failure.exception.reason, "projectContext_initialize_http_503")

            sleep.reset_mock()
            for rejection in (401, 403):
                opener = Mock()
                opener.open.side_effect = [http_error(rejection), Response()]
                with self.assertRaises(execution.PreflightFailure) as failure:
                    execution.initialize_service(opener, "request", "projectContext")
                self.assertEqual(failure.exception.reason, f"projectContext_initialize_http_{rejection}")
                self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()

    def test_launcher_events_are_kept_when_the_failure_report_is_not_delivered(self):
        args = SimpleNamespace(
            provider=["codex"], check_only=False, client_id="fixture", project_mappings=Path("mappings.json"),
            update_policy="manual", codex_root=Path("codex"), claude_root=Path("claude"),
        )
        desired = {"clientProtocol": 2, "release": {"releaseId": "fixture"}, "projectProfileAssignments": []}
        helper = speaking_client()
        helper.read_receipt.return_value = {}
        helper.read_launcher_events.return_value = [{"event": "self_check_failed", "at": "2026-09-29T17:27:10Z"}]
        helper.reportable_launcher_events.side_effect = lambda events, desired: events
        helper.load_project_mappings.side_effect = RuntimeError("stop")
        helper.report_state.side_effect = RuntimeError("Control Plane unavailable")
        with patch.object(execution, "preflight_credentials", return_value={}), patch("builtins.print"):
            self.assertEqual(execution.run_locked(args, desired, ["codex"], helper), 1)

        helper.clear_launcher_events.assert_not_called()

    def test_successful_report_marks_the_outcome_without_a_stage(self):
        payload = execution.make_report(
            SimpleNamespace(client_id="fixture"), [], None,
            {"agentMemory": {"status": "configured", "credentialRevision": None},
             "projectContext": {"status": "configured", "credentialRevision": None}},
            None, sync, failed=False,
        )
        self.assertEqual(payload["syncOutcome"], {"status": "succeeded", "stage": None})
        self.assertNotIn("launcherEvents", payload)


if __name__ == "__main__":
    unittest.main()
