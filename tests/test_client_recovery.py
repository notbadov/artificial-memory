"""A client that failed its self-check and was put back returns to the approved release.

What happened on a connected Mac: a release was installed, one of the client's files was damaged,
the previous client was restored and ran, and nothing ever installed the release's client
again. The client's own files were installed only together with the apps, and the apps'
receipts already named the release, so the computer kept the previous client while the service
was told it had the release.

Every scenario here goes through a real run of an installed client (`client_run_driver.py`):
the service approves a signed release, the client decides, installs, starts its follow-up run
and reports. Nothing calls the function that installs the client's files. The client that is
put back is the one a computer really has: the released client read from history, and the
client of this tree.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "tools", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_isolation import legacy_run  # noqa: E402
from test_managed_client_runtime import (  # noqa: E402
    FIXTURE_PART, FIXTURE_PART_SOURCE, launcher_preamble, load_sync_client, release_tree, release_with_part,
    released_client, self_check,
)

DRIVER = ROOT / "tests" / "client_run_driver.py"
ENTRY = "sync_client.py"
SCRIPTS = "adapters/client/scripts"
SERVICE_URL = "https://service.test/agent-api"
CLIENT_ID = "test-computer"
RELEASED = "20261011-195649-28bfcfeb8bda-cff97728ade1"
FIRST = "20261012-150231-62a20307ca35-f84003bbc99e"
SECOND = "20261013-090000-aaaaaaaaaaaa-bbbbbbbbbbbb"
THIRD = "20261014-090000-cccccccccccc-dddddddddddd"
REFUSED = "client_runtime_probe_failed"
# A file of the client was held by another program, and what a service says when it knows the reason.
BUSY = "client_files_busy"
REPORTS_BUSY = "syncOutcome.reason.client_files_busy"
# What a person's request to repair arrives as, and what a service says when it can be told of it.
REQUEST = "--repair-client"
REPORTS_REQUESTS = "clientRuntime.onRequest"
REPORTED_BEFORE_REQUESTS = {"runByReleaseClient", "installedMatchesRelease", "recovery", "entrySha256",
                            "removedFiles", "rollbackReplaced"}
# A release that is whole and does not start on one computer: its part cannot be imported
# where a certain file lies next to the client. The file is the computer's, not the release's.
BREAKS_HERE = "this-computer-breaks-the-release.txt"
PART_THAT_BREAKS_HERE = FIXTURE_PART_SOURCE + (
    f"\n\nif any((directory / {BREAKS_HERE!r}).is_file() for directory in list(Path(__file__).resolve().parents)[:2]):\n"
    "    raise RuntimeError('this release does not start on this computer')\n")
CHANNELS = [{"service": service, "configKey": key, "url": f"https://service.test/{service}"}
            for service, key in (("collaboration", "agent-platform-collaboration"),
                                 ("agentMemory", "agentMemory"), ("projectContext", "projectContext"))]

# What a release installs into Codex, as far as a run of the client can tell: the receipt that
# names the release and the note of what was reconciled. The real installer is not under test.
PROVIDER_INSTALLER = '''import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--bundle")
parser.add_argument("--codex-root", type=Path)
parser.add_argument("--external-skill-state-manifest", type=Path)
parser.add_argument("--external-skill-reconciliation-receipt", type=Path)
args = parser.parse_args()
state = json.loads(args.external_skill_state_manifest.read_text(encoding="utf-8"))
args.codex_root.mkdir(parents=True, exist_ok=True)
(args.codex_root / "agent-platform-install.json").write_text(json.dumps({
    "release_id": %(release)r, "agent_platform_sha": %(sha)r, "skills": [],
    "external_skill_registry_digest": state.get("registryDigest")}), encoding="utf-8")
args.external_skill_reconciliation_receipt.write_text(json.dumps({"provider": "codex"}), encoding="utf-8")
'''

# What the runtime's installer (companion_install.py) does once it has verified the approved
# release: it starts that release's own entry point as an explicit first install. The arguments
# are read by the real installer's parser, so a launcher that hands it something else fails here.
RUNTIME_INSTALLER = '''import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, %(scripts)r)
import companion_install
args = companion_install.parse_args(sys.argv[1:])
Path(%(record)r).write_text(json.dumps({"arguments": sys.argv[1:],
                                        "token": os.environ.get("AGENT_PLATFORM_CLIENT_TOKEN")}), encoding="utf-8")
command = [sys.executable, "-E", "-s", %(driver)r, %(entry)r, %(service)r,
           "--control-plane-url", args.control_plane_url, "--client-id", args.client_id,
           "--display-name", args.display_name, "--update-policy", args.update_policy,
           "--release-trust-file", str(args.release_trust_file), "--release-cache", str(args.release_cache),
           "--initial-install", "--scheduler", args.scheduler]
for provider in args.provider:
    command += ["--provider", provider]
raise SystemExit(subprocess.run(command, check=False).returncode)
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Computer:
    """A home with a connected client, the service it talks to and the releases the service has."""

    def __init__(self, test: unittest.TestCase):
        self.test = test
        self.directory = Path(test.enterContext(tempfile.TemporaryDirectory(prefix="client-recovery-")))
        self.home = self.directory / "home"
        self.managed = self.home / ".agent-platform" / "client"
        self.service = self.directory / "service"
        self.service.mkdir()
        self.codex = self.home / ".codex"
        self.key = Ed25519PrivateKey.generate()
        self.trust = self.directory / "release-trust.json"
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.trust.write_text(json.dumps({"version": 1, "keys": {"test-key": {
            "status": "active", "publicKey": base64.b64encode(public).decode()}}}), encoding="utf-8")
        self.sync = load_sync_client()
        self.releases: dict[str, dict] = {}
        self.seen = 0

    # ------------------------------------------------------------------ the service

    def publish(self, release_id: str, platform_root: Path) -> dict:
        """A signed release made of the client in ``platform_root`` (the tree `release_tree` or
        `release_with_part` built), and what the service says when it approves it."""
        sha = hashlib.sha1(release_id.encode()).hexdigest()
        root = platform_root.parent
        (platform_root / "tools/project_profiles.py").write_text("", encoding="utf-8")
        installer = platform_root / "adapters/codex/scripts/install_codex_release.py"
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_text(PROVIDER_INSTALLER % {"release": release_id, "sha": sha}, encoding="utf-8")
        (root / "manifest.json").write_text(json.dumps({
            "release_id": release_id, "layout": {"agent-platform": "agent-platform"},
            "sources": {"agent-platform": {"sha": sha}, "home-server-monorepo": {"sha": sha}}}), encoding="utf-8")
        files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "checksums.sha256")
        (root / "checksums.sha256").write_text("".join(
            f"{sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in files), encoding="utf-8")
        bundle = self.service / f"{release_id}.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            archive.add(root, arcname=release_id)
        manifest = {"releaseId": release_id, "agentPlatformSha": sha, "homeServerSha": sha,
                    "bundleSha256": sha256(bundle), "bundleSize": bundle.stat().st_size,
                    "createdAt": "2026-10-12T15:02:31Z", "minClientProtocol": 1}
        envelope = {"schemaVersion": 1, "keyId": "test-key", "manifest": manifest}
        signed = b"agent-platform-release-v1\x00" + json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        envelope["signature"] = base64.b64encode(self.key.sign(signed)).decode()
        self.releases[release_id] = {
            "platformRoot": platform_root, "bundle": bundle, "sha": sha,
            "files": self.sync.declared_runtime_files(platform_root),
            "desired": {"clientProtocol": 2, "appAgentChannels": CHANNELS, "projectProfileAssignments": [],
                        "externalSkillState": {"version": 1, "registryDigest": "0" * 64, "skills": []},
                        "release": {"releaseId": release_id, "agentPlatformSha": sha,
                                    "bundleSha256": manifest["bundleSha256"], "envelope": envelope}}}
        return self.releases[release_id]

    def tree(self, release_id: str, *, with_part: bool = False, part: str | None = None) -> dict:
        destination = self.directory / "releases" / release_id
        if part is not None:
            return self.publish(release_id, release_with_part(destination, part))
        return self.publish(release_id, (release_with_part if with_part else release_tree)(destination))

    def approve(self, release_id: str) -> None:
        release = self.releases[release_id]
        (self.service / "service.json").write_text(json.dumps({
            "desired": release["desired"], "bundle": str(release["bundle"])}), encoding="utf-8")

    def accept_reported_requests(self, release_id: str) -> None:
        """The service is one that can be told a repair was asked for, and says so."""
        self.releases[release_id]["desired"]["clientReportFields"] = [REPORTS_REQUESTS]
        self.approve(release_id)

    def reports(self) -> list[dict]:
        """The reports the service received since this was last asked."""
        try:
            lines = (self.service / "requests.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        new, self.seen = [json.loads(line) for line in lines[self.seen:]], len(lines)
        return [item["payload"]["report"] for item in new if item["path"] == "/v1/client-session/report"]

    # ------------------------------------------------------------------ the computer

    def environment(self, language: str = "en") -> dict:
        # Every program of a run speaks as it would on a Windows computer: in the code page,
        # unless it is the client and has made its streams UTF-8 (`legacy_run`). The answers
        # below are read as UTF-8, so a driver that starts the client without its streams
        # fails here and not only on Windows.
        self.interpreter_options, environment = legacy_run(self.home)
        environment.update({"AGENT_PLATFORM_CLIENT_TOKEN": "test-token", "ARTIFICIAL_MEMORY_LANG": language,
                            "AGENT_PLATFORM_CONSENT_ROOT": str(self.home / ".agent-platform" / "consents")})
        environment.pop("AGENT_PLATFORM_SYNC_FOLLOW_UP", None)
        return environment

    def arguments(self) -> list[str]:
        return ["--client-id", CLIENT_ID, "--display-name", "Test computer", "--control-plane-url", SERVICE_URL,
                "--release-trust-file", str(self.trust), "--release-cache", str(self.directory / "cache"),
                "--provider", "codex"]

    def run(self, entry: Path | None = None, *extra: str, language: str = "en") -> subprocess.CompletedProcess:
        """One run of the installed client, started as Desktop Companion starts it."""
        environment = self.environment(language)
        return subprocess.run(
            [sys.executable, "-E", "-s", *self.interpreter_options, str(DRIVER), str(entry or self.managed / ENTRY),
             str(self.service), *self.arguments(), "--scheduler", "companion", *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=600,
            env=environment)

    def place(self, files: dict[str, bytes], release_id: str) -> None:
        """A client as a computer has it after an earlier release: its files and the app's receipt."""
        self.managed.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (self.managed / name).write_bytes(content)
        self.codex.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "registryDigest": "0" * 64, "skills": []}
        (self.codex / "agent-platform-install.json").write_text(json.dumps({
            "release_id": release_id, "agent_platform_sha": "0" * 40, "skills": [],
            "external_skill_registry_digest": state["registryDigest"],
            "external_skill_state_digest": self.sync.state_digest(state)}), encoding="utf-8")

    def client_of(self, release_id: str) -> dict[str, bytes]:
        release = self.releases[release_id]
        return {name: (release["platformRoot"] / relative).read_bytes() for name, relative in release["files"].items()}

    def installed(self, directory: Path | None = None) -> dict[str, bytes]:
        """The files of a client; the guard next to a launcher is the launcher's."""
        return {path.name: path.read_bytes() for path in (directory or self.managed).glob("*.py")
                if path.name != self.sync.LAUNCH_GUARD_FILE}

    def receipt(self) -> str:
        return json.loads((self.codex / "agent-platform-install.json").read_text(encoding="utf-8"))["release_id"]

    def events(self) -> list[str]:
        return [event["event"] for event in self.sync.read_launcher_events(self.managed)]

    def recorded(self) -> dict:
        """The client's own record of what it decided, as the file holds it."""
        return json.loads((self.managed / "client-recovery.json").read_text(encoding="utf-8"))

    def answer(self, completed: subprocess.CompletedProcess) -> dict:
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def damage(self, name: str) -> None:
        (self.managed / name).write_text("raise RuntimeError('damaged on this computer')\n", encoding="utf-8")
        self.test.assertNotEqual(self_check(self.managed).returncode, 0)

    def record(self, event: str) -> None:
        with (self.managed / "launcher-events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"event": event, "at": "2026-10-12T15:48:00Z"}, separators=(",", ":")) + "\n")

    def companion_guard(self) -> str:
        """`guard_managed_client` of Desktop Companion (companion_sync.rs), step for step."""
        script, previous = self.managed / ENTRY, self.managed / "previous"
        if not script.is_file() or self_check(self.managed).returncode == 0:
            return "healthy"
        self.record("self_check_failed")
        if (previous / ENTRY).is_file() and self_check(previous).returncode == 0:
            for source in previous.iterdir():
                if source.suffix == ".py":
                    temporary = self.managed / f".restore-{source.name}"
                    shutil.copyfile(source, temporary)
                    temporary.replace(self.managed / source.name)
            self.record("runtime_restored")
            return "restored"
        self.record("restore_unavailable")
        script.unlink()
        return "reinstall"

    def launcher_preamble(self, source: str) -> subprocess.CompletedProcess:
        """The restore a launchd job does before it starts the client: the real text, run by sh."""
        script = self.directory / "preamble.sh"
        script.write_text("set -eu\n" + source, encoding="utf-8")
        return subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False,
                              env=self.environment(), encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ what a test asks

    def assert_on(self, release_id: str) -> None:
        """The computer has the client of this release, whole, and nothing else of a client."""
        self.test.assertEqual(self.installed(), self.client_of(release_id))
        completed = self_check(self.managed)
        self.test.assertEqual((completed.returncode, completed.stderr), (0, ""))

    def update(self, release_id: str) -> list[dict]:
        """The service approves a release and the computer takes it in one run."""
        self.approve(release_id)
        completed = self.run()
        self.test.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.test.assertEqual(self.receipt(), release_id)
        self.assert_on(release_id)
        return self.reports()


def runtime_of(report: dict) -> dict:
    return report.get("clientRuntime") or {}


class RestoredClientReturnsToTheReleaseTests(unittest.TestCase):
    """The observed sequence, with the client that is put back being each client a computer
    can have, and the restore being each one that exists."""

    def setUp(self) -> None:
        self.computer = Computer(self)

    def released_computer(self) -> Computer:
        """A computer on the released client that has just taken the first release with parts."""
        computer = self.computer
        released_client(computer.directory / "released")
        computer.place(computer.installed(computer.directory / "released"), RELEASED)
        computer.tree(FIRST)
        reports = computer.update(FIRST)
        # The released client installed the release and the release's client finished the run.
        self.assertEqual(len(reports), 2)
        return computer

    def tree_computer(self) -> Computer:
        """A computer on the client of this tree that has taken a later release, whose client
        has one file more."""
        computer = self.computer
        computer.tree(FIRST)
        computer.place(computer.client_of(FIRST), FIRST)
        computer.tree(SECOND, with_part=True)
        computer.update(SECOND)
        self.assertEqual(computer.installed(computer.managed / "previous"), computer.client_of(FIRST))
        return computer

    def assert_told_the_truth(self, reports: list[dict], restore: list[str]) -> None:
        """The run after a restore said that the client was put back, and no report said the
        release's client was running while another one was."""
        self.assertTrue(reports)
        self.assertEqual([event["event"] for event in reports[0].get("launcherEvents", [])], restore)
        for report in reports:
            self.assertIn("clientRuntime", report)
        self.assertTrue(runtime_of(reports[-1])["runByReleaseClient"])
        self.assertTrue(runtime_of(reports[-1])["installedMatchesRelease"])

    def test_after_desktop_companion_restores_the_released_client_the_computer_is_on_the_release(self) -> None:
        computer = self.released_computer()
        computer.damage("sync_rules.py")

        self.assertEqual(computer.companion_guard(), "restored")
        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(computer.receipt(), FIRST)
        computer.assert_on(FIRST)
        self.assert_told_the_truth(computer.reports(), ["self_check_failed", "runtime_restored"])

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_after_the_launcher_restores_the_released_client_the_computer_is_on_the_release(self) -> None:
        computer = self.released_computer()
        computer.damage("sync_rules.py")
        # The launcher on the computer was written by the released client.
        released = computer.directory / "released"
        preamble = subprocess.run(
            [sys.executable, "-E", "-s", "-c",
             "import importlib.util, sys\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "spec = importlib.util.spec_from_file_location('released', sys.argv[1] + '/sync_client.py')\n"
             "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
             "from pathlib import Path\n"
             "print(module.runtime_restore_shell(sys.executable, Path(sys.argv[2])), end='')\n",
             str(released), str(computer.managed)], capture_output=True, text=True, check=True, encoding="utf-8", errors="replace").stdout

        restored = computer.launcher_preamble(preamble)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("restoring the previous runtime", restored.stderr)
        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(FIRST)
        self.assert_told_the_truth(computer.reports(), ["self_check_failed", "runtime_restored"])

    def test_after_desktop_companion_restores_this_client_it_installs_the_release_again(self) -> None:
        computer = self.tree_computer()
        computer.damage(FIXTURE_PART)

        self.assertEqual(computer.companion_guard(), "restored")
        # The previous client is back and starts; the damaged file is still next to it.
        self.assertEqual(computer.installed()[ENTRY], computer.client_of(FIRST)[ENTRY])
        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        reports = computer.reports()
        self.assert_told_the_truth(reports, ["self_check_failed", "runtime_restored"])
        # In between: the restored client said that it is not the release's and that it put the
        # release's client back; the run was then finished by that client.
        self.assertEqual(len(reports), 2)
        self.assertEqual((runtime_of(reports[0])["runByReleaseClient"], runtime_of(reports[0])["recovery"]),
                         (False, "repaired"))
        self.assertEqual(runtime_of(reports[1])["recovery"], "none")
        self.assertEqual([item["releaseId"] for item in reports[0]["providers"]], [SECOND])

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_after_the_launcher_restores_this_client_it_installs_the_release_again(self) -> None:
        computer = self.tree_computer()
        computer.damage(FIXTURE_PART)

        restored = computer.launcher_preamble(launcher_preamble(computer.sync, computer.managed))
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(computer.installed()[ENTRY], computer.client_of(FIRST)[ENTRY])
        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        self.assert_told_the_truth(computer.reports(), ["self_check_failed", "runtime_restored"])

    def test_a_client_left_half_installed_is_completed_from_the_release(self) -> None:
        # The copy of a new client stopped before its entry point, which goes last: the apps'
        # receipts name the release and the old entry point starts with the new modules.
        computer = self.computer
        computer.tree(FIRST)
        computer.tree(SECOND, with_part=True)
        mixed = {**computer.client_of(SECOND), ENTRY: computer.client_of(FIRST)[ENTRY]}
        computer.place(mixed, SECOND)
        computer.approve(SECOND)
        self.assertEqual(self_check(computer.managed).returncode, 0)

        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        self.assertEqual(runtime_of(computer.reports()[0])["recovery"], "repaired")

    def test_a_release_that_fails_here_again_is_not_installed_a_third_time(self) -> None:
        computer = self.tree_computer()
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assertEqual(computer.run().returncode, 0)
        computer.assert_on(SECOND)
        computer.reports()

        # The same release does not start on this computer a second time.
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        previous = computer.client_of(FIRST)
        for language, sentence in (("en", "did not start on this computer twice"),
                                   ("ru", "дважды не запустился на этом компьютере"), ("en", "twice")):
            with self.subTest(language=language):
                completed = computer.run(language=language)
                # Taken before anything is checked: a case that fails must not leave its
                # report for the next one to count.
                reports = computer.reports()

                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                answer = json.loads(completed.stdout.strip().splitlines()[-1])
                self.assertEqual((answer["status"], answer["phase"], answer["reason"]),
                                 ("failed", "client_runtime", REFUSED))
                self.assertIn(sentence, answer["message"])
                self.assertIn(SECOND, answer["message"])
                # The previous client keeps working and was not replaced again.
                self.assertEqual(computer.installed()[ENTRY], previous[ENTRY])
                self.assertEqual(self_check(computer.managed).returncode, 0)
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]["syncOutcome"],
                                 {"status": "failed", "stage": "client_runtime", "reason": REFUSED})
                self.assertEqual((runtime_of(reports[0])["runByReleaseClient"],
                                  runtime_of(reports[0])["installedMatchesRelease"], runtime_of(reports[0])["recovery"]),
                                 (False, False, "refused"))
                # The apps are as they were: the release is installed into them.
                self.assertEqual([(item["releaseId"], item["status"]) for item in reports[0]["providers"]],
                                 [(SECOND, "current")])
        receipt = json.loads((computer.managed / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["stage"], receipt["reason"]), ("failed", "client_runtime", REFUSED))
        # What was decided is one small record of the client's own.
        state = json.loads((computer.managed / "client-recovery.json").read_text(encoding="utf-8"))
        self.assertEqual((state["version"], state["release"]["id"], state["release"]["refused"]), (1, SECOND, True))
        self.assertLess(len(json.dumps(state)), 600)

        # A different release is approved: it is installed as usual and the refusal is over.
        computer.tree(THIRD)
        reports = computer.update(THIRD)
        self.assertTrue(runtime_of(reports[-1])["installedMatchesRelease"])
        self.assertEqual(runtime_of(reports[-1])["recovery"], "none")
        self.assertNotIn("release", computer.sync.read_recovery_state(computer.managed))

    def test_files_the_running_client_does_not_declare_are_removed(self) -> None:
        computer = self.tree_computer()
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assertEqual(computer.run().returncode, 0)
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assertIn(FIXTURE_PART, computer.installed())

        # The release is refused, so the previous client stays; the file it does not know, the
        # damaged one, does not stay next to it.
        self.assertEqual(computer.run().returncode, 1)

        self.assertEqual(computer.installed(), computer.client_of(FIRST))
        self.assertEqual(runtime_of(computer.reports()[-1])["removedFiles"], 1)
        self.assertTrue((computer.managed / "previous" / ENTRY).is_file())

    def test_a_release_without_a_file_the_previous_one_had_leaves_none_behind(self) -> None:
        computer = self.tree_computer()
        computer.tree(THIRD)

        computer.update(THIRD)

        self.assertNotIn(FIXTURE_PART, computer.installed())
        # The client it replaced, with that file, is still the one to go back to.
        self.assertIn(FIXTURE_PART, computer.installed(computer.managed / "previous"))


