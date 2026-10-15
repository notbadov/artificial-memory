"""What the hourly job does before it starts the client: starts that overlap, and a self-check
that does not return.

A launcher checks that the client can start and puts the previous client back when it cannot.
It used to do so before any lock was taken, in shell and PowerShell text:

* a job that began while another run was installing a release found a client that did not
  start (the one being replaced) and restored the previous client over the installation;
* two jobs that began together restored at the same time, through the same temporary names;
* a self-check that never returned kept the job, and every later one, waiting for good.

Every launcher here is the file the client really writes for the OS schedule
(`install_scheduled_task`); only what it starts in the end is a script of the test. The
launchers of a Mac are run by `/bin/sh`. Those of Windows are run by PowerShell on a Windows
computer only (`WindowsLauncherRunTests`); everywhere else their text is held to the letter.
"""

from __future__ import annotations

import errno
import importlib.util
import inspect
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "tools", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_client_busy_files import Held, HeldRead, hold_without_sharing, let_go  # noqa: E402
from test_isolation import WINDOWS_SYSTEM_ROOT, sandboxed_environment  # noqa: E402
from test_managed_client_runtime import load_sync_client, release_tree, self_check  # noqa: E402

SCRIPTS = ROOT / "adapters/client/scripts"
ENTRY = "sync_client.py"
GUARD = "sync-client-guard.py"
CLIENT_ID = "test-computer"
SERVICE_URL = "https://service.test/agent-api"
POSIX = unittest.skipIf(os.name == "nt", "the launcher of a Mac is run by /bin/sh; Windows runs its own below")

# A client as far as a launcher can tell: an entry point that answers the self-check in one of
# three ways and writes down that it was started. `record` is a file outside the client.
STUB_CLIENT = '''import json, os, sys, time
RECORD, ROLE, SELF_CHECK, EXIT = %(record)r, %(role)r, %(self_check)r, %(exit)d


def note(what):
    with open(RECORD, "a", encoding="utf-8") as log:
        log.write(json.dumps(what) + "\\n")


if "--self-check" in sys.argv[1:]:
    if SELF_CHECK == "hang":
        note({"hung": os.getpid()})
        time.sleep(600)
    raise SystemExit(0 if SELF_CHECK == "ok" else 1)
note({"started": ROLE, "arguments": sys.argv[1:]})
raise SystemExit(EXIT)
'''

# A run of the client as far as the lock is concerned: it takes the lock the way every run
# does, says that it has it, and installs a release (when given one) before it lets go.
RUN_THAT_HOLDS_THE_LOCK = '''import importlib.util, sys, time
from pathlib import Path

scripts, client, release, ready, seconds = sys.argv[1:6]
sys.path.insert(0, scripts)
import sync_guard

spec = importlib.util.spec_from_file_location("installing_client", scripts + "/sync_client.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
with sync_guard.sync_guard(Path(client)):
    Path(ready).write_text("held", encoding="utf-8")
    time.sleep(float(seconds))
    if release:
        entry.install_managed_runtime(Path(release), Path(client))
'''

# What a launcher starts in the end, when it is not a client of the test: it writes down that it ran.
STARTED = '''import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps({"started": "client", "arguments": sys.argv[2:]}) + "\\n")
'''


