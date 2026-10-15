"""The client changes its own files on a computer where another program holds one of them.

On Windows a file that is open in another program cannot be replaced, renamed or removed, and
neither can a directory that contains it. A real-time antivirus opens every file a program has
just written, so the client meets this where it writes most: copying a release's client into
place and renewing the copy in `previous/`. Before these tests the copy in `previous/` was
deleted first and its replacement renamed afterwards, so one held file left the computer with
no client to go back to; and a copy that failed once was a client that "did not start".

A held file does not fail these calls on a Mac or on Linux. So the tests here raise, at the
call that Windows fails, exactly what Windows raises (`Held`), and the last class does it for
real on a Windows computer: a file opened with no sharing at all.

Reading a held file is refused there too, and in other words than replacing it. Measured on
Windows 11 with Python 3.12 (October 2026), for a file
another handle holds with no sharing:

    replace             PermissionError  errno 13  winerror 5
    unlink, rename      PermissionError  errno 13  winerror 32
    rename of its directory              errno 13  winerror 5
    open and read       PermissionError  errno 13  winerror None

`open` goes through the C runtime, which keeps the error number and drops the code of Windows.
`HeldRead` raises that last line on any computer, and `on_windows` makes the client take this
computer for a Windows one where it decides what the error means.
"""

from __future__ import annotations

import errno
import getpass
import inspect
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "tools", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_managed_client_runtime import (FIXTURE_PART, load_sync_client, release_tree, release_with_part,  # noqa: E402
                                         self_check)

ENTRY = "sync_client.py"
BUSY = "client_files_busy"
# The longest a client may spend on one held file before it gives up, with room for a slow
# computer: it waits a little over three seconds.
GIVES_UP_WITHIN_SECONDS = 20


def sharing_violation(path: object) -> PermissionError:
    """What Python on Windows raises when another program has the file open."""
    error = PermissionError(errno.EACCES, "The process cannot access the file because it is being used by "
                                          "another process", str(path))
    error.winerror = 32
    return error


class Held:
    """Windows' rule for a held file, on any computer: while `held(source, destination)` says so,
    replacing or renaming fails with a sharing violation. `times` is how often it fails before
    the other program lets go; None means it never does."""

    def __init__(self, held, times: int | None = None):
        self.held, self.times, self.refused = held, times, 0
        self.replace, self.rename = os.replace, os.rename

    def call(self, real, source, destination, **options):
        if (self.times is None or self.refused < self.times) and self.held(Path(source), Path(destination)):
            self.refused += 1
            raise sharing_violation(destination)
        return real(source, destination, **options)

    def __enter__(self) -> "Held":
        self.patches = [patch.object(os, "replace", lambda *a, **k: self.call(self.replace, *a, **k)),
                        patch.object(os, "rename", lambda *a, **k: self.call(self.rename, *a, **k))]
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *_: object) -> None:
        for item in self.patches:
            item.stop()


def refused_read(path: object) -> PermissionError:
    """What Python on Windows raises when a file another program holds is opened for reading:
    the error number alone. (There the attribute `winerror` exists and is None; elsewhere it
    does not exist, which reads the same.)"""
    return PermissionError(errno.EACCES, "Permission denied", str(path))


class HeldRead:
    """Windows' rule for reading a held file, on any computer: while the file of this name is
    held, `Path.read_bytes` is refused. `times` as in `Held`."""

    def __init__(self, name: str, times: int | None = None):
        self.name, self.times, self.refused = name, times, 0
        self.read = Path.read_bytes

    def call(self, path: Path) -> bytes:
        if path.name == self.name and (self.times is None or self.refused < self.times):
            self.refused += 1
            raise refused_read(path)
        return self.read(path)

    def __enter__(self) -> "HeldRead":
        self.patch = patch.object(Path, "read_bytes", lambda path: self.call(path))
        self.patch.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.patch.stop()


def on_windows(sync, windows: bool = True):
    """The client decides what an error means as it does on a Windows computer. Only that: no
    path, lock or process of this computer is treated differently."""
    return patch.dict(sync.copy_busy.__globals__, {"WINDOWS": windows})