class RepairOnRequestTests(unittest.TestCase):
    """A release refused on a computer is not installed again by a run nobody asked for. A
    person who asks gets one more attempt: it is not a reset, and when it fails the refusal
    stands as before and the person is told that the repair was tried."""

    def setUp(self) -> None:
        self.computer = Computer(self)

    def on_a_later_release(self, part: str | None = None) -> Computer:
        computer = self.computer
        computer.tree(FIRST)
        computer.place(computer.client_of(FIRST), FIRST)
        computer.tree(SECOND, with_part=True, part=part)
        computer.update(SECOND)
        return computer

    def refused_after_damage(self) -> Computer:
        """The release's client was damaged from outside twice; the release itself is whole."""
        computer = self.on_a_later_release()
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assertEqual(computer.run().returncode, 0)
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assert_refused(computer.run(), "did not start on this computer twice")
        computer.reports()
        return computer

    def assert_refused(self, completed: subprocess.CompletedProcess, sentence: str) -> None:
        computer = self.computer
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        answer = computer.answer(completed)
        self.assertEqual((answer["status"], answer["phase"], answer["reason"]), ("failed", "client_runtime", REFUSED))
        self.assertIn(sentence, answer["message"])
        self.assertIn(SECOND, answer["message"])
        # The previous client is the one installed, and it works.
        self.assertEqual(computer.installed()[ENTRY], computer.client_of(FIRST)[ENTRY])
        self.assertEqual(self_check(computer.managed).returncode, 0)
        self.assertTrue(computer.recorded()["release"]["refused"])

    def test_a_run_nobody_asked_for_leaves_the_refusal_in_place(self) -> None:
        computer = self.refused_after_damage()

        for _ in range(2):
            self.assert_refused(computer.run(), "did not start on this computer twice")

        reports = computer.reports()
        self.assertEqual([runtime_of(report)["recovery"] for report in reports], ["refused", "refused"])
        self.assertNotIn("request", computer.recorded())

    def test_a_person_who_asks_gets_the_release_back_when_its_client_starts_now(self) -> None:
        computer = self.refused_after_damage()

        completed = computer.run(None, REQUEST)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        reports = computer.reports()
        # The previous client installed the release's client and that client finished the run.
        self.assertEqual([(runtime_of(report)["runByReleaseClient"], runtime_of(report)["recovery"])
                          for report in reports], [(False, "repaired"), (True, "none")])
        # A service that does not say it can be told of a request gets the report it always got.
        for report in reports:
            self.assertEqual(set(runtime_of(report)), REPORTED_BEFORE_REQUESTS)
        state = computer.recorded()
        self.assertEqual((state["release"]["id"], state["release"]["repairs"], state["release"]["refused"]),
                         (SECOND, 1, False))
        self.assertEqual((state["request"]["id"], state["request"]["count"]), (SECOND, 1))
        self.assertEqual(computer.run().returncode, 0)

        # One more attempt and not a new beginning: when the same release fails here once more,
        # no run installs it by itself, and the person reads that the repair was already tried.
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assert_refused(computer.run(), "also after the repair you asked for")
        self.assert_refused(computer.run(language="ru"), "и после восстановления, которое вы запросили")
        self.assertEqual(computer.recorded()["request"]["count"], 1)

    def test_an_attempt_that_fails_again_is_refused_again_and_the_person_is_told(self) -> None:
        computer = self.on_a_later_release(PART_THAT_BREAKS_HERE)
        computer.accept_reported_requests(SECOND)
        (computer.managed / BREAKS_HERE).write_text("", encoding="utf-8")
        self.assertNotEqual(self_check(computer.managed).returncode, 0)
        self.assertEqual(computer.companion_guard(), "restored")
        # By itself: one repair, which fails, and from then on the refusal.
        failed = computer.run()
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("could not be installed again", computer.answer(failed)["message"])
        self.assert_refused(computer.run(), "did not start on this computer twice")
        self.assertEqual([runtime_of(report)["recovery"] for report in computer.reports()], ["failed", "refused"])

        for count, (language, sentence, advice) in enumerate((
                ("en", "The repair you asked for was tried", "connect this computer again or write to support"),
                ("ru", "Восстановление, которое вы запросили, выполнено",
                 "переподключите компьютер или напишите в поддержку"),
        ), start=1):
            with self.subTest(language=language):
                completed = computer.run(None, REQUEST, language=language)

                self.assert_refused(completed, sentence)
                self.assertIn(advice, computer.answer(completed)["message"])
                reports = computer.reports()
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]["syncOutcome"],
                                 {"status": "failed", "stage": "client_runtime", "reason": REFUSED})
                # The service is told that this attempt was a person's and that it failed.
                self.assertEqual((runtime_of(reports[0])["recovery"], runtime_of(reports[0]).get("onRequest")),
                                 ("failed", True))
                self.assertEqual(set(runtime_of(reports[0])), REPORTED_BEFORE_REQUESTS | {"onRequest"})
                state = computer.recorded()
                self.assertEqual((state["request"]["id"], state["request"]["count"]), (SECOND, count))
                self.assertLess(len(json.dumps(state)), 600)

        # The refusal stands as before for every run nobody asked for.
        completed = computer.run()
        self.assert_refused(completed, "also after the repair you asked for")
        self.assertIn("connect this computer again or write to support", computer.answer(completed)["message"])
        reports = computer.reports()
        self.assertEqual(runtime_of(reports[0])["recovery"], "refused")
        self.assertNotIn("onRequest", runtime_of(reports[0]))
        self.assertEqual(computer.recorded()["request"]["count"], 2)
        receipt = json.loads((computer.managed / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["stage"], receipt["reason"]), ("failed", "client_runtime", REFUSED))

        # What stopped the release on this computer is gone, and the person asks once more.
        (computer.managed / BREAKS_HERE).unlink()
        completed = computer.run(None, REQUEST)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        reports = computer.reports()
        self.assertEqual((runtime_of(reports[0])["recovery"], runtime_of(reports[0]).get("onRequest")),
                         ("repaired", True))
        self.assertNotIn("onRequest", runtime_of(reports[1]))

    def test_the_installer_hands_a_request_on_and_lifts_nothing_by_itself(self) -> None:
        # The runtime's installer starts the release's own entry point as a first install: for
        # a launcher of an hourly job that found nothing to restore, and for a person whose
        # installed client cannot be given the request.
        computer = self.refused_after_damage()
        entry = computer.releases[SECOND]["platformRoot"] / SCRIPTS / ENTRY

        self.assert_refused(computer.run(entry, "--initial-install"), "did not start on this computer twice")
        completed = computer.run(entry, "--initial-install", REQUEST)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        self.assertEqual(computer.recorded()["request"]["count"], 1)
        sys.path.insert(0, str(ROOT / SCRIPTS))
        try:
            import companion_install
        finally:
            sys.path.remove(str(ROOT / SCRIPTS))
        required = ["--control-plane-url", SERVICE_URL, "--client-id", CLIENT_ID, "--display-name", "Mac",
                    "--provider", "codex", "--release-trust-file", str(computer.trust)]
        self.assertFalse(companion_install.parse_args(required).repair_client)
        self.assertTrue(companion_install.parse_args([*required, REQUEST]).repair_client)
        source = (ROOT / SCRIPTS / "companion_install.py").read_text(encoding="utf-8")
        self.assertIn('if args.repair_client and "repairOnRequest" in release_capabilities(script, environment):\n', source)

    def test_asking_when_nothing_was_refused_is_an_ordinary_run(self) -> None:
        computer = self.on_a_later_release()
        computer.accept_reported_requests(SECOND)

        completed = computer.run(None, REQUEST)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        reports = computer.reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(set(runtime_of(reports[0])), REPORTED_BEFORE_REQUESTS)
        self.assertEqual(runtime_of(reports[0])["recovery"], "none")
        self.assertNotIn("request", computer.sync.read_recovery_state(computer.managed))

        # The first failure of a release is repaired whoever started the run; that repair is
        # the client's own and is not counted as a person's request.
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        completed = computer.run(None, REQUEST)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        reports = computer.reports()
        self.assertEqual(runtime_of(reports[0])["recovery"], "repaired")
        self.assertNotIn("onRequest", runtime_of(reports[0]))
        self.assertNotIn("request", computer.recorded())

    def test_the_client_says_that_it_can_be_asked(self) -> None:
        # Read by whoever starts the client before the request is handed to it: a client from
        # before this rule stops on an argument it does not know.
        self.assertIn("repairOnRequest", self.computer.sync.CLIENT_CAPABILITIES)
        source = (ROOT / SCRIPTS / ENTRY).read_text(encoding="utf-8")
        # Named once, where the arguments are read: no launcher and no installer of an hourly
        # job is ever written with it.
        self.assertEqual(source.count(REQUEST), 1)
        # The launchers and the hourly job are written by parts of the entry point: none names it.
        for part in self.computer.sync.MANAGED_CLIENT_PARTS:
            self.assertNotIn(REQUEST, (ROOT / SCRIPTS / part).read_text(encoding="utf-8"), part)


