"""No real program is started by a test of the computer client, except Python running a script.

The client's code starts system tools by name: `schtasks`, `cmdkey`, `powershell`, `launchctl`,
`security`. A sandboxed home does not sandbox the Windows scheduler or the Keychain, and a test
that calls such code without its model would act on the computer of the person who runs it.
While the guard is installed, starting anything is refused, loudly and with the program's name,
unless it is this very interpreter, running a script of the repository or of a temporary
directory, with an environment whose home is not the person's.

The guard follows the processes it lets through: each is started through this file, which
installs the same guard there before the script runs. (They are started with `-E -s`, so no
environment variable could load it.) Every refusal, in whichever process, is also written to
one file, so a test that swallowed the error still fails the run.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# How a guarded process learns what its parent refuses and where refusals are written.
GUARD_VARIABLE = "AGENT_PLATFORM_TEST_PROCESS_GUARD"
PROFILE_VARIABLES = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
# Interpreter options that stand alone, and the ones followed by a value.
PLAIN_OPTIONS = re.compile(r"-[bBdEhiIOPqRsSuvx]+")
VALUE_OPTIONS = ("-X", "-W")
OS_STARTERS = ("system", "popen", "startfile", "posix_spawn", "posix_spawnp",
               "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
               "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe")


class RealProcessRefused(BaseException):
    """A test reached a real program. Not an Exception on purpose: the client's code catches
    OSError and Exception around what it starts, and this must not become one of its ordinary
    failures."""


def same_path(left: str, right: str) -> bool:
    """Two spellings of one file: whatever the case, with or without `.exe`."""
    def spelled(value: str) -> str:
        value = os.path.abspath(value).replace("\\", "/").lower()
        return value[:-4] if value.endswith(".exe") else value
    return spelled(left) == spelled(right)


def inside(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
    except ValueError:
        return False


class ProcessGuard:
    def __init__(self, forbidden: list[str], record: Path, interpreter: str = sys.executable):
        # The person's own home and profile directories, as their environment named them.
        self.forbidden = sorted({value for value in forbidden if value})
        self.record = Path(record)
        self.interpreter = interpreter
        self.installed: list[tuple[object, str, object]] = []

    # -- what is written down
    def refuse(self, program: object, reason: str) -> None:
        line = {"program": str(program), "reason": reason, "pid": os.getpid()}
        with self.record.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\n")
        raise RealProcessRefused(
            f"a test tried to start {str(program)!r}: {reason}. Tests of the computer client start nothing but "
            "Python on a script of their own; a system tool is reached through the Host model.")

    def refusals(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line]
        except OSError:
            return []

    def forgive(self) -> None:
        """Forget the refusals so far: for the tests that ask for them."""
        self.record.write_text("", encoding="utf-8")

    # -- the one kind of process that is let through
    def checked(self, arguments: object, executable: object, shell: object, environment: object) -> tuple[list[str], dict]:
        """The command and environment a permitted launch is really started with."""
        if shell:
            self.refuse(arguments, "a shell command")
        if isinstance(arguments, (str, bytes, os.PathLike)):
            arguments = [arguments]
        words = [os.fsdecode(word) for word in arguments]
        program = os.fsdecode(executable) if executable else (words[0] if words else "")
        if not words or not same_path(program, self.interpreter) or not same_path(words[0], self.interpreter):
            self.refuse(program, "it is not the interpreter the tests run on")
        index = 1
        while index < len(words) and words[index] not in ("-c", "-m", "-") and words[index].startswith("-"):
            if words[index].startswith(VALUE_OPTIONS):
                index += 1 if len(words[index]) > 2 else 2
            elif PLAIN_OPTIONS.fullmatch(words[index]):
                index += 1
            else:
                self.refuse(program, f"the interpreter option {words[index]!r} is not one the guard follows")
        if index >= len(words) or words[index] in ("-m", "-"):
            self.refuse(program, "the interpreter is started without a script")
        if words[index] != "-c" and not (inside(words[index], str(ROOT)) or inside(words[index], tempfile.gettempdir())):
            self.refuse(program, f"the script {words[index]!r} is neither of the repository nor of a temporary directory")
        effective = {key.upper(): value for key, value in (os.environ if environment is None else environment).items()}
        for variable in ("HOME", "USERPROFILE"):
            if not effective.get(variable):
                self.refuse(program, f"{variable} is not set, so its home is the person's")
        for variable in PROFILE_VARIABLES:
            if any(same_path(effective.get(variable) or ".", real) for real in self.forbidden):
                self.refuse(program, f"{variable} is the person's own {effective[variable]!r}")
        if effective.get("HOMEDRIVE") or effective.get("HOMEPATH"):
            self.refuse(program, "HOMEDRIVE and HOMEPATH lead to the person's profile")
        passed = dict(os.environ if environment is None else environment)
        passed[GUARD_VARIABLE] = json.dumps({"forbidden": self.forbidden, "record": str(self.record)})
        return [*words[:index], str(Path(__file__).resolve()), *words[index:]], passed

    # -- installing
    def replace(self, owner: object, name: str, value: object) -> None:
        self.installed.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def install(self) -> "ProcessGuard":
        original = subprocess.Popen.__init__
        signature = inspect.signature(original)
        guard = self

        def guarded(popen, *arguments, **options):
            bound = signature.bind(popen, *arguments, **options)
            given = bound.arguments
            given["args"], given["env"] = guard.checked(
                given.get("args"), given.get("executable"), given.get("shell"), given.get("env"))
            given.pop("executable", None)
            return original(*bound.args, **bound.kwargs)

        self.replace(subprocess.Popen, "__init__", guarded)
        for name in OS_STARTERS:
            if hasattr(os, name):
                def refused(*arguments, _name=name, **_):
                    guard.refuse(arguments[0] if arguments else f"os.{_name}", f"started with os.{_name}")
                self.replace(os, name, refused)
        return self

    def remove(self) -> None:
        while self.installed:
            owner, name, value = self.installed.pop()
            setattr(owner, name, value)


def run_guarded(arguments: list[str]) -> None:
    """What `python [options] process_guard.py <script | -c code> [arguments]` does: the guard
    of the process that started this one, then the script as `python` itself would run it."""
    try:
        settings = json.loads(os.environ[GUARD_VARIABLE])
        guard = ProcessGuard(settings["forbidden"], Path(settings["record"]))
    except (KeyError, TypeError, ValueError):
        raise SystemExit("process_guard.py runs a script only for a process that is guarded itself") from None
    if not arguments:
        raise SystemExit("process_guard.py: no script")
    guard.install()
    if arguments[0] == "-c":
        sys.argv = ["-c", *arguments[2:]]
        sys.path[0] = ""
        main = types.ModuleType("__main__")
        sys.modules["__main__"] = main
        exec(compile(arguments[1], "<string>", "exec"), main.__dict__)
        return
    sys.argv = list(arguments)
    sys.path[0] = os.path.dirname(os.path.abspath(arguments[0]))
    runpy.run_path(arguments[0], run_name="__main__")


if __name__ == "__main__":
    run_guarded(sys.argv[1:])
