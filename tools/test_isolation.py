"""Explicit, disposable client state shared by test runners and pytest."""

import codecs
from contextlib import contextmanager
from functools import cache
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_guard import PROFILE_VARIABLES, ProcessGuard  # noqa: E402


STATE_DIRECTORIES = {
    "AGENT_PLATFORM_CONSENT_ROOT": "consents",
    "AGENT_PLATFORM_SKILL_BACKUP_ROOT": "skill-backups",
}
# Where a process finds its home: HOME on a Mac and on Linux; on Windows USERPROFILE, and
# HOMEDRIVE with HOMEPATH when USERPROFILE is missing.
HOME_VARIABLES = ("HOME", "USERPROFILE")
HOME_PARTS = ("HOMEDRIVE", "HOMEPATH")
# What Windows keeps under the profile and names by a variable of its own.
PROFILE_DIRECTORIES = {"APPDATA": "AppData/Roaming", "LOCALAPPDATA": "AppData/Local"}
# Variables that send the client to an app's directory wherever the home is.
APP_HOME_VARIABLES = ("KIMI_CODE_HOME", "KIMI_DESKTOP_DATA_ROOT")
# Where the person's own home and profile directories are remembered (a JSON list) once this
# process has left them, so the guard still knows what to refuse.
PERSON_VARIABLE = "AGENT_PLATFORM_TEST_PERSON_PROFILE"


# Variables that choose the locale of a process, and with it the encoding of its streams.
LOCALE_VARIABLES = ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE")
# Ways to start an interpreter with streams that are not UTF-8: options, variables.
#  * Windows needs nothing: the streams of `python -E` are in the system's code page.
#  * macOS, and a Linux that has the locale, give cp1251 exactly as a Russian Windows does.
#  * Anywhere else the C locale gives ASCII, but only with UTF-8 mode refused by name: under
#    `-E` the interpreter still turns that mode on by itself when the locale is C.
LEGACY_STARTS = (
    ((), {}),
    ((), {"LC_ALL": "ru_RU.CP1251"}),
    (("-X", "utf8=0"), {"LC_ALL": "C"}),
)
LEGACY_PROBE = "import sys; print(sys.stdout.encoding, sys.getfilesystemencoding())"
# What a Windows computer always has in its environment, and what the client reads to find the
# programs of the system (`windows_system_tool`). A test that models Windows on another system
# gives this process the same, as that computer would have it.
WINDOWS_SYSTEM_ROOT = {"SystemRoot": os.environ.get("SystemRoot") or "C:\\Windows"}


def person_profile() -> list[str]:
    """The home and profile directories of the person who runs the tests, whether or not this
    process still has them."""
    try:
        remembered = [str(value) for value in json.loads(os.environ.get(PERSON_VARIABLE, "[]"))]
    except (TypeError, ValueError):
        remembered = []
    present = [os.path.expanduser("~"), *(os.environ.get(variable) for variable in PROFILE_VARIABLES)]
    return sorted({value for value in [*remembered, *present] if value})


def sandboxed_environment(home, base=None):
    """The environment of a process that runs client code in a test: `base` (this process's own
    by default) with the home, and everything else that leads into the person's profile, inside
    `home`. Whatever a program needs to start (PATH, SystemRoot, TEMP, PATHEXT, COMSPEC) stays.

    Every process a test of the computer client starts gets its environment from here."""
    home = Path(home)
    replaced = {*HOME_VARIABLES, *HOME_PARTS, *PROFILE_DIRECTORIES, *APP_HOME_VARIABLES}
    # Windows reads variables whatever their case: `UserProfile` must not survive next to USERPROFILE.
    environment = {key: value for key, value in (os.environ if base is None else base).items()
                   if key.upper() not in replaced}
    for variable in HOME_VARIABLES:
        environment[variable] = str(home)
    for variable, relative in PROFILE_DIRECTORIES.items():
        directory = home / relative
        directory.mkdir(parents=True, exist_ok=True)
        environment[variable] = str(directory)
    return environment


