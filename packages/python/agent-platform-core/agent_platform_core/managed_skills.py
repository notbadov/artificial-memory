"""Install platform skills into a provider's skills directory without losing anyone's work.

Replacing ``~/.claude/skills/<id>`` (and the same in Codex, Kimi and OpenCode) for every
platform skill would lose a user's own skill of the same name, and skills dropped from a
release would never be removed. This module is the one owner of that directory for the
platform:

- a skill the platform did not install is never changed: a same-named user skill is left in
  place and reported as a conflict (the platform copy is not installed for that provider);
- a platform skill the user edited is moved to the backup root before it is replaced or
  retired;
- a platform skill no longer delivered is removed only when it is unchanged.

The caller passes what it recorded last time: skill id -> digest, or ``None`` when an older
receipt proves ownership without a digest. Such a copy is backed up before any change.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

IGNORED_NAMES = {"__pycache__", ".DS_Store"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
BACKUPS_KEPT_PER_SKILL = 5


def _ignored(relative: Path) -> bool:
    return any(part in IGNORED_NAMES for part in relative.parts) or relative.suffix in IGNORED_SUFFIXES


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES or Path(name).suffix in IGNORED_SUFFIXES}


def skill_digest(path: Path) -> str:
    """Content digest of a skill directory; build caches never count."""

    entries: list[tuple[str, str]] = []
    # pathlib orders WindowsPath values case-insensitively, while the Control Plane
    # canonicalizes archive paths as case-sensitive POSIX strings. Sort by that same
    # transport representation so one skill has one digest on Windows, macOS and Linux.
    for item in sorted(
        path.rglob("*"),
        key=lambda candidate: PurePosixPath(candidate.relative_to(path).as_posix()).parts,
    ):
        relative = item.relative_to(path)
        if item.is_file() and not _ignored(relative):
            entries.append((relative.as_posix(), hashlib.sha256(item.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("utf-8")).hexdigest()


def default_backup_root(provider: str) -> Path:
    # The override keeps test runs out of the developer's own home: installer tests would
    # otherwise leave backups in ~/.agent-platform.
    base = os.environ.get("AGENT_PLATFORM_SKILL_BACKUP_ROOT")
    return (Path(base) if base else Path.home() / ".agent-platform" / "backups" / "skills") / provider


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    root = root.resolve(strict=False)
    return resolved == root or root in resolved.parents


def _install(source: Path, destination: Path) -> None:
    """Copy ``source`` into place atomically; whatever was at ``destination`` is discarded."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
        staged = Path(temporary) / destination.name
        shutil.copytree(source, staged, ignore=_copy_ignore)
        if destination.exists():
            previous = Path(temporary) / f"previous-{uuid.uuid4().hex}"
            destination.rename(previous)
            try:
                staged.rename(destination)
            except Exception:
                previous.rename(destination)
                raise
        else:
            staged.rename(destination)


def _back_up(destination: Path, backup_root: Path, identifier: str, stamp: str) -> Path:
    target = backup_root / identifier / stamp
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(destination), str(target))
    kept = sorted((path for path in target.parent.iterdir() if path.is_dir()), reverse=True)
    for stale in kept[BACKUPS_KEPT_PER_SKILL:]:
        shutil.rmtree(stale)
    return target


def reconcile_skills(
    *,
    skills_root: Path,
    desired: dict[str, Path],
    recorded: dict[str, str | None],
    backup_root: Path,
    platform_sources: dict[str, Path] | None = None,
) -> dict:
    """Bring ``skills_root`` to the ``desired`` platform skills.

    ``platform_sources`` (every platform skill in this release, delivered or not) lets a copy
    recorded without a digest be retired without a backup when it equals the release copy.

    Returns ``managed`` (id -> digest the platform now owns, for the next receipt),
    ``installed``, ``unchanged``, ``removed``, ``conflicts`` (user skills left in place) and
    ``backups`` ({id, path, reason}).
    """

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    result: dict = {"managed": {}, "installed": [], "unchanged": [], "removed": [], "conflicts": [], "backups": []}
    for identifier in sorted(set(recorded) - set(desired)):
        destination = skills_root / identifier
        if not destination.exists():
            continue
        if not _inside(destination, skills_root):
            raise ValueError(f"refusing to change a skill outside {skills_root}: {destination}")
        current = skill_digest(destination)
        expected = recorded[identifier]
        if expected is None and platform_sources and identifier in platform_sources:
            expected = skill_digest(platform_sources[identifier])
        if expected is not None and current == expected:
            shutil.rmtree(destination)
        else:
            path = _back_up(destination, backup_root, identifier, stamp)
            result["backups"].append({"id": identifier, "path": str(path), "reason": "retired_with_local_changes"})
        result["removed"].append(identifier)

    for identifier, source in sorted(desired.items()):
        destination = skills_root / identifier
        if not _inside(destination, skills_root):
            raise ValueError(f"refusing to change a skill outside {skills_root}: {destination}")
        wanted = skill_digest(source)
        if not destination.exists():
            _install(source, destination)
            result["installed"].append(identifier)
            result["managed"][identifier] = wanted
            continue
        current = skill_digest(destination)
        if current == wanted:
            result["unchanged"].append(identifier)
            result["managed"][identifier] = wanted
            continue
        if identifier not in recorded:
            # Someone else's skill with the same name: never ours to change.
            result["conflicts"].append(identifier)
            continue
        if recorded[identifier] is None or current != recorded[identifier]:
            path = _back_up(destination, backup_root, identifier, stamp)
            result["backups"].append({"id": identifier, "path": str(path), "reason": "replaced_local_changes"})
        _install(source, destination)
        result["installed"].append(identifier)
        result["managed"][identifier] = wanted
    return result


def recorded_from_receipt(receipt: dict) -> dict[str, str | None]:
    """What an installer receipt proves the platform installed.

    New receipts carry ``managed_skill_digests``. Older ones list ``skills`` (ownership
    without digests) and ``managed_external_skill_digests``.
    """

    digests = receipt.get("managed_skill_digests")
    if isinstance(digests, dict):
        return {key: value for key, value in digests.items() if isinstance(key, str) and isinstance(value, str)}
    recorded: dict[str, str | None] = {}
    for identifier in receipt.get("skills") or []:
        if isinstance(identifier, str):
            recorded[identifier] = None
    external = receipt.get("managed_external_skill_digests")
    if isinstance(external, dict):
        for key, value in external.items():
            if isinstance(key, str) and isinstance(value, str):
                recorded[key] = value
    return recorded


def platform_skill_sources(platform_root: Path) -> dict[str, Path]:
    """Every platform skill directory in a release, delivered to this computer or not."""

    return {
        path.name: path
        for path in sorted((platform_root / "shared" / "skills").iterdir())
        if path.is_dir() and (path / "SKILL.md").is_file()
    }


def legacy_recorded(skills_root: Path, platform_root: Path, installed_before: bool) -> dict[str, str | None]:
    """Ownership for a provider whose installer kept no receipt.

    When the platform was installed before, every platform-named skill directory present was
    placed there by it (and overwrote anything with that name), so it is ours without a digest.
    On a computer the platform never touched, nothing is ours.
    """

    if not installed_before:
        return {}
    return {name: None for name in platform_skill_sources(platform_root) if (skills_root / name).exists()}
