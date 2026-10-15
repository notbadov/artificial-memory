"""The lock a run of the synchronization client holds: one run at a time for one user.

Not blocking: a run that finds the lock held raises ClientBusy and leaves the work to the run
that holds it. It imports nothing of the client, so it says nothing itself: the entry point
answers a busy run with its own sentence.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class ClientBusy(RuntimeError):
    pass


@contextmanager
def sync_guard(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "sync.lock").open("a+b") as handle:
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
        except OSError as error:
            import errno
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ClientBusy() from None
            raise
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
