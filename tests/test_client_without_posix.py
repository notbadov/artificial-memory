"""The computer client on an interpreter that has nothing of POSIX, which is Python on Windows.

A test module of the terminal client that imports `fcntl` at its top fails to import on
Windows, so none of its tests run there, while every run on a Mac stays green. Nothing here
needs Windows: each file is imported by a fresh interpreter from which the
modules and the names Windows does not have were taken away, and the locks are entered on their
Windows branch against a model of `msvcrt`. What only a real Windows computer shows is not
shown here.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "adapters/client/scripts"
for directory in (SCRIPTS, ROOT / "tools"):
    sys.path.insert(0, str(directory))

import command_line as cli  # noqa: E402
import sync_client  # noqa: E402
import sync_guard  # noqa: E402
import sync_launch_guard  # noqa: E402
from process_guard import GUARD_VARIABLE, RealProcessRefused  # noqa: E402
from test_isolation import client_test_sandbox, sandboxed_environment  # noqa: E402

# Standard modules that exist on a Mac and on Linux and not on Windows.
POSIX_MODULES = ("fcntl", "grp", "pty", "pwd", "resource", "syslog", "termios", "tty")
# Names the `os` and `signal` modules have on POSIX only. Not all of them: the standard library
# of the computer the tests run on reads a few (os.WNOHANG) when it is imported.
POSIX_OS_NAMES = ("getuid", "geteuid", "getgid", "getegid", "getgroups", "getpgid", "getpgrp", "setsid", "fork",
                  "killpg", "chown", "fchown", "lchown", "mkfifo", "uname", "nice", "O_NOFOLLOW", "O_DIRECTORY")
POSIX_SIGNALS = ("SIGKILL", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGCHLD", "SIGSTOP", "SIGCONT")

# Imports one file the way Python on Windows would have to: argv is the file, then the absent
# modules, the absent `os` names and the absent signals, each list joined with commas.
PROBE = """
import importlib.abc, importlib.util, os, signal, sys

path, modules, os_names, signals = sys.argv[1], *(set(value.split(",")) for value in sys.argv[2:5])


class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in modules:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


for name in modules:
    sys.modules.pop(name, None)
sys.meta_path.insert(0, Absent())
for owner, names in ((os, os_names), (signal, signals)):
    for name in names:
        if hasattr(owner, name):
            delattr(owner, name)