def elsewhere(sync):
    """... and as it does on a Mac or on Linux, also when these tests run on Windows."""
    return on_windows(sync, False)


def fresh_previous_copy(source: Path, destination: Path) -> bool:
    """The directory of freshly copied files that becomes previous/: an antivirus is reading one."""
    return source.name.startswith(".previous-")


def onto(name: str):
    return lambda source, destination: destination.name == name


def files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.py"))}


class BusyFileCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sync = load_sync_client()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="client-busy-")))
        self.managed = self.directory / "home" / ".agent-platform" / "client"
        self.first = release_tree(self.directory / "first")
        # The next release: one file more, and so an entry point of its own.
        self.second = release_with_part(self.directory / "second")

    def computer_with_a_previous_client(self) -> tuple[dict[str, bytes], dict[str, bytes]]:
        """A computer that took two releases: (the installed client, the copy in previous/)."""
        self.sync.install_managed_runtime(self.first, self.managed)
        self.sync.install_managed_runtime(self.first, self.managed)
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        self.assertEqual(self_check(previous).returncode, 0)
        return files(self.managed), files(previous)

    def leftovers(self) -> list[str]:
        """Whatever an installation left in the client's directory besides the client, its
        previous copy and what its launcher writes."""
        kept = {self.sync.PREVIOUS_CLIENT_RUNTIME, self.sync.LAUNCHER_EVENTS_FILE, "__pycache__"}
        return sorted(path.name for path in self.managed.iterdir() if path.suffix != ".py" and path.name not in kept)


class PreviousCopyTests(BusyFileCase):
    def test_a_file_held_for_good_never_leaves_the_computer_without_a_previous_client(self) -> None:
        installed, kept = self.computer_with_a_previous_client()
        started = time.monotonic()

        with Held(fresh_previous_copy) as held, self.assertRaises(OSError) as stopped:
            self.sync.install_managed_runtime(self.second, self.managed)

        # The copy to go back to is the one that was there, whole, and it starts.
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        self.assertEqual(files(previous), kept)
        self.assertEqual(self_check(previous).returncode, 0)
        # Nothing of the new release was published: the installation stopped before its first file.
        self.assertEqual(files(self.managed), installed)
        self.assertEqual(self.leftovers(), [])
        # It was held, and that is what the client says; not that a client did not start.
        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertNotIsInstance(stopped.exception, self.sync.ClientRuntimeProbeFailed)
        # It waited for the other program, and not without end.
        self.assertGreater(held.refused, 1)
        self.assertLessEqual(held.refused, 10)
        self.assertLess(time.monotonic() - started, GIVES_UP_WITHIN_SECONDS + 60)

    def test_a_file_that_is_let_go_after_a_moment_does_not_fail_the_installation(self) -> None:
        installed, _ = self.computer_with_a_previous_client()

        with Held(lambda source, destination: fresh_previous_copy(source, destination)
                  or destination.name in ("sync_rules.py", ENTRY), times=3) as held:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(held.refused, 3)
        expected = {name: (self.second / relative).read_bytes()
                    for name, relative in self.sync.declared_runtime_files(self.second).items()}
        self.assertEqual(files(self.managed), expected)
        self.assertEqual(self_check(self.managed).returncode, 0)
        # The client that was replaced is the one to go back to.
        self.assertEqual(files(self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME), installed)
        self.assertEqual(self.leftovers(), [])

    def test_a_client_file_held_for_good_stops_the_installation_and_says_what_stopped_it(self) -> None:
        installed, _ = self.computer_with_a_previous_client()

        with Held(onto("sync_rules.py")) as held, self.assertRaises(OSError) as stopped:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertGreater(held.refused, 1)
        # The entry point goes last, so the one that was here still is, and it starts.
        self.assertEqual(files(self.managed)[ENTRY], installed[ENTRY])
        self.assertEqual(self_check(self.managed).returncode, 0)
        # The copy to go back to was renewed before any file was replaced, and is whole.
        self.assertEqual(files(self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME), installed)
        self.assertEqual(self.leftovers(), [])

        # The other program lets go: the same installation goes through.
        self.sync.install_managed_runtime(self.second, self.managed)
        self.assertEqual(self_check(self.managed).returncode, 0)
        self.assertEqual(self.leftovers(), [])