class HeldFileTests(unittest.TestCase):
    """A repair that stopped because another program held a file of the client says nothing
    about the release. On a Windows computer an antivirus opens every file the client has just
    written; counted as a failure, two such moments would refuse a release that is whole.
    (`tests/test_client_busy_files.py` holds the calls themselves; here it is the run.)"""

    def setUp(self) -> None:
        self.computer = Computer(self)
        computer = self.computer
        computer.tree(FIRST)
        computer.place(computer.client_of(FIRST), FIRST)
        computer.tree(SECOND, with_part=True)
        computer.update(SECOND)
        # The release's client was damaged and the previous one put back: the next run repairs.
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        computer.reports()

    def hold(self, *names: str) -> None:
        (self.computer.service / "held.json").write_text(json.dumps(names), encoding="utf-8")

    def let_go(self) -> None:
        (self.computer.service / "held.json").unlink()

    def test_a_repair_that_a_held_file_stopped_is_tried_again_and_never_counted(self) -> None:
        computer = self.computer
        self.hold("sync_rules.py")

        # More often than a release is repaired before it is refused.
        for language, sentence in (("en", "Another program was holding a file"),
                                   ("ru", "был занят другой программой"), ("en", "holding a file")):
            with self.subTest(language=language):
                completed = computer.run(language=language)
                reports = computer.reports()

                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                answer = computer.answer(completed)
                self.assertEqual((answer["status"], answer["phase"], answer["reason"]),
                                 ("failed", "client_runtime", BUSY))
                self.assertIn(sentence, answer["message"])
                self.assertEqual(answer["messageLanguage"], language)
                # The client that was put back is still the one here, and it works.
                self.assertEqual(computer.installed()[ENTRY], computer.client_of(FIRST)[ENTRY])
                self.assertEqual(self_check(computer.managed).returncode, 0)
                self.assertEqual(computer.installed(computer.managed / "previous"), computer.client_of(FIRST))
                self.assertEqual(len(reports), 1)
                # A service that does not name the reason is told the stage and no more.
                self.assertEqual(reports[0]["syncOutcome"], {"status": "failed", "stage": "client_runtime"})
                self.assertEqual(runtime_of(reports[0])["recovery"], "failed")
                # Nothing is held against the release.
                self.assertNotIn("release", computer.sync.read_recovery_state(computer.managed))
        receipt = json.loads((computer.managed / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["status"], receipt["stage"], receipt["reason"]), ("failed", "client_runtime", BUSY))

        # A service that names the reason is told it.
        computer.releases[SECOND]["desired"]["clientReportFields"] = [REPORTS_BUSY]
        computer.approve(SECOND)
        self.assertEqual(computer.run().returncode, 1)
        self.assertEqual(computer.reports()[0]["syncOutcome"],
                         {"status": "failed", "stage": "client_runtime", "reason": BUSY})

        # The other program lets go: the same run repairs the client, as its first repair.
        self.let_go()
        completed = computer.run()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(SECOND)
        self.assertEqual(runtime_of(computer.reports()[0])["recovery"], "repaired")
        self.assertEqual((computer.recorded()["release"]["repairs"], computer.recorded()["release"]["refused"]),
                         (1, False))

    def test_a_held_file_during_the_repair_a_person_asked_for_leaves_the_refusal_and_says_why(self) -> None:
        computer = self.computer
        self.assertEqual(computer.run().returncode, 0)
        computer.damage(FIXTURE_PART)
        self.assertEqual(computer.companion_guard(), "restored")
        self.assertEqual(computer.run().returncode, 1)
        self.assertTrue(computer.recorded()["release"]["refused"])
        computer.reports()
        self.hold(ENTRY)

        completed = computer.run(None, REQUEST)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        answer = computer.answer(completed)
        # What stopped the attempt is what the person reads, not that the release does not start.
        self.assertEqual((answer["phase"], answer["reason"]), ("client_runtime", BUSY))
        self.assertIn("Another program was holding a file", answer["message"])
        # The release is as refused as it was, with its one repair of before.
        self.assertEqual((computer.recorded()["release"]["repairs"], computer.recorded()["release"]["refused"]),
                         (1, True))
        self.let_go()
        self.assertEqual(computer.run(None, REQUEST).returncode, 0)
        computer.assert_on(SECOND)


