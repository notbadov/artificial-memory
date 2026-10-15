#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
ADAPTERS_DIR = Path(__file__).resolve().parents[2]
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))
TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from _codex_install import (
    atomic_write_text,
    install_hooks,
    remove_hooks,
    install_profile,
    install_rule,
    install_workflows,
    migrate_memory_server_config,
    verify_installed_skills,
    verify_installed_workflows,
)
from common.safe_tar import extract_data_archive
from compile_agent_catalog import write_compiled_catalog

PLATFORM_SOURCE_ROOT = Path(__file__).resolve().parents[3]
CORE_PACKAGE_ROOT = PLATFORM_SOURCE_ROOT / "packages/python/agent-platform-core"
if str(CORE_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_PACKAGE_ROOT))

from agent_platform_core.external_skills import (
    default_external_skill_state_manifest,
    external_skill_registry_digest,
    load_external_skill_registry,
    validate_external_skill_state_manifest,
    utc_now,
)
from agent_platform_core.managed_skills import (
    default_backup_root,
    platform_skill_sources,
    reconcile_skills,
    recorded_from_receipt,
)
from agent_platform_core.skill_delivery import (
    RETIRED_POLICY_BLOCK_IDS,
    delivered_policies,
    delivered_skill_sources,
    load_skill_delivery_manifest,
    remove_managed_blocks,
)


