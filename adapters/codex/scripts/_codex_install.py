from __future__ import annotations

import json
import hashlib
import io
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path


def directory_checksums(root: Path) -> dict[str, str]:
    """Return a deterministic content manifest for one installed directory."""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if "__pycache__" not in path.parts and path.suffix != ".pyc" and path.name != ".DS_Store"
    }


def committed_directory_checksums(repository_root: Path, root: Path) -> dict[str, str]:
    """Return checksums for the committed bytes, independent of checkout EOLs."""
    repository = repository_root.resolve()
    directory = root.resolve()
    try:
        relative = directory.relative_to(repository).as_posix()
    except ValueError:
        raise SystemExit(f"canonical directory is outside repository: {root}") from None

    diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--quiet", "--ignore-cr-at-eol", "HEAD", "--", relative],
        capture_output=True,
        check=False,
    )
    if diff.returncode not in (0, 1):
        raise SystemExit(
            f"unable to inspect canonical Git source: {diff.stderr.decode(errors='replace').strip()}"
        )
    untracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--others", "--exclude-standard", "--", relative],
        capture_output=True,
        check=False,
    )
    if untracked.returncode:
        raise SystemExit(
            f"unable to inspect canonical Git source: {untracked.stderr.decode(errors='replace').strip()}"
        )
    if diff.returncode == 1 or untracked.stdout.strip():
        raise SystemExit(f"canonical Git source has uncommitted changes: {relative}")

    archive = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repository), "archive", "--format=tar", "HEAD", "--", relative],
        capture_output=True,
        check=False,
    )
    if archive.returncode:
        raise SystemExit(
            f"unable to read canonical Git source: {archive.stderr.decode(errors='replace').strip()}"
        )

    prefix = relative.rstrip("/") + "/"
    checksums: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        for member in bundle.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            name = member.name[len(prefix):]
            parts = Path(name).parts
            if "__pycache__" in parts or name.endswith(".pyc") or Path(name).name == ".DS_Store":
                continue
            stream = bundle.extractfile(member)
            if stream is None:
                raise SystemExit(f"unable to read canonical Git file: {member.name}")
            checksums[Path(name).as_posix()] = hashlib.sha256(stream.read()).hexdigest()
    return checksums


def verify_installed_skills(
    shared_skills_root: Path,
    codex_root: Path,
    *,
    expected_skills: set[str] | None = None,
    canonical_repository_root: Path | None = None,
) -> list[str]:
    """Prove that every expected canonical skill is present and byte-identical."""
    if not shared_skills_root.is_dir():
        raise SystemExit(f"canonical skills directory not found: {shared_skills_root}")

    verified: list[str] = []
    failures: list[str] = []
    for source in sorted(path for path in shared_skills_root.iterdir() if path.is_dir()):
        if expected_skills is not None and source.name not in expected_skills:
            continue
        destination = codex_root / "skills" / source.name
        if not destination.is_dir():
            failures.append(f"missing skill: {source.name}")
            continue
        expected = (
            committed_directory_checksums(canonical_repository_root, source)
            if canonical_repository_root is not None
            else directory_checksums(source)
        )
        actual = directory_checksums(destination)
        missing_files = sorted(set(expected) - set(actual))
        extra_files = sorted(set(actual) - set(expected))
        changed_files = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
        if missing_files or extra_files or changed_files:
            details = []
            if missing_files:
                details.append(f"missing files={','.join(missing_files)}")
            if extra_files:
                details.append(f"extra files={','.join(extra_files)}")
            if changed_files:
                details.append(f"changed files={','.join(changed_files)}")
            failures.append(f"skill mismatch: {source.name} ({'; '.join(details)})")
            continue
        verified.append(source.name)

    if expected_skills is not None:
        canonical = {
            path.name for path in shared_skills_root.iterdir() if path.is_dir()
        }
        unknown = sorted(expected_skills - canonical)
        if unknown:
            failures.append("unknown expected skills: " + ",".join(unknown))

    if failures:
        raise SystemExit("Codex skill installation verification failed:\n- " + "\n- ".join(failures))
    return verified