class RollbackTargetTests(unittest.TestCase):
    """What lies in previous/ is what a launcher or Desktop Companion puts back without asking
    anyone. It has to be a client that returns to the approved release."""

    def setUp(self) -> None:
        self.computer = Computer(self)

    def test_a_previous_client_that_cannot_return_to_a_release_is_replaced_by_the_running_one(self) -> None:
        computer = self.computer
        released_client(computer.directory / "released")
        computer.place(computer.installed(computer.directory / "released"), RELEASED)
        computer.tree(FIRST)

        reports = computer.update(FIRST)

        self.assertEqual(computer.installed(computer.managed / "previous"), computer.client_of(FIRST))
        self.assertEqual(self_check(computer.managed / "previous").returncode, 0)
        self.assertTrue(runtime_of(reports[-1])["rollbackReplaced"])
        # Once: the next run finds a client it can go back to.
        self.assertEqual(computer.run().returncode, 0)
        self.assertFalse(runtime_of(computer.reports()[-1])["rollbackReplaced"])

    def test_a_previous_client_that_returns_to_a_release_is_kept(self) -> None:
        computer = self.computer
        computer.tree(FIRST)
        computer.place(computer.client_of(FIRST), FIRST)
        computer.tree(SECOND, with_part=True)

        reports = computer.update(SECOND)

        self.assertEqual(computer.installed(computer.managed / "previous"), computer.client_of(FIRST))
        self.assertFalse(runtime_of(reports[-1])["rollbackReplaced"])

    def test_the_client_says_that_it_returns_to_the_release(self) -> None:
        # What the next client reads before it keeps this one as the client to go back to.
        self.assertIn("releaseRecovery", self.computer.sync.CLIENT_CAPABILITIES)


