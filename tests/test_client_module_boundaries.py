"""The seams of the synchronization client: what a file of it may use, import and say.

The installed client is a flat directory that the client already on the computer fills from a
release, and its entry point (`sync_client.py`) is being split into part files. A function
moved to a part still compiles when it uses a name that stayed behind, and the self-check
still passes: the mistake shows as a `NameError` in the middle of a run on a computer. These
tests read the files and start them in both layouts, so that it shows here instead.

What they do not see: a name that exists but is defined after its first use, or only on one
branch; a module loaded by path with `importlib` (those are named next to the code that loads
them, and `tests/test_managed_client_runtime.py` installs and starts the whole set).
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import string
import subprocess
import symtable
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "adapters/client/scripts"
for directory in (SCRIPTS, ROOT / "tools", ROOT / "tests"):
    sys.path.insert(0, str(directory))

import sync_client  # noqa: E402
from test_isolation import client_test_sandbox, sandboxed_environment  # noqa: E402
from test_managed_client_runtime import release_tree  # noqa: E402

ENTRY = sync_client.MANAGED_CLIENT_ENTRY
ORCHESTRATOR = SCRIPTS / "control_plane_sync.py"
# Names every module has without defining them.
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__loader__", "__builtins__"}

# Loads the entry point given as the first argument the way a caller that is not the scheduler
# does: by path, with neither its directory on the path nor it as the working directory.
LOAD_ENTRY = """
import argparse, importlib.util, json, sys
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("entry_under_test", sys.argv[1])
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
"""


def setUpModule() -> None:
    global PROCESS_GUARD
    PROCESS_GUARD = unittest.enterModuleContext(client_test_sandbox())


def run_python(code: str, *arguments: object, home: Path) -> subprocess.CompletedProcess:
    """Client code in an interpreter of its own, isolated like every run of the client: no
    PYTHON* variables, no user site packages, and a home that is not the person's."""
    return subprocess.run(
        [sys.executable, "-E", "-s", "-c", code, *map(str, arguments)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=home, check=False, timeout=120, env=sandboxed_environment(home))


def run_entry(entry: Path, option: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-E", "-s", str(entry), option], capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=entry.parent, check=False, timeout=120, env=sandboxed_environment(home))


def client_sources() -> dict[str, Path]:
    """Installed name -> source in this tree, for everything an installed client is made of."""
    return {name: ROOT / relative for name, relative in sync_client.runtime_files().items()}


def part_sources() -> dict[str, Path]:
    return {name: ROOT / relative for name, relative in sync_client.managed_client_parts().items()}


def bound_names() -> set[str]:
    return {name for names in sync_client.MANAGED_CLIENT_PARTS.values() for name in names}


def scopes(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from scopes(child)


def module_level_names(source: str) -> set[str]:
    """Every name the module itself binds: assignments, functions, classes, imports, and what
    a function declares `global` and assigns."""
    top = symtable.symtable(source, "<client file>", "exec")
    names = {symbol.get_name() for symbol in top.get_symbols()
             if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
    for table in scopes(top):
        if table is not top:
            names |= {symbol.get_name() for symbol in table.get_symbols()
                      if symbol.is_global() and symbol.is_assigned()}
    return names


def undefined_globals(source: str, bound: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Global names the file reads anywhere (functions, classes, comprehensions, annotations)
    and binds nowhere. `bound` is what the file is given from outside: for the entry point, the
    names it takes from its parts. The compiler's own scope analysis decides what is global."""
    top = symtable.symtable(source, "<client file>", "exec")
    used = set()
    for table in scopes(top):
        for symbol in table.get_symbols():
            if symbol.is_referenced() and (table is top or symbol.is_global()):
                used.add(symbol.get_name())
    return sorted(used - module_level_names(source) - set(bound) - set(dir(builtins)) - MODULE_NAMES)


def imported_modules(source: str) -> list[tuple[str, bool]]:
    """(top-level module, imported inside a function) for every import statement of the file.
    A relative import is reported as ".": a flat client directory is not a package."""
    found: list[tuple[str, bool]] = []

    def visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.extend((alias.name.split(".")[0], inside_function) for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                found.append((".", inside_function) if child.level or not child.module
                             else (child.module.split(".")[0], inside_function))
            visit(child, inside_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    visit(ast.parse(source), False)
    return found


def not_standard(modules) -> set[str]:
    return {module for module in modules if module not in sys.stdlib_module_names}


def binding_problems(entry_source: str, declared: dict[str, tuple[str, ...]], parts: dict[str, str]) -> list[str]:
    """What is wrong with how an entry point takes names from its parts, as sentences."""
    problems = []
    entry_names = module_level_names(entry_source)
    taken: dict[str, str] = {}
    for file_name, names in declared.items():
        defined = module_level_names(parts[file_name])
        for name in names:
            if name not in defined:
                problems.append(f"{file_name} does not define {name}")
            if name in entry_names:
                problems.append(f"{name} is taken from {file_name} and still defined in the entry point")
            if name in taken:
                problems.append(f"{name} is taken from both {taken[name]} and {file_name}")
            taken[name] = file_name
    return problems


def part_import_problems(declared: dict[str, tuple[str, ...]], parts: dict[str, str], siblings: set[str]) -> list[str]:
    """A part imports the standard library, the client's own modules and the parts declared
    before it: never the entry point, never a part declared after it."""
    problems = []
    entry = Path(ENTRY).stem
    earlier: set[str] = set()
    for file_name in declared:
        for module in sorted(not_standard(module for module, _ in imported_modules(parts[file_name]))):
            if module == entry:
                problems.append(f"{file_name} imports the entry point")
            elif module not in earlier | siblings:
                problems.append(f"{file_name} imports {module}")
        earlier.add(Path(file_name).stem)
    return problems


def orchestrator_names(source: str) -> set[str]:
    """Every name the orchestrator reads from the entry point it is given as `sync`: written
    as `sync.name`, or asked for with `getattr(sync, "name", ...)` when an older entry point
    may lack it."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sync":
            names.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "sync"
              and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            names.add(node.args[1].value)
    return names


class NameTests(unittest.TestCase):
    def test_no_file_of_the_client_reads_a_global_name_it_does_not_have(self):
        files = {**client_sources(), **{path.name: path for path in SCRIPTS.glob("*.py")}}
        self.assertIn(ENTRY, files)
        self.assertLessEqual(set(sync_client.MANAGED_CLIENT_PARTS), set(files))
        for name, path in sorted(files.items()):
            with self.subTest(file=name):
                given = bound_names() if name == ENTRY else frozenset()
                self.assertEqual(undefined_globals(path.read_text(encoding="utf-8"), given), [])

    def test_the_check_sees_a_name_that_stayed_behind_when_a_function_moved(self):
        part = ("import os\n\n\n"
                "def moved(path: Annotated) -> Returned:\n"
                "    limit: Local = LIMIT\n"
                "    return [helper(item) for item in os.listdir(path)][:limit] + (lambda: later)()\n\n\n"
                "class Kept(Base):\n"
                "    value = default()\n")
        self.assertEqual(undefined_globals(part),
                         ["Annotated", "Base", "LIMIT", "Local", "Returned", "default", "helper", "later"])
        whole = ("from typing import Annotated, Any as Returned\n"
                 "LIMIT = 3\nLocal = Base = object\n"
                 "def helper(item): return item\n"
                 "def default():\n    global later\n    later = 1\n") + part
        self.assertEqual(undefined_globals(whole), [])
        # The entry point after the move: it has the name only because a part gives it.
        entry = "def run():\n    return moved('.')\n"
        self.assertEqual(undefined_globals(entry), ["moved"])
        self.assertEqual(undefined_globals(entry, {"moved"}), [])

    def test_every_name_taken_from_a_part_lives_there_and_only_there(self):
        declared = sync_client.MANAGED_CLIENT_PARTS
        parts = {name: path.read_text(encoding="utf-8") for name, path in part_sources().items()}
        entry = (SCRIPTS / ENTRY).read_text(encoding="utf-8")
        self.assertEqual(binding_problems(entry, declared, parts), [])

    def test_the_check_sees_a_name_that_was_copied_or_is_not_there(self):
        entry = "def stays():\n    return 1\n\n\ndef copied():\n    return 2\n"
        parts = {"sync_one.py": "def copied():\n    return 2\n", "sync_two.py": "def copied():\n    return 5\n"}
        declared = {"sync_one.py": ("copied", "absent"), "sync_two.py": ("copied",)}
        self.assertEqual(binding_problems(entry, declared, parts), [
            "copied is taken from sync_one.py and still defined in the entry point",
            "sync_one.py does not define absent",
            "copied is taken from sync_two.py and still defined in the entry point",
            "copied is taken from both sync_one.py and sync_two.py",
        ])
        self.assertEqual(binding_problems("def stays():\n    return 1\n", {"sync_two.py": ("copied",)}, parts), [])


class OrchestratorTests(unittest.TestCase):
    """`control_plane_sync.run` is given the entry point's globals; nothing else is in reach."""

    def test_every_name_the_orchestrator_reads_exists_on_the_entry_point(self):
        names = orchestrator_names(ORCHESTRATOR.read_text(encoding="utf-8"))
        # Both spellings are read: a plain attribute and one an older entry point may lack.
        for expected in ("shutil", "ClientRuntimeProbeFailed", "apply_instruction_rules", "record_current_connection"):
            self.assertIn(expected, names)
        self.assertEqual(sorted(name for name in names if not hasattr(sync_client, name)), [])

    def test_the_orchestrator_is_given_the_names_the_parts_brought(self):
        source = (SCRIPTS / ENTRY).read_text(encoding="utf-8")
        # Taken into the module's own globals while it is imported, which is what is handed over.
        self.assertIn("\n    bind_client_parts(globals())\n", source)
        self.assertIn("return module.run(args, desired, SimpleNamespace(**globals()))", source)
        self.assertLess(source.index("bind_client_parts(globals())"), source.index("\ndef parse_args("))


class PartRuleTests(unittest.TestCase):
    def test_a_part_is_a_flat_file_of_the_client_under_a_name_of_its_own(self):
        declared = sync_client.MANAGED_CLIENT_PARTS
        others = {ENTRY, *sync_client.MANAGED_CLIENT_MODULES, *sync_client.MANAGED_CLIENT_TOOLS}
        for name, path in part_sources().items():
            with self.subTest(part=name):
                self.assertRegex(name, r"\Async_[a-z0-9_]+\.py\Z")
                # A bare name would hide a module of the standard library from the whole client.
                self.assertNotIn(Path(name).stem, sys.stdlib_module_names)
                self.assertNotIn(name, others)
                self.assertNotIn(name, sync_client.RETIRED_CLIENT_FILES)
                self.assertEqual(path, SCRIPTS / name)
                self.assertTrue(path.is_file())
                self.assertTrue(declared[name], "a part nothing is taken from")
        self.assertEqual(set(sync_client.managed_client_files()), others | set(declared))
        self.assertEqual(set(sync_client.runtime_files()), others | set(declared))

    def test_what_the_arguments_write_out_is_what_the_part_has(self):
        # The declaration of a release parses the arguments with no part bound, so the parser
        # cannot read the layouts and the Keychain service from the part that owns them.
        with patch.object(sync_client.argparse.ArgumentParser, "parse_args", lambda parser: parser):
            options = {action.dest: action for action in sync_client.parse_args()._actions}
        self.assertEqual(tuple(options["schedule_layout"].choices), sync_client.SCHEDULE_LAYOUTS)
        self.assertEqual(options["schedule_layout"].default, sync_client.SCHEDULE_LAYOUTS[0])
        self.assertEqual(options["keychain_service"].default, sync_client.KEYCHAIN_SERVICE)
        source = (SCRIPTS / ENTRY).read_text(encoding="utf-8")
        parser = source[source.index("def parse_args("):source.index("def default_client_id(")]
        read = {node.id for node in ast.walk(ast.parse(parser)) if isinstance(node, ast.Name)}
        taken = {name for names in sync_client.MANAGED_CLIENT_PARTS.values() for name in names}
        self.assertEqual(read & taken, set())

    def test_no_part_joins_the_release_gate(self):
        # Each name in the gate is a contract with every client that ever shipped; a part stays
        # free to be renamed because the installer asks the release which files it has.
        gate = ORCHESTRATOR.read_text(encoding="utf-8")
        for relative in sync_client.managed_client_parts().values():
            self.assertNotIn(relative, gate)

    def test_no_file_of_the_client_imports_the_entry_point(self):
        # It runs as __main__: `import sync_client` anywhere would load a second copy with its
        # own state. The terminal client starts the installed one as a process instead.
        entry = Path(ENTRY).stem
        for name, path in sorted({**client_sources(), **{path.name: path for path in SCRIPTS.glob("*.py")}}.items()):
            with self.subTest(file=name):
                modules = {module for module, _ in imported_modules(path.read_text(encoding="utf-8"))}
                self.assertNotIn(entry, modules)
                self.assertNotIn(".", modules)

    def test_a_part_imports_only_what_is_declared_before_it(self):
        siblings = {Path(name).stem for name in (*sync_client.MANAGED_CLIENT_MODULES, *sync_client.MANAGED_CLIENT_TOOLS)}
        parts = {name: path.read_text(encoding="utf-8") for name, path in part_sources().items()}
        self.assertEqual(part_import_problems(sync_client.MANAGED_CLIENT_PARTS, parts, siblings), [])

    def test_the_check_sees_a_part_that_reaches_the_wrong_way(self):
        parts = {"sync_base.py": "import os\nfrom sync_rules import blocks\n",
                 "sync_rules.py": "from sync_base import token\n\n\ndef late():\n    import sync_client\n    import requests\n"
                                  "    from release_transport import request_json\n    from . import sibling\n"}
        declared = {"sync_base.py": ("token",), "sync_rules.py": ("blocks",)}
        self.assertEqual(part_import_problems(declared, parts, {"release_transport"}), [
            "sync_base.py imports sync_rules", "sync_rules.py imports .", "sync_rules.py imports requests",
            "sync_rules.py imports the entry point"])

    def test_a_file_of_the_client_needs_only_the_client_to_be_imported(self):
        # While a file is imported it may reach the standard library and the other files of the
        # client: the self-check imports each of them in a directory that holds nothing else.
        # `common` is the package a release keeps safe_tar in, asked only when the flat copy is
        # not next to the file.
        sources = client_sources()
        client = {Path(name).stem for name in sources}
        outside: set[tuple[str, str, str]] = set()
        for name in sorted(sources):
            for module, inside in imported_modules(sources[name].read_text(encoding="utf-8")):
                if module not in sys.stdlib_module_names and module not in client | {"common"}:
                    outside.add((name, module, "inside a function" if inside else "while imported"))
        # Everything else a file of the client reaches for. Each line is a decision: the keys of
        # the app agents and the signature of a release need the interpreter's cryptography,
        # which every interpreter that runs the client is given.
        self.assertEqual(outside, {("agent_identity.py", "cryptography", "while imported"),
                                   ("release_transport.py", "cryptography", "inside a function")})


def message_calls(source: str) -> list[tuple[str | None, set[str], int]]:
    """(key, names of the values, line) for every call of `text(...)` or `sync.text(...)` in a
    file. The key is None when it is not written as a literal."""
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if not ((isinstance(called, ast.Name) and called.id == "text")
                or (isinstance(called, ast.Attribute) and called.attr == "text"
                    and isinstance(called.value, ast.Name) and called.value.id in ("sync", "sync_messages"))):
            continue
        first = node.args[0] if node.args else None
        key = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        calls.append((key, {keyword.arg for keyword in node.keywords}, node.lineno))
    return calls


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


# Every table of messages, each with both languages. The parts of the synchronization client
# share one (`sync_messages.py`) and the terminal client has its own. The two files that connect
# a computer for the first time are delivered by the service alone, without any part of the
# client, so each says what it says from a table of its own:
# the transport, which every tool that installs a release loads by itself, and the installer.
CATALOGS = ("sync_messages.py", "cli_messages.py", "release_transport.py", "onboard_control_plane.py")
OWN_TABLES = ("release_transport.py", "onboard_control_plane.py")
# Every file of the client: any of them may say something to a person, in a sentence it raises,
# prints, returns into an answer or shows as its help, so none may hold a Russian sentence of its
# own. The hooks that keep agents to their learning gate (`adapters/*/scripts/learning_gate_hook.py`)
# match Russian on purpose and are not part of the client.
def client_script_files() -> list[Path]:
    return sorted(SCRIPTS.rglob("*.py"))


# Russian words the client keeps outside a table, each with why. None is said to a person by
# the client in the language of the computer, and each must stay the same on every computer.
RUSSIAN_DATA = {
    ("control_plane_sync.py", "Установка проектного профиля не завершена"):
        "`error` of a project profile in the report; the service keeps it and shows a profile's status, not this",
    ("control_plane_sync.py", "Синхронизация не завершена"):
        "`commandResult.error` in the report; the service keeps a command's result once and refuses a retry that "
        "differs from it, and a run started by the scheduler may see another language than one started in a terminal",
    ("cli_engine.py", "д"): "a reply a person types to the consent question, accepted whatever the language",
    ("cli_engine.py", "да"): "a reply a person types to the consent question, accepted whatever the language",
}
CYRILLIC = re.compile("[\u0400-\u04ff]")


def catalog(path: Path) -> dict[str, tuple[str, str]] | None:
    """The table `MESSAGES` of a file, read from its source and not imported: a file of the
    bootstrap imports what lies next to it on a computer, not here."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "MESSAGES" for target in targets):
            return ast.literal_eval(node.value)
    return None


def russian_outside_a_table(source: str) -> list[tuple[int, str, bool]]:
    """(line, literal, whether it is raised or printed) for every literal with a Russian letter
    that is not in the file's table of messages, f-strings included."""
    tree = ast.parse(source)
    table = {id(node) for statement in tree.body
             if isinstance(statement, (ast.Assign, ast.AnnAssign))
             and any(isinstance(target, ast.Name) and target.id == "MESSAGES"
                     for target in (statement.targets if isinstance(statement, ast.Assign) else [statement.target]))
             for node in ast.walk(statement)}
    said = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            said |= {id(inner) for inner in ast.walk(node.exc)}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            said |= {id(inner) for argument in [*node.args, *node.keywords] for inner in ast.walk(argument)}
    return sorted((node.lineno, node.value, id(node) in said) for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and CYRILLIC.search(node.value) and id(node) not in table)


class MessageTests(unittest.TestCase):
    """What the client says to a person comes from one table with both languages."""

    CYRILLIC = re.compile("[\u0400-\u04ff]")

    def setUp(self) -> None:
        self.messages = sync_client.text.__globals__

    def test_the_two_clients_decide_the_language_the_same_way(self):
        # They are released apart and cannot share a file; a person must not read English from
        # one and Russian from the other on the same computer.
        def language_function(path: Path) -> str:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "language"]
            self.assertEqual(len(found), 1, path.name)
            body = found[0].body
            if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # the description of the function is each client's own
            return ast.dump(ast.Module(body=body, type_ignores=[]))

        self.assertEqual(language_function(SCRIPTS / "sync_messages.py"), language_function(SCRIPTS / "cli_messages.py"))
        # The transport travels in a release and alone in the bootstrap; the first installer
        # asks it which language the computer has.
        self.assertEqual(language_function(SCRIPTS / "release_transport.py"), language_function(SCRIPTS / "cli_messages.py"))

    def test_the_language_follows_the_computer(self):
        table = {"fixture": ("release {release_id} is here", "релиз {release_id} здесь")}
        cases = (({"ARTIFICIAL_MEMORY_LANG": "ru"}, "релиз 7 здесь"), ({"ARTIFICIAL_MEMORY_LANG": "en"}, "release 7 is here"),
                 ({"LANG": "ru_RU.UTF-8"}, "релиз 7 здесь"), ({"LANG": "de_DE.UTF-8"}, "release 7 is here"),
                 ({"LC_ALL": "C", "LANG": "ru_RU.UTF-8"}, "release 7 is here"))
        cleared = {name: "" for name in ("ARTIFICIAL_MEMORY_LANG", "LC_ALL", "LC_MESSAGES", "LANG")}
        for environment, expected in cases:
            with self.subTest(environment=environment), patch.dict(os.environ, {**cleared, **environment}), \
                    patch.dict(self.messages["MESSAGES"], table):
                self.assertEqual(sync_client.text("fixture", release_id=7), expected)
        for system, expected in (("ru_RU", "ru"), ("Russian_Russia", "ru"), ("en_US", "en"), (None, "en")):
            with self.subTest(system=system), patch.dict(os.environ, cleared), \
                    patch.object(self.messages["locale"], "getlocale", return_value=(system, "UTF-8")):
                self.assertEqual(sync_client.language(), expected)

    def test_every_message_has_both_languages_and_the_same_values(self):
        for key, pair in sync_client.MESSAGES.items():
            with self.subTest(message=key):
                self.assertRegex(key, r"\A[a-z][a-z0-9_]*\Z")
                self.assertEqual((type(pair), len(pair)), (tuple, 2))
                english, russian = pair
                self.assertTrue(english.strip() and russian.strip())
                self.assertIsNone(self.CYRILLIC.search(english))
                self.assertIsNotNone(self.CYRILLIC.search(russian))
                self.assertEqual(placeholders(english), placeholders(russian))

    def test_every_catalog_has_both_languages_for_every_key(self):
        for name in CATALOGS:
            table = catalog(SCRIPTS / name)
            with self.subTest(file=name):
                self.assertTrue(table, f"{name} has no table of messages")
            for key, pair in (table or {}).items():
                with self.subTest(file=name, message=key):
                    self.assertRegex(key, r"\A[a-z][a-z0-9_]*\Z")
                    self.assertEqual((type(pair), len(pair)), (tuple, 2))
                    english, russian = pair
                    self.assertTrue(english.strip() and russian.strip())
                    self.assertIsNone(CYRILLIC.search(english))
                    if english != russian:  # a name, the same in both languages
                        self.assertIsNotNone(CYRILLIC.search(russian))
                    self.assertEqual(placeholders(english), placeholders(russian))

    def test_no_error_or_status_is_said_in_russian_outside_a_table(self):
        files = client_script_files()
        self.assertIn(SCRIPTS / "mcp_bridge.py", files)
        self.assertFalse([path for path in files if path.name == "learning_gate_hook.py"])
        found = {}
        for path in files:
            name = path.relative_to(SCRIPTS).as_posix()
            for line, literal, said in russian_outside_a_table(path.read_text(encoding="utf-8")):
                with self.subTest(file=name, line=line):
                    self.assertFalse(said, f"{name}:{line} raises or prints a Russian sentence: {literal!r}")
                    self.assertIn((name, literal), RUSSIAN_DATA, f"{name}:{line} says {literal!r} outside the table")
                found[name, literal] = line
        self.assertEqual(sorted(set(RUSSIAN_DATA) - set(found)), [], "Russian data that is no longer there")

    def test_the_check_sees_russian_that_is_raised_printed_or_kept(self):
        source = ('MESSAGES = {"one": ("one", "один")}\n'
                  'def run(code):\n'
                  '    if code:\n'
                  '        raise ValueError(f"код {code}")\n'
                  '    print(json.dumps({"message": "готово"}), file=sys.stderr)\n'
                  '    return {"error": "ошибка", "english": "fine"}\n')
        self.assertEqual(russian_outside_a_table(source), [(4, "код ", True), (5, "готово", True), (6, "ошибка", False)])

    def test_every_message_that_is_asked_for_exists_and_is_given_its_values(self):
        for name in OWN_TABLES:
            table = catalog(SCRIPTS / name) or {}
            used: set[str] = set()
            for key, values, line in message_calls((SCRIPTS / name).read_text(encoding="utf-8")):
                with self.subTest(file=name, line=line, message=key):
                    self.assertIsNotNone(key, f"{name}:{line} asks for a message by a computed key")
                    self.assertIn(key, table)
                    self.assertEqual(values, placeholders(table[key][0]))
                used.add(key)
            self.assertEqual(sorted(set(table) - used), [], f"messages nothing says in {name}")
        used = set()
        files = {name: path for name, path in {**client_sources(), **part_sources()}.items() if name not in OWN_TABLES}
        for name, path in sorted(files.items()):
            for key, values, line in message_calls(path.read_text(encoding="utf-8")):
                with self.subTest(file=name, line=line, message=key):
                    self.assertIsNotNone(key, f"{name}:{line} asks for a message by a computed key")
                    self.assertIn(key, sync_client.MESSAGES)
                    self.assertEqual(values, placeholders(sync_client.MESSAGES[key][0]))
                used.add(key)
        self.assertEqual(sorted(set(sync_client.MESSAGES) - used), [], "messages nothing says")

    def test_the_check_sees_a_message_that_is_missing_or_computed(self):
        source = ("from sync_messages import text\n\n\n"
                  "def run(sync, key):\n"
                  "    return text('one', release_id=1), sync.text('two'), text(key), other.text('three')\n")
        self.assertEqual(message_calls(source), [("one", {"release_id"}, 5), ("two", set(), 5), (None, set(), 5)])


class TerminalPartTests(unittest.TestCase):
    """The terminal client is one file that is started and parts next to it, delivered together
    in the signed runtime. The parts are imported, not bound: the runtime is one archive, so a
    part is never there without the others."""

    ORDER = ("cli_messages", "cli_host", "cli_credentials", "cli_engine", "cli_connection", "cli_leaving")
    STARTED = "command_line"

    def sources(self) -> dict[str, str]:
        return {path.stem: path.read_text(encoding="utf-8")
                for path in (*sorted(SCRIPTS.glob("cli_*.py")), SCRIPTS / f"{self.STARTED}.py")}

    def test_every_part_is_known_and_travels_in_the_runtime(self):
        self.assertEqual(sorted(self.sources()), sorted((*self.ORDER, self.STARTED)))
        runtime = json.loads((ROOT / "adapters/client/runtime/sources.json").read_text(encoding="utf-8"))
        delivered = {Path(relative).name: relative for relative in runtime["bootstrap"]}
        self.assertEqual(len(delivered), len(runtime["bootstrap"]), "two files of the runtime under one name")
        for name in self.sources():
            self.assertEqual(delivered.get(f"{name}.py"), f"adapters/client/scripts/{name}.py")

    def test_a_part_imports_only_the_standard_library_and_the_parts_before_it(self):
        for name, source in self.sources().items():
            earlier = set(self.ORDER if name == self.STARTED else self.ORDER[:self.ORDER.index(name)])
            for module, _ in imported_modules(source):
                with self.subTest(file=name, module=module):
                    self.assertTrue(module in sys.stdlib_module_names or module == "__future__" or module in earlier)

    def test_a_name_lives_in_one_file_and_the_started_file_takes_only_what_it_uses(self):
        import command_line

        def defined(source: str):
            for node in ast.parse(source).body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    yield node.name
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        for element in (target.elts if isinstance(target, ast.Tuple) else [target]):
                            if isinstance(element, ast.Name):
                                yield element.id

        home: dict[str, str] = {}
        for file, source in self.sources().items():
            for name in defined(source):
                self.assertNotIn(name, home, f"{name} is defined in {file} and in {home.get(name)}")
                home[name] = file
        self.assertGreater(len(home), 130)
        # The started file is no catalogue of its parts: a caller imports a name from the part
        # that defines it, and the file takes only what it uses (the public pipeline checks that
        # with ruff). What it takes is the part's own object, not a copy.
        for name, value in vars(command_line).items():
            if name in home and not name.startswith("__"):
                part = sys.modules[home[name].removesuffix(".py")]
                self.assertIs(value, getattr(part, name), f"{name} of {home[name]} is not the part's own")


class TwoLayouts(unittest.TestCase):
    """The client as a release has it and as a computer has it, built once for the class."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.home = directory / "home"
        cls.home.mkdir()
        cls.release = release_tree(directory / "release")
        cls.release_scripts = cls.release / "adapters/client/scripts"
        cls.installed = directory / "installed"
        sync_client.install_managed_runtime(cls.release, cls.installed)

    def fresh_home(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))


class OutputTests(TwoLayouts):
    """Desktop Companion reads the whole self-check output as one JSON document, and the
    installing client reads the last line of the declaration: a module that prints while it is
    imported breaks the first and, on an error stream nobody reads, hides a warning."""

    IMPORT = ("import importlib.util, sys\n"
              "spec = importlib.util.spec_from_file_location('imported_for_its_output', sys.argv[1])\n"
              "module = importlib.util.module_from_spec(spec)\n"
              "sys.path.insert(0, sys.argv[2])\n"
              "spec.loader.exec_module(module)\n")

    def test_no_file_of_the_client_writes_anything_while_it_is_imported(self):
        files = [*sorted(self.installed.glob("*.py")), *sorted(SCRIPTS.glob("*.py"))]
        self.assertGreaterEqual(len(files), len(sync_client.managed_client_files()) + 4)
        for path in files:
            layout = "installed" if path.parent == self.installed else "release"
            with self.subTest(file=path.name, layout=layout):
                result = run_python(self.IMPORT, path, path.parent, home=self.home)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_the_self_check_answers_with_one_document_and_what_it_always_said(self):
        result = run_entry(self.installed / ENTRY, "--self-check", self.home)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(json.loads(result.stdout), {
            "status": "ok",
            "modules": ["agent_identity.py", "control_plane_sync.py", "mcp_bridge.py", "release_transport.py",
                        "safe_tar.py", "sync_guard.py"],
            "complete": True, "missing": [],
            "capabilities": ["schedulerCompanion", "disconnect", "appProviders", "consents", "schedulerManual",
                             "terminalSchedule", "releaseRecovery", "repairOnRequest", "cursorApp"],
        })

    def test_the_release_declares_the_files_it_always_declared_and_its_parts(self):
        result = run_entry(self.release_scripts / ENTRY, "--runtime-files", self.home)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout.count("\n"), 1)
        declared = json.loads(result.stdout)
        # The installing client reads "files", Desktop Companion's installer "capabilities".
        self.assertEqual(list(declared), ["files", "capabilities"])
        self.assertEqual(declared["files"], {
            "sync_client.py": "adapters/client/scripts/sync_client.py",
            "sync_guard.py": "adapters/client/scripts/sync_guard.py",
            "release_transport.py": "adapters/client/scripts/release_transport.py",
            "control_plane_sync.py": "adapters/client/scripts/control_plane_sync.py",
            "safe_tar.py": "adapters/common/safe_tar.py",
            "agent_identity.py": "adapters/client/scripts/agent_identity.py",
            "mcp_bridge.py": "adapters/client/scripts/mcp_bridge.py",
            "install_project_context_mcp.py": "tools/install_project_context_mcp.py",
            "install_agent_memory_mcp.py": "tools/install_agent_memory_mcp.py",
            "install_managed_mcp_servers.py": "tools/install_managed_mcp_servers.py",
            "install_app_agent_channels.py": "tools/install_app_agent_channels.py",
            # The parts of the entry point, in the order it binds them.
            "sync_messages.py": "adapters/client/scripts/sync_messages.py",
            "sync_launch_guard.py": "adapters/client/scripts/sync_launch_guard.py",
            "sync_base.py": "adapters/client/scripts/sync_base.py",
            "sync_rules.py": "adapters/client/scripts/sync_rules.py",
            "sync_consents.py": "adapters/client/scripts/sync_consents.py",
            "sync_recovery.py": "adapters/client/scripts/sync_recovery.py",
            "sync_projects.py": "adapters/client/scripts/sync_projects.py",
            "sync_launchers.py": "adapters/client/scripts/sync_launchers.py",
            "sync_schedule.py": "adapters/client/scripts/sync_schedule.py",
            "sync_disconnect.py": "adapters/client/scripts/sync_disconnect.py",
            "sync_codex_versions.py": "adapters/client/scripts/sync_codex_versions.py",
            "sync_mcp.py": "adapters/client/scripts/sync_mcp.py",
            "sync_app_agents.py": "adapters/client/scripts/sync_app_agents.py",
            "sync_skills.py": "adapters/client/scripts/sync_skills.py",
            "sync_release.py": "adapters/client/scripts/sync_release.py",
        })
        self.assertEqual(list(declared["files"])[-15:], list(sync_client.MANAGED_CLIENT_PARTS))
        for name, relative in declared["files"].items():
            self.assertRegex(name, r"\A[A-Za-z0-9_]+\.py\Z")
            self.assertTrue((self.release / relative).is_file(), relative)


class LayoutTests(TwoLayouts):
    """A module imported inside a function is not looked for until the function runs, so the
    self-check cannot vouch for it. The first run on a computer is the release's own entry
    point (Desktop Companion's installer and the terminal client start it); every later run
    is the installed one."""

    def lazily_imported(self) -> list[str]:
        sources = [SCRIPTS / ENTRY, *part_sources().values()]
        modules = {module for path in sources
                   for module, inside in imported_modules(path.read_text(encoding="utf-8")) if inside}
        return sorted(not_standard(modules))

    def test_what_the_entry_point_imports_inside_functions_is_found_in_both_layouts(self):
        modules = self.lazily_imported()
        for expected in ("agent_identity", "install_managed_mcp_servers", "mcp_bridge"):
            self.assertIn(expected, modules)
        code = LOAD_ENTRY + ("print(json.dumps([name for name in sys.argv[2:] "
                             "if importlib.util.find_spec(name) is None]))\n")
        for layout, directory in (("release", self.release_scripts), ("installed", self.installed)):
            with self.subTest(layout=layout):
                result = run_python(code, directory / ENTRY, *modules, home=self.home)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr[-2000:])

    def test_the_first_run_from_a_release_places_the_instruction_rules(self):
        # Seen in this layout: `from install_managed_mcp_servers import atomic_write` raised
        # ModuleNotFoundError, also when the service sent no rules at all, and the run that
        # had just installed everything was reported as failed at provider_installation.
        code = LOAD_ENTRY + (
            "home = Path(sys.argv[2])\n"
            "args = argparse.Namespace(codex_root=home / '.codex', claude_root=home / '.claude')\n"
            "reports = [{'provider': 'codex', 'status': 'current'}]\n"
            "entry.apply_instruction_rules(args, {}, ['codex'], reports, home)\n"
            "rules = {'instructionRules': {'blocks': [{'id': 'rules-organization', 'text': 'Answer briefly.'}]}}\n"
            "entry.apply_instruction_rules(args, rules, ['codex'], reports, home)\n"
            "entry.remove_app_agent_channels('codex', args, home)\n"
            "print(json.dumps(reports))\n")
        for layout, directory in (("release", self.release_scripts), ("installed", self.installed)):
            with self.subTest(layout=layout):
                home = self.fresh_home()
                result = run_python(code, directory / ENTRY, home, home=home)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertEqual(json.loads(result.stdout), [{"provider": "codex", "status": "current"}])
                self.assertIn("Answer briefly.", (home / ".codex/AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