def safe_rmtree_under(root: Path, target: Path) -> None:
    """Safe exception: remove only a target inside the declared root."""
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise SystemExit(f"refusing to remove root or a target outside {resolved_root}: {target}")
    shutil.rmtree(resolved_target)


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=target.parent) as handle:
        handle.write(data)
        staged = Path(handle.name)
    staged.replace(target)


def atomic_write_text(target: Path, content: str) -> None:
    atomic_write_bytes(target, content.encode("utf-8"))


def install_directory(source: Path, destination: Path) -> None:
    root = destination.parent.parent.resolve()
    resolved_destination = destination.resolve(strict=False)
    if resolved_destination != root and root not in resolved_destination.parents:
        raise SystemExit(f"refusing to replace directory outside codex root: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temp_dir:
        staged = Path(temp_dir) / destination.name
        shutil.copytree(source, staged)
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.bak.{uuid.uuid4().hex}")
            destination.rename(backup)
            try:
                staged.rename(destination)
            except Exception:
                if not destination.exists() and backup.exists():
                    backup.rename(destination)
                raise
            else:
                safe_rmtree_under(destination.parent, backup)
        else:
            staged.rename(destination)


def install_committed_directory(repository_root: Path, source: Path, destination: Path) -> None:
    """Install the clean committed bytes instead of checkout-specific EOL bytes."""
    repository = repository_root.resolve()
    directory = source.resolve()
    # This also rejects uncommitted content before the installer can publish a
    # provider copy that no longer corresponds to the canonical repository.
    committed_directory_checksums(repository, directory)
    relative = directory.relative_to(repository).as_posix()
    archive = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repository), "archive", "--format=tar", "HEAD", "--", relative],
        capture_output=True,
        check=False,
    )
    if archive.returncode:
        raise SystemExit(
            f"unable to read canonical Git source: {archive.stderr.decode(errors='replace').strip()}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}-canonical-", dir=destination.parent) as temp_dir:
        extracted_root = Path(temp_dir)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            bundle.extractall(extracted_root)
        install_directory(extracted_root / Path(relative), destination)


def install_skill(
    source: Path,
    destination: Path,
    *,
    canonical_repository_root: Path | None = None,
) -> None:
    if canonical_repository_root is not None:
        install_committed_directory(canonical_repository_root, source, destination)
        return
    install_directory(source, destination)


def install_workflows(
    source: Path,
    codex_root: Path,
    *,
    canonical_repository_root: Path | None = None,
) -> None:
    destination = codex_root / "agent-platform" / "workflows"
    if canonical_repository_root is not None:
        install_committed_directory(canonical_repository_root, source, destination)
        return
    install_directory(source, destination)


def verify_installed_workflows(
    source: Path,
    codex_root: Path,
    *,
    canonical_repository_root: Path | None = None,
) -> None:
    destination = codex_root / "agent-platform" / "workflows"
    expected = (
        committed_directory_checksums(canonical_repository_root, source)
        if canonical_repository_root is not None
        else directory_checksums(source)
    )
    if expected != directory_checksums(destination):
        raise SystemExit("Codex workflow catalog installation verification failed")


def install_profile(source: Path, destination: Path) -> None:
    atomic_write_bytes(destination, source.read_bytes())


MEMORY_TOOL_RENAMES = {
    "list_codex_thread_archive_cards": "list_agent_thread_archive_cards",
    "generate_codex_thread_archive_memory_proposals": "generate_agent_thread_archive_memory_proposals",
    "list_codex_skills": "list_agent_skills",
    "generate_codex_skill_memory_proposals": "generate_agent_skill_memory_proposals",
}


LEGACY_AGENT_INSTRUCTION_RENAMES = {
    "codexMemory": "Agent Memory",
    "codex-memory": "agent-memory",
}


def migrate_agent_instructions(agents_path: Path) -> bool:
    """Remove former provider-specific memory names from installed instructions."""
    if not agents_path.is_file():
        return False
    current = agents_path.read_text(encoding="utf-8")
    updated = current
    for old_name, new_name in LEGACY_AGENT_INSTRUCTION_RENAMES.items():
        updated = updated.replace(old_name, new_name)
    if updated == current:
        return False
    atomic_write_text(agents_path, updated)
    return True


