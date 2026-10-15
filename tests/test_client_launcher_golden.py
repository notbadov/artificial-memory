"""Every file the client writes for the OS schedule, held to the byte.

What a launcher is made of is written once on a computer and compared afterwards: the first
run of a client whose launcher text, job definition or fingerprint differs writes the hourly
job of that computer again. So a change of one byte here is a change on every connected
computer, and has to be meant (a launcher revision goes up with it). The files in
`tests/fixtures/client_launchers` are what the client wrote when they were recorded; this
test writes them again and compares the bytes.

Written for a made-up computer: a home with a space, an apostrophe, an ampersand and letters
that are not ASCII, a computer name with quotation marks, and an interpreter that is or is not
a client runtime. `{ROOT}` in a recorded file stands for the temporary directory of the run.
The files of Windows are written by the same code with the platform named as Windows, on
whatever system runs the test; a Windows computer writes the same text with its own paths and
line ends, which the test of the PowerShell launcher runs there.

To record the files again after a change that is meant:

    python tests/test_client_launcher_golden.py --record
"""

from __future__ import annotations

import codecs
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "tools", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_managed_client_runtime import load_sync_client  # noqa: E402

SCRIPTS = ROOT / "adapters/client/scripts"
RECORDED = ROOT / "tests/fixtures/client_launchers"
PLACE = "{ROOT}"
ENTRY = "sync_client.py"
GUARD = "sync-client-guard.py"
CLIENT_ID = "golden-computer"
DISPLAY_NAME = 'Мария\'s "Mac" & PC'
SERVICE_URL = "https://service.test/agent-api"
HOME_NAME = "home of O'Neil & Сын"
# How each kind of file is written: the bytes before the text, and the encoding of the text.
ENCODINGS = {".sh": (b"", "utf-8"), ".plist": (b"", "utf-8"), ".json": (b"", "utf-8"),
             ".ps1": (codecs.BOM_UTF8, "utf-8"), ".vbs": (codecs.BOM_UTF16_LE, "utf-16-le")}
# name -> (platform, layout, the interpreter is a client runtime, what else differs)
VARIANTS = {
    "macos-legacy": ("macos", "legacy", False, {}),
    "macos-legacy-reinstall": ("macos", "legacy", True, {}),
    "macos-terminal": ("macos", "terminal", False, {}),
    "macos-terminal-reinstall": ("macos", "terminal", True, {"update_policy": "manual", "providers": ["codex", "claude", "kimi"]}),
    # A runtime, but an app was given a place of its own: the installer cannot be told, so none is named.
    "macos-own-places": ("macos", "legacy", True, {"codex_root": "own codex"}),
    "windows-legacy": ("windows", "legacy", False, {}),
    "windows-legacy-reinstall": ("windows", "legacy", True, {}),
    "windows-terminal-reinstall": ("windows", "terminal", True, {"update_policy": "manual", "providers": ["codex", "claude", "kimi"]}),
    "windows-own-places": ("windows", "terminal", True, {"codex_root": "own codex"}),
    # No address of the service: no credential and no PowerShell script, the job starts the client itself.
    "windows-without-service": ("windows", "legacy", False, {"control_plane_url": ""}),
}
LAUNCHER_FILES = {"macos": ("sync-client.sh",),
                  "windows": ("sync-client.ps1", "sync-client.vbs", "sync-client.arguments.json")}
POSIX = unittest.skipIf(os.name == "nt", "the recorded files hold POSIX paths and line ends; a Windows computer "
                                         "writes its own, which WindowsLauncherRunTests run there")


def of(function) -> dict:
    """The names the function reads: those of the file it is defined in."""
    return function.__globals__


