"""Which platform skills one organization's computers receive.

The skill delivery manifest arrives with the client desired state:

    {"version": 1, "audiences": ["everyone"], "disabled": []}

``disabled`` lists platform skills the organization switched off. Every other skill under
``shared/skills`` is delivered: a release holds product content only, the same for every
organization.

``audiences`` remains a wire field and is not consulted. Earlier releases used it to select
skills and policies of one audience that travelled inside the release and were withheld at
install time, so every computer still downloaded them. What belongs to one organization is
not part of a release: that organization publishes it as its own skills and standing
instructions through Control Plane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Values a deployed Control Plane may send in ``audiences``. They are accepted so that its
# manifest validates and are otherwise unused.
AUDIENCES = ("everyone", "operator")
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_DISABLED = 200

# Managed blocks earlier releases wrote into instruction files and no release writes any more.
# Installers remove them wherever they are found, so a computer never keeps text the platform
# stopped delivering. An id stays here for as long as a computer may still hold its block.
RETIRED_POLICY_BLOCK_IDS = (
    # Two policies of one organization that earlier releases carried; that organization now
    # delivers the text as its own standing instructions.
    "agent-platform-maintainer",
    "owner-conventions",
    # A rule that told every organization to verify each production change with the
    # verify-production-release skill before calling it done. The skill stays for whoever
    # wants it; the obligation is for an organization to set as its own rule.
    "production-release-verification",
    # The same for two more rules: recording a lesson for every escaped defect with
    # the learn-from-defect skill, and turning every request for tasks into work items. Both
    # are for an organization to choose; neither is written into anyone's instructions by
    # default.
    "learning-from-defects",
    "task-creation",
)


def default_skill_delivery_manifest() -> dict:
    return {"version": 1, "audiences": ["everyone"], "disabled": []}


def validate_skill_delivery_manifest(value: object) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"version", "audiences", "disabled"}:
        return ["skill delivery manifest must have exactly version, audiences and disabled"]
    errors: list[str] = []
    if value["version"] != 1:
        errors.append("skill delivery manifest version must be 1")
    audiences = value["audiences"]
    if (not isinstance(audiences, list) or "everyone" not in audiences
            or len(set(audiences)) != len(audiences) or any(item not in AUDIENCES for item in audiences)):
        errors.append("skill delivery audiences must be unique, include everyone, and use known audiences")
    disabled = value["disabled"]
    if (not isinstance(disabled, list) or len(disabled) > MAX_DISABLED
            or len(set(disabled)) != len(disabled)
            or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in disabled)):
        errors.append("skill delivery disabled must be a bounded list of unique skill identifiers")
    return errors


def load_skill_delivery_manifest(path: Path | None) -> dict:
    if path is None:
        return default_skill_delivery_manifest()
    value = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_skill_delivery_manifest(value)
    if errors:
        raise ValueError("invalid skill delivery manifest:\n- " + "\n- ".join(errors))
    return value


def delivered_skill_sources(
    platform_root: Path,
    manifest: dict,
    external_enabled: dict[str, bool],
) -> dict[str, Path]:
    """Skill id -> source directory for every platform skill this computer should hold.

    ``external_enabled`` is the publisher's state for registry skills; a registry skill is
    delivered when it is enabled there and not disabled by the organization.
    """

    disabled = set(manifest["disabled"])
    delivered: dict[str, Path] = {}
    for directory in sorted((platform_root / "shared" / "skills").iterdir()):
        if not directory.is_dir() or not (directory / "SKILL.md").is_file():
            continue
        identifier = directory.name
        if identifier in disabled:
            continue
        if identifier in external_enabled:
            if external_enabled[identifier]:
                delivered[identifier] = directory
            continue
        delivered[identifier] = directory
    return delivered


def policy_block_id(policy: Path) -> str:
    return re.sub(r"[^a-z0-9-]", "-", policy.stem.lower()).strip("-")


def delivered_policies(platform_root: Path) -> list[Path]:
    """The shared policies every computer receives, in a stable order."""

    return sorted((platform_root / "shared" / "policies").glob("*.md"))


def remove_managed_blocks(text: str, block_ids: list[str] | tuple[str, ...]) -> str:
    for block_id in block_ids:
        start = re.escape(f"<!-- agent-platform:{block_id}:start -->")
        end = re.escape(f"<!-- agent-platform:{block_id}:end -->")
        text = re.sub(f"\n?{start}.*?{end}\n?", "\n", text, flags=re.DOTALL)
    return text
