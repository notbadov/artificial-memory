"""What the owner of this computer agreed to, and the releases the platform has withdrawn.

Two things a computer remembers on its own, so that whoever controls the service cannot
decide them for it.

An organization's skills and tool servers are written in the web application of the service and
are not signed, so the service could send a skill with programs in it, or a server whose tools
run without asking. Text can run things too: a skill's markdown can grant it tools, declare
hooks or run a shell command when the skill is invoked.
Two things therefore wait for the person at this computer:
  * a skill that contains anything but text, or text that runs commands, once per content;
  * a server whose tools are approved automatically, once per address.
Until then the skill is not installed and the server asks before every tool. What a computer
already used when it was first asked about a kind is accepted as it is.

A signed release stays validly signed for ever, so the service could hand a computer an old
release with a known hole. Every release carries the list of releases withdrawn so far, inside
the signed bundle. A computer remembers every id it has ever seen there and never installs one
of them again. The memory only grows: a release cannot take an id back off the list. Going
back to an older release that was not withdrawn works as before.

This file is a part of `sync_client.py`, the entry point, which takes these functions from
here. Of the client it imports the base and the messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from sync_base import iso_now, write_private_json
from sync_messages import text


def consent_root() -> Path:
    """Where the owner's agreements are kept. The variable exists for tests: a test that runs a
    synchronization step must never write an agreement into the real computer, which would
    then look as if its owner had already been asked."""

    configured = os.environ.get("AGENT_PLATFORM_CONSENT_ROOT")
    return Path(configured) if configured else Path.home() / ".agent-platform" / "client"


TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".toml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
CONSENT_KEY = re.compile(r"(skill|mcp):[A-Za-z0-9._-]{1,120}:[0-9a-f]{16}")


def _read_consents(root: Path) -> tuple[dict, list[str]]:
    """What was agreed to, and the kinds (skill, mcp) this computer has been asked about before."""

    try:
        value = json.loads((root / "consents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    granted = value.get("granted") if isinstance(value, dict) else None
    kinds = value.get("kinds") if isinstance(value, dict) else None
    return (granted if isinstance(granted, dict) else {}), [kind for kind in kinds or [] if isinstance(kind, str)]


def seed_consents_of_a_new_computer(root: Path | None = None) -> bool:
    """A computer being connected for the first time has nothing "in use before the rule": what
    its organization sends with programs in it waits for the owner like anywhere else.

    Without this record `settle_consents` takes a computer for one that was in use before
    anyone was asked, and accepts everything it is shown. Called for an explicit first install
    only; a computer that already has its agreements keeps them. True when the file was
    written."""

    root = root or consent_root()
    if (root / "consents.json").exists():
        return False
    write_private_json(root / "consents.json", {"version": 1, "granted": {}, "kinds": ["mcp", "skill"]})
    return True


def marked_executable(path: Path) -> bool:
    """A file with the executable mark of a Mac or Linux. Windows has no such mark, and
    `os.access` answers True for every file there, which would make every skill on Windows,
    notes included, one that waits for the owner. What can run there is told by the suffix
    alone."""
    return os.name != "nt" and os.access(path, os.X_OK)


# Markdown that makes an AI app run something, so a skill made of it waits for consent like a
# program. In Claude
# Code the frontmatter of a skill can grant it tools without asking (`allowed-tools`) and
# declare hooks, which are commands; its body runs a shell command each time the skill is
# invoked, written as `!` right before an inline code span or as a fenced block opened with
# "```!". Any key of that name in the frontmatter counts, at any depth: a check that is too
# wide only asks the owner once more.
FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.DOTALL | re.MULTILINE)
COMMAND_KEYS = re.compile(r"^[ \t]*(?:-[ \t]+)?[\"']?(?:allowed[-_]tools|hooks)[\"']?[ \t]*:", re.IGNORECASE | re.MULTILINE)
COMMAND_SYNTAX = re.compile(r"!`|^[ \t]*(?:```|~~~)[ \t]*!", re.MULTILINE)
MARKDOWN_SUFFIXES = {".md"}


def runs_commands(path: Path) -> bool:
    """A markdown file of a skill that grants tools, declares hooks or runs a shell command."""
    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
        return False
    # Any line end counts as one: a file written twice in text mode on Windows ends its lines
    # with "\r\r\n", and an AI app still reads the frontmatter there.
    content = re.sub(r"\r+\n?", "\n", path.read_bytes().decode("utf-8", errors="replace"))
    frontmatter = FRONTMATTER.match(content)
    return bool((frontmatter and COMMAND_KEYS.search(frontmatter.group(1))) or COMMAND_SYNTAX.search(content))


def skill_consent_key(skill_id: str, directory: Path) -> str | None:
    """The key of a staged skill that carries more than text, or text that runs commands; None
    when it is text only."""

    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if all(path.suffix.lower() in TEXT_SUFFIXES and not marked_executable(path) and not runs_commands(path)
           for path in files):
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return f"skill:{skill_id}:{digest.hexdigest()[:16]}"


def mcp_consent_key(server: dict) -> str | None:
    """The key of a server whose tools would run without asking; None for any other server."""

    if server.get("approvalMode") != "approve" or not server.get("enabled"):
        return None
    name = re.sub(r"[^A-Za-z0-9._-]", "-", str(server.get("configKey") or server.get("id") or "server"))[:120]
    return f"mcp:{name}:{hashlib.sha256(str(server.get('url')).encode('utf-8')).hexdigest()[:16]}"


# "how" in a record of agreement, and "detail" of what waits, are values of the files this
# client keeps: the same words on every computer, whatever its language.
def settle_consents(candidates: list[dict], kind: str, root: Path | None = None) -> set[str]:
    """The keys of `candidates` the owner has agreed to; the rest are listed as waiting.

    `candidates` are {"key", "title", "detail"} of one kind. The first time this runs on a
    computer everything it is shown is accepted: it was in use before the rule existed.
    """

    root = root or consent_root()
    granted, kinds = _read_consents(root)
    if kind not in kinds:
        for item in candidates:
            granted[item["key"]] = {"grantedAt": iso_now(), "how": "in use before consent was asked"}
        write_private_json(root / "consents.json", {"version": 1, "granted": granted, "kinds": sorted({*kinds, kind})})
    try:
        waiting = json.loads((root / "pending-consents.json").read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError, AttributeError):
        waiting = []
    waiting = [item for item in waiting if isinstance(item, dict) and not str(item.get("key", "")).startswith(kind + ":")]
    waiting += [{**item, "kind": kind} for item in candidates if item["key"] not in granted]
    write_private_json(root / "pending-consents.json", {"version": 1, "items": waiting})
    return {item["key"] for item in candidates if item["key"] in granted}


def grant_consent(key: str, root: Path | None = None) -> bool:
    """Record the owner's agreement to something that is waiting. False when nothing waits under that key."""

    root = root or consent_root()
    if not CONSENT_KEY.fullmatch(key):
        return False
    try:
        waiting = json.loads((root / "pending-consents.json").read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError, AttributeError):
        return False
    if not any(isinstance(item, dict) and item.get("key") == key for item in waiting):
        return False
    granted, kinds = _read_consents(root)
    granted[key] = {"grantedAt": iso_now(), "how": "confirmed on this computer"}
    write_private_json(root / "consents.json", {"version": 1, "granted": granted, "kinds": kinds})
    write_private_json(root / "pending-consents.json", {
        "version": 1, "items": [item for item in waiting if not (isinstance(item, dict) and item.get("key") == key)]})
    return True