class NothingToRestoreTests(unittest.TestCase):
    """A client that cannot start and has no previous one: the first install of a computer, or
    a copy that is damaged too."""

    def setUp(self) -> None:
        self.computer = Computer(self)
        computer = self.computer
        computer.tree(FIRST)
        computer.place(computer.client_of(FIRST), FIRST)
        computer.approve(FIRST)
        self.assertEqual(computer.run().returncode, 0)
        computer.reports()
        self.assertFalse((computer.managed / "previous").exists())

    def runtime_installer(self) -> tuple[Path, Path]:
        """A client runtime as the terminal command downloads it, with its installer."""
        computer = self.computer
        installer = computer.directory / "runtime" / "bootstrap" / "companion_install.py"
        installer.parent.mkdir(parents=True)
        record = computer.directory / "installer-was-started.json"
        installer.write_text(RUNTIME_INSTALLER % {
            "scripts": str(ROOT / SCRIPTS), "record": str(record), "driver": str(DRIVER),
            "entry": str(computer.releases[FIRST]["platformRoot"] / SCRIPTS / ENTRY),
            "service": str(computer.service)}, encoding="utf-8")
        return installer, record

    def test_desktop_companion_installs_the_release_again_and_the_report_says_so(self) -> None:
        computer = self.computer
        computer.damage("sync_rules.py")

        self.assertEqual(computer.companion_guard(), "reinstall")
        entry = computer.releases[FIRST]["platformRoot"] / SCRIPTS / ENTRY
        completed = computer.run(entry, "--initial-install")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        computer.assert_on(FIRST)
        reports = computer.reports()
        self.assertEqual([event["event"] for event in reports[0]["launcherEvents"]],
                         ["self_check_failed", "restore_unavailable"])
        self.assertEqual(runtime_of(reports[0])["recovery"], "repaired")

    def test_the_interpreter_of_a_client_runtime_leads_to_its_installer(self) -> None:
        sync = self.computer.sync
        installer, _ = self.runtime_installer()
        root = installer.parents[1]
        for executable in ("python/bin/python3", "python/python.exe"):
            with self.subTest(executable=executable):
                self.assertEqual(sync.runtime_installer(str(root / executable)), installer)
        # An interpreter that is not a client runtime has none: nothing is guessed.
        self.assertIsNone(sync.runtime_installer(sys.executable))
        self.assertIsNone(sync.runtime_installer(str(self.computer.directory / "elsewhere" / "bin" / "python3")))

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher")
    def test_the_launcher_of_a_terminal_connection_installs_the_release_again(self) -> None:
        computer, sync = self.computer, self.computer.sync
        installer, record = self.runtime_installer()
        client = [sys.executable, "-E", "-s", str(DRIVER), str(computer.managed / ENTRY), str(computer.service),
                  *computer.arguments()]
        reinstall = [sys.executable, "-E", "-s", str(installer), *computer.arguments(),
                     "--update-policy", "automatic", "--scheduler", "os", "--schedule-layout", "terminal",
                     "--keychain-service", "test-service"]
        sync.write_launch_guard(computer.managed)
        launcher = computer.directory / "sync-client.sh"
        launcher.write_text(sync.macos_launcher(
            client, computer.managed, "AGENT_PLATFORM_CLIENT_TOKEN=test-token\n", reinstall), encoding="utf-8")
        environment = computer.environment()
        del environment["AGENT_PLATFORM_CLIENT_TOKEN"]

        def start() -> subprocess.CompletedProcess:
            return subprocess.run(["/bin/sh", str(launcher)], capture_output=True, text=True, check=False,
                                  env=environment, timeout=600, encoding="utf-8", errors="replace")

        # A healthy client is started and the installer is left alone.
        healthy = start()
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertFalse(record.exists())
        computer.reports()

        computer.damage("sync_rules.py")
        damaged = computer.installed()
        completed = start()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        started = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((started["arguments"], started["token"]), (reinstall[4:], "test-token"))
        # Nothing of the damaged client ran: its traceback would be in the output.
        self.assertNotIn("damaged on this computer", completed.stdout + completed.stderr)
        self.assertNotEqual(computer.installed(), damaged)
        computer.assert_on(FIRST)
        reports = computer.reports()
        self.assertEqual([event["event"] for event in reports[0]["launcherEvents"]],
                         ["self_check_failed", "restore_unavailable"])
        self.assertEqual(runtime_of(reports[0])["recovery"], "repaired")

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher")
    def test_a_launcher_without_a_client_runtime_changes_nothing_and_says_so(self) -> None:
        computer, sync = self.computer, self.computer.sync
        client = [sys.executable, "-E", "-s", str(DRIVER), str(computer.managed / ENTRY), str(computer.service),
                  *computer.arguments()]
        sync.write_launch_guard(computer.managed)
        launcher = computer.directory / "sync-client.sh"
        launcher.write_text(sync.macos_launcher(
            client, computer.managed, "AGENT_PLATFORM_CLIENT_TOKEN=test-token\n", None), encoding="utf-8")
        computer.damage("sync_rules.py")
        damaged = computer.installed()

        completed = subprocess.run(["/bin/sh", str(launcher)], capture_output=True, text=True, check=False,
                                   env=computer.environment(), timeout=600, encoding="utf-8", errors="replace")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(computer.installed(), damaged)
        self.assertEqual(computer.events(), ["self_check_failed", "restore_unavailable"])

    def test_the_reinstall_command_is_the_installer_with_what_the_connection_was_made_with(self) -> None:
        computer, sync = self.computer, self.computer.sync
        installer, _ = self.runtime_installer()
        python = str(installer.parents[1] / "python/bin/python3")
        home = Path.home()
        arguments = sync.argparse.Namespace(
            control_plane_url=SERVICE_URL, update_policy="manual", release_trust_file=computer.trust,
            release_cache=computer.directory / "cache", schedule_layout="terminal", keychain_service="test-service",
            codex_root=home / ".codex", claude_root=home / ".claude", claude_config=home / ".claude.json",
            bin_dir=home / ".local" / "bin", project_mappings=home / ".agent-platform/project-mappings.json")

        command = sync.reinstall_command(python, arguments, CLIENT_ID, "Test computer", ["codex", "kimi"])

        self.assertEqual(command, [
            python, "-E", "-s", str(installer), "--control-plane-url", SERVICE_URL, "--client-id", CLIENT_ID,
            "--display-name", "Test computer", "--update-policy", "manual",
            "--release-trust-file", str(computer.trust.resolve()),
            "--release-cache", str((computer.directory / "cache").resolve()),
            "--scheduler", "os", "--schedule-layout", "terminal", "--keychain-service", "test-service",
            "--provider", "codex", "--provider", "kimi"])
        # The real installer reads exactly these.
        sys.path.insert(0, str(ROOT / SCRIPTS))
        try:
            import companion_install
        finally:
            sys.path.remove(str(ROOT / SCRIPTS))
        parsed = companion_install.parse_args(command[4:])
        self.assertEqual((parsed.scheduler, parsed.schedule_layout, parsed.provider), ("os", "terminal", ["codex", "kimi"]))
        # The installer installs into the default places only; a connection that was given
        # others cannot be installed again by it, and no interpreter but a runtime has one.
        arguments.codex_root = computer.directory / "another-codex"
        self.assertIsNone(sync.reinstall_command(python, arguments, CLIENT_ID, "Test computer", ["codex"]))
        arguments.codex_root = home / ".codex"
        self.assertIsNone(sync.reinstall_command(sys.executable, arguments, CLIENT_ID, "Test computer", ["codex"]))

    def test_the_windows_launcher_starts_the_installer_when_nothing_can_be_restored(self) -> None:
        # Generated text only: no test here runs PowerShell.
        sync = self.computer.sync
        preamble = sync.runtime_restore_powershell()
        self.assertIn("$reinstall = $false", preamble)
        # Decided where the guard answers that nothing could be restored (it wrote
        # `restore_unavailable` itself), and only with an installer named in the arguments.
        self.assertIn(f"if ($guardExit -eq {sync.GUARD_NOTHING_TO_RESTORE}) {{\n  $reinstall = "
                      "($launch.reinstallCommandLine -is [string]) -and ", preamble)
        source = inspect.getsource(sync.install_scheduled_task)
        self.assertIn('"  $start.Arguments = if ($reinstall) { $launch.reinstallCommandLine } '
                      'else { $launch.commandLine }\\n"', source)
        self.assertIn('"reinstallCommandLine"', source)

    def test_launchers_already_on_computers_are_written_again(self) -> None:
        sync = self.computer.sync
        self.assertGreaterEqual(sync.MACOS_LAUNCHER_REVISION, 4)
        self.assertGreaterEqual(sync.WINDOWS_LAUNCHER_REVISION, 7)