def migrate_memory_server_config(config_path: Path) -> bool:
    """Migrate the former Codex-only MCP alias and tool names in place."""
    if not config_path.is_file():
        return False
    current = config_path.read_text(encoding="utf-8")
    legacy_pattern = re.compile(r"(?m)^\[mcp_servers\.(?:codexMemory|codexmemory)(?P<suffix>(?:\.[^\]]+)?)\]$")
    has_legacy_alias = bool(legacy_pattern.search(current))
    has_canonical_alias = bool(re.search(r"(?m)^\[mcp_servers\.agentMemory(?:\.|\])", current))
    if has_legacy_alias and has_canonical_alias:
        raise SystemExit(
            f"both legacy and canonical Agent Memory MCP aliases exist in {config_path}; "
            "refusing to choose between credentials"
        )
    updated = legacy_pattern.sub(lambda match: f"[mcp_servers.agentMemory{match.group('suffix')}]", current)
    for old_name, new_name in MEMORY_TOOL_RENAMES.items():
        updated = updated.replace(
            f"[mcp_servers.agentMemory.tools.{old_name}]",
            f"[mcp_servers.agentMemory.tools.{new_name}]",
        )
    if updated == current:
        return False
    atomic_write_text(config_path, updated)
    return True


def install_rule(rule_path: Path, agents_path: Path) -> None:
    rule = rule_path.read_text(encoding="utf-8").strip()
    rule_id = rule_path.stem
    legacy_start = f"<!-- codex-tools:{rule_id}:start -->"
    legacy_end = f"<!-- codex-tools:{rule_id}:end -->"
    rule_start = f"<!-- agent-platform:{rule_id}:start -->"
    rule_end = f"<!-- agent-platform:{rule_id}:end -->"
    block = f"{rule_start}\n{rule}\n{rule_end}"
    migrate_agent_instructions(agents_path)
    current = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    legacy_pattern = re.compile(f"{re.escape(legacy_start)}.*?{re.escape(legacy_end)}", re.DOTALL)
    pattern = re.compile(f"{re.escape(rule_start)}.*?{re.escape(rule_end)}", re.DOTALL)
    current = legacy_pattern.sub("", current)
    updated = pattern.sub(block, current) if pattern.search(current) else f"{current.rstrip()}\n\n{block}\n"
    atomic_write_text(agents_path, updated.lstrip() if not current else updated)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def install_hooks(fragment_path: Path, hooks_path: Path) -> None:
    fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
    additions = fragment.get("hooks")
    if not isinstance(additions, dict):
        raise SystemExit(f"hooks fragment must contain an object at 'hooks': {fragment_path}")
    current = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {}
    current_hooks = current.setdefault("hooks", {})
    if not isinstance(current_hooks, dict):
        raise SystemExit(f"existing hooks root is not an object: {hooks_path}")
    for event_name, entries in additions.items():
        if not isinstance(entries, list):
            raise SystemExit(f"hook event must be a list: {event_name}")
        destination = current_hooks.setdefault(event_name, [])
        if not isinstance(destination, list):
            raise SystemExit(f"existing hook event is not a list: {event_name}")
        known = {canonical_json(entry) for entry in destination}
        for entry in entries:
            serialized = canonical_json(entry)
            if serialized not in known:
                destination.append(entry)
                known.add(serialized)
    atomic_write_text(hooks_path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")


def remove_hooks(fragment_path: Path, hooks_path: Path) -> None:
    """Take a fragment's entries out of hooks.json; everything else in the file stays."""

    if not hooks_path.exists():
        return
    fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
    current = json.loads(hooks_path.read_text(encoding="utf-8"))
    current_hooks = current.get("hooks") if isinstance(current, dict) else None
    additions = fragment.get("hooks")
    if not isinstance(current_hooks, dict) or not isinstance(additions, dict):
        return
    changed = False
    for event_name, entries in additions.items():
        destination = current_hooks.get(event_name)
        if not isinstance(destination, list) or not isinstance(entries, list):
            continue
        ours = {canonical_json(entry) for entry in entries}
        kept = [entry for entry in destination if canonical_json(entry) not in ours]
        if len(kept) != len(destination):
            changed = True
            if kept:
                current_hooks[event_name] = kept
            else:
                del current_hooks[event_name]
    if changed:
        atomic_write_text(hooks_path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
