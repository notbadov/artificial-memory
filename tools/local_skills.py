#!/usr/bin/env python3
"""Install an organization's skills on this computer, list the skills it has, or read one of the
user's own skills so its owner can keep it in Console.

Run by the sync client from the verified release (agent_platform_core.local_skills).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/python/agent-platform-core"))

from agent_platform_core.local_skills import (  # noqa: E402
    app_plugins,
    export_plugin_skill,
    export_skill,
    install_organization_skills,
    plugin_roots,
    skill_inventory,
)
from agent_platform_core.hook_inventory import hook_inventory  # noqa: E402
from agent_platform_core.managed_skills import recorded_from_receipt  # noqa: E402
from agent_platform_core.managed_skills import default_backup_root  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-organization")
    install.add_argument("--provider", choices=("codex", "claude", "kimi", "opencode"), required=True)
    install.add_argument("--skills-root", type=Path, required=True)
    install.add_argument("--staging", type=Path, required=True)
    install.add_argument("--delivered", type=Path, required=True, help="JSON list from the desired state")
    install.add_argument("--projects", type=Path, required=True, help="JSON list of {projectId, projectRoot}")
    install.add_argument("--receipt-root", type=Path, default=Path.home() / ".agent-platform/organization-skills")
    install.add_argument("--backup-root", type=Path)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--provider", choices=("codex", "claude", "kimi", "opencode"), required=True)
    inventory.add_argument("--skills-root", type=Path, required=True)
    inventory.add_argument("--platform-receipt", type=Path, required=True)
    inventory.add_argument("--receipt-root", type=Path, default=Path.home() / ".agent-platform/organization-skills")
    inventory.add_argument("--claude-root", type=Path, default=Path.home() / ".claude")
    export = commands.add_parser("export")
    export.add_argument("--skills-root", type=Path, required=True)
    export.add_argument("--skill-id", required=True)
    export.add_argument("--provider", choices=("codex", "claude"))
    export.add_argument("--plugin-id", help="read the skill from this plugin of the app, not from the skills folder")
    hooks = commands.add_parser("hooks", help="hooks and mods the app has: events and owners, never commands")
    hooks.add_argument("--provider", choices=("codex", "claude", "kimi", "opencode"), required=True)
    hooks.add_argument("--skills-root", type=Path, required=True)
    hooks.add_argument("--platform-receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "hooks":
        try:
            receipt = json.loads(args.platform_receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        root = args.skills_root.parent
        result = hook_inventory(args.provider, provider_root=root, plugins=plugin_roots(args.provider, root),
                                platform_skills=set(recorded_from_receipt(receipt if isinstance(receipt, dict) else {})))
    elif args.command == "export":
        # The app's home is the folder its skills directory lives in (~/.claude, ~/.codex).
        result = (export_plugin_skill(provider=args.provider, provider_root=args.skills_root.parent,
                                      plugin_id=args.plugin_id, skill_id=args.skill_id)
                  if args.plugin_id and args.provider
                  else export_skill(skills_root=args.skills_root, skill_id=args.skill_id))
    elif args.command == "install-organization":
        result = install_organization_skills(
            provider=args.provider, skills_root=args.skills_root, staging=args.staging,
            delivered=json.loads(args.delivered.read_text(encoding="utf-8")),
            projects=json.loads(args.projects.read_text(encoding="utf-8")),
            receipt_root=args.receipt_root, backup_root=args.backup_root or default_backup_root(args.provider),
        )
    else:
        plugins = app_plugins(args.provider, args.claude_root if args.provider == "claude" else args.skills_root.parent)
        shipped = ROOT / "shared" / "skills"
        result = skill_inventory(skills_root=args.skills_root, platform_receipt=args.platform_receipt,
                                 organization_receipt=args.receipt_root / f"{args.provider}.json", plugins=plugins,
                                 platform_names={path.name for path in shipped.iterdir() if path.is_dir()} if shipped.is_dir() else None)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