class LauncherWritingTests(unittest.TestCase):
    """What the client writes for the OS schedule of a connection made on a client runtime."""

    def setUp(self) -> None:
        self.sync = load_sync_client()
        # A space in the home, as in a Windows profile named after a person: every path the
        # client writes into a launcher has to stay one word there.
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="client launcher ")))
        self.runtime = self.home / ".agent-platform" / "runtime" / "20261012"
        (self.runtime / "bootstrap").mkdir(parents=True)
        (self.runtime / "bootstrap" / "companion_install.py").write_text("", encoding="utf-8")
        self.client = self.home / ".agent-platform" / "client"

    def arguments(self, **changed: object) -> SimpleNamespace:
        home = self.home
        return SimpleNamespace(**{
            "control_plane_url": SERVICE_URL, "release_trust_file": home / "trust.json", "release_cache": home / "cache",
            "client_token": "private-test-token", "update_policy": "automatic", "schedule_layout": "terminal",
            "keychain_service": "test-service", "codex_root": home / ".codex", "claude_root": home / ".claude",
            "claude_config": home / ".claude.json", "bin_dir": home / ".local" / "bin",
            "project_mappings": home / ".agent-platform/project-mappings.json", **changed})

    def install(self, platform: str, python: Path, name: str, **changed: object) -> None:
        sync = self.sync
        # The platform and the sealing are replaced where the job is written: in the file that function is in.
        job = {"client_platform": lambda: platform, "protect_windows_token": lambda value: "sealed"}
        with patch.dict(sync.install_scheduled_task.__globals__, job), \
                patch.object(sync.Path, "home", return_value=self.home), \
                patch.object(sync.sys, "executable", str(python)), \
                patch.object(sync.os, "getuid", return_value=501, create=True), patch.object(sync.os, "chmod"), \
                patch.dict("os.environ", {"SystemRoot": "C:\\Windows"}), \
                patch.object(sync.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            sync.install_scheduled_task(self.client / ENTRY, self.arguments(**changed), CLIENT_ID, name, ["codex"])

    def test_the_mac_launcher_of_a_runtime_names_its_installer(self) -> None:
        python = self.runtime / "python" / "bin" / "python3"
        self.install("macos", python, "Mac")
        launcher = (self.client / "sync-client.sh").read_text(encoding="utf-8")
        # As the shell reads them: the launcher quotes whatever is not a plain word.
        python, installer = (shlex.quote(str(path))
                             for path in (python, self.runtime / "bootstrap" / "companion_install.py"))
        self.assertIn(f"  exec {python} -E -s {installer} --control-plane-url {SERVICE_URL} --client-id {CLIENT_ID} ", launcher)
        self.assertIn("--scheduler os --schedule-layout terminal --keychain-service test-service --provider codex\n",
                      launcher)
        # Decided after the restore and before the client is started; no credential is in the text.
        self.assertLess(launcher.index("reinstall=1"), launcher.index('if [ -n "$reinstall" ]'))
        self.assertLess(launcher.index('if [ -n "$reinstall" ]'), launcher.rindex(f"exec {python} "))
        self.assertNotIn("private-test-token", launcher)

    def test_a_run_a_person_asked_for_writes_a_job_that_asks_for_nothing(self) -> None:
        self.install("macos", self.runtime / "python" / "bin" / "python3", "Mac", repair_client=True)
        self.assertNotIn(REQUEST, (self.client / "sync-client.sh").read_text(encoding="utf-8"))
        self.install("windows", self.runtime / "python" / "python.exe", "PC", repair_client=True)
        self.assertNotIn(REQUEST, (self.client / "sync-client.arguments.json").read_text(encoding="utf-8"))

    def test_a_launcher_for_any_other_interpreter_is_what_it_was(self) -> None:
        self.install("macos", Path("/usr/bin/python3"), "Mac", schedule_layout="legacy")
        launcher = (self.client / "sync-client.sh").read_text(encoding="utf-8")
        self.assertNotIn("reinstall", launcher)
        self.assertNotIn("companion_install.py", launcher)
        # Places of the apps that the installer cannot be told: no installer either.
        self.install("macos", self.runtime / "python" / "bin" / "python3", "Mac", codex_root=self.home / "elsewhere")
        self.assertNotIn("reinstall", (self.client / "sync-client.sh").read_text(encoding="utf-8"))

    def test_the_windows_arguments_name_the_installer_as_one_command_line(self) -> None:
        python = self.runtime / "python" / "python.exe"
        name = 'Office" --control-plane-url "https://evil.example'
        self.install("windows", python, name)
        launch = json.loads((self.client / "sync-client.arguments.json").read_text(encoding="utf-8"))
        self.assertEqual(set(launch), {"version", "python", "commandLine", "reinstallCommandLine"})
        with patch.object(self.sync.Path, "home", return_value=self.home):
            expected = self.sync.reinstall_command(str(python), self.arguments(), CLIENT_ID, name, ["codex"])
        self.assertEqual(expected[3], str(self.runtime / "bootstrap" / "companion_install.py"))
        # Written by the rules python.exe reads a command line with, like the client's own
        # line: a name cannot become further options of the installer.
        self.assertEqual(launch["reinstallCommandLine"], subprocess.list2cmdline(expected[1:]))
        self.assertEqual(expected[expected.index("--display-name") + 1], name)
        script = (self.client / "sync-client.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("companion_install", script)
        self.assertNotIn("Office", script)
        self.assertLess(script.index("$reinstall = $false"), script.index("ConvertTo-SecureString"))

    def test_windows_arguments_without_a_runtime_are_what_they_were(self) -> None:
        self.install("windows", Path("C:/Python312/python.exe"), "PC")
        launch = json.loads((self.client / "sync-client.arguments.json").read_text(encoding="utf-8"))
        self.assertEqual(set(launch), {"version", "python", "commandLine"})


class RecoveryDecisionTests(unittest.TestCase):
    """The rule on its own: when the installed client is installed again, and when it stops."""

    def setUp(self) -> None:
        self.sync = load_sync_client()
        self.decide = self.sync.decide_client_recovery

    def test_a_client_that_is_the_release_s_is_left_alone(self) -> None:
        self.assertEqual(self.decide({}, FIRST, differs=False, accepted=True, started=True)[0], "none")

    def test_an_update_the_computer_has_not_taken_is_not_a_repair(self) -> None:
        # The apps are on an earlier release: the update decides, with the person's policy.
        self.assertEqual(self.decide({}, FIRST, differs=True, accepted=False, started=True), ("none", {}))

    def test_a_computer_that_never_had_a_client_is_not_repaired(self) -> None:
        self.assertEqual(self.decide({}, FIRST, differs=True, accepted=True, started=False), ("none", {}))

    def test_the_first_failure_of_a_release_is_repaired_and_the_second_refused(self) -> None:
        action, state = self.decide({}, FIRST, differs=True, accepted=True, started=True)
        self.assertEqual((action, state["id"], state["repairs"], state["refused"]), ("repair", FIRST, 1, False))
        action, state = self.decide(state, FIRST, differs=True, accepted=True, started=True)
        self.assertEqual((action, state["refused"]), ("refused", True))
        self.assertEqual(self.decide(state, FIRST, differs=True, accepted=True, started=True)[0], "refused")
        # Whole again by other means (a person connected the computer again): still remembered.
        self.assertEqual(self.decide(state, FIRST, differs=False, accepted=True, started=True), ("none", state))

    def test_a_request_lifts_the_refusal_for_one_decision_and_changes_nothing_else(self) -> None:
        _, state = self.decide({}, FIRST, differs=True, accepted=True, started=True)
        _, refused = self.decide(state, FIRST, differs=True, accepted=True, started=True)
        action, state = self.decide(refused, FIRST, differs=True, accepted=True, started=True, requested=True)
        self.assertEqual((action, state["id"], state["repairs"], state["refused"]), ("repair", FIRST, 1, False))
        # The count of the client's own repairs is as it was: the next failure is refused.
        self.assertEqual(self.decide(state, FIRST, differs=True, accepted=True, started=True)[0], "refused")
        # Nothing to repair, an update not yet taken, a computer without a client: a request
        # decides nothing there.
        self.assertEqual(self.decide(refused, FIRST, differs=False, accepted=True, started=True, requested=True),
                         ("none", refused))
        self.assertEqual(self.decide({}, FIRST, differs=True, accepted=False, started=True, requested=True), ("none", {}))
        self.assertEqual(self.decide({}, FIRST, differs=True, accepted=True, started=False, requested=True), ("none", {}))

    def test_the_record_of_requests_is_small_and_read_only_as_the_client_wrote_it(self) -> None:
        release = {"id": FIRST, "repairs": 1, "refused": True, "at": "2026-10-12T15:48:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            written = {"version": 1, "release": release,
                       "request": {"id": FIRST, "count": 3, "at": "2026-10-12T16:00:00Z"}}
            (root / "client-recovery.json").write_text(json.dumps(written), encoding="utf-8")
            self.assertEqual(self.sync.read_recovery_state(root)["request"]["count"], 3)
            for request in ({"id": FIRST, "count": 0, "at": "2026-10-12T16:00:00Z"},
                            {"id": FIRST, "count": 10_000, "at": "2026-10-12T16:00:00Z"},
                            {"id": FIRST, "count": True, "at": "2026-10-12T16:00:00Z"},
                            {"id": "../x", "count": 1, "at": "2026-10-12T16:00:00Z"},
                            {"id": FIRST, "count": 1}, "yes"):
                (root / "client-recovery.json").write_text(json.dumps({**written, "request": request}), encoding="utf-8")
                self.assertEqual(self.sync.read_recovery_state(root), {})

    def test_another_release_starts_from_nothing(self) -> None:
        _, state = self.decide({}, FIRST, differs=True, accepted=True, started=True)
        _, state = self.decide(state, FIRST, differs=True, accepted=True, started=True)
        self.assertEqual(self.decide(state, SECOND, differs=False, accepted=True, started=True), ("none", {}))
        action, state = self.decide(state, SECOND, differs=True, accepted=True, started=True)
        self.assertEqual((action, state["id"]), ("repair", SECOND))

    def test_a_record_that_is_not_the_client_s_own_is_read_as_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for content in ("not json", "[]", json.dumps({"version": 2, "release": {"id": FIRST}}),
                            json.dumps({"version": 1, "release": {"id": "../x", "repairs": 1, "refused": True}}),
                            json.dumps({"version": 1, "release": {"id": FIRST, "repairs": "1", "refused": True}}),
                            "x" * 100_000):
                (root / "client-recovery.json").write_text(content, encoding="utf-8")
                self.assertEqual(self.sync.read_recovery_state(root), {})
                self.assertFalse(self.sync.release_client_refused(FIRST, root))
            (root / "client-recovery.json").write_text(json.dumps({"version": 1, "release": {
                "id": FIRST, "repairs": 1, "refused": True, "at": "2026-10-12T15:48:00Z"}}), encoding="utf-8")
            self.assertTrue(self.sync.release_client_refused(FIRST, root))
            self.assertFalse(self.sync.release_client_refused(SECOND, root))

    def test_only_files_of_a_client_are_ever_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kept = ("sync_client.py", "sync_guard.py", "last-run.json", "sync-client.sh", ".restore-sync_rules.py",
                    "notes.txt")
            for name in (*kept, "sync_orphan.py", "left_over.py"):
                (root / name).write_text("", encoding="utf-8")
            (root / "previous").mkdir()
            (root / "previous" / "sync_orphan.py").write_text("", encoding="utf-8")
            (root / "package.py").mkdir()

            removed = self.sync.remove_undeclared_client_files(root, {"sync_client.py", "sync_guard.py"})

            self.assertEqual(removed, ["left_over.py", "sync_orphan.py"])
            self.assertEqual(sorted(path.name for path in root.iterdir()), sorted([*kept, "previous", "package.py"]))
            self.assertTrue((root / "previous" / "sync_orphan.py").is_file())

    def test_ending_the_connection_forgets_what_was_decided(self) -> None:
        self.assertIn("client-recovery.json", self.sync.ACCESS_CLIENT_FILES)


if __name__ == "__main__":
    unittest.main()
