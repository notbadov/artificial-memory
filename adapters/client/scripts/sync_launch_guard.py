#!/usr/bin/env python3
"""What a launcher starts before the client, and how the client changes files a program holds.

A launcher (the hourly job of a Mac or of Windows) does not start the client straight away. It
starts this file, which checks under the client's run lock that the client can start, puts the
previous client back when it cannot, and tells the launcher what to start next. The check and
the restore used to be shell and PowerShell text and ran before any lock was taken: a job that
began while another run was installing a release could restore the previous client over that
installation, and a self-check that never returned stopped the hourly synchronization for good.

This file stands alone. A launcher starts the copy that was written next to it together with
the launcher itself (`sync-client-guard.py`), not the file of the installed client: what
restores a damaged client must not be one of the files that can be damaged, and it changes only
when the launcher does. So it imports the standard library and nothing of the client.

Because it stands alone, two things the rest of the client needs as well are defined here once:

* waiting, for a bounded time, for a file another program holds. On Windows a file that an
  antivirus is reading cannot be replaced, renamed or removed, and neither can a directory that
  contains it; a file a program holds with no sharing cannot be read either. That passes
  within moments, so the call is repeated a few times with growing pauses; a file still held
  after that is reported as held (`ClientFilesBusy`) and never as a client that does not start;
* telling a client that does not start from one whose file is held while it is asked: the
  interpreter cannot read such a file, and the self-check fails as that of a damaged client does;
* renewing the copy in `previous/` without a moment in which no complete copy exists.

This file is a part of the entry point. Every part declared after it may import it.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# The entry point of a client, by the name the entry point gives itself.
CLIENT_ENTRY = "sync_client.py"
PREVIOUS_CLIENT_RUNTIME = "previous"
# Where the copy in previous/ waits while a newer one takes its place.
RETIRED_CLIENT_RUNTIME = "previous.retired"
RUN_LOCK_FILE = "sync.lock"
# The copy of this file a launcher starts. Not named like a file of a client, so neither a
# restore (previous/*.py) nor the removal of undeclared client files ever touches it.
LAUNCH_GUARD_FILE = "sync-client-guard.py"
LAUNCHER_EVENTS_FILE = "launcher-events.jsonl"
# Everything a launcher and this guard write down for the next report.
LAUNCHER_EVENTS = frozenset({
    "self_check_failed", "runtime_restored", "restore_unavailable",
    # The self-check did not return within its limit and was ended.
    "self_check_timed_out",
    # Another program held a file of the client or of the previous one: nothing was put back
    # and nothing was started.
    "restore_busy",
    # Another run held the lock for as long as the guard waits; nothing was looked at.
    "guard_busy",
    # Written by the launcher itself: the guard could not be started or stopped unexpectedly.
    "guard_failed",
})

# What the guard tells its launcher. Any other exit code means the guard itself did not work.
GUARD_START_CLIENT = 0
GUARD_NOTHING_TO_RESTORE = 10
GUARD_RUN_IN_PROGRESS = 11
GUARD_RESTORE_FAILED = 74
GUARD_FILES_BUSY = 75

# Windows error codes of a file or directory another program has open: access denied, sharing
# violation, lock violation. Python raises all three as PermissionError.
BUSY_WINDOWS_ERRORS = (5, 32, 33)
# What Windows answers when a file is opened that this person may not read, or a directory
# where a file is expected. A file another program holds is answered with 32.
WINDOWS_ACCESS_DENIED = 5
# Whether this computer is a Windows one, for the one error that says nothing else about itself
# (held_by_another_program).
WINDOWS = os.name == "nt"
# What other systems say for the same thing; a system that lacks a name never raises it.
BUSY_ERROR_NUMBERS = tuple(getattr(errno, name) for name in ("EBUSY", "ETXTBSY") if hasattr(errno, name))
# Six attempts with pauses of 0.1, 0.2, 0.4, 0.8 and 1.6 seconds: a little over three seconds
# for one file, which outlasts a scan and never becomes a wait nobody can see the end of.
BUSY_ATTEMPTS = 6
BUSY_FIRST_PAUSE_SECONDS = 0.1
LOCK_POLL_SECONDS = 0.2


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


# ---------------------------------------------------------------- files another program holds


class ClientFilesBusy(OSError):
    """A file of the client stayed held by another program for as long as the client waits.

    Nothing is wrong with the client or with the release: the next run tries again. An OSError,
    so code that already steps over a file it cannot write steps over this one as well."""

    code = "client_files_busy"

    def __init__(self, path: object):
        super().__init__(errno.EBUSY, "held by another program", str(path))


def held_by_another_program(error: BaseException) -> bool:
    """Whether an error says "in use right now" and not "not allowed".

    Windows names the cause in `winerror` when a file is replaced, renamed or removed. It does
    not when a file is opened: `open` goes through the C runtime, which keeps the error number
    and drops the code, so reading a file another program holds raises PermissionError with
    EACCES and nothing else (measured on Windows 11 with Python 3.12, October 2026). On
    Windows that error is therefore "in use" as well, unless Windows, asked about the file the
    error names, says that this person may not read it (`windows_open_refusal`): no waiting
    changes that, and a client with such a file is one that does not start.

    Everywhere else a PermissionError without a code is what POSIX raises for a file or a
    directory that may not be read or written, which no waiting changes."""
    if isinstance(error, ClientFilesBusy) or not isinstance(error, OSError):
        return False
    code = getattr(error, "winerror", None)
    if code in BUSY_WINDOWS_ERRORS or error.errno in BUSY_ERROR_NUMBERS:
        return True
    if not (WINDOWS and code is None and isinstance(error, PermissionError) and error.errno == errno.EACCES):
        return False
    return error.filename is None or windows_open_refusal(error.filename) != WINDOWS_ACCESS_DENIED


def windows_open_refusal(path: object) -> int | None:
    """The code Windows gives when `path` is opened for reading with every kind of sharing
    allowed: 0 when it opens, None when Windows cannot be asked (another system, no ctypes).

    Reading a file fails with the same Python error whether another program holds it or this
    person may not read it. Windows itself tells the two apart: a sharing violation (32) for
    the first, however much sharing is offered, and "access denied" (5) for the second, which
    it decides before it looks at who else has the file open. The same 5 is the answer for a
    directory, and for a file that is being deleted; neither is waited for."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, share_everything, open_existing, normal = 0x80000000, 0x1 | 0x2 | 0x4, 3, 0x80
        handle = kernel32.CreateFileW(str(path), generic_read, share_everything, None, open_existing, normal, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            return ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        return 0
    except Exception:
        return None


def retry_busy(action, path: object):
    """Do `action`, again after a pause while the file is held, and give up after the last attempt."""
    pause = BUSY_FIRST_PAUSE_SECONDS
    for attempt in range(1, BUSY_ATTEMPTS + 1):
        try:
            return action()
        except OSError as error:
            if not held_by_another_program(error):
                raise
            if attempt == BUSY_ATTEMPTS:
                raise ClientFilesBusy(path) from error
        time.sleep(pause)
        pause *= 2


def replace_busy(source: object, destination: object) -> None:
    retry_busy(lambda: os.replace(source, destination), destination)


def rename_busy(source: object, destination: object) -> None:
    retry_busy(lambda: os.rename(source, destination), source)


def copy_busy(source: object, destination: object) -> None:
    retry_busy(lambda: shutil.copy2(source, destination), source)


def remove_busy(path: Path) -> None:
    retry_busy(lambda: path.unlink(missing_ok=True), path)


def read_busy(path: Path) -> bytes:
    """The bytes of a file. One that is not there is not there (FileNotFoundError), as ever."""
    return retry_busy(path.read_bytes, path)


def remove_tree_busy(path: Path) -> None:
    if path.exists():
        retry_busy(lambda: shutil.rmtree(path), path)


def held_client_file(directory: Path) -> Path | None:
    """A file of the client in `directory` that another program holds at this moment, or None.

    Asked after a self-check of that client failed. The check is a process of its own and says
    nothing but that it failed: an interpreter that cannot read a file of the client ends as it
    does for a file that is damaged. So the files are read here, once each and without waiting.
    A file that is missing, or that this person may not read, is not a held one."""
    for path in sorted(directory.glob("*.py")):
        if path.name == LAUNCH_GUARD_FILE:
            continue
        try:
            path.read_bytes()
        except OSError as error:
            if held_by_another_program(error):
                return path
    return None


def copy_into_place(source: Path, destination: Path) -> None:
    """Put a copy of `source` where `destination` is, in one step: a reader finds the file as it
    was or as it will be. The copy is made under a name no other process uses."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    try:
        copy_busy(source, temporary)
        replace_busy(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def recover_previous_runtime(root: Path) -> None:
    """Finish a renewal of previous/ that stopped between its two steps: the copy that was moved
    aside is the previous client again."""
    previous, retired = root / PREVIOUS_CLIENT_RUNTIME, root / RETIRED_CLIENT_RUNTIME
    if retired.is_dir() and not previous.exists():
        rename_busy(retired, previous)


def replace_previous_runtime(root: Path, staged: Path) -> None:
    """Make the directory `staged` the copy in previous/.

    The copy that is there is moved aside first and removed only when the new one has taken its
    place, so a complete copy to go back to exists at every moment; when the new one cannot take
    its place (a file in it is held), the earlier one is put back. No system this client runs on
    exchanges two directories in one step, so between the two renames the complete copy lies
    under the other name: `recover_previous_runtime` is what a run and a launcher's guard call
    before they look for it."""
    previous, retired = root / PREVIOUS_CLIENT_RUNTIME, root / RETIRED_CLIENT_RUNTIME
    recover_previous_runtime(root)
    # Left by a renewal that could not remove it then.
    remove_tree_busy(retired)
    if previous.exists():
        rename_busy(previous, retired)
    try:
        rename_busy(staged, previous)
    except BaseException:
        try:
            recover_previous_runtime(root)
        except OSError:
            pass
        raise
    # Held now, it is removed by the next renewal, which waits for it and says so.
    shutil.rmtree(retired, ignore_errors=True)


# ---------------------------------------------------------------- the run lock


class RunInProgress(Exception):
    """Another run of the client held the lock for as long as this one was willing to wait."""


@contextmanager
def run_lock(root: Path, wait_seconds: float):
    """The lock every run of the client holds (sync_guard.py), waited for up to `wait_seconds`.

    Taken exactly as a run takes it, on the same file, so the two exclude each other: the first
    byte of the file on Windows, the whole file elsewhere."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / RUN_LOCK_FILE).open("a+b") as handle:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise RunInProgress() from None
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- the guard


def record_launcher_event(root: Path, event: str) -> None:
    """One line for the next report. A line that cannot be written is said aloud instead: the
    guard goes on, and its launcher's log keeps what happened."""
    line = json.dumps({"event": event, "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      separators=(",", ":"))
    try:
        with (root / LAUNCHER_EVENTS_FILE).open("a", encoding="utf-8", newline="\n") as log:
            log.write(line + "\n")
    except OSError as error:
        print(f"agent-platform: launcher event {event} could not be written down: {error}", file=sys.stderr)


def client_starts(python: str, entry: Path, limit_seconds: float) -> str:
    """Ask a client whether it can start: "ok", "failed", "timed_out" when it did not answer
    within the limit and was ended, or "held" when it failed while another program holds a
    file of it. Nothing is read from it, so nothing can keep this waiting after the process is
    gone."""
    try:
        completed = subprocess.run([python, "-E", "-s", str(entry), "--self-check"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=limit_seconds, check=False)
    except subprocess.TimeoutExpired:
        return "timed_out"
    except OSError:
        return "failed"
    if completed.returncode == 0:
        return "ok"
    return "held" if held_client_file(entry.parent) is not None else "failed"


def client_starts_once_free(python: str, entry: Path, limit_seconds: float) -> str:
    """`client_starts`, asked again after a pause while a file of that client is held: as often
    and for as long as any held file is waited for (`retry_busy`). "held" when it still is."""
    pause = BUSY_FIRST_PAUSE_SECONDS
    for attempt in range(1, BUSY_ATTEMPTS + 1):
        answer = client_starts(python, entry, limit_seconds)
        if answer != "held" or attempt == BUSY_ATTEMPTS:
            break
        time.sleep(pause)
        pause *= 2
    return answer


def checked(root: Path, python: str, entry: Path, limit_seconds: float) -> str:
    """Whether the client starts: "ok", "failed", "timed_out" or "held". A check that failed or
    was ended is written down; a held file is not, it says nothing about the client.

    On Windows a check that failed is made once more before it stands. A program that held a
    file for an instant may have let go between the check and the look at the files, and
    nothing then tells that check from the one of a damaged client; a damaged client fails
    again. Elsewhere no program can hold a file so that it cannot be read."""
    answer = client_starts_once_free(python, entry, limit_seconds)
    if answer == "failed" and WINDOWS:
        answer = client_starts_once_free(python, entry, limit_seconds)
    if answer == "timed_out":
        print(f"agent-platform: the self-check of {entry} did not return in {limit_seconds:g} seconds and was ended",
              file=sys.stderr)
        record_launcher_event(root, "self_check_timed_out")
    elif answer == "failed" and entry.parent == root:
        record_launcher_event(root, "self_check_failed")
    return answer


def left_to_the_next_start(root: Path, directory: Path) -> int:
    """A file of the client in `directory` stayed held: nothing is put back and nothing is
    started, and the next start looks again."""
    held = held_client_file(directory)
    print(f"agent-platform: {held or 'a file in ' + str(directory)} is held by another program; the sync client "
          "was left as it is and not started, the next start tries again", file=sys.stderr)
    record_launcher_event(root, "restore_busy")
    return GUARD_FILES_BUSY


def restore_previous_client(root: Path) -> None:
    """Copy previous/*.py over the client, the entry point last, as an installer copies it last:
    a restore that stops in the middle leaves the entry point that failed, so the next start
    fails its self-check and restores again, and the previous entry point never starts with
    files that are not its own."""
    previous = root / PREVIOUS_CLIENT_RUNTIME
    names = sorted(path.name for path in previous.glob("*.py"))
    for name in sorted(names, key=lambda name: name == CLIENT_ENTRY):
        copy_into_place(previous / name, root / name)


def guard_client(root: Path, python: str, *, lock_wait_seconds: float, self_check_limit_seconds: float) -> int:
    """Check the client in `root` and put the previous one back when it cannot start; the answer
    is what the launcher does next (the GUARD_* codes)."""
    try:
        with run_lock(root, lock_wait_seconds):
            answer = checked(root, python, root / CLIENT_ENTRY, self_check_limit_seconds)
            if answer == "ok":
                return GUARD_START_CLIENT
            if answer == "held":
                return left_to_the_next_start(root, root)
            try:
                recover_previous_runtime(root)
                previous_entry = root / PREVIOUS_CLIENT_RUNTIME / CLIENT_ENTRY
                answer = (checked(root, python, previous_entry, self_check_limit_seconds)
                          if previous_entry.is_file() else "failed")
                if answer == "held":
                    return left_to_the_next_start(root, previous_entry.parent)
                if answer != "ok":
                    record_launcher_event(root, "restore_unavailable")
                    return GUARD_NOTHING_TO_RESTORE
                print("agent-platform: installed sync client failed its self-check; restoring the previous runtime",
                      file=sys.stderr)
                restore_previous_client(root)
            except ClientFilesBusy as error:
                print(f"agent-platform: {error.filename} is held by another program; the previous sync client "
                      "was not put back, the next start tries again", file=sys.stderr)
                record_launcher_event(root, "restore_busy")
                return GUARD_FILES_BUSY
            except OSError as error:
                print(f"agent-platform: the previous sync client could not be put back: {error}", file=sys.stderr)
                return GUARD_RESTORE_FAILED
            record_launcher_event(root, "runtime_restored")
            return GUARD_START_CLIENT
    except RunInProgress:
        print("agent-platform: another synchronization is running; this start leaves the sync client to it",
              file=sys.stderr)
        record_launcher_event(root, "guard_busy")
        return GUARD_RUN_IN_PROGRESS


def main(argv: list[str] | None = None) -> int:
    utf8_streams()
    parser = argparse.ArgumentParser(description="Check the synchronization client before a launcher starts it.")
    parser.add_argument("--lock-wait", type=float, required=True,
                        help="seconds to wait for a run that holds the client's lock")
    parser.add_argument("--self-check-limit", type=float, required=True,
                        help="seconds a self-check may take before it is ended")
    args = parser.parse_args(argv)
    # The client is the directory this copy lies in: a launcher hands it no path.
    return guard_client(Path(__file__).resolve().parent, sys.executable, lock_wait_seconds=args.lock_wait,
                        self_check_limit_seconds=args.self_check_limit)


if __name__ == "__main__":
    raise SystemExit(main())
