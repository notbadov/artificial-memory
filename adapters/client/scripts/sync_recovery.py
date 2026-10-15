"""Keeping the installed client the client of the approved release.

Whoever starts the client checks first that it can start, and puts the previous client back
when it cannot: the launcher of the hourly job, the desktop application. That code is already
on computers and is not replaced by a release, and the client it puts back is an older one. So
the rule that brings a computer back to its release lives here, in the client that runs after
a restore, and in what it reads on the computer:

* the files of the installed client are compared, byte for byte, with the client of the
  verified release. The apps' receipts are not asked: they name the release from the moment
  it was installed into the apps, whatever happened to the client's own files afterwards;
* a client that differs, on a computer whose apps already have the release, is installed
  again from that release, once. If the same release is found failing a second time it is
  refused until another release is approved, and the run says so: a release that cannot stay
  installed on this computer would otherwise be installed and put back every hour;
* a repair that stopped because another program held a file of the client (an antivirus
  reading it) is no failure of the release and is not counted as one;
* a person who asks for a repair gets one more attempt at a refused release, in the run they
  started and in no other. The refusal protects a computer from runs nobody watches; a person
  who asks is watching, and may have removed what stopped the client. It is an attempt and
  not a new beginning: the client's own count of repairs stays, so when the release fails
  again the refusal is back at once, and the person is told that the repair was tried;
* files of a client that the installed entry point does not declare are removed, so a file
  left behind by another generation (a damaged one among them) lies next to nothing that
  could import it;
* the copy in previous/ has to be a client that knows this rule. One that does not is replaced
  by a copy of the running client, so that what a launcher puts back is the release itself.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base and
the launch guard. What it needs from the entry point (its file lists, the installer of the
client) arrives as the namespace the orchestrator (`control_plane_sync.py`) already holds; a
part never imports the entry point.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from sync_base import iso_now, write_private_json
from sync_launch_guard import ClientFilesBusy, held_client_file, read_busy, remove_busy

# The client's own record of what it decided, next to its files. One small object: the release
# it had to install again (and whether it gave up on it), and the previous client it has
# already looked at. Removed when the connection ends.
RECOVERY_STATE_FILE = "client-recovery.json"
RECOVERY_STATE_VERSION = 1
MAX_RECOVERY_STATE_BYTES = 4096
# How often the client of one release is installed again before that release is refused here.
MAX_RELEASE_REPAIRS = 1
# Requests of a person are counted up to here and no further, so the record keeps its size
# however often somebody asks.
MAX_COUNTED_REQUESTS = 99
# Announced in the self-check by a client that has this rule; read from the copy in previous/.
RECOVERY_CAPABILITY = "releaseRecovery"
# What a service names in the desired state when its reports may say that a repair was asked
# for. A service that does not name it validates the report field by field and would refuse
# the whole report, so it is told nothing more than before.
REPORTS_REQUESTS = "clientRuntime.onRequest"
CLIENT_FILE = re.compile(r"[A-Za-z0-9_]+\.py")
RECORDED_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CAPABILITY_PROBE_TIMEOUT_SECONDS = 60


def managed_client_root() -> Path:
    return Path.home() / ".agent-platform" / "client"


def file_sha256(path: Path) -> str | None:
    """The digest of a file, or None when there is no such file to read.

    A file another program holds is neither: it is waited for, and one that stays held stops
    the caller with ClientFilesBusy. Taken for a file that is not there, it made a client that
    is whole differ from its release, and the repair of it was counted against the release."""
    try:
        return hashlib.sha256(read_busy(path)).hexdigest()
    except ClientFilesBusy:
        raise
    except OSError:
        return None


def read_recovery_state(root: Path | None = None) -> dict:
    """What the client recorded, or nothing when the file is not a record it wrote: a file
    that is damaged or was edited decides nothing."""
    try:
        # Waited for, and a record that stays held stops the run (ClientFilesBusy): read as
        # no record, it would be written again without the refusal it holds.
        raw = read_busy((root or managed_client_root()) / RECOVERY_STATE_FILE)
        value = json.loads(raw) if len(raw) <= MAX_RECOVERY_STATE_BYTES else None
    except ClientFilesBusy:
        raise
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("version") != RECOVERY_STATE_VERSION:
        return {}
    state: dict = {}
    release = value.get("release")
    if release is not None:
        if (not isinstance(release, dict) or set(release) != {"id", "repairs", "refused", "at"}
                or not isinstance(release["id"], str) or not RECORDED_RELEASE.fullmatch(release["id"])
                or type(release["repairs"]) is not int or not 1 <= release["repairs"] <= MAX_RELEASE_REPAIRS
                or type(release["refused"]) is not bool
                or not isinstance(release["at"], str) or len(release["at"]) > 40):
            return {}
        state["release"] = release
    rollback = value.get("rollback")
    if rollback is not None:
        if (not isinstance(rollback, dict) or set(rollback) != {"entry"}
                or not isinstance(rollback["entry"], str) or not SHA256.fullmatch(rollback["entry"])):
            return {}
        state["rollback"] = rollback
    # Next to the release and not inside it: a client from before requests reads the two
    # records above exactly as it wrote them and passes over this one.
    request = value.get("request")
    if request is not None:
        if (not isinstance(request, dict) or set(request) != {"id", "count", "at"}
                or not isinstance(request["id"], str) or not RECORDED_RELEASE.fullmatch(request["id"])
                or type(request["count"]) is not int or not 1 <= request["count"] <= MAX_COUNTED_REQUESTS
                or not isinstance(request["at"], str) or len(request["at"]) > 40):
            return {}
        state["request"] = request
    return state


def write_recovery_state(state: dict, root: Path | None = None) -> None:
    path = (root or managed_client_root()) / RECOVERY_STATE_FILE
    if state:
        write_private_json(path, {"version": RECOVERY_STATE_VERSION, **state})
    else:
        path.unlink(missing_ok=True)


def release_client_refused(release_id: str, root: Path | None = None) -> bool:
    """Whether this computer gave up on the client of this release."""
    release = read_recovery_state(root).get("release") or {}
    return release.get("id") == release_id and release.get("refused") is True


def decide_client_recovery(release: dict, release_id: str, *, differs: bool, accepted: bool,
                           started: bool, requested: bool = False) -> tuple[str, dict]:
    """What to do about the installed client, and the record of it to keep.

    ``release`` is the record so far. ``differs``: the installed client is not the client of
    the approved release. ``accepted``: the apps on this computer already have that release,
    so installing its client is a repair and not an update the person may still be deciding
    on. ``started``: a client was here and failed, as opposed to a computer that never had
    one. ``requested``: a person asked for a repair in this run. Answers "none", "repair" or
    "refused"."""
    if release.get("id") != release_id:
        # Another release is approved: what was decided about the earlier one is over.
        release = {}
    if not (differs and accepted and started):
        return "none", release
    if release.get("repairs", 0) >= MAX_RELEASE_REPAIRS:
        if requested:
            # The refusal is lifted for this decision only. The count of the client's own
            # repairs is kept, so the next run nobody asked for refuses again.
            return "repair", {**release, "refused": False, "at": iso_now()}
        return "refused", {**release, "refused": True}
    return "repair", {"id": release_id, "repairs": release.get("repairs", 0) + 1, "refused": False, "at": iso_now()}


def release_client_files(entry, platform_root: Path) -> dict[str, str]:
    """Installed name -> path in the release, for the client of the verified release.

    The client that runs is nearly always that client, and then its own list is the release's.
    Only a client of another release has to ask the release, as an installer does."""
    release_entry = platform_root / "adapters" / "client" / "scripts" / entry.MANAGED_CLIENT_ENTRY
    running = file_sha256(Path(entry.__file__))
    if running is not None and running == file_sha256(release_entry):
        return entry.runtime_files()
    return entry.declared_runtime_files(platform_root)


def differing_client_files(directory: Path, digests: dict[str, str | None]) -> list[str]:
    """The files of the release's client that the directory does not have as the release has them."""
    return sorted(name for name, digest in digests.items()
                  if digest is None or file_sha256(directory / name) != digest)