def load_guard():
    """The guard as a module of this process, to call what its launcher's copy runs."""
    spec = importlib.util.spec_from_file_location("launch_guard_under_test", SCRIPTS / "sync_launch_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files(directory: Path) -> dict[str, bytes]:
    """The files of a client in a directory; the guard next to a launcher is not one of them."""
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.py")) if path.name != GUARD}


def tool(command) -> str:
    name = str(command[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


class LauncherCase(unittest.TestCase):
    """A home with a client in it and the launcher the client writes for the OS schedule."""

    # A space and letters that are not ASCII, as in a profile named after a person.
    HOME_PREFIX = "launcher home "

    def setUp(self) -> None:
        self.sync = load_sync_client()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix=self.HOME_PREFIX)))
        self.home = self.directory / "home"
        self.client = self.home / ".agent-platform" / "client"
        self.client.mkdir(parents=True)
        self.record = self.directory / "started.jsonl"
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self.end_processes)
        # Every computer modelled here may be Windows: its system directory is where Windows has it.
        self.enterContext(patch.dict(os.environ, WINDOWS_SYSTEM_ROOT))

    # ------------------------------------------------------------------ the computer

    def arguments(self) -> SimpleNamespace:
        home = self.home
        return SimpleNamespace(
            control_plane_url=SERVICE_URL, release_trust_file=home / "trust.json", release_cache=home / "cache",
            client_token="test-token", update_policy="automatic", schedule_layout="legacy", keychain_service=None,
            scheduler="os", codex_root=home / ".codex", claude_root=home / ".claude",
            claude_config=home / ".claude.json", bin_dir=home / ".local" / "bin",
            project_mappings=home / ".agent-platform/project-mappings.json")

    def write_launcher(self, platform: str, command: list[str], *, reinstall: list[str] | None = None,
                       lock_wait: float | None = None, self_check_limit: float | None = None) -> Path:
        """The launcher as `install_scheduled_task` writes it. The OS scheduler is not asked,
        and what the launcher starts in the end is `command`."""
        sync, started = self.sync, subprocess.run

        def run(arguments, *positional, **options):
            if tool(arguments) in ("launchctl", "schtasks"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            # Sealing an invented token with the DPAPI of whoever runs the tests, on Windows.
            return started(arguments, *positional, **options)

        # A name is replaced where it is read: in the file of the function that reads it.
        job, guard = sync.install_scheduled_task.__globals__, sync.launch_guard_arguments.__globals__
        with ExitStack() as stack:
            for item in (patch.dict(job, {"client_platform": lambda: platform,
                                          "_scheduled_command": lambda *_: command,
                                          "reinstall_command": lambda *_: reinstall}),
                         patch.object(sync.Path, "home", return_value=self.home),
                         patch.object(sync.subprocess, "run", run)):
                stack.enter_context(item)
            if platform == "macos":
                stack.enter_context(patch.object(sync.os, "getuid", return_value=501, create=True))
            if lock_wait is not None:
                stack.enter_context(patch.dict(guard, {"LAUNCHER_LOCK_WAIT_SECONDS": lock_wait}))
            if self_check_limit is not None:
                stack.enter_context(patch.dict(guard, {"CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS": self_check_limit}))
            sync.install_scheduled_task(self.client / ENTRY, self.arguments(), CLIENT_ID, "Test computer",
                                        ["codex"], self.client)
        return self.client / ("sync-client.sh" if platform == "macos" else "sync-client.ps1")

    def stub(self, directory: Path, role: str, check: str = "ok", exit_code: int = 0) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ENTRY).write_text(STUB_CLIENT % {"record": str(self.record), "role": role, "self_check": check,
                                                      "exit": exit_code}, encoding="utf-8")

    def stub_command(self) -> list[str]:
        return [sys.executable, "-E", "-s", str(self.client / ENTRY), "--run", "a name with spaces"]

    def recorder_command(self) -> list[str]:
        recorder = self.directory / "started.py"
        recorder.write_text(STARTED, encoding="utf-8")
        return [sys.executable, "-E", "-s", str(recorder), str(self.record)]

    def real_client(self) -> dict[str, bytes]:
        """The client of this tree, installed twice so that it has a previous copy."""
        self.release = release_tree(self.directory / "release")
        self.sync.install_managed_runtime(self.release, self.client)
        self.sync.install_managed_runtime(self.release, self.client)
        self.assertEqual(self_check(self.client / "previous").returncode, 0)
        return files(self.client)

    def damage(self, name: str = "sync_rules.py") -> None:
        (self.client / name).write_text("raise RuntimeError('damaged on this computer')\n", encoding="utf-8")
        self.assertNotEqual(self_check(self.client).returncode, 0)

    def run_holding_the_lock(self, seconds: float, release: Path | None = None) -> subprocess.Popen:
        script, ready = self.directory / "run.py", self.directory / f"lock-held-{len(self.processes)}"
        script.write_text(RUN_THAT_HOLDS_THE_LOCK, encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-E", "-s", str(script), str(SCRIPTS), str(self.client), str(release or ""), str(ready),
             str(seconds)], env=sandboxed_environment(self.home), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 60
        while not ready.exists():
            self.assertIsNone(process.poll(), "the run that was to hold the lock ended at once")
            self.assertLess(time.monotonic(), deadline, "the run that was to hold the lock never took it")
            time.sleep(0.02)
        return process

    # ------------------------------------------------------------------ launchers

    def start(self, command: list[str]) -> subprocess.Popen:
        options = {} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, env=sandboxed_environment(self.home), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **options)
        self.processes.append(process)
        return process

    def finish(self, process: subprocess.Popen, seconds: float = 120) -> tuple[int, str]:
        try:
            _, errors = process.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            self.end(process)
            self.fail(f"a launcher was still running after {seconds} seconds")
        return process.returncode, errors.decode("utf-8", "replace")

    def end(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            if os.name == "nt":
                process.kill()
            else:
                # The launcher and whatever it started and is waiting for.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
        try:
            process.communicate(timeout=30)
        except (subprocess.TimeoutExpired, ValueError):
            pass

    def end_processes(self) -> None:
        for process in self.processes:
            self.end(process)

    def events(self) -> list[str]:
        """What the launcher wrote down for the next report, as the file holds it."""
        try:
            lines = (self.client / "launcher-events.jsonl").read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return []
        return [json.loads(line)["event"] for line in lines if line.strip()]

    def recorded(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def started(self) -> list[str]:
        return [item["started"] for item in self.recorded() if "started" in item]


@POSIX
class OverlappingStartsTests(LauncherCase):
    def test_two_jobs_wait_for_a_run_that_is_installing_and_restore_nothing_over_it(self) -> None:
        release_client = self.real_client()
        # The client does not start, and a run that was started by hand is already installing
        # the release again when the hourly job begins, twice.
        self.damage()
        launcher = self.write_launcher("macos", self.recorder_command())
        run = self.run_holding_the_lock(1.0, self.release)

        jobs = [self.start(["/bin/sh", str(launcher)]) for _ in range(2)]
        results = [self.finish(job) for job in jobs]
        _, errors = run.communicate(timeout=120)

        self.assertEqual(run.returncode, 0, errors.decode("utf-8", "replace"))
        self.assertEqual([code for code, _ in results], [0, 0], results)
        # Neither job looked at the client while it was being replaced, so neither found a
        # client that did not start and neither put the previous one back.
        self.assertEqual(self.events(), [])
        self.assertEqual(files(self.client), release_client)
        self.assertEqual(self_check(self.client).returncode, 0)
        # Both then started the client; which of the two synchronizes is the lock's to decide.
        self.assertEqual(self.started(), ["client", "client"])

    def test_a_job_leaves_everything_alone_when_a_run_holds_the_lock_longer_than_it_waits(self) -> None:
        self.real_client()
        self.damage()
        before = files(self.client)
        launcher = self.write_launcher("macos", self.recorder_command(), lock_wait=1)
        self.run_holding_the_lock(6.0)
        started = time.monotonic()

        code, errors = self.finish(self.start(["/bin/sh", str(launcher)]))

        self.assertEqual(code, 0, errors)
        self.assertLess(time.monotonic() - started, 5, "the job waited longer than it was told to")
        # It touched nothing and started nothing: the run that holds the lock is the synchronization.
        self.assertEqual(files(self.client), before)
        self.assertEqual(self.started(), [])
        # And it said so.
        self.assertEqual(self.events(), ["guard_busy"])
        self.assertIn("another synchronization is running", errors)

    def test_two_jobs_that_find_a_damaged_client_together_restore_it_once(self) -> None:
        self.real_client()
        previous = files(self.client / "previous")
        self.damage()
        launcher = self.write_launcher("macos", self.recorder_command())

        jobs = [self.start(["/bin/sh", str(launcher)]) for _ in range(2)]
        results = [self.finish(job) for job in jobs]

        self.assertEqual([code for code, _ in results], [0, 0], results)
        self.assertEqual(files(self.client), previous)
        self.assertEqual(self_check(self.client).returncode, 0)
        # One of them restored; the other waited for it and found a client that starts.
        self.assertEqual(self.events(), ["self_check_failed", "runtime_restored"])
        self.assertEqual(self.started(), ["client", "client"])
        self.assertEqual([path.name for path in self.client.iterdir() if path.name.startswith(".")], [])


@POSIX
class HangingSelfCheckTests(LauncherCase):
    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_a_self_check_that_does_not_return_is_ended_and_the_previous_client_is_put_back(self) -> None:
        self.stub(self.client, "installed", check="hang")
        self.stub(self.client / "previous", "previous")
        launcher = self.write_launcher("macos", self.stub_command(), self_check_limit=2)
        started = time.monotonic()

        code, errors = self.finish(self.start(["/bin/sh", str(launcher)]), seconds=40)

        self.assertEqual(code, 0, errors)
        self.assertLess(time.monotonic() - started, 30)
        hung = [item["hung"] for item in self.recorded() if "hung" in item]
        self.assertEqual(len(hung), 1)
        self.assertFalse(self.alive(hung[0]), "the self-check that did not return is still running")
        # The job says what happened, and went on as after any failed self-check.
        self.assertEqual(self.events(), ["self_check_timed_out", "runtime_restored"])
        self.assertIn("did not return", errors)
        self.assertEqual(self.started(), ["previous"])

        # The next hourly job finds a client that starts and has nothing to say.
        code, errors = self.finish(self.start(["/bin/sh", str(launcher)]), seconds=40)
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.events(), ["self_check_timed_out", "runtime_restored"])
        self.assertEqual(self.started(), ["previous", "previous"])

    def test_a_previous_client_that_does_not_return_either_is_ended_too(self) -> None:
        self.stub(self.client, "installed", check="hang", exit_code=3)
        self.stub(self.client / "previous", "previous", check="hang")
        launcher = self.write_launcher("macos", self.stub_command(), self_check_limit=1)

        code, errors = self.finish(self.start(["/bin/sh", str(launcher)]), seconds=40)

        hung = [item["hung"] for item in self.recorded() if "hung" in item]
        self.assertEqual(len(hung), 2)
        self.assertEqual([self.alive(pid) for pid in hung], [False, False])
        self.assertEqual(self.events(), ["self_check_timed_out", "self_check_timed_out", "restore_unavailable"])
        # Nothing to restore and no installer to start: the client is started as it is, as before.
        self.assertEqual((code, self.started()), (3, ["installed"]), errors)


@POSIX
class GuardOutcomeTests(LauncherCase):
    """Each thing the guard can tell its launcher, and what the launcher then does."""

    def launcher(self, **options) -> Path:
        return self.write_launcher("macos", self.stub_command(), **options)

    def run_launcher(self, launcher: Path) -> tuple[int, str]:
        return self.finish(self.start(["/bin/sh", str(launcher)]), seconds=60)

    def test_a_client_that_starts_is_started_and_nothing_is_written_down(self) -> None:
        self.stub(self.client, "installed", exit_code=7)
        self.stub(self.client / "previous", "previous")

        code, errors = self.run_launcher(self.launcher())

        self.assertEqual((code, errors), (7, ""))
        self.assertEqual((self.events(), self.started()), ([], ["installed"]))
        self.assertEqual(self.recorded()[0]["arguments"], ["--run", "a name with spaces"])

    def test_the_guard_is_a_file_of_the_launcher_and_needs_nothing_of_the_client(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        launcher = self.launcher()

        # Next to the stub there is no file of the client at all: the guard is the copy the
        # launcher was written with, byte for byte the client's own file.
        self.assertEqual((self.client / GUARD).read_bytes(), (SCRIPTS / "sync_launch_guard.py").read_bytes())
        self.assertEqual(sorted(path.name for path in self.client.glob("*.py")), sorted([ENTRY, GUARD]))
        self.assertIn(f"/{GUARD} --lock-wait ", launcher.read_text(encoding="utf-8"))
        code, errors = self.run_launcher(launcher)

        self.assertEqual(code, 0, errors)
        self.assertEqual((self.events(), self.started()), (["self_check_failed", "runtime_restored"], ["previous"]))
        # The copy is not a file of a client: the removal of files no client declares, which
        # takes the stub away, leaves it where it is, and a restore copies previous/*.py only.
        self.assertEqual(self.sync.remove_undeclared_client_files(self.client, set()), [ENTRY])
        self.assertTrue((self.client / GUARD).is_file())
        self.assertFalse((self.client / "previous" / GUARD).exists())

    def test_with_nothing_to_restore_the_installer_is_started_instead_of_the_client(self) -> None:
        self.stub(self.client, "installed", check="fail")
        installer = self.directory / "installer.py"
        installer.write_text(STARTED.replace('"client"', '"installer"'), encoding="utf-8")
        reinstall = [sys.executable, "-E", "-s", str(installer), str(self.record), "--client-id", CLIENT_ID]

        code, errors = self.run_launcher(self.launcher(reinstall=reinstall))

        self.assertEqual(code, 0, errors)
        self.assertEqual((self.events(), self.started()), (["self_check_failed", "restore_unavailable"], ["installer"]))

    def test_with_nothing_to_restore_and_no_installer_the_client_is_started_as_before(self) -> None:
        self.stub(self.client, "installed", check="fail", exit_code=5)

        code, _ = self.run_launcher(self.launcher())

        self.assertEqual(code, 5)
        self.assertEqual((self.events(), self.started()), (["self_check_failed", "restore_unavailable"], ["installed"]))

    def test_a_guard_that_cannot_run_is_written_down_and_the_client_is_started_unchecked(self) -> None:
        self.stub(self.client, "installed", exit_code=0)
        launcher = self.launcher()
        for how in ("missing", "broken"):
            with self.subTest(guard=how):
                if how == "missing":
                    (self.client / GUARD).unlink()
                else:
                    (self.client / GUARD).write_text("raise RuntimeError('not the guard')\n", encoding="utf-8")
                (self.client / "launcher-events.jsonl").unlink(missing_ok=True)

                code, errors = self.run_launcher(launcher)

                # The run that follows reports the event, and writes the guard again.
                self.assertEqual(code, 0, errors)
                self.assertEqual(self.events(), ["guard_failed"])
                self.assertIn("the launch guard did not run", errors)
                self.assertFalse(self.sync.launch_guard_is_current(self.client))
        self.assertEqual(self.started(), ["installed", "installed"])

    def test_a_restore_that_cannot_be_finished_starts_nothing(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        # A file of the previous client that cannot be copied.
        (self.client / "previous" / "aa_cannot_be_copied.py").mkdir()

        code, errors = self.run_launcher(self.launcher())

        self.assertEqual(code, 74, errors)
        self.assertIn("could not be put back", errors)
        self.assertEqual((self.events(), self.started()), (["self_check_failed"], []))


class GuardTests(LauncherCase):
    """The guard itself, called as its launcher's copy calls it: with a file another program
    holds (see test_client_busy_files.py for how that is said here) and with the lock."""

    def setUp(self) -> None:
        super().setUp()
        self.guard = load_guard()

    def check(self, **options) -> int:
        return self.guard.guard_client(self.client, sys.executable, lock_wait_seconds=options.get("lock_wait", 0.3),
                                       self_check_limit_seconds=options.get("limit", 60))

    def test_a_client_file_held_for_good_stops_the_restore_before_the_entry_point(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        (self.client / "sync_part.py").write_text("installed\n", encoding="utf-8")
        (self.client / "previous" / "sync_part.py").write_text("previous\n", encoding="utf-8")
        failed, kept = (self.client / ENTRY).read_bytes(), files(self.client / "previous")

        with Held(lambda source, destination: destination == self.client / ENTRY) as held:
            answer = self.check()

        self.assertEqual(answer, self.guard.GUARD_FILES_BUSY)
        self.assertEqual(held.refused, self.guard.BUSY_ATTEMPTS)
        self.assertEqual(self.events(), ["self_check_failed", "restore_busy"])
        # The entry point that failed is still here, so the next start restores again; the
        # copy to restore from is untouched, and no half-written file is left behind.
        self.assertEqual((self.client / ENTRY).read_bytes(), failed)
        self.assertEqual(files(self.client / "previous"), kept)
        self.assertEqual([path.name for path in self.client.iterdir() if path.name.endswith(".tmp")], [])

        # The other program let go: the next start finishes the restore.
        self.assertEqual(self.check(), self.guard.GUARD_START_CLIENT)
        self.assertEqual(files(self.client), kept)
        self.assertEqual(self.events(), ["self_check_failed", "restore_busy", "self_check_failed", "runtime_restored"])

    def test_a_file_held_for_a_moment_does_not_stop_the_restore(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")

        with Held(lambda source, destination: destination == self.client / ENTRY, times=2) as held:
            answer = self.check()

        self.assertEqual((answer, held.refused), (self.guard.GUARD_START_CLIENT, 2))
        self.assertEqual(self.events(), ["self_check_failed", "runtime_restored"])

    def test_a_previous_copy_that_was_moved_aside_when_a_run_stopped_is_the_one_restored(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / self.guard.RETIRED_CLIENT_RUNTIME, "previous")

        self.assertEqual(self.check(), self.guard.GUARD_START_CLIENT)

        self.assertEqual(self.events(), ["self_check_failed", "runtime_restored"])
        self.assertTrue((self.client / "previous" / ENTRY).is_file())
        self.assertFalse((self.client / self.guard.RETIRED_CLIENT_RUNTIME).exists())
        self.assertEqual(files(self.client), files(self.client / "previous"))

    def test_the_guard_and_a_run_exclude_each_other_through_the_one_lock(self) -> None:
        spec = importlib.util.spec_from_file_location("sync_guard_of_a_run", SCRIPTS / "sync_guard.py")
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        failed = (self.client / ENTRY).read_bytes()

        with run.sync_guard(self.client):
            started = time.monotonic()
            self.assertEqual(self.check(lock_wait=0.5), self.guard.GUARD_RUN_IN_PROGRESS)
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.5)
        self.assertLess(waited, 5)
        self.assertEqual(self.events(), ["guard_busy"])
        self.assertEqual((self.client / ENTRY).read_bytes(), failed)

        with self.guard.run_lock(self.client, 0):
            with self.assertRaises(run.ClientBusy):
                with run.sync_guard(self.client):
                    self.fail("a run started while the guard was looking at the client")
        # Each gives the lock back.
        with run.sync_guard(self.client):
            pass
        self.assertEqual(self.check(), self.guard.GUARD_START_CLIENT)

    def test_only_a_file_in_use_is_waited_for(self) -> None:
        guard = self.guard
        in_use = PermissionError(13, "in use")
        in_use.winerror = 32
        # The error number alone: what a Mac says for a directory that may not be written, and
        # what Windows says when a file another program holds is opened (test_client_busy_files.py).
        bare = PermissionError(13, "Permission denied")
        for windows, means_in_use in ((False, False), (True, True)):
            with self.subTest(windows=windows), patch.object(guard, "WINDOWS", windows):
                for error, expected in ((in_use, True), (OSError(16, "busy"), True), (bare, means_in_use),
                                        (FileNotFoundError(2, "gone"), False), (guard.ClientFilesBusy("x"), False),
                                        (ValueError("x"), False)):
                    self.assertEqual(guard.held_by_another_program(error), expected, repr(error))
        calls = []

        def refused():
            calls.append(1)
            raise PermissionError(13, "Permission denied")

        with patch.object(guard, "WINDOWS", False), self.assertRaises(PermissionError) as raised:
            guard.retry_busy(refused, "a file")
        self.assertNotIsInstance(raised.exception, guard.ClientFilesBusy)
        self.assertEqual(len(calls), 1)
        # The whole wait for one file is a few seconds, whatever happens.
        pauses = [guard.BUSY_FIRST_PAUSE_SECONDS * 2 ** attempt for attempt in range(guard.BUSY_ATTEMPTS - 1)]
        self.assertLess(sum(pauses), 5)
        self.assertGreater(sum(pauses), 1)

    # ------------------------------------------------- a self-check that failed over a held file

    def conditional_stub(self, directory: Path, condition: str) -> None:
        """A client whose self-check fails while `condition` (Python, with `os` at hand) holds."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ENTRY).write_text(
            "import os, sys\n"
            'if "--self-check" in sys.argv[1:]:\n'
            f"    raise SystemExit(1 if {condition} else 0)\n", encoding="utf-8")

    def as_windows(self, refusal=lambda path: 32):
        """The guard decides what an error means as on a Windows computer, and Windows answers
        `refusal(path)` when asked why a file cannot be opened: 32, another program holds it;
        5, this person may not read it."""
        stack = ExitStack()
        stack.enter_context(patch.object(self.guard, "WINDOWS", True))
        stack.enter_context(patch.object(self.guard, "windows_open_refusal", refusal))
        return stack

    def test_a_client_whose_file_is_held_is_left_alone_and_nothing_is_put_back_over_it(self) -> None:
        # What a launcher's guard meets when an antivirus has a file of the client open with no
        # sharing: the interpreter cannot read the file, so the self-check ends with a failure.
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        installed, kept = files(self.client), files(self.client / "previous")
        started = time.monotonic()

        errors = io.StringIO()
        with self.as_windows(), HeldRead(ENTRY) as held, redirect_stderr(errors):
            answer = self.check()

        # Nothing says that the client does not start, and the previous one is not put back.
        self.assertEqual(answer, self.guard.GUARD_FILES_BUSY)
        self.assertEqual(self.events(), ["restore_busy"])
        self.assertEqual((files(self.client), files(self.client / "previous")), (installed, kept))
        # It waited for the other program as long as the client waits for any held file, and
        # looked once more to name the file in the launcher's log.
        self.assertEqual(held.refused, self.guard.BUSY_ATTEMPTS + 1)
        self.assertIn(f"{self.client / ENTRY} is held by another program", errors.getvalue())
        self.assertLess(time.monotonic() - started, 60)

    def test_a_client_whose_file_is_let_go_after_a_moment_is_started(self) -> None:
        held_flag = self.directory / "held"
        held_flag.write_text("", encoding="utf-8")
        self.conditional_stub(self.client, f"os.path.exists({str(held_flag)!r})")
        self.stub(self.client / "previous", "previous")
        installed = files(self.client)

        class LetGo(HeldRead):
            def call(self, path: Path) -> bytes:
                if path.name == self.name and self.refused == self.times:
                    held_flag.unlink(missing_ok=True)
                return super().call(path)

        with self.as_windows(), LetGo(ENTRY, times=2) as held:
            answer = self.check()

        self.assertEqual((answer, held.refused), (self.guard.GUARD_START_CLIENT, 2))
        self.assertEqual(self.events(), [])
        self.assertEqual(files(self.client), installed)

    def test_a_check_that_failed_while_a_file_was_held_for_an_instant_is_asked_again(self) -> None:
        # The other program let go between the self-check and the guard's look at the files:
        # nothing is held any more, and the client starts. On Windows one failed check is
        # therefore not yet a client that does not start.
        count = self.directory / "checks"
        self.conditional_stub(self.client, f"not os.path.exists({str(count)!r}) and not open({str(count)!r}, 'w').close()")
        self.stub(self.client / "previous", "previous")
        installed = files(self.client)

        with self.as_windows():
            answer = self.check()

        self.assertEqual((answer, self.events()), (self.guard.GUARD_START_CLIENT, []))
        self.assertEqual(files(self.client), installed)

        # Elsewhere no program can hold a file so that it cannot be read: the first answer stands.
        count.unlink()
        with patch.object(self.guard, "WINDOWS", False):
            answer = self.check()
        self.assertEqual((answer, self.events()), (self.guard.GUARD_START_CLIENT, ["self_check_failed", "runtime_restored"]))

    def test_a_previous_client_whose_file_is_held_is_not_taken_for_one_that_cannot_be_restored(self) -> None:
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous", check="fail")
        (self.client / "previous" / "sync_held_part.py").write_text("previous\n", encoding="utf-8")
        installed, kept = files(self.client), files(self.client / "previous")

        with self.as_windows(), HeldRead("sync_held_part.py"):
            answer = self.check()

        # Not "nothing to restore", which starts the installer of the runtime or the damaged
        # client: the copy is there and may well start once the other program lets go.
        self.assertEqual(answer, self.guard.GUARD_FILES_BUSY)
        self.assertEqual(self.events(), ["self_check_failed", "restore_busy"])
        self.assertEqual((files(self.client), files(self.client / "previous")), (installed, kept))

    def test_a_file_this_person_may_not_read_is_a_client_that_does_not_start(self) -> None:
        # Windows says the same for it when it is read, and something else when asked: such a
        # file is never let go, so the previous client is put back as it always was.
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        kept = files(self.client / "previous")

        with self.as_windows(refusal=lambda path: 5), HeldRead(ENTRY) as held:
            answer = self.check()

        self.assertEqual(answer, self.guard.GUARD_START_CLIENT)
        self.assertEqual(self.events(), ["self_check_failed", "runtime_restored"])
        self.assertEqual(files(self.client), kept)
        # Looked at, not waited for.
        self.assertLessEqual(held.refused, 2)

    def test_a_restore_that_stops_for_another_reason_than_a_held_file_starts_nothing(self) -> None:
        # The answer 74, on every system (a launcher of a Mac is run with it above; on Windows
        # a directory in the place of a file is refused in the words of a held one).
        self.stub(self.client, "installed", check="fail")
        self.stub(self.client / "previous", "previous")
        failed = files(self.client)

        def full(source, destination):
            raise OSError(errno.ENOSPC, "No space left on device", str(destination))

        with patch.object(self.guard, "copy_into_place", full):
            answer = self.check()

        self.assertEqual(answer, self.guard.GUARD_RESTORE_FAILED)
        self.assertEqual((self.events(), files(self.client)), (["self_check_failed"], failed))

    def test_what_windows_says_when_asked_decides_a_refused_read(self) -> None:
        guard = self.guard
        refused = PermissionError(errno.EACCES, "Permission denied", str(self.client / ENTRY))
        asked = []

        def answers(code):
            def refusal(path):
                asked.append(str(path))
                return code
            return refusal

        # 32 and 33: another program. 0: it opens now, so it was held a moment ago. None:
        # Windows could not be asked, and the answer stays the one that waits.
        for code, held in ((32, True), (33, True), (0, True), (None, True), (5, False)):
            with self.subTest(windows_says=code), self.as_windows(refusal=answers(code)):
                self.assertEqual(guard.held_by_another_program(refused), held)
        self.assertEqual(set(asked), {str(self.client / ENTRY)})
        # An error that names no file has nothing to ask about.
        with self.as_windows(refusal=answers(5)):
            self.assertTrue(guard.held_by_another_program(PermissionError(errno.EACCES, "Permission denied")))
        # Nothing is asked where Windows already named the cause, or on another system.
        asked.clear()
        in_use = PermissionError(errno.EACCES, "in use", "x")
        in_use.winerror = 5
        with self.as_windows(refusal=answers(5)):
            self.assertTrue(guard.held_by_another_program(in_use))
        with patch.object(guard, "WINDOWS", False), patch.object(guard, "windows_open_refusal", answers(32)):
            self.assertFalse(guard.held_by_another_program(refused))
        self.assertEqual(asked, [])
        # On a computer that is not a Windows one there is nothing to ask.
        if os.name != "nt":
            self.assertIsNone(guard.windows_open_refusal(self.client / ENTRY))

    def test_a_refused_read_this_person_may_not_make_is_final_at_once(self) -> None:
        guard, calls = self.guard, []

        def refused():
            calls.append(1)
            raise PermissionError(errno.EACCES, "Permission denied", str(self.client / ENTRY))

        with self.as_windows(refusal=lambda path: 5), self.assertRaises(PermissionError) as raised:
            guard.retry_busy(refused, self.client / ENTRY)
        self.assertNotIsInstance(raised.exception, guard.ClientFilesBusy)
        self.assertEqual(len(calls), 1)

    def test_the_guard_names_the_client_s_files_as_the_client_does(self) -> None:
        guard, sync = self.guard, self.sync
        self.assertEqual(guard.CLIENT_ENTRY, sync.MANAGED_CLIENT_ENTRY)
        self.assertEqual(GUARD, sync.LAUNCH_GUARD_FILE)
        for name in ("PREVIOUS_CLIENT_RUNTIME", "LAUNCHER_EVENTS_FILE", "LAUNCHER_EVENTS", "LAUNCH_GUARD_FILE"):
            self.assertEqual(getattr(guard, name), getattr(sync, name), name)
        # It reads the standard library and nothing else, whatever lies next to it.
        source = (SCRIPTS / "sync_launch_guard.py").read_text(encoding="utf-8")
        imported = {line.split()[1].split(".")[0] for line in source.splitlines()
                    if line.lstrip().startswith(("import ", "from "))}
        self.assertEqual(imported - set(sys.stdlib_module_names), set())


class LauncherTextTests(LauncherCase):
    """The text of both launchers, to the letter. The PowerShell one is run on Windows only."""

    def test_the_launcher_of_a_mac(self) -> None:
        if os.name == "nt":
            self.skipTest("the launcher of a Mac is written with POSIX file modes")
        sync, quote = self.sync, shlex.quote
        self.stub(self.client, "installed")
        command = self.stub_command()
        reinstall = [sys.executable, "-E", "-s", "/runtime/bootstrap/companion_install.py", "--client-id", CLIENT_ID]
        python, client = quote(sys.executable), quote(str(self.client))
        events = quote(str(self.client / "launcher-events.jsonl"))
        token = f"IFS= read -r AGENT_PLATFORM_CLIENT_TOKEN < {quote(str(self.client / 'credential'))}\n"
        guard = ("guard=0\n"
                 f"{python} -E -s {client}/sync-client-guard.py --lock-wait 30 --self-check-limit 60 || guard=$?\n"
                 'case "$guard" in\n'
                 "  0) ;;\n"
                 "  10) NOTHING TO RESTORE ;;\n"
                 "  11) exit 0 ;;\n"
                 '  74|75) exit "$guard" ;;\n'
                 "  *)\n"
                 "    printf '{\"event\":\"%s\",\"at\":\"%s\"}\\n' guard_failed "
                 f"\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> {events}\n"
                 "    echo 'agent-platform: the launch guard did not run; starting the sync client unchecked' >&2\n"
                 "    ;;\n"
                 "esac\n")
        started = f"exec {' '.join(quote(part) for part in command)}\n"

        launcher = self.write_launcher("macos", command)
        self.assertEqual(launcher.read_text(encoding="utf-8"),
                         "#!/bin/sh\nset -eu\n" + guard.replace("NOTHING TO RESTORE", ":") + token
                         + "export AGENT_PLATFORM_CLIENT_TOKEN\n" + started)

        launcher = self.write_launcher("macos", command, reinstall=reinstall)
        self.assertEqual(launcher.read_text(encoding="utf-8"),
                         "#!/bin/sh\nset -eu\nreinstall=\n" + guard.replace("NOTHING TO RESTORE", "reinstall=1") + token
                         + "export AGENT_PLATFORM_CLIENT_TOKEN\n"
                         'if [ -n "$reinstall" ]; then\n'
                         "  echo 'agent-platform: no sync client can start; installing the approved release again' >&2\n"
                         f"  exec {' '.join(quote(part) for part in reinstall)}\n"
                         "fi\n" + started)
        # What the guard is told are this client's own limits.
        self.assertEqual((sync.LAUNCHER_LOCK_WAIT_SECONDS, sync.CLIENT_RUNTIME_PROBE_TIMEOUT_SECONDS), (30, 60))

    def test_the_launcher_of_windows(self) -> None:
        sync = self.sync
        self.assertEqual(sync.runtime_restore_powershell(), (
            "$reinstall = $false\n"
            "$guard = Join-Path $clientRoot 'sync-client-guard.py'\n"
            "$launcherEvents = Join-Path $clientRoot 'launcher-events.jsonl'\n"
            "function Write-AgentPlatformEvent([string]$name) {\n"
            "  $at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)\n"
            "  Add-Content -LiteralPath $launcherEvents -Encoding UTF8 "
            "-Value ('{\"event\":\"' + $name + '\",\"at\":\"' + $at + '\"}')\n"
            "}\n"
            "function Invoke-AgentPlatformGuard {\n"
            "  $ErrorActionPreference = 'Continue'\n"
            "  $global:LASTEXITCODE = 70\n"
            "  & $python -E -s $guard --lock-wait 30 --self-check-limit 60 *> $null\n"
            "  return $LASTEXITCODE\n"
            "}\n"
            "$guardExit = Invoke-AgentPlatformGuard\n"
            "if ($guardExit -eq 11) { exit 0 }\n"
            "if (($guardExit -eq 74) -or ($guardExit -eq 75)) { exit $guardExit }\n"
            "if ($guardExit -eq 10) {\n"
            "  $reinstall = ($launch.reinstallCommandLine -is [string]) -and "
            "(-not [string]::IsNullOrWhiteSpace($launch.reinstallCommandLine))\n"
            "} elseif ($guardExit -ne 0) {\n"
            "  Write-AgentPlatformEvent 'guard_failed'\n"
            "  [Console]::Error.WriteLine('agent-platform: the launch guard did not run; "
            "starting the sync client unchecked')\n"
            "}\n"))
        # The numbers in it are the guard's own, so the two cannot drift apart.
        guard = load_guard()
        self.assertEqual((guard.GUARD_START_CLIENT, guard.GUARD_NOTHING_TO_RESTORE, guard.GUARD_RUN_IN_PROGRESS,
                          guard.GUARD_RESTORE_FAILED, guard.GUARD_FILES_BUSY), (0, 10, 11, 74, 75))

    def test_the_script_windows_runs_holds_the_guard_between_its_arguments_and_the_credential(self) -> None:
        self.stub(self.client, "installed")
        with patch.dict(self.sync.install_scheduled_task.__globals__, {"protect_windows_token": lambda value: "sealed"}):
            script = self.write_launcher("windows", self.stub_command()).read_text(encoding="utf-8-sig")
        preamble = self.sync.runtime_restore_powershell()
        self.assertEqual(script.count(preamble), 1)
        self.assertLess(script.index("$python = $launch.python\n"), script.index(preamble))
        self.assertLess(script.index(preamble), script.index("ConvertTo-SecureString"))
        # Nothing of the earlier check and restore is left in the script.
        for gone in ("Test-AgentPlatformClient", "Copy-Item", "--self-check *", "$previousRoot"):
            self.assertNotIn(gone, script)
        self.assertEqual((self.client / GUARD).read_bytes(), (SCRIPTS / "sync_launch_guard.py").read_bytes())

    def test_a_launcher_whose_guard_is_not_this_client_s_is_written_again(self) -> None:
        sync = self.sync
        self.assertFalse(sync.launch_guard_is_current(self.client))
        sync.write_launch_guard(self.client)
        self.assertTrue(sync.launch_guard_is_current(self.client))
        (self.client / GUARD).write_bytes((self.client / GUARD).read_bytes() + b"\n")
        self.assertFalse(sync.launch_guard_is_current(self.client))
        # Whatever else the receipt of the job says, a job without its guard is not current.
        current = inspect.getsource(sync.scheduled_task_is_current)
        self.assertIn("    if not launch_guard_is_current(client_root or Path.home() / \".agent-platform\" / \"client\"):\n"
                      "        return False\n", current)
        self.assertEqual((sync.MACOS_LAUNCHER_REVISION, sync.WINDOWS_LAUNCHER_REVISION), (6, 9))

    def test_a_computer_whose_schedule_is_not_the_os_s_keeps_no_guard(self) -> None:
        sync = self.sync
        sync.write_launch_guard(self.client)
        (self.client / "sync-client.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        sync.remove_launch_guard_without_launcher(self.client)
        self.assertTrue((self.client / GUARD).is_file())
        (self.client / "sync-client.sh").unlink()
        sync.remove_launch_guard_without_launcher(self.client)
        self.assertFalse((self.client / GUARD).exists())


class WindowsScheduleTests(LauncherCase):
    """What a run does about the hourly task of a Windows computer: nothing, unless something
    the task is made of has changed.

    Every run asks (`ensure_scheduled_task`). Writing the task again registers it with the
    scheduler, writes the launcher files and seals the credential with DPAPI once more, so a
    run that finds everything as it left it must write nothing. The scheduler is a model here
    (a task is what `/Create` was given, and `/Query /XML` answers with it as Windows does);
    the platform is named as Windows where the schedule reads it, on whatever system runs this."""

    LAUNCHER_FILES = ("sync-client.ps1", "sync-client.vbs", "sync-client.arguments.json", "credential.dpapi")

    def setUp(self) -> None:
        super().setUp()
        self.tasks: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.sealed = 0
        self.answer_encoding: str | None = None
        self.stub(self.client, "installed")

    def schtasks(self, command, *positional, **options):
        self.assertEqual(tool(command), "schtasks", command)
        self.calls.append([str(word) for word in command[1:]])
        name = command[command.index("/TN") + 1]
        if command[1] == "/Create":
            self.tasks[name] = command[command.index("/TR") + 1]
            return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")
        if name not in self.tasks:
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")
        program, _, arguments = self.tasks[name].partition(" ")
        answer = ('<?xml version="1.0" encoding="UTF-16"?>\r\n<Task version="1.2"><Actions Context="Author">\r\n'
                  f"<Exec><Command>{program}</Command><Arguments>{arguments}</Arguments></Exec>\r\n</Actions></Task>\r\n")
        if self.answer_encoding:
            # What the client would read if the answer came in the encoding its first line names.
            answer = answer.encode(self.answer_encoding).decode("utf-8", errors="replace")
        return SimpleNamespace(returncode=0, stdout=answer, stderr="")

    def seal(self, value: str) -> str:
        self.sealed += 1
        return f"sealed {self.sealed} {value}"

    def run_once(self, **changed) -> dict:
        """What one run of the client does about the schedule; `changed` are names of the
        schedule's own file as another client would have them."""
        sync = self.sync
        self.calls.clear()
        with ExitStack() as stack:
            for function in (sync.ensure_scheduled_task, sync.scheduled_task_fingerprint, sync.install_scheduled_task):
                stack.enter_context(patch.dict(function.__globals__, {
                    "client_platform": lambda: "windows", "protect_windows_token": self.seal, **changed}))
            stack.enter_context(patch.object(sync.Path, "home", return_value=self.home))
            stack.enter_context(patch.object(sync.subprocess, "run", self.schtasks))
            return sync.ensure_scheduled_task(self.client / ENTRY, self.arguments(), CLIENT_ID, "Test computer", ["codex"])

    def written(self) -> dict[str, tuple[int, bytes]]:
        """When each file of the task was written, and what it holds."""
        names = (*self.LAUNCHER_FILES, GUARD, "scheduled-task.json")
        return {name: ((self.client / name).stat().st_mtime_ns, (self.client / name).read_bytes()) for name in names}

    def created(self) -> int:
        return sum(1 for call in self.calls if call[0] == "/Create")

    def test_a_run_that_finds_the_task_as_it_left_it_writes_nothing(self) -> None:
        report = self.run_once()
        self.assertEqual((report["status"], report["launcher"], self.created(), self.sealed), ("installed", "wscript.exe", 1, 1))
        first = self.written()

        for _ in range(3):
            report = self.run_once()
            self.assertEqual((report["status"], report["launcher"]), ("installed", "wscript.exe"))
            # Asked, and not registered again; no file written, no credential sealed.
            self.assertEqual((self.created(), self.sealed), (0, 1))
            self.assertEqual({call[0] for call in self.calls}, {"/Query"})
            self.assertEqual(self.written(), first)

    def test_what_the_task_is_made_of_decides_when_it_is_written_again(self) -> None:
        sync = self.sync
        for what, change in (
                ("the launcher revision", lambda: {"WINDOWS_LAUNCHER_REVISION": sync.WINDOWS_LAUNCHER_REVISION + 1}),
                ("the guard next to the launcher", lambda: (self.client / GUARD).write_bytes(b"# another client's\n") and {}),
                ("the task is gone from the scheduler", lambda: self.tasks.clear() or {}),
                ("the task starts something else", lambda: self.tasks.update(
                    {name: "powershell.exe -File other.ps1" for name in self.tasks}) or {}),
                ("the receipt is gone", lambda: (self.client / "scheduled-task.json").unlink() or {}),
                *((f"{name} is gone", lambda name=name: (self.client / name).unlink() or {}) for name in self.LAUNCHER_FILES)):
            with self.subTest(changed=what):
                self.run_once()
                before, sealed = self.written(), self.sealed
                self.assertEqual(self.run_once() and self.created(), 0, "a run that changes nothing")
                time.sleep(0.02)

                changed = change()
                report = self.run_once(**changed)

                self.assertEqual((report["status"], self.created(), self.sealed), ("installed", 1, sealed + 1))
                after = self.written()
                for name in before:
                    self.assertGreater(after[name][0], before[name][0], name)
                self.assertEqual((self.client / GUARD).read_bytes(), (SCRIPTS / "sync_launch_guard.py").read_bytes())
                # And the run after it has nothing to do again.
                self.assertEqual(self.run_once(**changed) and self.created(), 0)
                self.assertEqual(self.written(), after)

    def test_the_scheduler_s_answer_is_read_in_whichever_encoding_it_comes(self) -> None:
        # The answer names UTF-16 in its first line and has been seen in the console's code
        # page. Read as the one when it is the other, the task would never be found as it was
        # left and every run would write it again.
        self.run_once()
        first = self.written()
        for encoding in (None, "utf-16", "utf-16-le", "cp866", "cp1251"):
            with self.subTest(encoding=encoding):
                self.answer_encoding = encoding
                self.assertEqual(self.run_once()["status"], "installed")
                self.assertEqual((self.created(), self.written()), (0, first))

    def test_a_task_without_a_service_has_one_file(self) -> None:
        # No address of the service: no credential and no PowerShell script, the task starts
        # the client itself. Only the file that task starts is looked for.
        arguments = self.arguments()
        arguments.control_plane_url = ""
        sync = self.sync

        def run_once() -> dict:
            self.calls.clear()
            with ExitStack() as stack:
                for function in (sync.ensure_scheduled_task, sync.scheduled_task_fingerprint, sync.install_scheduled_task):
                    stack.enter_context(patch.dict(function.__globals__, {"client_platform": lambda: "windows"}))
                stack.enter_context(patch.object(sync.Path, "home", return_value=self.home))
                stack.enter_context(patch.object(sync.subprocess, "run", self.schtasks))
                return sync.ensure_scheduled_task(self.client / ENTRY, arguments, CLIENT_ID, "Test computer", ["codex"])

        self.assertEqual((run_once()["status"], self.created()), ("installed", 1))
        self.assertFalse((self.client / "sync-client.ps1").exists())
        self.assertEqual((run_once()["status"], self.created()), ("installed", 0))
        (self.client / "sync-client.vbs").unlink()
        self.assertEqual((run_once()["status"], self.created()), ("installed", 1))


def powershells() -> list[tuple[str, str]]:
    """(name, program) of every PowerShell on this computer: Windows PowerShell 5.1 and PowerShell 7."""
    found = []
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if windows.is_file():
        found.append(("Windows PowerShell 5.1", str(windows)))
    if shutil.which("pwsh"):
        found.append(("PowerShell 7", shutil.which("pwsh")))
    return found


@unittest.skipUnless(os.name == "nt", "PowerShell runs the launcher of a Windows computer; here its text is held "
                                      "(LauncherTextTests) and the guard it starts is run directly (GuardTests)")
class WindowsLauncherRunTests(LauncherCase):
    """The script Windows starts every hour, run by each PowerShell the computer has, in a home
    with a space and Cyrillic letters. Nothing is registered with the scheduler and no real
    credential is read: the token sealed for the script is an invented one."""

    HOME_PREFIX = "профиль с пробелом "

    def launch(self, shell: str, script: Path) -> tuple[int, str]:
        return self.finish(self.start([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                       "-File", str(script)]), seconds=180)

    def case(self, name: str):
        """A fresh client directory for one case of one shell."""
        shutil.rmtree(self.client, ignore_errors=True)
        self.client.mkdir(parents=True)
        self.record.unlink(missing_ok=True)
        return self.subTest(case=name)

    def test_every_case_of_the_launcher_on_every_powershell(self) -> None:
        shells = powershells()
        self.assertTrue(shells, "no PowerShell was found on this Windows computer")
        self.assertEqual(shells[0][0], "Windows PowerShell 5.1", "the scheduled task is run by Windows PowerShell")
        installer = self.directory / "installer.py"
        installer.write_text(STARTED.replace('"client"', '"installer"'), encoding="utf-8")
        reinstall = [sys.executable, "-E", "-s", str(installer), str(self.record), "--display-name", "Компьютер с пробелом"]
        for name, shell in shells:
            with self.subTest(shell=name):
                with self.case("the client starts"):
                    self.stub(self.client, "installed", exit_code=7)
                    self.stub(self.client / "previous", "previous")
                    code, _ = self.launch(shell, self.write_launcher("windows", self.stub_command()))
                    self.assertEqual((code, self.events(), self.started()), (7, [], ["installed"]))
                    self.assertEqual(self.recorded()[0]["arguments"], ["--run", "a name with spaces"])

                with self.case("the self-check fails and there is a previous client"):
                    self.stub(self.client, "installed", check="fail")
                    self.stub(self.client / "previous", "previous", exit_code=0)
                    code, _ = self.launch(shell, self.write_launcher("windows", self.stub_command()))
                    self.assertEqual((code, self.events(), self.started()),
                                     (0, ["self_check_failed", "runtime_restored"], ["previous"]))
                    self.assertEqual((self.client / ENTRY).read_bytes(), (self.client / "previous" / ENTRY).read_bytes())

                with self.case("nothing to restore, an installer to start"):
                    self.stub(self.client, "installed", check="fail")
                    code, _ = self.launch(shell, self.write_launcher("windows", self.stub_command(), reinstall=reinstall))
                    self.assertEqual((code, self.events(), self.started()),
                                     (0, ["self_check_failed", "restore_unavailable"], ["installer"]))
                    self.assertEqual(self.recorded()[0]["arguments"], ["--display-name", "Компьютер с пробелом"])

                with self.case("nothing to restore, no installer"):
                    self.stub(self.client, "installed", check="fail", exit_code=5)
                    code, _ = self.launch(shell, self.write_launcher("windows", self.stub_command()))
                    self.assertEqual((code, self.events(), self.started()),
                                     (5, ["self_check_failed", "restore_unavailable"], ["installed"]))

                with self.case("the self-check does not return"):
                    self.stub(self.client, "installed", check="hang")
                    self.stub(self.client / "previous", "previous")
                    script = self.write_launcher("windows", self.stub_command(), self_check_limit=3)
                    started = time.monotonic()
                    code, _ = self.launch(shell, script)
                    self.assertLess(time.monotonic() - started, 60)
                    self.assertEqual((code, self.events(), self.started()),
                                     (0, ["self_check_timed_out", "runtime_restored"], ["previous"]))

                with self.case("the entry point of a client that starts is held by another program"):
                    # The interpreter cannot read the file, so the self-check fails as that of
                    # a damaged client does. Nothing is wrong with this one.
                    self.stub(self.client, "installed", exit_code=7)
                    self.stub(self.client / "previous", "previous")
                    whole = (self.client / ENTRY).read_bytes()
                    script = self.write_launcher("windows", self.stub_command())
                    handle = hold_without_sharing(self.client / ENTRY)
                    try:
                        code, _ = self.launch(shell, script)
                    finally:
                        let_go(handle)
                    # Nothing is started, nothing says that the client does not start, and the
                    # previous client is not put back over it.
                    self.assertEqual((code, self.events(), self.started()), (75, ["restore_busy"], []))
                    self.assertEqual((self.client / ENTRY).read_bytes(), whole)
                    # The other program let go: the next hourly start starts the client.
                    code, _ = self.launch(shell, script)
                    self.assertEqual((code, self.events(), self.started()), (7, ["restore_busy"], ["installed"]))

                with self.case("a file the client imports is held by another program"):
                    self.stub(self.client, "installed", exit_code=7)
                    entry = self.client / ENTRY
                    entry.write_text("import sync_held_part\n" + entry.read_text(encoding="utf-8"), encoding="utf-8")
                    (self.client / "sync_held_part.py").write_text("HELD = True\n", encoding="utf-8")
                    self.stub(self.client / "previous", "previous")
                    whole = files(self.client)
                    script = self.write_launcher("windows", self.stub_command())
                    handle = hold_without_sharing(self.client / "sync_held_part.py")
                    try:
                        code, _ = self.launch(shell, script)
                    finally:
                        let_go(handle)
                    self.assertEqual((code, self.events(), self.started()), (75, ["restore_busy"], []))
                    self.assertEqual(files(self.client), whole)
                    code, _ = self.launch(shell, script)
                    self.assertEqual((code, self.events(), self.started()), (7, ["restore_busy"], ["installed"]))

                with self.case("a file of a client that does not start is held by another program"):
                    self.stub(self.client, "installed", check="fail")
                    self.stub(self.client / "previous", "previous")
                    failed = (self.client / ENTRY).read_bytes()
                    script = self.write_launcher("windows", self.stub_command())
                    handle = hold_without_sharing(self.client / ENTRY)
                    try:
                        code, _ = self.launch(shell, script)
                    finally:
                        let_go(handle)
                    # While it is held nothing is known about the client: its check is not
                    # written down as failed, and the restore waits for the next start.
                    self.assertEqual((code, self.events(), self.started()), (75, ["restore_busy"], []))
                    self.assertEqual((self.client / ENTRY).read_bytes(), failed)
                    # The other program let go: the next hourly start restores.
                    code, _ = self.launch(shell, script)
                    self.assertEqual((code, self.events()[1:], self.started()),
                                     (0, ["self_check_failed", "runtime_restored"], ["previous"]))

                with self.case("another run holds the lock"):
                    self.stub(self.client, "installed", check="fail")
                    self.stub(self.client / "previous", "previous")
                    failed = (self.client / ENTRY).read_bytes()
                    script = self.write_launcher("windows", self.stub_command(), lock_wait=1)
                    run = self.run_holding_the_lock(20.0)
                    code, _ = self.launch(shell, script)
                    self.end(run)
                    self.assertEqual((code, self.events(), self.started()), (0, ["guard_busy"], []))
                    self.assertEqual((self.client / ENTRY).read_bytes(), failed)

                with self.case("the guard is gone"):
                    self.stub(self.client, "installed", exit_code=0)
                    script = self.write_launcher("windows", self.stub_command())
                    (self.client / GUARD).unlink()
                    code, _ = self.launch(shell, script)
                    self.assertEqual((code, self.events(), self.started()), (0, ["guard_failed"], ["installed"]))

    # What follows was written from the cases above and from the tests of a Mac's launcher of
    # the same names (GuardOutcomeTests, HangingSelfCheckTests, OverlappingStartsTests), on a
    # computer that cannot run PowerShell. Each is a test of its own, so that one that does
    # not hold on Windows is named by itself.

    def shells(self) -> list[tuple[str, str]]:
        found = powershells()
        self.assertTrue(found, "no PowerShell was found on this Windows computer")
        return found

    def command(self, shell: str, script: Path) -> list[str]:
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]

    def real_client_again(self) -> dict[str, bytes]:
        """`real_client` for a client directory that was emptied: the release is copied once."""
        if not hasattr(self, "release"):
            self.release = release_tree(self.directory / "release")
        self.sync.install_managed_runtime(self.release, self.client)
        self.sync.install_managed_runtime(self.release, self.client)
        self.assertEqual(self_check(self.client / "previous").returncode, 0)
        return files(self.client)

    def test_a_restore_that_cannot_be_finished_starts_nothing(self) -> None:
        """The guard's answer 74. On Windows nothing a test can arrange makes the guard give it:
        a directory in the place of a file, the way a Mac is made to, is refused there in the
        words of a held file (75, above). What is Windows' own is the script's reading of the
        answer, so the guard of this test is a file that gives it."""
        for name, shell in self.shells():
            with self.subTest(shell=name), self.case("the restore did not finish"):
                self.stub(self.client, "installed", check="fail")
                self.stub(self.client / "previous", "previous")
                failed = (self.client / ENTRY).read_bytes()
                script = self.write_launcher("windows", self.stub_command())
                (self.client / GUARD).write_text("raise SystemExit(74)\n", encoding="utf-8")

                code, _ = self.launch(shell, script)

                self.assertEqual((code, self.events(), self.started()), (74, [], []))
                self.assertEqual((self.client / ENTRY).read_bytes(), failed)

    def test_a_previous_client_that_does_not_return_either_is_ended_too(self) -> None:
        for name, shell in self.shells():
            with self.subTest(shell=name), self.case("neither client returns from its self-check"):
                self.stub(self.client, "installed", check="hang", exit_code=3)
                self.stub(self.client / "previous", "previous", check="hang")
                script = self.write_launcher("windows", self.stub_command(), self_check_limit=3)
                started = time.monotonic()

                code, _ = self.launch(shell, script)

                self.assertLess(time.monotonic() - started, 90)
                hung = [item["hung"] for item in self.recorded() if "hung" in item]
                self.assertEqual(len(hung), 2)
                self.assertEqual([running_on_windows(pid) for pid in hung], [False, False])
                self.assertEqual(self.events(), ["self_check_timed_out", "self_check_timed_out", "restore_unavailable"])
                # Nothing to restore and no installer to start: the client is started as it is.
                self.assertEqual((code, self.started()), (3, ["installed"]))

    def test_two_jobs_wait_for_a_run_that_is_installing_and_restore_nothing_over_it(self) -> None:
        for name, shell in self.shells():
            with self.subTest(shell=name), self.case("two jobs and a run that installs"):
                release_client = self.real_client_again()
                self.damage()
                script = self.write_launcher("windows", self.recorder_command())
                run = self.run_holding_the_lock(1.0, self.release)

                jobs = [self.start(self.command(shell, script)) for _ in range(2)]
                results = [self.finish(job, seconds=300) for job in jobs]
                _, errors = run.communicate(timeout=300)

                self.assertEqual(run.returncode, 0, errors.decode("utf-8", "replace"))
                self.assertEqual([code for code, _ in results], [0, 0], results)
                # Neither job looked at the client while it was being replaced.
                self.assertEqual(self.events(), [])
                self.assertEqual(files(self.client), release_client)
                self.assertEqual(self_check(self.client).returncode, 0)
                self.assertEqual(self.started(), ["client", "client"])

    def test_two_jobs_that_find_a_damaged_client_together_restore_it_once(self) -> None:
        for name, shell in self.shells():
            with self.subTest(shell=name), self.case("two jobs and a damaged client"):
                self.real_client_again()
                previous = files(self.client / "previous")
                self.damage()
                script = self.write_launcher("windows", self.recorder_command())

                jobs = [self.start(self.command(shell, script)) for _ in range(2)]
                results = [self.finish(job, seconds=300) for job in jobs]

                self.assertEqual([code for code, _ in results], [0, 0], results)
                # The launcher of Windows with a real client: its self-check, and its restore.
                self.assertEqual(files(self.client), previous)
                self.assertEqual(self_check(self.client).returncode, 0)
                # One of them restored; the other waited for it and found a client that starts.
                self.assertEqual(self.events(), ["self_check_failed", "runtime_restored"])
                self.assertEqual(self.started(), ["client", "client"])
                self.assertEqual([path.name for path in self.client.iterdir() if path.name.startswith(".")], [])


    def test_a_real_client_whose_part_is_held_is_left_alone_and_started_at_the_next_start(self) -> None:
        """The cases with stubs hold a file of a made-up client. Here the client is the one of
        this tree: its entry point binds its parts while it is loaded, and one of them cannot
        be read. Its compiled copies are taken away first: an interpreter that finds one that
        is current does not open the source at all, and a held source then stops nothing."""
        for name, shell in self.shells():
            with self.subTest(shell=name), self.case("a part of a real client is held"):
                release_client = self.real_client_again()
                shutil.rmtree(self.client / "__pycache__", ignore_errors=True)
                script = self.write_launcher("windows", self.recorder_command())
                handle = hold_without_sharing(self.client / "sync_rules.py")
                try:
                    self.assertNotEqual(self_check(self.client).returncode, 0, "the held part did not stop the self-check")
                    code, _ = self.launch(shell, script)
                finally:
                    let_go(handle)

                self.assertEqual((code, self.events(), self.started()), (75, ["restore_busy"], []))
                self.assertEqual(files(self.client), release_client)

                code, _ = self.launch(shell, script)
                self.assertEqual((code, self.events(), self.started()), (0, ["restore_busy"], ["client"]))
                self.assertEqual(files(self.client), release_client)


def running_on_windows(pid: int) -> bool:
    """Whether a process of this id is still running. Windows only: `os.kill(pid, 0)`, which
    asks that on a Mac, sends an event to a console there."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    query_limited_information, still_active = 0x1000, 259
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