def skills_the_owner_agreed_to(delivered: list[dict], staging: Path, root: Path | None = None) -> list[dict]:
    """`delivered` without the skills that carry programs and still wait for the owner."""

    keys: dict[str, str] = {}
    for item in delivered:
        directory = staging / str(item.get("id"))
        if directory.is_dir():
            key = skill_consent_key(str(item["id"]), directory)
            if key:
                keys[str(item["id"])] = key
    agreed = settle_consents([
        {"key": key, "title": skill_id, "detail": "skill with programs"} for skill_id, key in sorted(keys.items())
    ], "skill", root)
    for skill_id, key in keys.items():
        if key not in agreed:
            shutil.rmtree(staging / skill_id, ignore_errors=True)
    return [item for item in delivered if keys.get(str(item.get("id"))) in (None, *agreed)]


def servers_the_owner_agreed_to(servers: list[dict], root: Path | None = None) -> list[dict]:
    """`servers` where automatic approval the owner has not agreed to is turned into asking."""

    keys = {index: mcp_consent_key(server) for index, server in enumerate(servers)}
    agreed = settle_consents([
        {"key": key, "title": str(servers[index].get("displayName") or servers[index].get("configKey") or "server"),
         "detail": str(servers[index].get("url"))[:200]}
        for index, key in keys.items() if key
    ], "mcp", root)
    return [{**server, "approvalMode": "prompt"} if keys[index] and keys[index] not in agreed else server
            for index, server in enumerate(servers)]


# Where a release lists the releases withdrawn so far (`shared/` is a directory of a signed
# release), and what an id in that list looks like.
WITHDRAWN_POLICY = Path("shared") / "release-policy" / "withdrawn-releases.json"
RELEASE_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{12}-[0-9a-f]{12}")


def known_withdrawn_releases(root: Path | None = None) -> set[str]:
    """Every withdrawn release this computer has ever been told about."""

    try:
        value = json.loads(((root or consent_root()) / "withdrawn-releases.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    listed = value.get("withdrawn") if isinstance(value, dict) else None
    return {item for item in listed or [] if isinstance(item, str) and RELEASE_ID.fullmatch(item)}


def learn_withdrawn_releases(platform_root: Path, root: Path | None = None) -> set[str]:
    """Add what this verified release lists to what the computer remembers; returns all of it."""

    root = root or consent_root()
    known = known_withdrawn_releases(root)
    try:
        policy = json.loads((platform_root / WITHDRAWN_POLICY).read_text(encoding="utf-8"))
    except OSError:
        return known  # a release older than the list
    listed = policy.get("withdrawn") if isinstance(policy, dict) else None
    if not isinstance(listed, list):
        raise RuntimeError(text("withdrawn_list_malformed"))
    learned = set()
    for item in listed:
        release_id = item.get("releaseId") if isinstance(item, dict) else None
        if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
            raise RuntimeError(text("withdrawn_list_malformed"))
        learned.add(release_id)
    if not learned <= known:
        write_private_json(root / "withdrawn-releases.json", {"version": 1, "withdrawn": sorted(known | learned)})
    return known | learned


def refuse_withdrawn_release(release_id: str, withdrawn: set[str] | None = None) -> None:
    """Stop before a release the platform withdrew is fetched or installed."""

    if release_id in (known_withdrawn_releases() if withdrawn is None else withdrawn):
        raise RuntimeError(text("release_withdrawn", release_id=release_id))
