"""Installing an approved release into the apps it has installers for.

Codex and Claude Code are installed by the release's own installers, started here one app at
a time, and each keeps a receipt in its home: that receipt against the desired state is what
says whether the computer has the release. The profiles of the mapped projects and the
context passports are installed for the same app right after it.

The order around it stays with the entry point (`run_installers`): the bundle is verified and
unpacked before, the client itself is installed after.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base,
the projects and the skills.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from sync_base import installer_accepts, read_receipt, skills_digest, state_digest
from sync_projects import (install_context_passports, project_profile_report, project_receipt_changed,
                           read_project_receipt)
from sync_skills import skill_delivery

def needs_update(receipt: dict, desired: dict) -> bool:
    release = desired.get("release") if isinstance(desired.get("release"), dict) else {}
    return (
        receipt.get("release_id") != release.get("releaseId")
        or receipt.get("external_skill_registry_digest") != desired["externalSkillState"].get("registryDigest")
        or receipt.get("external_skill_state_digest") != state_digest(desired["externalSkillState"])
        # Receipts from installers that predate skill delivery lack the key; comparing it would
        # reinstall on every run after a rollback to such a release.
        or ("skill_delivery" in receipt and receipt["skill_delivery"] != skill_delivery(desired))
    )


# Apps a release is installed into by its own installer, with a receipt in the app's home.
RELEASE_PROVIDERS = ("codex", "claude")


def install_release_into_app(
    provider: str,
    args: argparse.Namespace,
    desired: dict,
    temporary: Path,
    projects: list[dict],
    platform_root: Path,
    archive: Path,
    state_path: Path,
    delivery_path: Path,
    reports: list[dict],
) -> None:
    """Install the verified release into one app, then the profiles of its projects, and add
    the app's report to ``reports``.

    The body of the loop over the apps in the entry point's run_installers, which keeps the
    order around it: the bundle is verified and unpacked before, the client itself is
    installed after. A failure is the app's report and never an exception: the other apps
    are still installed."""
    provider_started_at = time.monotonic()
    reconciliation_path = temporary / f"{provider}-reconciliation.json"
    root = args.codex_root if provider == "codex" else args.claude_root
    before = read_receipt(root)
    if provider == "codex":
        installer = platform_root / "adapters" / "codex" / "scripts" / "install_codex_release.py"
        command = [
            sys.executable, str(installer), "--bundle", str(archive),
            "--codex-root", str(args.codex_root),
            "--external-skill-state-manifest", str(state_path),
            "--external-skill-reconciliation-receipt", str(reconciliation_path),
        ]
        if installer_accepts(installer, "--skill-delivery-manifest"):
            command += ["--skill-delivery-manifest", str(delivery_path)]
    else:
        bootstrap = platform_root / "adapters" / "claude" / "scripts" / "bootstrap_claude.py"
        command = [
            sys.executable, str(bootstrap), "--bundle", str(archive),
            "--claude-root", str(args.claude_root),
            "--claude-config", str(args.claude_config),
            "--codex-config", str(args.codex_root / "config.toml"),
            "--bin-dir", str(args.bin_dir),
            "--external-skill-state-manifest", str(state_path),
            "--external-skill-reconciliation-receipt", str(reconciliation_path),
            "--skip-memory",
        ]
        if installer_accepts(bootstrap, "--skill-delivery-manifest"):
            command += ["--skill-delivery-manifest", str(delivery_path)]
    try:
        subprocess.run(command, check=True)
        receipt = read_receipt(root)
        receipt["external_skill_state_digest"] = state_digest(desired["externalSkillState"])
        receipt_path = root / "agent-platform-install.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append({
            "provider": provider,
            "releaseId": receipt.get("release_id"),
            "agentPlatformSha": receipt.get("agent_platform_sha"),
            "skillCount": len(receipt.get("skills", [])),
            "skillsDigest": skills_digest(receipt),
            "status": "updated" if before != receipt else "current",
            "restartRequired": before != receipt,
            "durationMs": int((time.monotonic() - provider_started_at) * 1000),
            "error": None,
            # Platform skills left out because the user has a skill with that name.
            "skillConflicts": sorted(set(receipt.get("skill_conflicts") or [])),
        })
        reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        if not isinstance(reconciliation, dict) or reconciliation.get("provider") != provider:
            raise RuntimeError(f"{provider} installer returned an invalid reconciliation receipt")
        project_reports = []
        project_errors = []
        project_installer = platform_root / "tools" / "project_profiles.py"
        for project in projects:
            previous_project_receipt = read_project_receipt(project, provider)
            try:
                assignment_file = temporary / f"project-assignment-{project['projectId']}.json"
                assignment_file.write_text(
                    json.dumps(project["assignment"], ensure_ascii=False),
                    encoding="utf-8",
                )
                assignment_file.chmod(0o600)
                completed = subprocess.run([
                    sys.executable, str(project_installer), "--root", str(platform_root), "install",
                    "--project-root", str(project["projectRoot"]), "--provider", provider,
                    "--assignment-file", str(assignment_file),
                ], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
                installed_receipt = json.loads(completed.stdout)
                project_reports.append(project_profile_report(
                    installed_receipt,
                    status="updated" if project_receipt_changed(previous_project_receipt, installed_receipt) else "current",
                ))
            except Exception as error:
                project_errors.append(f"{project['projectId']}: {error}")
                if previous_project_receipt:
                    project_reports.append(project_profile_report(
                        previous_project_receipt, status="failed", error=str(error)[:1000]
                    ))
        reports[-1]["projectProfiles"] = project_reports
        reports[-1]["contextPassports"] = install_context_passports(desired, provider)
        if any(item["status"] == "updated" for item in project_reports):
            reports[-1]["restartRequired"] = True
        if project_errors:
            reports[-1]["status"] = "failed"
            reports[-1]["error"] = "; ".join(project_errors)[:1000]
        reports[-1]["durationMs"] = int((time.monotonic() - provider_started_at) * 1000)
    except Exception as error:
        if reports and reports[-1].get("provider") == provider and reports[-1].get("status") != "failed":
            reports[-1]["status"] = "failed"
            reports[-1]["error"] = str(error)[:1000]
            return
        reports.append({
            "provider": provider,
            "releaseId": before.get("release_id"),
            "agentPlatformSha": before.get("agent_platform_sha"),
            "skillCount": len(before.get("skills", [])),
            "skillsDigest": skills_digest(before),
            "status": "failed",
            "restartRequired": False,
            "durationMs": int((time.monotonic() - provider_started_at) * 1000),
            "error": str(error)[:1000],
        })