class HeldReadTests(BusyFileCase):
    """A file another program holds cannot be read either, and Windows says so without its own
    code. Before these tests such a read was "not allowed": nothing waited for it, a digest of
    the file was no digest, and a client that was whole was taken for one to repair."""

    def held(self, error: BaseException) -> bool:
        return self.sync.copy_busy.__globals__["held_by_another_program"](error)

    def release(self) -> dict:
        return {"release": {"releaseId": "20261013-090000-aaaaaaaaaaaa-bbbbbbbbbbbb"}}

    def test_a_refused_read_is_a_file_in_use_on_windows_and_not_allowed_anywhere_else(self) -> None:
        refused = refused_read(self.managed / "sync_rules.py")
        # A Mac and Linux say this for a file or a directory that may not be read or written;
        # no waiting changes that, and it is never reported as a file another program holds.
        with elsewhere(self.sync):
            self.assertFalse(self.held(refused))
            self.assertTrue(self.held(OSError(errno.EBUSY, "busy")))
        with on_windows(self.sync):
            self.assertTrue(self.held(refused))
            # The codes Windows gives the other calls are what they were.
            self.assertTrue(self.held(sharing_violation("x")))
            # Only "access denied" without a code: every other error keeps its meaning.
            for other in (FileNotFoundError(errno.ENOENT, "gone"), IsADirectoryError(errno.EISDIR, "a directory"),
                          OSError(errno.ENOSPC, "full"), PermissionError(errno.EPERM, "not permitted"),
                          self.sync.ClientFilesBusy("x"), ValueError("x")):
                self.assertFalse(self.held(other), repr(other))

    def test_a_read_refused_for_a_moment_is_waited_for(self) -> None:
        installed, _ = self.computer_with_a_previous_client()
        retry = self.sync.copy_busy.__globals__["retry_busy"]
        target = self.managed / "sync_rules.py"

        with on_windows(self.sync), HeldRead("sync_rules.py", times=3) as held:
            self.assertEqual(retry(target.read_bytes, target), installed["sync_rules.py"])
        self.assertEqual(held.refused, 3)

        with on_windows(self.sync), HeldRead("sync_rules.py") as held, self.assertRaises(OSError) as stopped:
            retry(target.read_bytes, target)
        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertEqual(held.refused, self.sync.copy_busy.__globals__["BUSY_ATTEMPTS"])

        # Elsewhere the same error is final at once.
        with elsewhere(self.sync), HeldRead("sync_rules.py") as held, self.assertRaises(PermissionError) as stopped:
            retry(target.read_bytes, target)
        self.assertNotEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertEqual(held.refused, 1)

    def test_a_client_whose_file_is_being_read_by_another_program_is_not_taken_for_a_damaged_one(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)

        with on_windows(self.sync), HeldRead("sync_rules.py", times=3):
            plan = self.sync.plan_client_recovery(self.sync, self.release(), self.first, accepted=True,
                                                  root=self.managed)

        # The client is the release's: nothing to repair, and nothing written down against it.
        self.assertEqual((plan["action"], plan["report"]["installedMatchesRelease"]), ("none", True))
        self.assertEqual(self.sync.read_recovery_state(self.managed), {})

    def test_a_client_file_that_stays_unreadable_stops_the_run_as_held_and_counts_nothing(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)

        with on_windows(self.sync), HeldRead("sync_rules.py"), self.assertRaises(OSError) as stopped:
            self.sync.plan_client_recovery(self.sync, self.release(), self.first, accepted=True, root=self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertEqual(self.sync.read_recovery_state(self.managed), {})
        self.assertFalse((self.managed / self.sync.RECOVERY_STATE_FILE).exists())

    def test_a_file_that_is_not_there_is_still_a_file_that_differs(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        (self.managed / "sync_rules.py").unlink()

        with on_windows(self.sync):
            plan = self.sync.plan_client_recovery(self.sync, self.release(), self.first, accepted=True,
                                                  root=self.managed)

        self.assertEqual((plan["action"], plan["report"]["installedMatchesRelease"]), ("repair", False))


class HeldSelfCheck:
    """What a self-check answers on Windows while a file of its client is held: the interpreter
    cannot read the file and ends with a failure. `Path.read_bytes` of this process is refused
    by `HeldRead`; a self-check is a process of its own, so the same is said for it here: while
    `held()` holds, the self-check of a client in `directory` ends with the code 1 and is not
    started at all."""

    def __init__(self, sync, directory: Path, held):
        self.sync, self.directory, self.held, self.asked = sync, directory, held, 0
        self.run = sync.subprocess.run

    def call(self, command, *positional, **options):
        if "--self-check" in command and Path(command[-2]).parent == self.directory:
            self.asked += 1
            if self.held():
                return self.sync.subprocess.CompletedProcess(command, 1, stdout="", stderr="PermissionError: "
                                                             "[Errno 13] Permission denied\n")
        return self.run(command, *positional, **options)

    def __enter__(self) -> "HeldSelfCheck":
        self.patch = patch.object(self.sync.subprocess, "run", lambda *a, **k: self.call(*a, **k))
        self.patch.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.patch.stop()


class HeldSelfCheckTests(BusyFileCase):
    """A client asks another client whether it starts in three places: the one it is about to
    replace (to keep it as the copy to go back to), the staged copy of a release, and the copy
    in previous/. A file of that client held by another program fails the answer exactly as a
    damaged client does. Taken for a damaged one, the first was not kept, the second refused a
    release that is whole, and the third was replaced."""

    def test_an_installed_client_that_is_held_stops_the_installation_before_anything_is_replaced(self) -> None:
        installed, kept = self.computer_with_a_previous_client()

        with on_windows(self.sync), HeldRead("sync_rules.py") as held, \
                HeldSelfCheck(self.sync, self.managed, lambda: True) as checks, self.assertRaises(OSError) as stopped:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertNotIsInstance(stopped.exception, self.sync.ClientRuntimeProbeFailed)
        # Asked again while the file was held, a bounded number of times.
        self.assertEqual(checks.asked, self.sync.copy_busy.__globals__["BUSY_ATTEMPTS"])
        self.assertGreaterEqual(held.refused, checks.asked)
        # The client that runs is whole and so is the copy to go back to: nothing was published
        # next to an entry point that could not be read, and previous/ was not left behind.
        self.assertEqual(files(self.managed), installed)
        self.assertEqual(files(self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME), kept)
        self.assertEqual(self.leftovers(), [])

    def test_an_installed_client_held_for_a_moment_is_kept_as_the_copy_to_go_back_to(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        installed = files(self.managed)

        with on_windows(self.sync), HeldRead("sync_rules.py", times=2) as held, \
                HeldSelfCheck(self.sync, self.managed, lambda: held.refused < 2):
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(self_check(self.managed).returncode, 0)
        self.assertEqual(files(self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME), installed)

    def test_a_staged_release_held_for_a_moment_is_not_a_release_that_does_not_start(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        staged = lambda: next(iter(self.managed.glob(".staging-*")), None)  # noqa: E731

        class Staged(HeldSelfCheck):
            def call(inner, command, *positional, **options):
                inner.directory = staged()
                return super().call(command, *positional, **options)

        # An antivirus reads what was just written: the files of the staged copy.
        with on_windows(self.sync), HeldRead(FIXTURE_PART, times=2) as held, \
                Staged(self.sync, None, lambda: held.refused < 2) as checks:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertGreater(checks.asked, 1)
        self.assertIn(FIXTURE_PART, files(self.managed))
        self.assertEqual(self_check(self.managed).returncode, 0)

    def test_a_previous_client_that_is_held_is_not_replaced_as_one_that_does_not_know_the_rule(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        self.sync.install_managed_runtime(self.second, self.managed)
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        kept, state = files(previous), {}
        settle = self.sync.settle_client_recovery.__globals__["settle_rollback_target"]

        with on_windows(self.sync), HeldRead("sync_rules.py"), HeldSelfCheck(self.sync, previous, lambda: True):
            replaced = settle(self.sync, self.managed, state)

        # Not looked at now; the next run looks again.
        self.assertEqual((replaced, state, files(previous)), (False, {}, kept))
        # Let go, it is found to know the rule and is kept.
        self.assertFalse(settle(self.sync, self.managed, state))
        self.assertEqual((sorted(state), files(previous)), (["rollback"], kept))

    def test_a_previous_client_is_not_renewed_from_a_client_that_is_held_and_the_run_goes_on(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        self.sync.install_managed_runtime(self.second, self.managed)
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        kept, state = files(previous), {}
        recovery = self.sync.settle_client_recovery.__globals__
        settle = recovery["settle_rollback_target"]

        # The copy is a client from before the rule, so it is to be replaced by the installed
        # client; a file of that one is held while it is asked whether it starts. The run has
        # done everything else by then and is not failed for it.
        with on_windows(self.sync), patch.dict(recovery, {"client_capabilities": lambda entry_file: []}), \
                HeldRead("sync_rules.py") as held, HeldSelfCheck(self.sync, self.managed, lambda: True):
            replaced = settle(self.sync, self.managed, state)

        self.assertGreater(held.refused, 1)
        self.assertEqual((replaced, state, files(previous)), (False, {}, kept))

    def test_a_client_that_does_not_start_with_no_file_held_is_still_one_that_does_not_start(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)

        with on_windows(self.sync), HeldSelfCheck(self.sync, self.managed, lambda: True) as checks, \
                self.assertRaises(self.sync.ClientRuntimeProbeFailed):
            self.sync.probe_client_runtime(self.managed)
        self.assertEqual(checks.asked, 1)


class FingerprintTests(BusyFileCase):
    """A run compares the client's files before and after itself to know whether it replaced
    them, and finishes through the new client when it did. A file that could not be read at the
    first look was "missing" there and present at the second: one more run for nothing."""

    def test_a_file_held_at_the_first_look_does_not_make_a_client_that_was_replaced(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        unheld = self.sync.runtime_fingerprint(self.managed)

        with on_windows(self.sync), HeldRead("sync_rules.py", times=3) as held:
            before = self.sync.runtime_fingerprint(self.managed)
        after = self.sync.runtime_fingerprint(self.managed)

        self.assertEqual(held.refused, 3)
        self.assertEqual((before, after), (unheld, unheld))
        self.assertFalse(self.sync.follow_up_needed(before, after, 0, {}))

    def test_a_file_that_stays_held_or_is_not_there_is_no_reason_to_stop(self) -> None:
        self.sync.install_managed_runtime(self.first, self.managed)
        whole = self.sync.runtime_fingerprint(self.managed)

        with on_windows(self.sync), HeldRead("sync_rules.py"):
            held = self.sync.runtime_fingerprint(self.managed)
        (self.managed / "sync_rules.py").unlink()

        self.assertNotEqual(held, whole)
        self.assertEqual(self.sync.runtime_fingerprint(self.managed), held)

    def test_a_question_about_the_client_is_answered_without_looking_for_a_follow_up(self) -> None:
        # The declaration of a release is answered with no part bound, so it must not reach
        # what waits for a held file; and neither answer is a run that could have replaced anything.
        source = inspect.getsource(self.sync.run_main_with_receipt)
        self.assertIn('if {"--self-check", "--runtime-files"} & set(sys.argv[1:]):', source)
        self.assertIn("        return main_once()\n", source)
        self.sync.install_managed_runtime(self.first, self.managed)
        # A release is asked what it consists of, an installed client whether it starts.
        for question, entry in (("--runtime-files", self.first / "adapters/client/scripts" / ENTRY),
                                ("--self-check", self.managed / ENTRY)):
            with self.subTest(question=question):
                completed = self.sync.subprocess.run(
                    [sys.executable, "-E", "-s", str(entry), question],
                    capture_output=True, text=True, encoding="utf-8", check=False)
                self.assertEqual((completed.returncode, completed.stderr), (0, ""))
                self.assertEqual(len(completed.stdout.strip().splitlines()), 1)


def hold_without_sharing(path: Path) -> int:
    """Open a file as a program that shares it with nobody: no other handle can be opened on it,
    for reading, writing or deleting, until this one is closed. Windows only."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    generic_read, share_nothing, open_existing, normal = 0x80000000, 0, 3, 0x80
    handle = kernel32.CreateFileW(str(path), generic_read, share_nothing, None, open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def let_go(handle: int) -> None:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "a sharing violation exists only on Windows: elsewhere a held file can be "
                                      "replaced, and the classes above raise what Windows raises")
class RealWindowsSharingTests(BusyFileCase):
    """The same on a Windows computer, with a file that really is open in another handle."""

    def held(self, path: Path, seconds: float | None = None) -> None:
        handle = hold_without_sharing(path)
        if seconds is None:
            self.addCleanup(let_go, handle)
        else:
            timer = threading.Timer(seconds, let_go, [handle])
            timer.start()
            self.addCleanup(timer.join)

    def test_windows_refuses_what_these_tests_say_it_refuses(self) -> None:
        installed, _ = self.computer_with_a_previous_client()
        target, other = self.managed / "sync_rules.py", self.directory / "other.py"
        other.write_bytes(installed["sync_rules.py"])
        self.held(target)
        held_by_another_program = self.sync.copy_busy.__globals__["held_by_another_program"]
        said = {}
        for name, call in (("replace", lambda: os.replace(other, target)), ("unlink", target.unlink),
                           ("rename", lambda: os.rename(target, self.managed / "moved.py")),
                           ("read", target.read_bytes),
                           # A directory that contains the held file cannot be renamed either.
                           ("rename of the directory",
                            lambda: os.rename(self.managed, self.managed.with_name("client-moved")))):
            with self.subTest(call=name):
                with self.assertRaises(PermissionError) as refused:
                    call()
                said[name] = (refused.exception.errno, refused.exception.winerror)
                # Whatever words Windows chose, the client takes it for a file in use.
                self.assertTrue(held_by_another_program(refused.exception), said[name])
        # What `Held` and `HeldRead` raise on other computers is what was raised here. Printed
        # in full when it is not: the next reader needs the table and not one line of it.
        expected = {"replace": (errno.EACCES, 5), "unlink": (errno.EACCES, 32), "rename": (errno.EACCES, 32),
                    "read": (errno.EACCES, None), "rename of the directory": (errno.EACCES, 5)}
        self.assertEqual(said, expected)
        self.assertEqual((refused_read(target).errno, getattr(refused_read(target), "winerror", None)),
                         expected["read"])

    def test_windows_says_why_a_file_cannot_be_opened(self) -> None:
        self.computer_with_a_previous_client()
        target = self.managed / "sync_rules.py"
        refusal = self.sync.copy_busy.__globals__["windows_open_refusal"]
        held_by_another_program = self.sync.copy_busy.__globals__["held_by_another_program"]

        self.assertEqual(refusal(target), 0)
        handle = hold_without_sharing(target)
        try:
            # A sharing violation, however much sharing the one who asks offers.
            self.assertEqual(refusal(target), 32)
        finally:
            let_go(handle)
        self.assertEqual(refusal(target), 0)
        self.assertEqual(refusal(self.managed / "not_there.py"), 2)
        # A directory where a file is expected is "access denied" for good, not a held file.
        directory = self.managed / "a_directory.py"
        directory.mkdir()
        self.assertEqual(refusal(directory), 5)
        with self.assertRaises(OSError) as refused:
            directory.read_bytes()
        self.assertFalse(held_by_another_program(refused.exception),
                         (refused.exception.errno, getattr(refused.exception, "winerror", None)))

    def test_a_file_this_person_may_not_read_is_not_waited_for_and_is_not_a_held_one(self) -> None:
        self.computer_with_a_previous_client()
        target = self.managed / "sync_rules.py"
        user = os.environ.get("USERNAME") or getpass.getuser()
        denied = subprocess.run(["icacls", str(target), "/deny", f"{user}:(R)"], capture_output=True, text=True,
                                errors="replace", check=False, encoding="utf-8")
        if denied.returncode != 0:
            self.skipTest(f"icacls could not take the right to read away from {user}: "
                          f"{denied.stdout.strip()} {denied.stderr.strip()}")
        self.addCleanup(subprocess.run, ["icacls", str(target), "/remove:d", user], capture_output=True, check=False)
        refusal = self.sync.copy_busy.__globals__["windows_open_refusal"]
        held_by_another_program = self.sync.copy_busy.__globals__["held_by_another_program"]

        with self.assertRaises(PermissionError) as refused:
            target.read_bytes()
        said = (refused.exception.errno, refused.exception.winerror)
        # Python says for it what it says for a held file; Windows, asked, does not.
        self.assertEqual((said[0], refusal(target)), (errno.EACCES, 5), said)
        self.assertFalse(held_by_another_program(refused.exception), said)
        started = time.monotonic()
        with self.assertRaises(PermissionError) as stopped:
            self.sync.read_busy(target)
        self.assertNotEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertLess(time.monotonic() - started, 2)
        # To the client it is a file that is not the release's: a client to repair, as it was
        # before a held read was waited for.
        plan = self.sync.plan_client_recovery(
            self.sync, {"release": {"releaseId": "20261013-090000-aaaaaaaaaaaa-bbbbbbbbbbbb"}}, self.first,
            accepted=True, root=self.managed)
        self.assertEqual((plan["action"], plan["report"]["installedMatchesRelease"]), ("repair", False))

    def test_a_client_file_read_while_it_is_held_for_a_moment_is_read(self) -> None:
        installed, _ = self.computer_with_a_previous_client()
        self.held(self.managed / "sync_rules.py", seconds=0.7)

        self.assertEqual(self.sync.read_busy(self.managed / "sync_rules.py"), installed["sync_rules.py"])

    def test_a_client_file_that_cannot_be_read_for_good_is_held_and_no_repair_is_counted(self) -> None:
        self.computer_with_a_previous_client()
        self.held(self.managed / "sync_rules.py")

        with self.assertRaises(OSError) as stopped:
            self.sync.plan_client_recovery(
                self.sync, {"release": {"releaseId": "20261013-090000-aaaaaaaaaaaa-bbbbbbbbbbbb"}}, self.first,
                accepted=True, root=self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertEqual(self.sync.read_recovery_state(self.managed), {})

    def test_a_client_file_held_for_a_moment_does_not_fail_the_installation(self) -> None:
        installed, _ = self.computer_with_a_previous_client()
        self.held(self.managed / "sync_rules.py", seconds=0.7)

        self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(self_check(self.managed).returncode, 0)
        self.assertEqual(files(self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME), installed)
        self.assertEqual(self.leftovers(), [])

    def test_a_client_file_held_for_good_stops_the_installation_and_keeps_both_clients(self) -> None:
        installed, kept = self.computer_with_a_previous_client()
        self.held(self.managed / ENTRY)

        with self.assertRaises(OSError) as stopped:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertEqual(sorted(files(self.managed / "previous")), sorted(installed))
        # The client that is replaced could not be asked whether it starts, so the
        # installation stopped before previous/ was renewed and before any file was replaced:
        # the copy that was there is still the one to go back to.
        self.assertEqual({name: (self.managed / name).read_bytes() for name in installed if name != ENTRY},
                         {name: content for name, content in installed.items() if name != ENTRY})
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        self.assertEqual(files(previous), kept)
        self.assertEqual(self_check(previous).returncode, 0)

    def test_a_file_of_the_previous_copy_held_for_good_keeps_that_copy(self) -> None:
        installed, kept = self.computer_with_a_previous_client()
        previous = self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME
        self.held(previous / "sync_rules.py")

        with self.assertRaises(OSError) as stopped:
            self.sync.install_managed_runtime(self.second, self.managed)

        self.assertEqual(getattr(stopped.exception, "code", None), BUSY)
        self.assertTrue(previous.is_dir())
        self.assertEqual(sorted(path.name for path in previous.glob("*.py")), sorted(kept))
        self.assertEqual(files(self.managed), installed)


if __name__ == "__main__":
    unittest.main()