def remove_undeclared_client_files(directory: Path, declared: set[str]) -> list[str]:
    """Remove the files of a client that the installed entry point does not declare.

    Only what an installer could have put there is looked at: a regular file directly in the
    client's directory under the name of a client file. The copy in previous/, records, logs
    and launchers are not files of a client."""
    removed = []
    for path in sorted(directory.iterdir()):
        if CLIENT_FILE.fullmatch(path.name) and path.name not in declared and path.is_file():
            remove_busy(path)
            removed.append(path.name)
    return removed


def client_capabilities(entry_file: Path) -> list[str]:
    """What a client says it can do, asked through its self-check in an interpreter of its own.

    A client that does not answer says nothing. One that does not answer while another program
    holds a file of it was not asked: ClientFilesBusy."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    try:
        completed = subprocess.run(
            [sys.executable, "-E", "-s", str(entry_file), "--self-check"], cwd=entry_file.parent, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CAPABILITY_PROBE_TIMEOUT_SECONDS, check=False)
        answer = json.loads(completed.stdout.strip().splitlines()[-1]) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return []
    if completed.returncode != 0:
        held = held_client_file(entry_file.parent)
        if held is not None:
            raise ClientFilesBusy(held)
    capabilities = answer.get("capabilities") if isinstance(answer, dict) else None
    return [item for item in capabilities or [] if isinstance(item, str)]


def settle_rollback_target(entry, root: Path, state: dict) -> bool:
    """Make sure the client in previous/ is one that returns to the approved release.

    A launcher or the desktop application puts that copy back without asking anything. A client
    from before this rule would then stay, with the apps' receipts naming a release it is not;
    so would a copy that does not start. Either is replaced by a copy of the installed client,
    which the caller has just found to be the release's: what is put back is then the release
    itself. A copy that has the rule is kept, and looked at once. True when it was replaced."""
    previous_entry = root / entry.PREVIOUS_CLIENT_RUNTIME / entry.MANAGED_CLIENT_ENTRY
    try:
        digest = file_sha256(previous_entry)
        if digest is None or (state.get("rollback") or {}).get("entry") == digest:
            return False
        knows_the_rule = RECOVERY_CAPABILITY in client_capabilities(previous_entry)
    except ClientFilesBusy:
        # Another program holds the copy, so it is not looked at now. Everything else of this
        # run is done and is reported as done; the next run looks again.
        return False
    replaced = False
    if not knows_the_rule:
        try:
            entry.preserve_previous_runtime(root)
            replaced = file_sha256(previous_entry) != digest
        except ClientFilesBusy:
            # The same for the installed client, which could not be asked or copied now.
            return False
        if not replaced:
            # The installed client could not be copied; the next run looks again.
            return False
    state["rollback"] = {"entry": file_sha256(previous_entry)}
    return replaced


def refuse_again(root: Path) -> None:
    """The attempt a person asked for left the client as it was: the refusal stands again."""
    state = read_recovery_state(root)
    if state.get("release") and not state["release"]["refused"]:
        write_recovery_state({**state, "release": {**state["release"], "refused": True}}, root)


def forget_repair(recovery: dict) -> None:
    """A repair that another program stopped by holding a file was no attempt at the release.

    The record of the release goes back to what it was before this run decided: a repair the
    client made by itself is not counted, so the next run repairs again instead of refusing a
    release that is whole; a refusal that a person's request lifted stands as it stood."""
    root: Path = recovery["root"]
    state = read_recovery_state(root)
    if (state.get("release") or {}).get("id") != recovery["releaseId"]:
        return
    before = recovery["before"] if recovery["before"].get("id") == recovery["releaseId"] else None
    remembered = {key: value for key, value in {**state, "release": before}.items() if value}
    if remembered != state:
        write_recovery_state(remembered, root)


