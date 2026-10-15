"""Start one run of a synchronization client the way its launcher does, against a service that
exists only as files.

    python client_run_driver.py <entry point> <service directory> [client arguments...]

The entry point is started as the interpreter starts it (`started_entry_point`): the file that
lies there, under the name `__main__`, every statement of it, the last one included. So the
whole run is the client's: what it does to its streams before anything else, its arguments,
its lock, the desired state it asks for, the decision whether to install, the installation of
its own files, the follow-up run and the report. Two things are replaced, between the moment
the file has been read and the moment it starts its run, because a test may not reach them:

* the connection to the service. The client's own transport module is used, signature and
  digest checks included; only its `open_request` answers from `<service directory>`
  (`service.json`: the desired state and the path of the bundle) and writes every request to
  `requests.jsonl`;
* what a run does outside the client's own directory and is not about its files: the OS
  schedule, the apps' channels, skills, rules and inventories.

A follow-up run, which the client starts as a process of its own, comes back through this file.

One thing of a Windows computer can be asked for on any computer: `held.json` in the service
directory, a list of file names. While it is there, replacing a file of one of these names
fails the way Windows fails it when another program (an antivirus) has that file open.
"""

from __future__ import annotations

import ast
import errno
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path

SERVICE_FILE = "service.json"
REQUESTS_FILE = "requests.jsonl"
HELD_FILE = "held.json"


def started_entry_point(script: Path):
    """An entry point as the interpreter has it when it starts the file, stopped before its
    last statement: (the module, what runs that statement).

    A file that is started is `__main__`, and a client does things only then: it sets its
    streams to UTF-8 before it loads its parts, so that a part that does not load is reported
    in them. Imported under another name it does none of that, and calling a function of it is
    then a run no launcher ever starts: on a Windows computer every sentence that is not ASCII
    left such a run in the code page (seen on Windows 11, October 2026).

    The last statement of an entry point is the one that starts the run
    (`if __name__ == "__main__": raise SystemExit(...)`). Everything before it is executed
    here; the caller replaces what a test may not reach and then runs that statement itself,
    which ends this process with the client's own exit code."""
    tree = ast.parse(script.read_bytes(), filename=str(script))
    last = tree.body[-1] if tree.body else None
    if not (isinstance(last, ast.If) and isinstance(last.test, ast.Compare)
            and ast.unparse(last.test) in ("__name__ == '__main__'", '__name__ == "__main__"')):
        raise SystemExit(f"{script} does not end with the statement that starts it")
    entry = types.ModuleType("__main__")
    entry.__file__ = str(script)
    # What other code finds under this name is the client, as in a real start.
    sys.modules["__main__"] = entry
    exec(compile(ast.Module(body=tree.body[:-1], type_ignores=[]), str(script), "exec"), vars(entry))

    def start() -> None:
        exec(compile(ast.Module(body=[last], type_ignores=[]), str(script), "exec"), vars(entry))

    return entry, start


def hold_files(directory: Path) -> None:
    replace = os.replace

    def held_replace(source, destination, **options):
        try:
            held = json.loads((directory / HELD_FILE).read_text(encoding="utf-8"))
        except OSError:
            held = []
        if Path(destination).name in held:
            error = PermissionError(errno.EACCES, "The process cannot access the file because it is being used "
                                                  "by another process", str(destination))
            error.winerror = 32
            raise error
        return replace(source, destination, **options)

    os.replace = held_replace


class Answer(io.BytesIO):
    """What the transport reads from: a body and the one header it looks at."""

    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
        self.status = 200


def service(directory: Path):
    def open_request(base_url: str, path: str, payload: dict):
        described = json.loads((directory / SERVICE_FILE).read_text(encoding="utf-8"))
        with (directory / REQUESTS_FILE).open("a", encoding="utf-8") as log:
            log.write(json.dumps({"path": path, "payload": {key: value for key, value in payload.items()
                                                            if key != "clientToken"}}) + "\n")
        if path == "/v1/client-session/desired":
            return Answer(json.dumps(described["desired"]).encode("utf-8"))
        if path == "/v1/client-session/bundle":
            return Answer(Path(described["bundle"]).read_bytes())
        if path == "/v1/client-session/claim-command":
            return Answer(json.dumps({"command": None}).encode("utf-8"))
        if path == "/v1/client-session/report":
            return Answer(json.dumps({"client": {}}).encode("utf-8"))
        raise ValueError(f"the test service has no answer for {path}")
    return open_request


def main() -> int:
    script, directory, arguments = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    sys.argv = [str(script), *arguments]
    # As when the interpreter starts the script: its directory is first on the path.
    sys.path.insert(0, str(script.parent))
    hold_files(directory)
    entry, start = started_entry_point(script)

    transport = entry.release_transport

    def release_transport():
        module = transport()
        module.open_request = service(directory)
        return module

    entry.release_transport = release_transport
    # A function that was moved to a part reads the name in its own file. A client from before
    # the parts has every function in the entry point, and the line above is all of it.
    for value in list(vars(entry).values()):
        names = getattr(value, "__globals__", None)
        if isinstance(names, dict) and names.get("release_transport") is transport:
            names["release_transport"] = release_transport
    scheduler = {"status": "installed", "schemaVersion": 1, "launcher": "companion", "error": None}
    entry.ensure_scheduled_task = lambda *_, **__: dict(scheduler)
    entry.record_current_connection = lambda *_, **__: None
    entry.configure_managed_mcp_servers = lambda *_, **__: []
    entry.configure_app_agent_channels = lambda platform_root, args, providers, *_, **__: {
        provider: {"status": "configured"} for provider in providers}
    for name in ("apply_local_skills", "apply_mirrored_platform", "apply_mirrored_skills",
                 "apply_instruction_rules", "apply_mcp_inventory"):
        setattr(entry, name, lambda *_, **__: None)

    started = subprocess.run

    def run(command, *positional, **keywords):
        if list(command[:2]) == [sys.executable, str(script.resolve())]:
            command = [sys.executable, "-E", "-s", __file__, str(script), str(directory), *command[2:]]
        return started(command, *positional, **keywords)

    subprocess.run = run
    start()
    # Reached only by an entry point that does not end its own process.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