class Computer:
    """One made-up computer in a temporary directory, and what the client writes on it."""

    def __init__(self, sync, root: Path) -> None:
        self.sync, self.root = sync, root
        self.home = root / HOME_NAME
        self.client = self.home / ".agent-platform" / "client"
        self.client.mkdir(parents=True)
        self.runtime = root / "runtime 1.2"
        (self.runtime / "bootstrap").mkdir(parents=True)
        (self.runtime / "bootstrap" / "companion_install.py").write_text("", encoding="utf-8")
        self.calls: list[list[str]] = []

    def python(self, runtime: bool) -> str:
        return str((self.runtime if runtime else self.root / "system python") / "python" / "bin" / "python3")

    def arguments(self, layout: str, changed: dict) -> SimpleNamespace:
        home = self.home
        return SimpleNamespace(
            control_plane_url=changed.get("control_plane_url", SERVICE_URL), release_trust_file=home / "trust.json",
            release_cache=home / "cache", client_token="golden-token", update_policy=changed.get("update_policy", "automatic"),
            schedule_layout=layout, keychain_service="golden.service" if layout == "terminal" else None, scheduler="os",
            codex_root=home / changed.get("codex_root", ".codex"), claude_root=home / ".claude",
            claude_config=home / ".claude.json", bin_dir=home / ".local" / "bin",
            project_mappings=home / ".agent-platform/project-mappings.json")

    def run(self, command, *positional, **options):
        self.calls.append([str(part) for part in command])
        # No job is loaded yet, so a Mac loads the one just written.
        return SimpleNamespace(returncode=1 if command[:2] == ["launchctl", "print"] else 0, stdout="", stderr="")

    def patched(self, platform: str, runtime: bool) -> ExitStack:
        sync, stack = self.sync, ExitStack()
        for function in (sync.install_scheduled_task, sync.scheduled_task_fingerprint):
            stack.enter_context(patch.dict(of(function), {"client_platform": lambda: platform}))
        stack.enter_context(patch.dict(of(sync.install_scheduled_task), {"protect_windows_token": lambda value: "sealed " + value}))
        stack.enter_context(patch.object(sync.Path, "home", return_value=self.home))
        stack.enter_context(patch.object(sync.sys, "executable", self.python(runtime)))
        stack.enter_context(patch.object(sync.os, "getuid", return_value=501, create=True))
        stack.enter_context(patch.object(sync.subprocess, "run", self.run))
        # The recorded commands name the programs of Windows where a default installation has them.
        stack.enter_context(patch.dict(os.environ, {"SystemRoot": "C:\\Windows"}))
        return stack

    def install(self, name: str) -> dict[str, bytes]:
        """Write the job of one variant; the files it is made of, by the name they are recorded under."""
        platform, layout, runtime, changed = VARIANTS[name]
        arguments, providers = self.arguments(layout, changed), changed.get("providers", ["codex"])
        self.calls.clear()
        with self.patched(platform, runtime):
            receipt = self.sync.install_scheduled_task(self.client / ENTRY, arguments, CLIENT_ID, DISPLAY_NAME,
                                                       providers, self.client)
            fingerprint = self.sync.scheduled_task_fingerprint(self.client / ENTRY, arguments, CLIENT_ID,
                                                               DISPLAY_NAME, providers)
        written = {}
        for file_name in LAUNCHER_FILES[platform]:
            path = self.client / file_name
            if path.is_file():
                written[file_name] = path.read_bytes()
                path.unlink()
        if platform == "macos":
            plist = self.home / "Library" / "LaunchAgents" / (receipt["taskName"] + ".plist")
            written["job.plist"] = plist.read_bytes()
            plist.unlink()
        credentials = {}
        for file_name in ("credential", "credential.dpapi"):
            if (self.client / file_name).is_file():
                credentials[file_name] = (self.client / file_name).read_text(encoding="utf-8")
                (self.client / file_name).unlink()
        if (self.client / GUARD).read_bytes() != (SCRIPTS / "sync_launch_guard.py").read_bytes():
            raise AssertionError("the guard next to the launcher is not the client's own file")
        if receipt["fingerprint"] != fingerprint:
            raise AssertionError("the receipt of the job does not hold the job's fingerprint")
        receipt = {key: value for key, value in receipt.items() if key not in ("fingerprint", "updatedAt")}
        written["written.json"] = (json.dumps(
            {"receipt": receipt, "credentials": credentials, "scheduler": self.calls}, ensure_ascii=False, indent=2,
        ) + "\n").encode("utf-8")
        return written


def constant_texts(sync) -> dict[str, bytes]:
    """The texts that depend on no computer, and those of a computer whose paths are given."""
    python, client = "/opt/golden runtime/python/bin/python3", Path("/golden home/it's/.agent-platform/client")
    command = [python, str(client / ENTRY), "--client-id", CLIENT_ID, "--display-name", DISPLAY_NAME]
    reinstall = [python, "-E", "-s", "/opt/golden runtime/bootstrap/companion_install.py", "--client-id", CLIENT_ID]
    token = "IFS= read -r AGENT_PLATFORM_CLIENT_TOKEN < /golden/credential\n"
    texts = {
        "preamble.sh": sync.runtime_restore_shell(python, client),
        "preamble-reinstall.sh": sync.runtime_restore_shell(python, client, reinstall=True),
        "preamble.ps1": sync.runtime_restore_powershell(),
        "launcher.sh": sync.macos_launcher(command, client, token, None),
        "launcher-reinstall.sh": sync.macos_launcher(command, client, token, reinstall),
    }
    return {name: text.encode("utf-8") for name, text in texts.items()}