# Hook fragments that belong to one skill: installed with it, removed without it.
SKILL_HOOKS = {"learning-loop.json": "learn-from-defect"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Codex adapter from an agent-platform release bundle.")
    parser.add_argument("--bundle", type=Path, required=True, help="Path to the built release directory or tarball")
    parser.add_argument("--codex-root", type=Path, required=True)
    parser.add_argument(
        "--external-skill-state-manifest",
        type=Path,
        help="Verified desired external-skill state exported by Agent Platform Runtime",
    )
    parser.add_argument(
        "--external-skill-reconciliation-receipt",
        type=Path,
        help="Write provider reconciliation evidence after a successful install",
    )
    parser.add_argument("--skill-delivery-manifest", type=Path)
    parser.add_argument("--skill-backup-root", type=Path, default=default_backup_root("codex"))
    return parser.parse_args()


def materialize_bundle(bundle: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if bundle.is_dir():
        return bundle, None
    if not bundle.is_file():
        raise SystemExit(f"bundle path not found: {bundle}")
    if bundle.suffixes[-2:] != [".tar", ".gz"] and not bundle.name.endswith(".tar.gz"):
        raise SystemExit(f"unsupported bundle format: {bundle}")
    temp_dir = tempfile.TemporaryDirectory(prefix="agent-platform-release-")
    with tarfile.open(bundle, "r:gz") as archive:
        extract_data_archive(archive, temp_dir.name)
    extracted_root = next(Path(temp_dir.name).iterdir())
    if not extracted_root.is_dir():
        temp_dir.cleanup()
        raise SystemExit(f"unexpected bundle archive contents: {bundle}")
    return extracted_root, temp_dir


def load_manifest(bundle_root: Path) -> dict[str, object]:
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"bundle manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def verify_checksums(bundle_root: Path) -> None:
    checksum_path = bundle_root / "checksums.sha256"
    if not checksum_path.is_file():
        raise SystemExit(f"bundle checksums not found: {checksum_path}")
    for line_number, line in enumerate(checksum_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            expected, relative_value = line.split("  ", 1)
        except ValueError as error:
            raise SystemExit(f"invalid checksum line {line_number}: {checksum_path}") from error
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe checksum path on line {line_number}: {relative}")
        target = bundle_root / relative
        if not target.is_file():
            raise SystemExit(f"checksummed file missing: {target}")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected:
            raise SystemExit(f"bundle checksum mismatch: {relative}")


def platform_root_from_manifest(bundle_root: Path, manifest: dict[str, object]) -> Path:
    layout = manifest.get("layout")
    if not isinstance(layout, dict):
        raise SystemExit("bundle manifest layout is invalid")
    platform_root_name = layout.get("agent-platform")
    if not isinstance(platform_root_name, str):
        raise SystemExit("bundle manifest is missing agent-platform layout")
    return bundle_root / platform_root_name


def load_external_skill_state(
    platform_root: Path,
    path: Path | None,
) -> tuple[dict, dict]:
    registry = load_external_skill_registry(platform_root)
    state = (
        default_external_skill_state_manifest(registry)
        if path is None
        else json.loads(path.read_text(encoding="utf-8"))
    )
    errors = validate_external_skill_state_manifest(state, registry)
    if errors:
        raise SystemExit("invalid external skill state manifest:\n- " + "\n- ".join(errors))
    return registry, state


def install_shared_policy(
    bundle_root: Path,
    codex_root: Path,
    manifest: dict[str, object],
    external_registry: dict,
    external_state: dict,
    delivery: dict,
    backup_root: Path,
) -> tuple[dict, list[dict]]:
    platform_root = platform_root_from_manifest(bundle_root, manifest)
    shared_root = platform_root / "shared"
    adapter_root = platform_root / "adapters" / "codex"
    if not shared_root.is_dir() or not adapter_root.is_dir():
        raise SystemExit(f"Codex adapter sources not found under: {platform_root}")

    # ~/.codex/skills: a user's own skill is never changed (agent_platform_core.managed_skills).
    state_by_id = {item["id"]: item for item in external_state["skills"]}
    desired = delivered_skill_sources(
        platform_root, delivery, {identifier: item["enabled"] for identifier, item in state_by_id.items()}
    )
    for identifier, item in state_by_id.items():
        if item["enabled"] and identifier not in delivery["disabled"] and identifier not in desired:
            raise SystemExit(f"enabled external skill has no canonical source: {identifier}")
    receipt_path = codex_root / "agent-platform-install.json"
    try:
        prior_receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        prior_receipt = {}
    skills = reconcile_skills(
        skills_root=codex_root / "skills",
        desired=desired,
        recorded=recorded_from_receipt(prior_receipt if isinstance(prior_receipt, dict) else {}),
        backup_root=backup_root,
        platform_sources=platform_skill_sources(platform_root),
    )
    reconciliation: list[dict] = []
    for identifier, state in sorted(state_by_id.items()):
        destination = codex_root / "skills" / identifier
        if identifier in skills["managed"]:
            actual_state = "installed"
            evidence = {"destination": f"skills/{identifier}", "skillDigest": skills["managed"][identifier], "managedCopyRemoved": False}
        else:
            actual_state = "not_installed"
            evidence = {"destination": f"skills/{identifier}", "destinationAbsent": not destination.exists(),
                        "managedCopyRemoved": identifier in skills["removed"],
                        "conflict": identifier in skills["conflicts"]}
        reconciliation.append({
            "id": identifier,
            "enabled": state["enabled"],
            "revision": state["revision"],
            "actualState": actual_state,
            "evidence": evidence,
        })

    install_workflows(shared_root / "workflows", codex_root)
    write_compiled_catalog(
        platform_root,
        "codex",
        codex_root / "agent-platform" / "catalog.json",
    )

    for rule_file in delivered_policies(platform_root):
        install_rule(rule_file, codex_root / "AGENTS.md")
    agents = codex_root / "AGENTS.md"
    if agents.is_file():
        current = agents.read_text(encoding="utf-8")
        stripped = remove_managed_blocks(current, RETIRED_POLICY_BLOCK_IDS)
        if stripped != current:
            atomic_write_text(agents, stripped)

    # Scripts the platform's own hooks run; installed before the hooks that name them.
    scripts = codex_root / "agent-platform" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        scripts / "work_session_hook.py",
        (platform_root / "adapters" / "common" / "work_session_hook.py").read_text(encoding="utf-8"),
    )
    for hook_file in sorted((adapter_root / "hooks").glob("*.json")):
        # A skill's own hooks run a script from the skill's directory, so they follow the skill:
        # an organization that has it switched off gets neither the skill nor its hooks, and a
        # computer that had them loses them (learn-from-defect, for one, may be switched off).
        skill = SKILL_HOOKS.get(hook_file.name)
        if skill is not None and not (codex_root / "skills" / skill / "SKILL.md").is_file():
            remove_hooks(hook_file, codex_root / "hooks.json")
            continue
        install_hooks(hook_file, codex_root / "hooks.json")

    for profile_file in sorted((adapter_root / "profiles").glob("*.config.toml")):
        install_profile(profile_file, codex_root / profile_file.name)
    return skills, reconciliation


def main() -> None:
    args = parse_args()
    bundle_root, temp_dir = materialize_bundle(args.bundle)
    try:
        manifest = load_manifest(bundle_root)
        verify_checksums(bundle_root)
        codex_root = args.codex_root.resolve()
        codex_root.mkdir(parents=True, exist_ok=True)

        migrated_memory_alias = migrate_memory_server_config(codex_root / "config.toml")

        platform_root = platform_root_from_manifest(bundle_root, manifest)
        external_registry, external_state = load_external_skill_state(
            platform_root, args.external_skill_state_manifest
        )
        delivery = load_skill_delivery_manifest(args.skill_delivery_manifest)
        skills, reconciliation = install_shared_policy(
            bundle_root, codex_root, manifest, external_registry, external_state, delivery,
            args.skill_backup_root.expanduser(),
        )
        registry_ids = {item["id"] for item in external_registry["skills"]}
        managed_external = sorted(identifier for identifier in skills["managed"] if identifier in registry_ids)

        installed_skills = verify_installed_skills(
            platform_root / "shared" / "skills",
            codex_root,
            expected_skills=set(skills["managed"]),
        )
        verify_installed_workflows(platform_root / "shared" / "workflows", codex_root)
        receipt = {
            "release_id": manifest.get("release_id"),
            "agent_platform_sha": (
                manifest.get("sources", {}).get("agent-platform", {}).get("sha")
                if isinstance(manifest.get("sources"), dict)
                and isinstance(manifest.get("sources", {}).get("agent-platform"), dict)
                else None
            ),
            "skills": installed_skills,
            # Every platform skill on this computer and its digest: what the next run may change.
            "managed_skill_digests": skills["managed"],
            "skill_conflicts": skills["conflicts"],
            "skill_backups": skills["backups"],
            "skill_delivery": delivery,
            # Read by older installers after a rollback.
            "managed_external_skills": managed_external,
            "managed_external_skill_digests": {identifier: skills["managed"][identifier] for identifier in managed_external},
            "external_skill_state_source": external_state["source"],
            "external_skill_registry_digest": external_skill_registry_digest(external_registry),
        }
        atomic_write_text(
            codex_root / "agent-platform-install.json",
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        )
        if args.external_skill_reconciliation_receipt is not None:
            reconciliation_receipt = {
                "version": 1,
                "provider": "codex",
                "releaseId": manifest.get("release_id"),
                "registryDigest": external_skill_registry_digest(external_registry),
                "generatedAt": utc_now(),
                "skills": reconciliation,
            }
            atomic_write_text(
                args.external_skill_reconciliation_receipt,
                json.dumps(reconciliation_receipt, ensure_ascii=False, indent=2) + "\n",
            )

        print(f"installed Codex release bundle from {bundle_root} into {codex_root}")
        print(f"verified {len(installed_skills)} canonical Codex skills")
        for identifier in skills["conflicts"]:
            print(f"left your own skill in place: skills/{identifier} (a platform skill has the same name)")
        for backup in skills["backups"]:
            print(f"kept the previous copy of skills/{backup['id']} in {backup['path']}")
        if migrated_memory_alias:
            print("migrated Agent Memory MCP alias to agentMemory")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    main()