sys.path.insert(0, os.path.dirname(path))
spec = importlib.util.spec_from_file_location("imported_without_posix", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print("imported")
"""


def setUpModule() -> None:
    global PROCESS_GUARD
    PROCESS_GUARD = unittest.enterModuleContext(client_test_sandbox())


def import_without_posix(path: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(path), ",".join(POSIX_MODULES), ",".join(POSIX_OS_NAMES), ",".join(POSIX_SIGNALS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT, check=False, timeout=120,
        env=sandboxed_environment(home))


def client_files() -> list[Path]:
    """Everything a computer runs: the client's scripts and what an installed client is made
    of, and this module, which a person on Windows runs before a release."""
    # Everything the release declares for an installed client, the parts of the entry point included.
    installed = sync_client.runtime_files()
    files = {*SCRIPTS.glob("*.py"), *(ROOT / relative for relative in installed.values()), Path(__file__)}
    return sorted(files)


class ImportTests(unittest.TestCase):
    def test_every_file_of_the_client_is_imported_without_anything_of_posix(self):
        files = client_files()
        for required in ("command_line.py", "sync_client.py", "sync_guard.py", "companion_install.py", "agent_identity.py",
                         "release_transport.py", "control_plane_sync.py", "client_status.py", "mcp_bridge.py",
                         "safe_tar.py", *sync_client.MANAGED_CLIENT_PARTS):
            self.assertIn(required, [path.name for path in files])
        self.assertLessEqual({"sync_messages.py", "sync_base.py", "sync_rules.py", "sync_consents.py",
                              "sync_projects.py", "sync_launchers.py", "sync_schedule.py", "sync_disconnect.py",
                              "sync_mcp.py", "sync_app_agents.py", "sync_skills.py", "sync_release.py"},
                             set(sync_client.MANAGED_CLIENT_PARTS))
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))), tempfile.TemporaryDirectory() as home:
                result = import_without_posix(path, Path(home))
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "imported"), result.stderr[-2000:])

    def test_the_probe_refuses_a_file_that_needs_posix(self):
        # Without this the test above would pass for a probe that takes nothing away.
        with tempfile.TemporaryDirectory() as temporary:
            for source, expected in (("import fcntl\n", "No module named 'fcntl'"),
                                     ("import shutil, pwd\n", "No module named 'pwd'"),
                                     ("import os\nOWNER = os.getuid()\n", "has no attribute 'getuid'"),
                                     ("import signal\nLAST = signal.SIGKILL\n", "has no attribute 'SIGKILL'")):
                path = Path(temporary) / "needs_posix.py"
                path.write_text(source, encoding="utf-8")
                result = import_without_posix(path, Path(temporary) / "home")
                self.assertNotEqual(result.returncode, 0, source)
                self.assertIn(expected, result.stderr)


# The modules whose tests start client code as processes of their own.
SANDBOXED_TEST_MODULES = ("tests/test_client_without_posix.py", "tests/test_client_module_boundaries.py")
PROCESS_STARTERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "os": {"system", "popen", "startfile", "fork", "forkpty", "posix_spawn", "posix_spawnp"},
}


def unsandboxed_launches(source: str) -> list[str]:
    """Every place in a test module that starts a process without the environment
    `sandboxed_environment` builds: "line N: what is wrong". Such a process would have the home
    of the person who runs the tests."""
    found = []
    tree = ast.parse(source)
    # The guard's own tests start what must be refused; what they start runs under the guard.
    deliberate = {id(inner) for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GuardTests"
                  for inner in ast.walk(node)}
    for node in ast.walk(tree):
        if id(node) in deliberate:
            continue
        if isinstance(node, ast.ImportFrom) and node.module in PROCESS_STARTERS:
            # `from subprocess import run` would hide a launch from the check below.
            for alias in node.names:
                if alias.name in PROCESS_STARTERS[node.module] or alias.name.startswith(("spawn", "exec")):
                    found.append(f"line {node.lineno}: {node.module}.{alias.name} is imported by name")
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)):
            continue
        module, name = node.func.value.id, node.func.attr
        starts = name in PROCESS_STARTERS.get(module, ()) or (module == "os" and name.startswith(("spawn", "exec")))
        if not starts:
            continue
        environment = next((keyword.value for keyword in node.keywords if keyword.arg == "env"), None)
        sandboxed = (isinstance(environment, ast.Call) and isinstance(environment.func, ast.Name)
                     and environment.func.id == "sandboxed_environment")
        if not sandboxed:
            found.append(f"line {node.lineno}: {module}.{name} without env=sandboxed_environment(...)")
    return sorted(found)


class SandboxTests(unittest.TestCase):
    """No test of the computer client can reach the profile of the person who runs it."""

    def test_every_launch_written_in_the_test_modules_names_a_sandboxed_home(self):
        # A reading of the files, no more: it sees the launches the tests themselves write,
        # not what the client's code starts when a test calls it. That is what the guard is for
        # (GuardTests): it stops a real program where it would start, whoever asked.
        for relative in SANDBOXED_TEST_MODULES:
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(unsandboxed_launches(source), [], relative)
            self.assertIn("    PROCESS_GUARD = unittest.enterModuleContext(client_test_sandbox())\n", source, relative)

    def test_the_check_finds_a_process_started_with_the_inherited_home(self):
        for source, expected in (
                ("import subprocess\nsubprocess.run(['python', 'sync_client.py'], cwd='.')\n", "subprocess.run without"),
                ("import subprocess\nsubprocess.Popen(['x'], env={**os.environ})\n", "subprocess.Popen without"),
                ("import subprocess\nsubprocess.check_output(['x'], env=None)\n", "subprocess.check_output without"),
                ("import os\nos.system('python sync_client.py')\n", "os.system without"),
                ("import os\nos.spawnv(os.P_WAIT, 'python', ['python'])\n", "os.spawnv without"),
                ("from subprocess import run\nrun(['x'])\n", "subprocess.run is imported by name")):
            found = unsandboxed_launches(source)
            self.assertEqual(len(found), 1, source)
            self.assertIn(expected, found[0])
        self.assertEqual(unsandboxed_launches("import subprocess\nsubprocess.run(['x'], env=sandboxed_environment(home))\n"), [])
        # Only the tests of the guard are left out, not a class that merely looks like them.
        inside = "import subprocess\nclass {name}:\n    def test(self):\n        subprocess.run(['schtasks'])\n"
        self.assertEqual(unsandboxed_launches(inside.format(name="GuardTests")), [])
        self.assertEqual(len(unsandboxed_launches(inside.format(name="OtherTests"))), 1)

    def test_a_sandboxed_process_finds_no_way_to_the_real_profile(self):
        import ntpath
        import posixpath
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            real = {"HOME": "/Users/person", "UserProfile": r"C:\Users\person", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\person",
                    "APPDATA": r"C:\Users\person\AppData\Roaming", "LocalAppData": r"C:\Users\person\AppData\Local",
                    "KIMI_CODE_HOME": "/Users/person/.kimi-code", "KIMI_DESKTOP_DATA_ROOT": "/Users/person/kimi",
                    "SystemRoot": r"C:\Windows", "PATH": "/usr/bin", "TEMP": "/tmp", "TMP": "/tmp", "PATHEXT": ".EXE",
                    "COMSPEC": r"C:\Windows\system32\cmd.exe"}
            environment = sandboxed_environment(home, real)
            self.assertFalse([value for value in environment.values() if "person" in value])
            # What a program needs to start is kept as it was.
            for name in ("SystemRoot", "PATH", "TEMP", "TMP", "PATHEXT", "COMSPEC"):
                self.assertEqual(environment[name], real[name])
            self.assertEqual({name: environment[name] for name in ("HOME", "USERPROFILE")}, {"HOME": str(home), "USERPROFILE": str(home)})
            for name in ("APPDATA", "LOCALAPPDATA"):
                self.assertTrue(Path(environment[name]).is_dir() and home in Path(environment[name]).parents, name)
            # The home as Python finds it on Windows and on a Mac.
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual((ntpath.expanduser("~"), posixpath.expanduser("~")), (str(home), str(home)))

    def test_the_guard_knows_the_persons_home_under_a_sandboxed_runner_too(self):
        from test_isolation import PERSON_VARIABLE, person_profile, sandboxed_home
        before = person_profile()
        with sandboxed_home() as outer:
            # As under `run_tests.py --sandbox-home`: the home of this process is already not the person's.
            self.assertEqual(json.loads(os.environ[PERSON_VARIABLE]), before)
            self.assertEqual(set(person_profile()) - set(before), {str(outer), os.environ["APPDATA"], os.environ["LOCALAPPDATA"]})
        # What was the person's before this module left it is what the guard refuses.
        remembered = json.loads(os.environ[PERSON_VARIABLE])
        self.assertTrue(remembered)
        self.assertEqual(sorted(remembered), PROCESS_GUARD.forbidden)
        self.assertNotIn(str(Path.home()), remembered)

    def test_this_process_has_a_disposable_home_while_the_tests_run(self):
        home = Path.home()
        self.assertIn("agent-platform-test-home-", home.name)
        self.assertEqual({os.environ[name] for name in ("HOME", "USERPROFILE")}, {str(home)})
        self.assertFalse({"HOMEDRIVE", "HOMEPATH"} & set(os.environ))


# Every way the client, or anything else, could name a program of the system.
SYSTEM_TOOLS = (
    "schtasks", "schtasks.exe", "SCHTASKS.EXE", r"C:\Windows\System32\schtasks.exe", "cmdkey", "cmdkey.exe", "CmdKey.Exe",
    r"C:\Windows\System32\cmdkey.exe", "powershell", "powershell.exe", "PowerShell.EXE", "pwsh", "pwsh.exe",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "wscript.exe", "rundll32", "rundll32.exe", "cmd", "cmd.exe",
    r"C:\Windows\System32\cmd.exe", "reg.exe", "launchctl", "/bin/launchctl", "security", "/usr/bin/security", "plutil",
    "/usr/bin/plutil", "open", "/usr/bin/open", "sh", "/bin/sh", "python", "python.exe", "git")

# Run by a guarded child: it tries every tool, and then starts one more Python.
GUARDED_CHILD = """
import json, os, subprocess, sys
from unittest.mock import patch

tools, marker = json.loads(sys.argv[1]), sys.argv[2]
refused = []
with patch.object(subprocess.Popen, "_execute_child") as started:
    for tool in tools:
        try:
            subprocess.run([tool, "/?"], capture_output=True)
        except BaseException as error:
            refused.append([tool, type(error).__name__, str(error)])
    for name, call in (("os.system", lambda: os.system("schtasks /Query")),
                       ("os.execv", lambda: os.execv("/bin/launchctl", ["launchctl", "list"]))):
        try:
            call()
        except BaseException as error:
            refused.append([name, type(error).__name__, str(error)])
    reached = started.call_count
grandchild = subprocess.run([sys.executable, "-E", "-s", "-c", "import sys; print(sys.argv[1])", "started"],
                            capture_output=True, text=True)
print(json.dumps({"refused": refused, "reached": reached, "grandchild": grandchild.stdout.strip(),
                  "guarded": bool(os.environ.get(sys.argv[3]))}))
"""


class GuardTests(unittest.TestCase):
    """While these modules run, no real program is started but Python on a script with a
    sandboxed home: not by a test, not by the client's code a test calls, not by a process
    either of them starts."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.addCleanup(PROCESS_GUARD.forgive)
        self.assertEqual(PROCESS_GUARD.refusals(), [], "a test before this one reached a real program")

    def test_no_system_tool_is_started_from_the_tests_by_any_name(self):
        with patch.object(subprocess.Popen, "_execute_child") as started:
            for tool in SYSTEM_TOOLS:
                for launch in (lambda tool=tool: subprocess.run([tool, "/?"], capture_output=True, check=False),
                               lambda tool=tool: subprocess.Popen([tool]),
                               lambda tool=tool: subprocess.check_output(tool, shell=True),
                               lambda tool=tool: subprocess.call([sys.executable, "-c", "pass"], executable=tool)):
                    with self.assertRaises(RealProcessRefused, msg=tool) as refused:
                        launch()
                    self.assertIn(repr(tool), str(refused.exception))
            for name, launch in (("system", lambda: os.system("schtasks /Query")),
                                 ("popen", lambda: os.popen("cmdkey /list")),
                                 ("execv", lambda: os.execv("/bin/launchctl", ["launchctl", "list"])),
                                 ("spawnv", lambda: os.spawnv(os.P_WAIT, "/usr/bin/security", ["security"]))):
                with self.assertRaises(RealProcessRefused, msg=name):
                    launch()
            started.assert_not_called()
        self.assertEqual(len(PROCESS_GUARD.refusals()), 4 * len(SYSTEM_TOOLS) + 4)

    def test_python_is_started_only_on_a_script_and_only_with_a_home_of_its_own(self):
        marker = self.directory / "started"
        script = self.directory / "script.py"
        script.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(sys.argv[2])\n", encoding="utf-8")
        person = PROCESS_GUARD.forbidden[0]
        sandboxed = sandboxed_environment(self.directory / "home")
        for command, environment in (
                ([sys.executable, str(script), str(marker), "x"], {**sandboxed, "HOME": person}),
                ([sys.executable, str(script), str(marker), "x"], {**sandboxed, "USERPROFILE": person.upper()}),
                ([sys.executable, str(script), str(marker), "x"], {**sandboxed, "HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\person"}),
                ([sys.executable, str(script), str(marker), "x"], {name: value for name, value in sandboxed.items() if name != "USERPROFILE"}),
                ([sys.executable, "-m", "pip", "--version"], sandboxed),
                ([sys.executable], sandboxed),
                ([sys.executable, str(Path(ROOT.anchor) / "elsewhere" / "script.py")], sandboxed)):
            with self.assertRaises(RealProcessRefused, msg=command):
                subprocess.run(command, env=environment, capture_output=True, check=False)
        self.assertFalse(marker.exists())
        self.assertEqual(len(PROCESS_GUARD.refusals()), 7)
        # The one launch that is let through, with and without the interpreter's options, and
        # with the environment this process has (its home is disposable).
        for command in ([sys.executable, str(script), str(marker), "plain"],
                        [sys.executable, "-E", "-s", str(script), str(marker), "isolated"],
                        [sys.executable.upper() if os.name == "nt" else sys.executable, "-X", "utf8", "-c",
                         "import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2])", str(marker), "code"]):
            for environment in (sandboxed, None):
                marker.unlink(missing_ok=True)
                done = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", check=False)
                self.assertEqual(done.returncode, 0, command)
                self.assertEqual(marker.read_text(encoding="utf-8"), command[-1])

    def test_a_process_python_starts_is_guarded_as_well(self):
        script = self.directory / "child.py"
        script.write_text(GUARDED_CHILD, encoding="utf-8")
        marker = self.directory / "started"
        result = subprocess.run(
            [sys.executable, "-E", "-s", str(script), json.dumps(SYSTEM_TOOLS), str(marker), GUARD_VARIABLE],
            env=sandboxed_environment(self.directory / "home"), capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
        outcome = json.loads(result.stdout)
        self.assertEqual([item[0] for item in outcome["refused"]], [*SYSTEM_TOOLS, "os.system", "os.execv"])
        self.assertEqual({item[1] for item in outcome["refused"]}, {"RealProcessRefused"})
        for tool, _, message in outcome["refused"][:len(SYSTEM_TOOLS)]:
            self.assertIn(repr(tool), message)
        self.assertEqual((outcome["reached"], outcome["grandchild"], outcome["guarded"]), (0, "started", True))
        # What the child refused is known here too: a test that swallowed the error fails the run.
        recorded = PROCESS_GUARD.refusals()
        self.assertEqual([item["program"] for item in recorded[:len(SYSTEM_TOOLS)]], list(SYSTEM_TOOLS))
        self.assertEqual(len(recorded), len(SYSTEM_TOOLS) + 2)
        self.assertNotIn(os.getpid(), {item["pid"] for item in recorded})

    def test_a_refusal_nobody_asked_for_fails_the_module(self):
        from test_isolation import client_test_sandbox as sandbox
        PROCESS_GUARD.remove()
        try:
            with self.assertRaisesRegex(AssertionError, "tried to start real programs: launchctl"):
                with sandbox():
                    try:
                        subprocess.run(["launchctl", "list"], check=False)
                    except BaseException:
                        pass  # as code that swallows whatever goes wrong would
        finally:
            PROCESS_GUARD.install()

    def test_a_script_is_run_by_the_guard_as_python_runs_it(self):
        script = self.directory / "main.py"
        (self.directory / "sibling.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")
        script.write_text("import json, sys, sibling\nprint(json.dumps([__name__, __file__, sys.argv, sys.path[0], sibling.VALUE]))\n"
                          "raise SystemExit(7)\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-E", "-s", str(script), "--flag", "a b"],
                                env=sandboxed_environment(self.directory / "home"), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["__main__", str(script), [str(script), "--flag", "a b"], str(self.directory), "sibling"])


class WindowsLocks:
    """`msvcrt.locking` as far as the client uses it: one byte at the current position of an open
    file, which no other handle can lock until this one unlocks it or is closed."""

    LK_UNLCK, LK_NBLCK = 0, 2

    def __init__(self):
        self.held: dict[tuple[int, int], int] = {}   # (file, position) -> the descriptor holding it
        self.calls: list[tuple[int, int]] = []       # (mode, position)

    def locking(self, descriptor: int, mode: int, count: int) -> None:
        assert count == 1
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        self.calls.append((mode, position))
        # Descriptors of closed files do not hold a byte any more.
        self.held = {key: holder for key, holder in self.held.items() if self.open(holder, key[0])}
        key = (os.fstat(descriptor).st_ino, position)
        if mode == self.LK_UNLCK:
            if self.held.get(key) != descriptor:
                raise OSError(errno.EACCES, "Permission denied")
            del self.held[key]
        elif key in self.held:
            raise OSError(errno.EACCES, "Permission denied")
        else:
            self.held[key] = descriptor

    @staticmethod
    def open(descriptor: int, file: int) -> bool:
        try:
            return os.fstat(descriptor).st_ino == file
        except OSError:
            return False

    def module(self) -> types.ModuleType:
        module = types.ModuleType("msvcrt")
        module.LK_UNLCK, module.LK_NBLCK, module.locking = self.LK_UNLCK, self.LK_NBLCK, self.locking
        return module


class WindowsLockTests(unittest.TestCase):
    """The run lock on its Windows branch: `os.name` is "nt", `fcntl` cannot be imported."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = SimpleNamespace(home=Path(temporary.name))
        self.root = cli.client_root(self.host)
        self.locks = WindowsLocks()
        for item in (patch.object(os, "name", "nt"), patch.dict(sys.modules, {"msvcrt": self.locks.module(), "fcntl": None})):
            item.start()
            self.addCleanup(item.stop)

    def test_a_run_holds_the_lock_and_gives_it_back(self):
        with sync_guard.sync_guard(self.root):
            with self.assertRaises(sync_guard.ClientBusy):
                with sync_guard.sync_guard(self.root):
                    self.fail("two runs at once")
        with sync_guard.sync_guard(self.root):
            pass
        # Every lock and unlock is of the same byte, the first one, which the file has to contain.
        self.assertEqual({position for _, position in self.locks.calls}, {0})
        self.assertEqual((self.root / "sync.lock").read_bytes(), b"0")
        self.assertEqual(self.locks.held, {})

    def test_the_command_line_client_and_a_run_exclude_each_other(self):
        with sync_guard.sync_guard(self.root):
            self.assertFalse(cli.sync_idle(self.host))
            with self.assertRaises(cli.Refusal) as refused:
                with cli.SyncLock(self.host):
                    self.fail("the job was stopped during a run")
            self.assertEqual(refused.exception.key, "disconnect_busy")
        with cli.SyncLock(self.host):
            with self.assertRaises(sync_guard.ClientBusy):
                with sync_guard.sync_guard(self.root):
                    self.fail("a run started while the job was being stopped")
        self.assertTrue(cli.sync_idle(self.host))
        self.assertEqual({position for _, position in self.locks.calls}, {0})
        self.assertEqual(self.locks.held, {})

    def test_the_guard_of_a_launcher_and_a_run_exclude_each_other(self):
        # The guard checks and restores the client under the lock a run holds, taken on the
        # same byte of the same file.
        with sync_guard.sync_guard(self.root):
            with self.assertRaises(sync_launch_guard.RunInProgress):
                with sync_launch_guard.run_lock(self.root, 0):
                    self.fail("the guard looked at the client during a run")
        with sync_launch_guard.run_lock(self.root, 0):
            self.assertFalse(cli.sync_idle(self.host))
            with self.assertRaises(sync_guard.ClientBusy):
                with sync_guard.sync_guard(self.root):
                    self.fail("a run started while the guard was looking at the client")
        self.assertTrue(cli.sync_idle(self.host))
        self.assertEqual({position for _, position in self.locks.calls}, {0})
        self.assertEqual((self.root / "sync.lock").read_bytes(), b"0")
        self.assertEqual(self.locks.held, {})

    def test_a_lock_first_taken_by_the_command_line_client_is_the_one_a_run_takes(self):
        with cli.SyncLock(self.host):
            self.assertEqual((self.root / "sync.lock").read_bytes(), b"0")
        with sync_guard.sync_guard(self.root):
            self.assertFalse(cli.sync_idle(self.host))


# Programs of Windows itself, under %SystemRoot%\System32. Windows looks for a program named
# without a path in the directory of the process that starts it before it looks in PATH, so a
# `schtasks.exe` or `powershell.exe` left in that directory runs instead of the real one, with
# the client's secret in its environment.
WINDOWS_TOOLS = frozenset({
    "attrib", "certutil", "cmd", "cmdkey", "cscript", "explorer", "icacls", "msiexec", "netsh", "powershell",
    "reg", "rundll32", "sc", "schtasks", "setx", "takeown", "taskkill", "tasklist", "where", "whoami", "wmic",
    "wscript",
})
# Commands with a bare system tool that this process does not start: (file, tool) -> why.
STARTED_BY_THE_SYSTEM = {
    ("sync_schedule.py", "powershell.exe"): "the command the hourly job's hidden launcher runs, started by the Task "
                                            "Scheduler in the system directory and pinned by tests/fixtures/client_launchers",
    ("sync_schedule.py", "wscript.exe"): "the command of the hourly job itself (/TR), started by the Task Scheduler in "
                                         "the system directory and pinned by tests/fixtures/client_launchers",
}
# Where a file of the product may start a program: the client, the adapters and the tools.
PRODUCT_SOURCES = sorted(path for path in [*ROOT.glob("adapters/**/*.py"), *ROOT.glob("tools/*.py")]
                         if "__pycache__" not in path.parts)
# What starts a program, by the name it is called by.
STARTERS = {"run", "Popen", "call", "check_call", "check_output", "system", "popen", "startfile"}


def bare_windows_tool(value: object) -> str | None:
    """The system tool a word names without a path, as the program of a command; None otherwise."""
    if not isinstance(value, str) or not value.strip():
        return None
    program = value.split()[0]
    if "\\" in program or "/" in program:
        return None
    return program if program.lower().removesuffix(".exe").removesuffix(".com") in WINDOWS_TOOLS else None


def windows_tools_by_bare_name(path: Path) -> list[str]:
    """Every command of a file whose program is a tool of Windows named without its full path:
    a list or tuple written with the tool first, and a command line handed to what starts a
    program."""
    found = []
    shown = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        words: list[ast.expr] = []
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            words = [node.elts[0]]
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
            if name in STARTERS:
                words = [node.args[0]]
        for word in words:
            program = bare_windows_tool(word.value if isinstance(word, ast.Constant) else None)
            if program and (path.name, program) not in STARTED_BY_THE_SYSTEM:
                found.append((node.lineno, f"{shown}:{node.lineno}: {program}"))
    return [line for _, line in sorted(found)]


def system_tool_helpers() -> dict[str, ast.FunctionDef]:
    """File -> its `windows_system_tool`, for every file of the product that has one."""
    found = {}
    for path in PRODUCT_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "windows_system_tool":
                found[path.relative_to(ROOT).as_posix()] = node
    return found


class WindowsSystemToolTests(unittest.TestCase):
    def test_no_file_of_the_product_starts_a_windows_tool_by_its_bare_name(self):
        found = [line for path in PRODUCT_SOURCES for line in windows_tools_by_bare_name(path)]
        self.assertEqual(found, [], "start it as windows_system_tool(name): the full path under %SystemRoot%\\System32")

    def test_the_check_sees_what_it_is_for(self):
        with tempfile.TemporaryDirectory(prefix="windows-tools-") as directory:
            sample = Path(directory) / "sample.py"
            sample.write_text(
                "import os, subprocess\n"
                "subprocess.run(['schtasks.exe', '/Query'])\n"
                "host.run([\"cmdkey\", \"/list\"], timeout=30)\n"
                "subprocess.check_output(('tasklist', '/NH'))\n"
                "LATER.append(['SCHTASKS', '/Delete'])\n"
                "subprocess.run('reg query HKCU', shell=True)\n"
                "os.system('taskkill /F /IM x.exe')\n"
                # None of these is a tool of Windows started by its bare name.
                "subprocess.run([windows_system_tool('schtasks.exe'), '/Query'])\n"
                "subprocess.run([r'C:\\\\Windows\\\\System32\\\\cmdkey.exe', '/list'])\n"
                "subprocess.run(['/usr/bin/security', 'list'])\n"
                "subprocess.run(['launchctl', 'list'])\n"
                "subprocess.run([sys.executable, '-c', 'pass'])\n"
                "subprocess.run(['git', 'status'])\n", encoding="utf-8")
            self.assertEqual([int(line.split(":")[1]) for line in windows_tools_by_bare_name(sample)], list(range(2, 8)))

    def test_every_file_resolves_a_windows_tool_the_same_way(self):
        # One helper in fact, though each set of files that is delivered on its own carries a
        # copy: the terminal client, the synchronization client, the first installer and the
        # installers of the apps never import across.
        helpers = system_tool_helpers()
        # At least four: the terminal client, the synchronization client, the first installer
        # and Kimi's.
        self.assertGreaterEqual(len(helpers), 4, sorted(helpers))
        self.assertEqual(len({ast.dump(node) for node in helpers.values()}), 1, sorted(helpers))

    def test_a_windows_tool_is_found_under_system_root_or_not_started_at_all(self):
        import ntpath
        helpers = system_tool_helpers()
        self.assertTrue(helpers)
        for name, node in helpers.items():
            namespace: dict = {"os": os, "ntpath": ntpath}
            exec(compile(ast.Module(body=[node], type_ignores=[]), name, "exec"), namespace)
            helper = namespace["windows_system_tool"]
            with self.subTest(name):
                with patch.dict(os.environ, {"SystemRoot": "C:\\Windows"}):
                    self.assertEqual(helper("schtasks.exe"), "C:\\Windows\\System32\\schtasks.exe")
                    self.assertEqual(helper("WindowsPowerShell", "v1.0", "powershell.exe"),
                                     "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
                # Without the variable, or with one that names no directory of a drive, the
                # system directory is unknown and nothing is started from wherever we are.
                for root in (None, "", "Windows", ".\\Windows", "\\Windows"):
                    environment = {key: value for key, value in os.environ.items() if key.upper() != "SYSTEMROOT"}
                    if root is not None:
                        environment["SystemRoot"] = root
                    with patch.dict(os.environ, environment, clear=True), self.assertRaises(OSError, msg=root):
                        helper("cmdkey.exe")


if __name__ == "__main__":
    unittest.main()
