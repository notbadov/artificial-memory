"""What the client says and writes is UTF-8, whatever the computer's code page.

Every caller starts the client and the runtime's tools as `python -E -s <file>` and reads what
they print as UTF-8. `-E` makes the interpreter ignore PYTHONUTF8 and PYTHONIOENCODING, so on a
Windows computer its streams are in the code page of the system (cp1251 on a Russian one) and
every Russian sentence reaches the terminal command and Desktop Companion as replacement
characters. The same default decides how a file opened without an encoding is read and
written, so a launcher written there was unreadable to the next reader, which opens it as
UTF-8.

The first half starts the real entry points in an interpreter whose streams are not UTF-8.
No Windows computer is needed for that: `legacy_start` finds what gives such an interpreter
here. The second half reads the sources, so that an entry point, a file or a child process
added later keeps the contract.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "adapters/client/scripts"
for directory in (SCRIPTS, ROOT / "tools"):
    sys.path.insert(0, str(directory))

import sync_client  # noqa: E402
import test_isolation  # noqa: E402
from test_isolation import client_test_sandbox, legacy_start  # noqa: E402

# Given to an interpreter as an argument, so written in ASCII: where the locale is C, the
# arguments of a process are not read as UTF-8 either.
SAY = "print('\\u0442\\u0435\\u0441\\u0442', flush=True)"
# The product files this contract is about: the client, the bootstrap of its runtime and the
# installers the client starts.
PRODUCT_FILES = sorted([*SCRIPTS.glob("*.py"), *(ROOT / "adapters/common").glob("*.py"),
                        *(ROOT / "tools").glob("install_*.py")])


def setUpModule() -> None:
    unittest.enterModuleContext(client_test_sandbox())


def legacy_environment(home: Path, variables: dict[str, str]) -> dict[str, str]:
    # The language of the messages is asked for by name, so it does not follow the locale
    # chosen for its encoding.
    return {**test_isolation.legacy_environment(home, variables), "ARTIFICIAL_MEMORY_LANG": "ru"}


class LegacyStreamsCase(unittest.TestCase):
    def setUp(self) -> None:
        found = legacy_start()
        if found is None:
            self.skipTest("every interpreter started here has UTF-8 streams")
        self.options, self.variables, self.encoding, self.utf8_names = found
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="client-encoding-")))

    def start(self, script: Path | str, *arguments: object, isolated: bool = True,
              **variables: str) -> subprocess.CompletedProcess:
        """A file of the product the way its callers start it, with nothing decoded."""
        command = [sys.executable, *(("-E", "-s") if isolated else ()), *self.options, str(script), *map(str, arguments)]
        return subprocess.run(command, capture_output=True, cwd=self.home, check=False, timeout=120,
                              env={**legacy_environment(self.home, self.variables), **variables})

    def utf8(self, output: bytes) -> str:
        try:
            return output.decode("utf-8")
        except UnicodeDecodeError:
            self.fail(f"not UTF-8 (the streams of this interpreter are {self.encoding}): {output[:400]!r}")


class EntryPointsSpeakUtf8Tests(LegacyStreamsCase):
    def test_the_interpreter_these_tests_start_does_not_speak_utf8_by_itself(self) -> None:
        # What every test below relies on: without the client's own doing, a Russian word
        # leaves this interpreter in other bytes or does not leave it at all.
        completed = self.start("-c", SAY)
        self.assertNotEqual(completed.stdout.strip(), "тест".encode("utf-8"))

    def test_the_answer_of_a_synchronization_is_utf8(self) -> None:
        # Refused before anything is sent: the address is not HTTPS.
        completed = self.start(
            SCRIPTS / "sync_client.py", "--client-id", "test-computer", "--control-plane-url", "http://service.test/agent-api",
            "--release-trust-file", self.home / "trust.json", "--client-token", "private-test-token", "--scheduler", "manual")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        answer = json.loads(self.utf8(completed.stdout).strip().splitlines()[-1])
        self.assertEqual((answer["status"], answer["phase"]), ("failed", "desired_state"))
        self.assertIn("Не удалось получить одобренную версию", answer["message"])

    def test_what_waits_for_the_owner_is_listed_in_utf8(self) -> None:
        waiting = {"version": 1, "items": [{"key": "skill:deploy:0123456789abcdef", "kind": "skill",
                                            "title": "Выкладка", "detail": "Навык с программами"}]}
        (self.home / "pending-consents.json").write_text(json.dumps(waiting, ensure_ascii=False), encoding="utf-8")
        completed = self.start(SCRIPTS / "sync_client.py", "--pending-consents", AGENT_PLATFORM_CONSENT_ROOT=str(self.home))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.utf8(completed.stdout)), waiting)

    def test_a_client_that_cannot_start_says_why_in_utf8(self) -> None:
        # The last line of this output is what an installer reports, and the lines before it
        # name files under the person's home, which on a Russian computer is often Cyrillic.
        # ... where this interpreter reads file names as UTF-8, as it does on Windows and macOS.
        client = self.home / ("клиент" if self.utf8_names else "client")
        client.mkdir()
        for name, relative in sync_client.runtime_files().items():
            shutil.copy2(ROOT / relative, client / name)
        (client / "sync_base.py").unlink()
        completed = self.start(client / "sync_client.py", "--self-check")
        self.assertNotEqual(completed.returncode, 0)
        said = self.utf8(completed.stderr)
        self.assertEqual(said.strip().splitlines()[-1], "ImportError: Client runtime is incomplete: sync_base.py")
        self.assertIn(client.name, said)

    def test_what_the_client_starts_from_a_release_speaks_utf8_too(self) -> None:
        # The installers of a release are started without -E and write into the client's own
        # output or are read by it; the client tells them the encoding the only way a process
        # it did not write can be told.
        code = ("import importlib.util, subprocess, sys\n"
                "spec = importlib.util.spec_from_file_location('entry_under_test', sys.argv[1])\n"
                "entry = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(entry)\n"
                "entry.start_in_utf8()\n"
                f"{SAY}\n"
                "subprocess.run([sys.executable, *sys.argv[2:], '-c', %r], check=True)\n" % SAY)
        completed = self.start("-c", code, SCRIPTS / "sync_client.py", *self.options)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.utf8(completed.stdout).split(), ["тест", "тест"])

    @unittest.skipUnless(sys.platform == "darwin" or os.name == "nt", "the installer of a runtime refuses any other system first")
    def test_the_installer_of_a_runtime_answers_in_utf8(self) -> None:
        completed = self.start(
            SCRIPTS / "companion_install.py", "--control-plane-url", "http://service.test/agent-api", "--client-id",
            "test-computer", "--display-name", "Компьютер", "--provider", "codex", "--release-trust-file",
            self.home / "trust.json", AGENT_PLATFORM_CLIENT_TOKEN="private-test-token")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        answer = json.loads(self.utf8(completed.stdout))
        self.assertEqual(answer["status"], "failed")
        self.assertIn("Control Plane требует HTTPS-адрес", answer["detail"])

    def test_the_terminal_command_speaks_utf8(self) -> None:
        # Nothing waits on a computer that was never connected, and saying so asks no system tool.
        completed = self.start(SCRIPTS / "command_line.py", "consents")
        self.assertRegex(self.utf8(completed.stdout) + self.utf8(completed.stderr), "[а-яё]{4}")

    def test_the_first_installer_says_why_it_stopped_in_utf8(self) -> None:
        # A copy of its own: it takes a lock next to itself.
        scripts = self.home / "bootstrap/adapters/client/scripts"
        scripts.mkdir(parents=True)
        for name in ("onboard_control_plane.py", "release_transport.py"):
            shutil.copy2(SCRIPTS / name, scripts / name)
        shutil.copytree(ROOT / "adapters/common", scripts.parents[1] / "common",
                        ignore=shutil.ignore_patterns("__pycache__"))
        completed = self.start(scripts / "onboard_control_plane.py", isolated=False, AGENT_PLATFORM_ONBOARDING="{}")
        self.assertEqual(completed.returncode, 1)
        self.assertRegex(self.utf8(completed.stderr), "Подключение не завершено|Для этого установщика")


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def called(call: ast.Call) -> tuple[str | None, str | None]:
    """(what is called, on which plain name) of a call: `open`, `path.open`, `subprocess.run`."""
    function = call.func
    if isinstance(function, ast.Name):
        return function.id, None
    if isinstance(function, ast.Attribute):
        return function.attr, function.value.id if isinstance(function.value, ast.Name) else None
    return None, None


# `.open(` that opens no file of this computer: (file, the call as written) -> why.
NOT_A_FILE = {
    ("cli_host.py", "urllib.request.build_opener(NoRedirect).open"): "an HTTP request",
    ("release_transport.py", "urllib.request.build_opener(NoRedirect).open"): "an HTTP request",
    ("control_plane_sync.py", "opener.open"): "an HTTP request",
    ("test_client_without_posix.py", "self.open"): "the stand-in for the locks of Windows",
}
# Modules whose `open` returns no text stream.
NO_TEXT_STREAM = {"os", "tarfile", "webbrowser", "zipfile"}
# Position of the mode among the arguments, and the mode when none is given.
OPENERS = {"open": (1, "r"), "fdopen": (1, "r"), "NamedTemporaryFile": (0, "w+b"),
           "TemporaryFile": (0, "w+b"), "SpooledTemporaryFile": (1, "w+b")}


def text_without_encoding(path: Path) -> list[str]:
    """Every place of a file that reads or writes text in the encoding of the computer."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name, owner = called(node)
        where = f"{path.name}:{node.lineno}"
        if name in ("read_text", "write_text"):
            # read_text(encoding) and write_text(data, encoding) take it by position as well.
            if keyword(node, "encoding") is None and len(node.args) <= (name == "write_text"):
                found.append(f"{where}: {name} without an encoding")
        elif name in OPENERS:
            if (name == "open" and owner in NO_TEXT_STREAM) or (path.name, ast.unparse(node.func)) in NOT_A_FILE:
                continue
            position, mode = OPENERS[name]
            if name == "open" and isinstance(node.func, ast.Attribute):
                position = 0  # path.open(mode)
            given = keyword(node, "mode") or (node.args[position] if len(node.args) > position else None)
            if given is not None:
                mode = given.value if isinstance(given, ast.Constant) and isinstance(given.value, str) else None
            if (mode is None or "b" not in mode) and keyword(node, "encoding") is None:
                found.append(f"{where}: {name} in text mode without an encoding")
        elif owner == "subprocess" and name in ("run", "Popen", "check_output", "check_call"):
            text = keyword(node, "text") or keyword(node, "universal_newlines")
            if text is not None and (keyword(node, "encoding") is None or keyword(node, "errors") is None):
                found.append(f"{where}: the output of a process is decoded without an encoding and an error rule")
    return found


