from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SYNC_CLIENT_PATH = ROOT / "adapters" / "client" / "scripts" / "sync_client.py"


def load_sync_client():
    spec = importlib.util.spec_from_file_location("sync_client_runtime_under_test", SYNC_CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_tree(destination: Path) -> Path:
    """Copy the files a release bundle provides for the managed client into a fake platform root."""
    sync = load_sync_client()
    platform_root = destination / "agent-platform"
    relatives = [f"adapters/client/scripts/{sync.MANAGED_CLIENT_ENTRY}", "adapters/common/__init__.py",
                 *sync.MANAGED_CLIENT_MODULES.values(), *sync.MANAGED_CLIENT_TOOLS.values(),
                 *sync.managed_client_parts().values()]
    for relative in relatives:
        target = platform_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return platform_root


FIXTURE_PART = "sync_fixture_part.py"
# A part as a later release will have them: its own imports, a sibling of the client imported
# inside a function, and the names the entry point takes from it.
FIXTURE_PART_SOURCE = (
    "from pathlib import Path\n\n"
    "FIXTURE_LIMIT = 7\n\n\n"
    "def fixture_answer() -> str:\n"
    "    import sync_guard\n"
    "    return f'{Path(sync_guard.__file__).name}:{FIXTURE_LIMIT}'\n"
)
BINDING_MARKER = "\n\ndef bind_client_parts("


def release_with_part(destination: Path, source: str | None = FIXTURE_PART_SOURCE,
                      names: tuple[str, ...] = ("fixture_answer", "FIXTURE_LIMIT")) -> Path:
    """A release whose entry point declares one more part than this tree has. The part exists
    only here: nothing is shipped to prove the mechanism. ``source=None`` leaves the declared
    file out of the release."""
    platform_root = release_tree(destination)
    scripts = platform_root / "adapters/client/scripts"
    entry = scripts / "sync_client.py"
    text = entry.read_text(encoding="utf-8")
    if text.count(BINDING_MARKER) != 1:
        raise AssertionError("the entry point no longer binds its parts where this fixture declares one")
    declared = f"MANAGED_CLIENT_PARTS = {{**MANAGED_CLIENT_PARTS, {FIXTURE_PART!r}: {names!r}}}\n"
    entry.write_text(text.replace(BINDING_MARKER, "\n" + declared + BINDING_MARKER, 1), encoding="utf-8")
    if source is not None:
        (scripts / FIXTURE_PART).write_text(source, encoding="utf-8")
    return platform_root


def bound_by_path(directory: Path, expression: str) -> subprocess.CompletedProcess:
    """Evaluate an expression on the entry point of ``directory`` loaded by path, from another
    working directory: the way a caller that is not the scheduler loads it."""
    code = ("import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('entry_loaded_by_path', sys.argv[1])\n"
            "entry = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(entry)\n"
            "print(eval(sys.argv[2], {'entry': entry}))\n")
    return subprocess.run([sys.executable, "-E", "-s", "-c", code, str(directory / "sync_client.py"), expression],
                          cwd=directory.parent, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")


def launcher_preamble(sync, managed: Path) -> str:
    """The first part of the launcher a client writes for a Mac, with the guard it starts next
    to the client, as the client leaves the two when it writes a launcher."""
    sync.write_launch_guard(managed)
    return sync.runtime_restore_shell(sys.executable, managed)


def self_check(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-E", "-s", str(directory / "sync_client.py"), "--self-check"],
        cwd=directory, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
    )


class DeclaredRuntimeTests(unittest.TestCase):
    """This client installs a newer release with the file list that release declares."""

    def setUp(self) -> None:
        self.sync = load_sync_client()

    def test_a_module_added_by_a_newer_release_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            entry = platform_root / "adapters/client/scripts/sync_client.py"
            entry.write_text(entry.read_text(encoding="utf-8").replace(
                '    "mcp_bridge.py": "adapters/client/scripts/mcp_bridge.py",\n',
                '    "mcp_bridge.py": "adapters/client/scripts/mcp_bridge.py",\n'
                '    "future_module.py": "adapters/client/scripts/future_module.py",\n', 1), encoding="utf-8")
            (platform_root / "adapters/client/scripts/future_module.py").write_text("VALUE = 1\n", encoding="utf-8")
            managed = Path(directory) / "home" / ".agent-platform" / "client"

            self.sync.install_managed_runtime(platform_root, managed)

            self.assertTrue((managed / "future_module.py").is_file())
            self.assertTrue(json.loads(self_check(managed).stdout)["complete"])

    def test_a_declared_module_missing_from_the_bundle_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            (platform_root / "adapters/client/scripts/mcp_bridge.py").unlink()
            managed = Path(directory) / "home" / ".agent-platform" / "client"

            with self.assertRaisesRegex(self.sync.ClientRuntimeProbeFailed, "mcp_bridge.py"):
                self.sync.install_managed_runtime(platform_root, managed)
            self.assertFalse((managed / "sync_client.py").exists())

    def test_a_release_from_before_declared_runtimes_gets_the_files_it_contains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            entry = platform_root / "adapters/client/scripts/sync_client.py"
            entry.write_text(entry.read_text(encoding="utf-8").replace(
                '    if getattr(args, "runtime_files", False):', '    if False:', 1).replace(
                '"--runtime-files", action="store_true"', '"--former-option", action="store_true"', 1),
                encoding="utf-8")

            files = self.sync.declared_runtime_files(platform_root)

            self.assertEqual(set(files), set(self.sync.managed_client_files()))

    def test_a_declaration_cannot_escape_the_release_or_the_client_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            entry = platform_root / "adapters/client/scripts/sync_client.py"
            entry.write_text(entry.read_text(encoding="utf-8").replace(
                '    "mcp_bridge.py": "adapters/client/scripts/mcp_bridge.py",\n',
                '    "mcp_bridge.py": "adapters/client/../../../outside.py",\n', 1), encoding="utf-8")

            with self.assertRaisesRegex(self.sync.ClientRuntimeProbeFailed, "declaration is invalid"):
                self.sync.declared_runtime_files(platform_root)


class ClientPartTests(unittest.TestCase):
    """A release whose entry point is split into parts, installed by the client of this tree.

    The installer is always the client already on the computer, so nothing here may depend on
    what the installing client knows about the part: it learns the file from the release."""

    def setUp(self) -> None:
        self.sync = load_sync_client()
        self.assertNotIn(FIXTURE_PART, self.sync.managed_client_files())

    def installed_client(self, directory: Path) -> tuple[Path, dict[str, bytes]]:
        """The client of this tree, installed and working, and its files as they are."""
        managed = directory / "home" / ".agent-platform" / "client"
        self.sync.install_managed_runtime(release_tree(directory / "current"), managed)
        return managed, {path.name: path.read_bytes() for path in managed.glob("*.py")}

    def assert_untouched(self, managed: Path, before: dict[str, bytes]) -> None:
        self.assertEqual({path.name: path.read_bytes() for path in managed.glob("*.py")}, before)
        self.assertEqual([path.name for path in managed.iterdir() if path.name.startswith(".staging-")], [])
        self.sync.probe_client_runtime(managed, require_complete=True)

    def test_the_release_declares_its_part_in_the_shape_installed_clients_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_with_part(Path(directory) / "release")
            completed = subprocess.run(
                [sys.executable, "-E", "-s", str(platform_root / "adapters/client/scripts/sync_client.py"), "--runtime-files"],
                cwd=platform_root, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            declared = json.loads(completed.stdout)
            self.assertEqual(set(declared), {"files", "capabilities"})
            self.assertEqual(declared["files"], {**self.sync.runtime_files(),
                                                 FIXTURE_PART: f"adapters/client/scripts/{FIXTURE_PART}"})
            # What the installing client makes of it: one more flat file, nothing else new.
            self.assertEqual(self.sync.declared_runtime_files(platform_root), declared["files"])

    def test_a_part_is_installed_next_to_the_entry_point_and_bound_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            managed, _ = self.installed_client(Path(directory))
            platform_root = release_with_part(Path(directory) / "release")

            self.sync.install_managed_runtime(platform_root, managed)

            self.assertEqual(sorted(path.name for path in managed.iterdir() if path.is_file()),
                             sorted([*self.sync.managed_client_files(), FIXTURE_PART]))
            completed = self_check(managed)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual((result["complete"], result["missing"]), (True, []))
            # The names are the entry point's own, also for a caller that loads it by path and
            # has neither its directory on the path nor it as the working directory.
            bound = bound_by_path(managed, "(entry.fixture_answer(), entry.FIXTURE_LIMIT, "
                                           "entry.fixture_answer.__module__, 'sync_fixture_part.py' in entry.runtime_files())")
            self.assertEqual((bound.returncode, bound.stdout.strip()),
                             (0, "('sync_guard.py:7', 7, 'sync_fixture_part', True)"), bound.stderr)
            # The client this one replaced had no part and is the rollback target as it was.
            previous = managed / self.sync.PREVIOUS_CLIENT_RUNTIME
            self.assertFalse((previous / FIXTURE_PART).exists())
            self.sync.probe_client_runtime(previous, require_complete=True)

    def test_a_client_with_a_part_keeps_it_in_its_rollback_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_with_part(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            installed = load_installed(managed)

            # From here the installer is the client with the part.
            installed.install_managed_runtime(platform_root, managed)

            previous = managed / installed.PREVIOUS_CLIENT_RUNTIME
            self.assertTrue((previous / FIXTURE_PART).is_file())
            installed.probe_client_runtime(previous, require_complete=True)

    def test_a_declared_part_that_the_release_does_not_contain_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            managed, before = self.installed_client(Path(directory))
            platform_root = release_with_part(Path(directory) / "release", source=None)

            with self.assertRaisesRegex(self.sync.ClientRuntimeProbeFailed, FIXTURE_PART):
                self.sync.install_managed_runtime(platform_root, managed)

            self.assert_untouched(managed, before)

    def test_a_part_that_cannot_be_bound_never_leaves_staging(self) -> None:
        broken = {
            "a name the entry point takes is gone": (
                "FIXTURE_LIMIT = 7\n", "does not define: fixture_answer"),
            "the part does not import": (
                FIXTURE_PART_SOURCE + "import a_module_no_release_has\n", "No module named 'a_module_no_release_has'"),
            "the part imports the entry point": (
                "import sync_client\n" + FIXTURE_PART_SOURCE, "imports the entry point"),
        }
        with tempfile.TemporaryDirectory() as directory:
            managed, before = self.installed_client(Path(directory))
            for number, (case, (source, expected)) in enumerate(broken.items()):
                with self.subTest(case=case):
                    platform_root = release_with_part(Path(directory) / f"release-{number}", source=source)

                    with self.assertRaises(self.sync.ClientRuntimeProbeFailed) as raised:
                        self.sync.install_managed_runtime(platform_root, managed)

                    self.assertIn(expected, str(raised.exception))
                    self.assertFalse((managed / FIXTURE_PART).exists())
                    self.assert_untouched(managed, before)

    def test_an_installed_client_that_lost_its_part_fails_the_self_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_with_part(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            (managed / FIXTURE_PART).unlink()

            completed = self_check(managed)

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr.strip().splitlines()[-1],
                             f"ImportError: Client runtime is incomplete: {FIXTURE_PART}")

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_the_launcher_restores_the_client_from_before_the_new_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            managed, before = self.installed_client(Path(directory))
            self.sync.install_managed_runtime(release_with_part(Path(directory) / "release"), managed)
            # One part corrupted on the computer: the new entry point cannot start.
            (managed / FIXTURE_PART).write_text("raise RuntimeError('corrupted')\n", encoding="utf-8")
            with self.assertRaises(self.sync.ClientRuntimeProbeFailed):
                self.sync.probe_client_runtime(managed)

            completed = self.run_restore(Path(directory), managed)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("restoring the previous runtime", completed.stderr)
            # The entry point that does not know the part is back and never looks at the file left behind.
            self.assertEqual((managed / "sync_client.py").read_bytes(), before["sync_client.py"])
            self.sync.probe_client_runtime(managed, require_complete=True)
            self.assertEqual([event["event"] for event in self.sync.read_launcher_events(managed)],
                             ["self_check_failed", "runtime_restored"])

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_the_launcher_restores_a_lost_part_from_the_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_with_part(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            installed = load_installed(managed)
            installed.install_managed_runtime(platform_root, managed)
            (managed / FIXTURE_PART).unlink()

            completed = self.run_restore(Path(directory), managed)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((managed / FIXTURE_PART).read_text(encoding="utf-8"), FIXTURE_PART_SOURCE)
            installed.probe_client_runtime(managed, require_complete=True)

    def run_restore(self, directory: Path, managed: Path) -> subprocess.CompletedProcess:
        preamble = directory / "restore.sh"
        preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed), encoding="utf-8")
        return subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")


def load_installed(managed: Path):
    """The installed entry point as a module of this process, to act as the next installer.
    Its parts are loaded from its own directory, whatever this process imported before."""
    spec = importlib.util.spec_from_file_location("installed_client_with_parts", managed / "sync_client.py")
    module = importlib.util.module_from_spec(spec)
    path, modules = list(sys.path), dict(sys.modules)
    try:
        # As when the scheduler starts it: the script's own directory is first on the path. An
        # entry point from before the parts relies on that to find the modules next to it.
        sys.path.insert(0, str(managed))
        spec.loader.exec_module(module)
    finally:
        # The entry point puts its directory on the path and its parts among the modules; a
        # temporary directory must not stay there for the tests that follow.
        sys.path[:] = path
        for name in set(sys.modules) - set(modules):
            del sys.modules[name]
    return module


# The commit of release 20261011-195649-28bfcfeb8bda-cff97728ade1: the client that installs the
# first release with parts on the computers connected when the parts were introduced. It has no
# parts and learns every file from the release it installs.
RELEASED_CLIENT = "28bfcfeb8bda"


def released_client(destination: Path) -> Path:
    """The released client as a flat directory, the way a computer has it, read from history.

    Skips the test where the history is not there (a shallow checkout, or the published copy
    of this repository, which carries no history): the client of a release is not kept in the
    tree, and a copy of it here would be 3500 lines nobody reads."""
    def show(relative: str) -> bytes:
        completed = subprocess.run(["git", "show", f"{RELEASED_CLIENT}:{relative}"], cwd=ROOT,
                                   capture_output=True, check=False)
        if completed.returncode != 0:
            raise unittest.SkipTest(f"commit {RELEASED_CLIENT} is not in this checkout (a shallow history, or "
                                    "the published copy, which has none); it runs where the full history is")
        return completed.stdout

    entry = show("adapters/client/scripts/sync_client.py")
    files = {"sync_client.py": "adapters/client/scripts/sync_client.py"}
    for node in ast.parse(entry).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("MANAGED_CLIENT_MODULES", "MANAGED_CLIENT_TOOLS")):
            files.update(ast.literal_eval(node.value))
    destination.mkdir(parents=True)
    for name, relative in files.items():
        (destination / name).write_bytes(entry if name == "sync_client.py" else show(relative))
    return destination


class RealPartDeliveryTests(unittest.TestCase):
    """The parts this tree really has, delivered to a computer: by the client of this tree and
    by the client already released, which has never heard of them."""

    def setUp(self) -> None:
        self.sync = load_sync_client()
        self.parts = sorted(self.sync.MANAGED_CLIENT_PARTS)
        self.assertGreaterEqual(len(self.parts), 4)
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = directory
        self.release = release_tree(directory / "release")
        self.managed = directory / "home" / ".agent-platform" / "client"

    def files(self, directory: Path) -> dict[str, bytes]:
        """The files of a client; the guard next to a launcher is the launcher's."""
        return {path.name: path.read_bytes() for path in directory.glob("*.py")
                if path.name != self.sync.LAUNCH_GUARD_FILE}

    def assert_installed_from_the_release(self) -> None:
        installed = self.files(self.managed)
        self.assertEqual(sorted(installed), sorted(self.sync.managed_client_files()))
        # Next to the client only what belongs to its launcher: the guard, the lock the guard
        # takes, and what it wrote about a restore.
        self.assertLessEqual({path.name for path in self.managed.iterdir() if path.is_file()} - set(installed),
                             {self.sync.LAUNCHER_EVENTS_FILE, self.sync.LAUNCH_GUARD_FILE, "sync.lock"})
        for name, relative in self.sync.runtime_files().items():
            self.assertEqual(installed[name], (ROOT / relative).read_bytes(), name)
        completed = self_check(self.managed)
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertEqual(json.loads(completed.stdout), {
            "status": "ok", "modules": sorted(self.sync.MANAGED_CLIENT_MODULES), "complete": True, "missing": [],
            "capabilities": list(self.sync.CLIENT_CAPABILITIES)})

    def restore(self, preamble: str) -> subprocess.CompletedProcess:
        script = self.directory / "restore.sh"
        script.write_text("set -eu\n" + preamble, encoding="utf-8")
        self.sync.clear_launcher_events(self.managed)
        return subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

    def damage(self, part: str, how: str) -> None:
        if how == "corrupted":
            (self.managed / part).write_text("raise RuntimeError('corrupted')\n", encoding="utf-8")
        else:
            (self.managed / part).unlink()
        completed = self_check(self.managed)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_the_client_of_this_tree_installs_the_parts_and_its_launcher_restores_them(self) -> None:
        self.sync.install_managed_runtime(self.release, self.managed)
        self.assert_installed_from_the_release()
        self.assertFalse((self.managed / self.sync.PREVIOUS_CLIENT_RUNTIME).exists())

        # The next release is installed by a client that has the parts: they are its rollback target too.
        installed = load_installed(self.managed)
        installed.install_managed_runtime(self.release, self.managed)
        self.assert_installed_from_the_release()
        before = self.files(self.managed)
        self.assertEqual(self.files(self.managed / installed.PREVIOUS_CLIENT_RUNTIME), before)

        preamble = launcher_preamble(installed, self.managed)
        for part in self.parts:
            for how in ("corrupted", "deleted"):
                with self.subTest(part=part, how=how):
                    self.damage(part, how)

                    completed = self.restore(preamble)

                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn("restoring the previous runtime", completed.stderr)
                    self.assertEqual(self.files(self.managed), before)
                    self.assertEqual([event["event"] for event in self.sync.read_launcher_events(self.managed)],
                                     ["self_check_failed", "runtime_restored"])
                    self.assert_installed_from_the_release()

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_the_released_client_installs_the_parts_and_its_launcher_brings_it_back(self) -> None:
        released_client(self.managed)
        old_files = self.files(self.managed)
        self.assertEqual(set(self.parts) & set(old_files), set())
        self.assertEqual(self_check(self.managed).returncode, 0)
        old = load_installed(self.managed)
        self.assertFalse(hasattr(old, "MANAGED_CLIENT_PARTS"))

        # It asks the release for its files and is told about the parts.
        self.assertEqual(old.declared_runtime_files(self.release), self.sync.runtime_files())
        fingerprint = old.runtime_fingerprint(self.managed)
        old.install_managed_runtime(self.release, self.managed)

        self.assert_installed_from_the_release()
        self.assertEqual(self.files(self.managed / old.PREVIOUS_CLIENT_RUNTIME), old_files)
        # The same run is then finished by the new client, as after any update of the client.
        self.assertTrue(old.follow_up_needed(fingerprint, old.runtime_fingerprint(self.managed), 0, {}))

        # The launcher on the computer was written by the released client, and it is the one
        # that restores here. This tree's launcher puts the entry point back last and can start
        # the runtime's installer when nothing can be restored, so launchers are written again.
        preamble = old.runtime_restore_shell(sys.executable, self.managed)
        self.assertGreater(self.sync.MACOS_LAUNCHER_REVISION, old.MACOS_LAUNCHER_REVISION)
        self.assertGreater(self.sync.WINDOWS_LAUNCHER_REVISION, old.WINDOWS_LAUNCHER_REVISION)

        for part, how in ((self.parts[0], "corrupted"), (self.parts[-1], "deleted")):
            with self.subTest(part=part, how=how):
                self.damage(part, how)

                completed = self.restore(preamble)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("restoring the previous runtime", completed.stderr)
                # Every file of the released client is back; the parts it does not know stay
                # where they are and nothing of it reads them.
                restored = self.files(self.managed)
                self.assertEqual({name: restored[name] for name in old_files}, old_files)
                completed = self_check(self.managed)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual([event["event"] for event in self.sync.read_launcher_events(self.managed)],
                                 ["self_check_failed", "runtime_restored"])
                # What the restored client does on its next run is not shown by calling its
                # installer from here: tests/test_client_recovery.py starts that run.
                old.install_managed_runtime(self.release, self.managed)
                self.assert_installed_from_the_release()


class ManagedClientRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sync = load_sync_client()

    def test_installed_runtime_passes_self_check_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"

            self.sync.install_managed_runtime(platform_root, managed)

            self.assertEqual(
                sorted(path.name for path in managed.iterdir() if path.is_file()),
                sorted(self.sync.managed_client_files()),
            )
            self.sync.probe_client_runtime(managed)
            # Desktop Companion 0.3.0 hands the schedule only to a runtime that says it supports it.
            completed = subprocess.run(
                [sys.executable, "-E", "-s", str(managed / "sync_client.py"), "--self-check"],
                cwd=managed, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
            )
            self.assertIn("schedulerCompanion", json.loads(completed.stdout)["capabilities"])

    def test_release_with_unpackaged_companion_import_is_never_published(self) -> None:
        # sync_client.py importing common.safe_tar via the repository layout while the managed
        # installer ships only the old module set.
        with tempfile.TemporaryDirectory() as directory:
            good = release_tree(Path(directory) / "good")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(good, managed)
            installed_entry = (managed / self.sync.MANAGED_CLIENT_ENTRY).read_bytes()

            broken = release_tree(Path(directory) / "broken")
            entry = broken / "adapters" / "client" / "scripts" / self.sync.MANAGED_CLIENT_ENTRY
            entry.write_text(
                entry.read_text(encoding="utf-8").replace(
                    "LOCAL_SAFE_TAR = Path(__file__).with_name(\"safe_tar.py\")",
                    "from common.unpackaged_companion import extract_data_archive  # noqa\n"
                    "LOCAL_SAFE_TAR = Path(__file__).with_name(\"safe_tar.py\")",
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(self.sync.ClientRuntimeProbeFailed) as raised:
                self.sync.install_managed_runtime(broken, managed)

            # The message such a client logs on every scheduled run.
            self.assertIn("No module named 'common'", str(raised.exception))
            self.assertEqual((managed / self.sync.MANAGED_CLIENT_ENTRY).read_bytes(), installed_entry)
            self.assertEqual([path.name for path in managed.iterdir() if path.name.startswith(".staging-")], [])
            self.sync.probe_client_runtime(managed)

    def test_a_client_that_lost_any_of_its_files_fails_the_self_check(self) -> None:
        # Nothing completes a client during a run. Whoever starts it restores the previous one
        # when this check fails, so the check has to fail for every file, not only for the ones
        # the entry point imports while it starts.
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            for name in sorted(set(self.sync.managed_client_files()) - {self.sync.MANAGED_CLIENT_ENTRY}):
                kept = (managed / name).read_bytes()
                (managed / name).unlink()
                completed = self_check(managed)
                (managed / name).write_bytes(kept)
                with self.subTest(file=name):
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(completed.stdout, "")
                    # The entry point imports safe_tar while it starts and looks for the copy a
                    # release keeps elsewhere; every other file is named by the check itself.
                    self.assertIn("No module named 'common'" if name == "safe_tar.py" else name,
                                  completed.stderr.strip().splitlines()[-1])
            self.assertEqual(json.loads(self_check(managed).stdout)["missing"], [])

    def test_self_check_does_not_write_the_client_run_receipt(self) -> None:
        # The probe runs on every self-update; it must not replace last-run.json with a
        # receipt that carries the default hostname instead of the enrolled client id.
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            home = Path(directory) / "home"
            managed = home / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)

            completed = subprocess.run(
                [sys.executable, "-E", "-s", str(managed / self.sync.MANAGED_CLIENT_ENTRY), "--self-check"],
                cwd=managed, env={"HOME": str(home), "USERPROFILE": str(home), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((managed / "last-run.json").exists())

    def test_upgrade_keeps_working_runtime_as_rollback_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            self.assertFalse((managed / self.sync.PREVIOUS_CLIENT_RUNTIME).exists())

            self.sync.install_managed_runtime(platform_root, managed)

            self.sync.probe_client_runtime(managed / self.sync.PREVIOUS_CLIENT_RUNTIME)

    def test_broken_installed_runtime_is_not_kept_as_rollback_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            self.sync.install_managed_runtime(platform_root, managed)
            previous = managed / self.sync.PREVIOUS_CLIENT_RUNTIME
            marker = previous / "known-good"
            marker.write_text("previous generation", encoding="utf-8")
            (managed / "safe_tar.py").unlink()

            self.sync.install_managed_runtime(platform_root, managed)

            self.assertTrue(marker.is_file())
            self.sync.probe_client_runtime(managed)

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_launcher_restores_previous_runtime_when_installed_one_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            self.sync.install_managed_runtime(platform_root, managed)
            # Reproduce the field state: an entry point whose companion module never arrived.
            (managed / "safe_tar.py").unlink()
            with self.assertRaises(self.sync.ClientRuntimeProbeFailed):
                self.sync.probe_client_runtime(managed)

            preamble = Path(directory) / "restore.sh"
            preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed),
                                encoding="utf-8")
            completed = subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("restoring the previous runtime", completed.stderr)
            self.sync.probe_client_runtime(managed)

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_launcher_records_restore_events_for_the_next_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            self.sync.install_managed_runtime(platform_root, managed)
            (managed / "safe_tar.py").unlink()
            preamble = Path(directory) / "restore.sh"
            preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed),
                                encoding="utf-8")

            subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")

            events = self.sync.read_launcher_events(managed)
            self.assertEqual([event["event"] for event in events], ["self_check_failed", "runtime_restored"])
            self.sync.clear_launcher_events(managed)
            self.assertEqual(self.sync.read_launcher_events(managed), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_launcher_records_that_no_rollback_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            (managed / "safe_tar.py").unlink()
            preamble = Path(directory) / "restore.sh"
            preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed),
                                encoding="utf-8")

            subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")

            events = self.sync.read_launcher_events(managed)
            self.assertEqual([event["event"] for event in events], ["self_check_failed", "restore_unavailable"])

    def test_launcher_event_reader_ignores_malformed_lines_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory)
            lines = ['{"event":"runtime_restored","at":"2026-09-29T17:27:10Z"}'] * 30
            lines += ["not json", '{"event":"unknown","at":"2026-09-29T17:27:10Z"}',
                      '{"event":"runtime_restored","at":"yesterday"}',
                      '{"event":"runtime_restored","at":"2026-09-29T17:27:10Z","extra":1}']
            (managed / self.sync.LAUNCHER_EVENTS_FILE).write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")

            events = self.sync.read_launcher_events(managed)

            self.assertLessEqual(len(events), self.sync.MAX_LAUNCHER_EVENTS)
            self.assertTrue(all(event == {"event": "runtime_restored", "at": "2026-09-29T17:27:10Z"} for event in events))

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_launcher_leaves_healthy_runtime_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            preamble = Path(directory) / "restore.sh"
            preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed),
                                encoding="utf-8")

            completed = subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")

    @unittest.skipIf(sys.platform == "win32", "POSIX launcher preamble")
    def test_launcher_puts_the_entry_point_back_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            platform_root = release_tree(Path(directory) / "release")
            managed = Path(directory) / "home" / ".agent-platform" / "client"
            self.sync.install_managed_runtime(platform_root, managed)
            self.sync.install_managed_runtime(platform_root, managed)
            entry, previous = managed / self.sync.MANAGED_CLIENT_ENTRY, managed / self.sync.PREVIOUS_CLIENT_RUNTIME
            failed = "raise SystemExit('the client that failed')\n"
            entry.write_text(failed, encoding="utf-8")
            (managed / "safe_tar.py").unlink()
            # A file of the previous client that cannot be put back, after the entry point in
            # the order the shell lists them.
            (previous / "zz_cannot_be_copied.py").mkdir()
            preamble = Path(directory) / "restore.sh"
            preamble.write_text("set -eu\n" + launcher_preamble(self.sync, managed),
                                encoding="utf-8")

            completed = subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")

            # The restore stopped. The other files were on their way back and the entry point
            # that failed is still the one here, so the next start restores again.
            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue((managed / "safe_tar.py").is_file())
            self.assertEqual(entry.read_text(encoding="utf-8"), failed)
            self.assertEqual([event["event"] for event in self.sync.read_launcher_events(managed)], ["self_check_failed"])

            (previous / "zz_cannot_be_copied.py").rmdir()
            subprocess.run(["/bin/sh", str(preamble)], capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")

            self.assertEqual(entry.read_bytes(), (previous / entry.name).read_bytes())
            self.sync.probe_client_runtime(managed)
            self.assertEqual([event["event"] for event in self.sync.read_launcher_events(managed)],
                             ["self_check_failed", "self_check_failed", "runtime_restored"])

    def test_windows_launcher_preamble_uses_the_same_restore_contract(self) -> None:
        # Both launchers start the same guard and do the same with its answer; the check, the
        # restore and its order (the entry point last) are the guard's, once, in Python
        # (tests/test_client_launch_guard.py holds both texts to the letter and runs them).
        source = self.sync.runtime_restore_powershell()
        shell = self.sync.runtime_restore_shell(sys.executable, Path("client"), reinstall=True)
        arguments = "--lock-wait 30 --self-check-limit 60"

        self.assertIn(f"& $python -E -s $guard {arguments} *> $null", source)
        self.assertIn(f"/sync-client-guard.py {arguments} || guard=$?", shell)
        self.assertIn("$ErrorActionPreference = 'Continue'", source)
        for gone in ("--self-check ", "Copy-Item", "cp -p", "'previous'"):
            self.assertNotIn(gone, source + shell)
        # The one event a launcher still writes itself: its guard did not run.
        self.assertEqual(source.count("Write-AgentPlatformEvent '"), 1)
        self.assertIn("Write-AgentPlatformEvent 'guard_failed'", source)
        self.assertEqual(shell.count("printf "), 1)
        self.assertIn(" guard_failed ", shell)
        self.assertIn("InvariantCulture", source)
        self.assertIn("guard_failed", self.sync.LAUNCHER_EVENTS)

    def test_control_plane_release_gate_requires_every_managed_client_file(self) -> None:
        source = (ROOT / "adapters" / "client" / "scripts" / "control_plane_sync.py").read_text(encoding="utf-8")
        for relative in (f"adapters/client/scripts/{self.sync.MANAGED_CLIENT_ENTRY}",
                         *self.sync.MANAGED_CLIENT_MODULES.values(), *self.sync.MANAGED_CLIENT_TOOLS.values()):
            self.assertIn(f'"{relative}"', source)

    def test_launcher_revisions_force_scheduler_reinstall(self) -> None:
        # Installed launchers without the restore preamble must be rewritten on the next run.
        # ... and so must launchers that did not put the entry point back last, and those that
        # checked and restored by themselves, before any lock and without a time limit.
        self.assertGreaterEqual(self.sync.MACOS_LAUNCHER_REVISION, 6)
        self.assertGreaterEqual(self.sync.WINDOWS_LAUNCHER_REVISION, 9)


if __name__ == "__main__":
    unittest.main()