def legacy_environment(home, variables: dict[str, str]) -> dict[str, str]:
    """`sandboxed_environment` without anything that chooses an encoding, and with the variables
    of one of LEGACY_STARTS."""
    environment = {key: value for key, value in sandboxed_environment(home).items()
                   if key not in LOCALE_VARIABLES and not key.startswith("PYTHON")}
    return {**environment, **variables}


@cache
def legacy_start() -> tuple[tuple[str, ...], dict[str, str], str, bool] | None:
    """(interpreter options, variables, encoding of the streams, whether file names are still
    UTF-8) of an isolated interpreter whose output is not UTF-8 on this computer, or None when
    every way gives UTF-8.

    That is the interpreter a client meets on a Windows computer, where whoever starts it reads
    what it prints as UTF-8. A test that starts the client in it finds a sentence that is not
    ASCII and was written in the encoding of the computer, without a Windows computer."""
    with tempfile.TemporaryDirectory(prefix="client-encoding-probe-") as home:
        for options, variables in LEGACY_STARTS:
            completed = subprocess.run(
                [sys.executable, "-E", "-s", *options, "-c", LEGACY_PROBE],
                capture_output=True, cwd=home, env=legacy_environment(Path(home), variables), check=False, timeout=60)
            try:
                streams, names = (codecs.lookup(name).name for name in completed.stdout.decode("ascii").split())
            except (LookupError, UnicodeError, ValueError):
                continue
            if completed.returncode == 0 and streams != "utf-8":
                return options, variables, streams, names == "utf-8"
    return None


def legacy_run(home) -> tuple[tuple[str, ...], dict[str, str]]:
    """(interpreter options, environment) to start the client in a home as a Windows computer
    starts it: with streams that are not UTF-8 unless the client makes them so.

    Used by tests that read what a whole run prints. Where no such interpreter exists, or it
    could not read the names of this computer's files either, the run is started in the
    sandbox as it is; the tests of the encoding itself say so (test_client_output_encoding.py)."""
    found = legacy_start()
    if found is None or not found[3]:
        return (), sandboxed_environment(home)
    return found[0], legacy_environment(home, found[1])


@contextmanager
def sandboxed_home():
    """This process itself in a disposable home, for as long as the context lasts: client code
    called by a test, and any process started without an environment of its own, finds this
    directory where the person's profile would be."""
    previous = dict(os.environ)
    person = person_profile()
    with tempfile.TemporaryDirectory(prefix="agent-platform-test-home-") as temporary:
        environment = sandboxed_environment(Path(temporary))
        environment[PERSON_VARIABLE] = json.dumps(person)
        try:
            os.environ.clear()
            os.environ.update(environment)
            yield Path(temporary)
        finally:
            os.environ.clear()
            os.environ.update(previous)


@contextmanager
def isolated_client_state():
    """Override inherited real paths, then restore the caller and remove all test state."""
    previous = {variable: os.environ.get(variable) for variable in STATE_DIRECTORIES}
    with tempfile.TemporaryDirectory(prefix="agent-platform-test-state-") as temporary:
        root = Path(temporary)
        try:
            for variable, directory in STATE_DIRECTORIES.items():
                path = root / directory
                path.mkdir(mode=0o700)
                os.environ[variable] = str(path)
            yield root
        finally:
            for variable, value in previous.items():
                if value is None:
                    os.environ.pop(variable, None)
                else:
                    os.environ[variable] = value


@contextmanager
def client_test_sandbox():
    """What a module of tests of the computer client runs in: a disposable home for this process
    (`sandboxed_home`), and a guard that refuses to start any real program but Python on a
    script, here and in every process that Python starts (`process_guard.py`). Yields the guard.
    A refusal nobody asked for fails the module when it ends, even if the test that caused it
    caught the error."""
    person = person_profile()
    with tempfile.TemporaryDirectory(prefix="agent-platform-test-guard-") as directory, sandboxed_home():
        guard = ProcessGuard(person, Path(directory) / "refused.jsonl").install()
        try:
            yield guard
        finally:
            guard.remove()
        refused = guard.refusals()
        if refused:
            raise AssertionError("tests tried to start real programs: " + "; ".join(
                f"{item['program']} ({item['reason']})" for item in refused))