def is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and ast.unparse(node.test) in ("__name__ == '__main__'", '__name__ == "__main__"'))


def first_call(body: list[ast.stmt]) -> str | None:
    """The name of what a block calls first, its documentation string aside."""
    for statement in body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            return ast.unparse(statement.value)
        return None
    return None


# Entry points that set their streams another way: file -> (the first call of main, why).
OWN_STREAMS = {
    "mcp_bridge.py": ("configure_stdio_utf8()", "the transport of an app: strict, and its input as well"),
}


class SourcesKeepTheContractTests(unittest.TestCase):
    def test_no_text_is_read_or_written_in_the_encoding_of_the_computer(self) -> None:
        found = [line for path in PRODUCT_FILES for line in text_without_encoding(path)]
        self.assertEqual(found, [], "name the encoding: UTF-8, or utf-8-sig for a file Windows tools write")

    def test_the_check_sees_what_it_is_for(self) -> None:
        with tempfile.TemporaryDirectory(prefix="client-encoding-") as directory:
            sample = Path(directory) / "sample.py"
            sample.write_text(
                "import os, subprocess, tempfile\nfrom pathlib import Path\n"
                "Path('a').write_text('x')\nPath('a').read_text()\nopen('a')\nopen('a', 'w')\nPath('a').open('a')\n"
                "os.fdopen(3, 'w')\ntempfile.NamedTemporaryFile('w')\nsubprocess.run(['x'], text=True)\n"
                "subprocess.run(['x'], capture_output=True, text=True, encoding='utf-8')\n"
                # None of these decodes anything by the computer's default.
                "Path('a').write_text('x', encoding='utf-8')\nPath('a').read_text('utf-8')\nopen('a', 'rb')\n"
                "Path('a').open('ab')\nos.fdopen(3, 'wb')\ntempfile.NamedTemporaryFile()\nos.open('a', 0)\n"
                "subprocess.run(['x'], capture_output=True)\n"
                "subprocess.run(['x'], text=True, encoding='utf-8', errors='replace')\n", encoding="utf-8")
            self.assertEqual([int(line.split(":")[1]) for line in text_without_encoding(sample)], list(range(3, 12)))

    def test_every_entry_point_sets_its_streams_before_it_says_anything(self) -> None:
        helpers = {}
        for path in PRODUCT_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if not any(is_main_guard(node) for node in tree.body):
                continue
            functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
            with self.subTest(entry=path.name):
                if path.name in OWN_STREAMS:
                    self.assertEqual(first_call(functions["main"].body), OWN_STREAMS[path.name][0])
                    continue
                self.assertIn("utf8_streams", functions)
                helpers[path.name] = ast.dump(functions["utf8_streams"])
                if path.name == sync_client.MANAGED_CLIENT_ENTRY:
                    # Before its parts are loaded: a part that does not load is reported in
                    # these streams. Its children are told in the same step.
                    self.assertEqual(first_call(functions["start_in_utf8"].body), "utf8_streams()")
                    guard = next(index for index, node in enumerate(tree.body) if is_main_guard(node))
                    self.assertEqual(first_call(tree.body[guard].body), "start_in_utf8()")
                    binding = next(index for index, node in enumerate(tree.body)
                                   if "bind_client_parts(globals())" in ast.unparse(node)
                                   and not isinstance(node, ast.FunctionDef))
                    self.assertLess(guard, binding)
                else:
                    self.assertEqual(first_call(functions["main"].body), "utf8_streams()")
        # One helper in fact, though each file has to carry it: the entry points are delivered
        # in three separate sets and none of them may import across.
        self.assertEqual(len(set(helpers.values())), 1, sorted(helpers))
        self.assertGreaterEqual(len(helpers), 10)


if __name__ == "__main__":
    unittest.main()