def plan_client_recovery(entry, desired: dict, platform_root: Path, *, accepted: bool, events=(),
                         requested: bool = False, root: Path | None = None) -> dict:
    """Compare the installed client with the client of the verified release and decide.

    Nothing is installed here. The answer carries the decision ("action"), what the release's
    client consists of, and the part of the report that says which client is at work.
    ``requested``: a person asked for a repair when starting this run. "requested" in the
    answer is narrower: the request is what lifted a refusal, so the repair is the person's
    and not one the client would have made anyway. "requests" is how often that happened for
    this release."""
    root = root or managed_client_root()
    # The run that finishes an installation carries the arguments of the run that made it. It
    # is the same request, and the same request is one attempt.
    requested = requested and not os.environ.get(entry.FOLLOW_UP_ENVIRONMENT)
    release_id = desired["release"]["releaseId"]
    files = release_client_files(entry, platform_root)
    digests = {name: file_sha256(platform_root / relative) for name, relative in files.items()}
    differs = differing_client_files(root, digests)
    # A client was here and did not start: its entry point is still there, or whoever started
    # this run wrote down that the self-check failed (the desktop application removes the
    # entry point of a client it cannot restore).
    started = bool(differs) and ((root / entry.MANAGED_CLIENT_ENTRY).is_file()
                                 or any(event.get("event") in ("self_check_failed", "self_check_timed_out")
                                        for event in events))
    state = read_recovery_state(root)
    before = state.get("release") or {}
    action, release = decide_client_recovery(before, release_id, differs=bool(differs), accepted=accepted,
                                             started=started, requested=requested)
    # Without the request this run would have refused: the attempt is the person's.
    on_request = (action == "repair" and before.get("id") == release_id
                  and before.get("repairs", 0) >= MAX_RELEASE_REPAIRS)
    # Like the release's record, what was asked about an earlier release is over.
    request = state.get("request") if (state.get("request") or {}).get("id") == release_id else None
    if on_request:
        request = {"id": release_id, "at": iso_now(),
                   "count": min((request or {}).get("count", 0) + 1, MAX_COUNTED_REQUESTS)}
    remembered = {key: value for key, value in (("release", release), ("rollback", state.get("rollback")),
                                                ("request", request)) if value}
    if remembered != state:
        write_recovery_state(remembered, root)
    running = file_sha256(Path(entry.__file__))
    report = {
        # The entry point doing this run is, byte for byte, the one of the approved release.
        "runByReleaseClient": running is not None and running == digests.get(entry.MANAGED_CLIENT_ENTRY),
        "installedMatchesRelease": not differs,
        # "failed" until the client is found whole again: a run that stops half-way says so.
        "recovery": {"none": "none", "repair": "failed", "refused": "refused"}[action],
        "entrySha256": running, "removedFiles": 0, "rollbackReplaced": False,
    }
    accepts = desired.get("clientReportFields")
    if on_request and isinstance(accepts, list) and REPORTS_REQUESTS in accepts:
        report["onRequest"] = True
    return {"action": action, "releaseId": release_id, "root": root, "digests": digests, "before": before,
            "requested": on_request, "requests": (request or {}).get("count", 0), "report": report}