def facts(sync) -> dict:
    """Names, limits and fingerprints: what a computer compares or is found by."""
    home = Path("/golden home")
    script = home / ".agent-platform" / "client" / ENTRY

    def arguments(layout: str) -> SimpleNamespace:
        return SimpleNamespace(
            control_plane_url=SERVICE_URL, release_trust_file=home / "trust.json", release_cache=home / "cache",
            update_policy="automatic", schedule_layout=layout, keychain_service="golden.service" if layout == "terminal" else None,
            codex_root=home / ".codex", claude_root=home / ".claude", claude_config=home / ".claude.json",
            bin_dir=home / ".local" / "bin", project_mappings=home / ".agent-platform/project-mappings.json")

    fingerprints = {}
    for platform in ("macos", "windows"):
        for layout in ("legacy", "terminal"):
            with patch.dict(of(sync.scheduled_task_fingerprint), {"client_platform": lambda platform=platform: platform}), \
                    patch.object(sync.sys, "executable", "/opt/golden runtime/python/bin/python3"):
                fingerprints[f"{platform}-{layout}"] = sync.scheduled_task_fingerprint(
                    script, arguments(layout), CLIENT_ID, DISPLAY_NAME, ["codex", "claude"])
    return {
        "revisions": {"macos": sync.MACOS_LAUNCHER_REVISION, "windows": sync.WINDOWS_LAUNCHER_REVISION,
                      "schema": sync.SCHEDULED_TASK_SCHEMA_VERSION},
        "guardArguments": sync.launch_guard_arguments(),
        "guardFile": sync.LAUNCH_GUARD_FILE,
        "argumentsFile": sync.WINDOWS_ARGUMENTS_FILE,
        "layouts": list(sync.SCHEDULE_LAYOUTS),
        "keychain": [sync.KEYCHAIN_SERVICE, sync.KEYCHAIN_ACCOUNT],
        "taskNames": {layout: sync.scheduled_task_name(CLIENT_ID, layout) for layout in sync.SCHEDULE_LAYOUTS},
        "launchAgentLabels": {layout: sync.launch_agent_label(CLIENT_ID, layout) for layout in sync.SCHEDULE_LAYOUTS},
        "literals": [sync.powershell_literal("sync-client.arguments.json"), sync.vbscript_literal('a "quoted" word')],
        "fingerprints": fingerprints,
    }


def expected(path: Path, root: Path | None = None) -> bytes:
    """A recorded file as the bytes the client writes: its text in the encoding of its kind."""
    text = path.read_text(encoding="utf-8")
    if root is not None:
        text = text.replace(PLACE, str(root))
    before, encoding = ENCODINGS[path.suffix]
    return before + text.encode(encoding)


def recordable(name: str, content: bytes, root: Path | None = None) -> str:
    before, encoding = ENCODINGS[Path(name).suffix]
    if not content.startswith(before):
        raise AssertionError(f"{name} does not begin as its kind of file does")
    text = content[len(before):].decode(encoding)
    return text if root is None else text.replace(str(root), PLACE)


def temporary_root(stack: ExitStack) -> Path:
    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="launcher-golden-"))).resolve()
    if not re.fullmatch(r"[A-Za-z0-9/_.-]+", str(root)):
        raise unittest.SkipTest(f"the temporary directory {root} would be quoted in a launcher; the recorded files "
                                "stand for one that is not")
    return root


@POSIX
class RecordedLauncherTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.sync = load_sync_client()

    def test_every_file_of_every_job_is_written_as_it_was_recorded(self) -> None:
        for name in VARIANTS:
            with self.subTest(job=name), ExitStack() as stack:
                root = temporary_root(stack)
                written = Computer(self.sync, root).install(name)
                recorded = {path.name: path for path in sorted((RECORDED / name).iterdir())}
                self.assertEqual(sorted(written), sorted(recorded), "the files a job is made of")
                for file_name, content in written.items():
                    self.assertEqual(content, expected(recorded[file_name], root), f"{name}/{file_name}")

    def test_every_job_the_client_can_write_is_recorded(self) -> None:
        self.assertEqual(sorted(path.name for path in RECORDED.iterdir() if path.is_dir()), sorted(VARIANTS))
        kinds = {(platform, layout, runtime) for platform, layout, runtime, _ in VARIANTS.values()}
        for platform in ("macos", "windows"):
            for layout in self.sync.SCHEDULE_LAYOUTS:
                self.assertIn((platform, layout, True), kinds)

    def test_the_texts_a_launcher_is_put_together_from(self) -> None:
        for name, content in constant_texts(self.sync).items():
            with self.subTest(text=name):
                self.assertEqual(content, (RECORDED / name).read_bytes())
        # The script of Windows is the same text on every computer but for one word of a comment.
        scripts = {name: (RECORDED / name / "sync-client.ps1").read_text(encoding="utf-8")
                   for name in VARIANTS if (RECORDED / name / "sync-client.ps1").is_file()}
        self.assertEqual(len(scripts), 4)
        self.assertEqual(len({text.replace("# --update-policy manual\n", "# --update-policy automatic\n")
                              for text in scripts.values()}), 1)
        for text in scripts.values():
            self.assertEqual(text.count((RECORDED / "preamble.ps1").read_text(encoding="utf-8")), 1)

    def test_the_names_limits_and_fingerprints(self) -> None:
        self.assertEqual(facts(self.sync), json.loads((RECORDED / "facts.json").read_text(encoding="utf-8")))


def record() -> None:
    sync = load_sync_client()
    RECORDED.mkdir(parents=True, exist_ok=True)
    for name in VARIANTS:
        with ExitStack() as stack:
            root = temporary_root(stack)
            (RECORDED / name).mkdir(exist_ok=True)
            for file_name, content in Computer(sync, root).install(name).items():
                (RECORDED / name / file_name).write_text(recordable(file_name, content, root), encoding="utf-8", newline="\n")
    for name, content in constant_texts(sync).items():
        (RECORDED / name).write_bytes(content)
    (RECORDED / "facts.json").write_text(json.dumps(facts(sync), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                         newline="\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        record()
    else:
        unittest.main()
