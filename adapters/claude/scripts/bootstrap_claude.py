#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parents[2]
if str(ADAPTERS_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTERS_DIR))

from common.safe_tar import extract_data_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Claude Code from an Agent Platform release bundle.")
    parser.add_argument("--bundle", type=Path, required=True, help="Local release directory or tar.gz archive")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--external-skill-state-manifest", type=Path)
    parser.add_argument("--external-skill-reconciliation-receipt", type=Path)
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--skill-delivery-manifest", type=Path)
    return parser.parse_args()


def materialize_bundle(bundle: Path, destination: Path) -> Path:
    if bundle.is_dir():
        return bundle.resolve()
    if not bundle.is_file() or not bundle.name.endswith(".tar.gz"):
        raise SystemExit(f"unsupported release bundle: {bundle}")
    with tarfile.open(bundle, "r:gz") as archive:
        extract_data_archive(archive, destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise SystemExit(f"unexpected release archive contents: {bundle}")
    return roots[0]


def verify_bundle(bundle_root: Path) -> dict[str, object]:
    manifest_path = bundle_root / "manifest.json"
    checksum_path = bundle_root / "checksums.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise SystemExit(f"release metadata is incomplete: {bundle_root}")
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
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"bundle checksum mismatch: {relative}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit(f"release manifest must be a JSON object: {manifest_path}")
    return manifest


def main() -> None:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="agent-platform-claude-") as temporary:
        temporary_root = Path(temporary)
        bundle_root = materialize_bundle(args.bundle, temporary_root / "unpacked")
        manifest = verify_bundle(bundle_root)
        layout = manifest.get("layout")
        if not isinstance(layout, dict) or not isinstance(layout.get("agent-platform"), str):
            raise SystemExit("release manifest has no agent-platform layout")
        platform_root = bundle_root / layout["agent-platform"]
        installer = platform_root / "adapters" / "claude" / "install.py"
        if not installer.is_file():
            raise SystemExit(f"Claude adapter installer is missing: {installer}")
        command = [
            sys.executable,
            str(installer),
            "--platform-root", str(platform_root),
            "--claude-root", str(args.claude_root),
            "--claude-config", str(args.claude_config),
            "--codex-config", str(args.codex_config),
            "--bin-dir", str(args.bin_dir),
            "--release-manifest", str(bundle_root / "manifest.json"),
        ]
        if args.external_skill_state_manifest is not None:
            command.extend(["--external-skill-state-manifest", str(args.external_skill_state_manifest)])
        if args.external_skill_reconciliation_receipt is not None:
            command.extend(["--external-skill-reconciliation-receipt", str(args.external_skill_reconciliation_receipt)])
        if args.skill_delivery_manifest:
            command.extend(["--skill-delivery-manifest", str(args.skill_delivery_manifest)])
        if args.skip_memory:
            command.append("--skip-memory")
        subprocess.run(command, check=True)
        print(f"bootstrapped Claude Code from release {manifest.get('release_id', 'unknown')}")


if __name__ == "__main__":
    main()