def settle_client_recovery(entry, recovery: dict, *, finished: bool = True, files_busy: bool = False) -> dict:
    """What the report says about the client once the run has done its work.

    ``finished``: the run reached its end, so the client's directory is put in order as well:
    undeclared files are removed and the copy in previous/ is looked at. A run that failed
    earlier changes nothing more and only says where the client stands. ``files_busy``: what
    stopped it was a file another program held, which says nothing about the release."""
    root: Path = recovery["root"]
    report = dict(recovery["report"])
    matches = not differing_client_files(root, recovery["digests"])
    report["installedMatchesRelease"] = matches
    if recovery["action"] == "repair":
        report["recovery"] = "repaired" if matches else "failed"
        if files_busy and not matches:
            forget_repair(recovery)
        elif recovery["requested"] and not matches:
            refuse_again(root)
    if not finished:
        return report
    declared: set[str] | None = None
    if matches:
        declared = set(recovery["digests"])
    elif Path(entry.__file__).resolve().parent == root.resolve():
        # Another release's client stays (an update the person has not taken, or a release
        # that was refused here): it is the one running, and its own list says what is its.
        declared = set(entry.runtime_files())
    if declared is not None:
        report["removedFiles"] = len(remove_undeclared_client_files(root, declared))
    if matches:
        state = read_recovery_state(root)
        before = json.dumps(state, sort_keys=True)
        report["rollbackReplaced"] = settle_rollback_target(entry, root, state)
        if json.dumps(state, sort_keys=True) != before:
            write_recovery_state(state, root)
    return report
