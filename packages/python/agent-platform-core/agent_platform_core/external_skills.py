"""Canonical external-skill registry validation and read-only update checks."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


PROVIDERS = ("codex", "claude", "kimi", "opencode")
LIFECYCLES = ("candidate", "approved", "blocked", "archived")
PROVIDER_STATES = ("supported", "planned", "unsupported")
RISK_LEVELS = ("low", "medium", "high")
TRACKING_MODES = ("default-branch", "releases")
PACKAGE_KINDS = ("standalone", "component-pack", "adapted-wrapper")
INSTALLATION_SCOPES = ("global", "project-profile")
INVOCATION_MODES = ("implicit-or-explicit", "explicit-only")
INTAKE_DECISIONS = ("approved", "pilot", "blocked")
INTAKE_CHECK_STATES = ("passed", "conditional", "blocked")
IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_external_skill_registry(platform_root: Path) -> dict:
    path = platform_root / "shared/external-skills/registry.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_external_skill_registry(value, platform_root)
    if errors:
        raise ValueError("invalid external skill registry: " + "; ".join(errors))
    return value


def load_external_skill_component_manifests(platform_root: Path, registry: dict) -> dict[str, dict]:
    """Load reviewed component manifests referenced by external skill packages."""

    manifests: dict[str, dict] = {}
    for skill in registry["skills"]:
        deployment = skill["deployment"]
        relative = deployment.get("componentManifest")
        if relative is None:
            continue
        path = platform_root / relative
        value = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_external_skill_component_manifest(value, skill)
        if errors:
            raise ValueError(
                f"invalid external skill component manifest {relative}: " + "; ".join(errors)
            )
        manifests[skill["id"]] = value
    return manifests


def external_skill_registry_digest(registry: dict) -> str:
    encoded = json.dumps(
        registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_external_skill_state_manifest(registry: dict) -> dict:
    """Make registry defaults explicit for a bootstrap without runtime rows."""

    return {
        "version": 1,
        "registryDigest": external_skill_registry_digest(registry),
        "source": "registry-defaults",
        "generatedAt": utc_now(),
        "skills": [
            {"id": skill["id"], "enabled": skill["defaultEnabled"], "revision": 0}
            for skill in registry["skills"]
        ],
    }


def validate_external_skill_state_manifest(manifest: object, registry: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["external skill state manifest root must be an object"]
    allowed = {"version", "registryDigest", "source", "generatedAt", "skills"}
    unknown = set(manifest) - allowed
    if unknown:
        errors.append("external skill state manifest has unknown fields: " + ", ".join(sorted(unknown)))
    if manifest.get("version") != 1:
        errors.append("external skill state manifest version must be 1")
    if manifest.get("registryDigest") != external_skill_registry_digest(registry):
        errors.append("external skill state manifest registryDigest does not match the release registry")
    if manifest.get("source") not in {"runtime", "registry-defaults"}:
        errors.append("external skill state manifest source is invalid")
    if not isinstance(manifest.get("generatedAt"), str) or not manifest["generatedAt"].strip():
        errors.append("external skill state manifest generatedAt must not be empty")
    expected = {skill["id"] for skill in registry["skills"]}
    states = manifest.get("skills")
    if not isinstance(states, list):
        errors.append("external skill state manifest skills must be an array")
        return errors
    actual: set[str] = set()
    for index, state in enumerate(states):
        if not isinstance(state, dict):
            errors.append(f"external skill state manifest skills[{index}] must be an object")
            continue
        if set(state) != {"id", "enabled", "revision"}:
            errors.append(f"external skill state manifest skills[{index}] has invalid fields")
        identifier = state.get("id")
        if not isinstance(identifier, str) or identifier not in expected:
            errors.append(f"external skill state manifest references unknown skill: {identifier!r}")
            continue
        if identifier in actual:
            errors.append(f"external skill state manifest duplicates skill: {identifier}")
        actual.add(identifier)
        if not isinstance(state.get("enabled"), bool):
            errors.append(f"external skill state manifest enabled must be boolean: {identifier}")
        revision = state.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            errors.append(f"external skill state manifest revision is invalid: {identifier}")
    missing = sorted(expected - actual)
    if missing:
        errors.append("external skill state manifest is missing skills: " + ", ".join(missing))
    return errors


def validate_external_skill_registry(value: object, platform_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["registry root must be an object"]
    if value.get("version") != 2:
        errors.append("external skill registry version must be 2")
    skills = value.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("external skill registry must contain skills")
        return errors

    seen: set[str] = set()
    for index, skill in enumerate(skills):
        label = f"skills[{index}]"
        if not isinstance(skill, dict):
            errors.append(f"{label} must be an object")
            continue
        identifier = skill.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            errors.append(f"{label}.id is invalid")
            continue
        label = identifier
        if identifier in seen:
            errors.append(f"duplicate external skill id: {identifier}")
        seen.add(identifier)
        _require_string(skill, "displayName", label, errors)
        lifecycle = skill.get("lifecycle")
        if lifecycle not in LIFECYCLES:
            errors.append(f"{label}.lifecycle is invalid")
        enabled = skill.get("defaultEnabled")
        if not isinstance(enabled, bool):
            errors.append(f"{label}.defaultEnabled must be boolean")
        if enabled and lifecycle != "approved":
            errors.append(f"{label}: only approved skills may be enabled by default")

        description = skill.get("description")
        if not isinstance(description, dict):
            errors.append(f"{label}.description must be an object")
        else:
            if description.get("locale") != "ru":
                errors.append(f"{label}.description.locale must be ru")
            for field in ("summary", "details"):
                _require_string(description, field, f"{label}.description", errors)
            for field in ("useWhen", "avoidWhen", "examples"):
                _require_string_list(description, field, f"{label}.description", errors, nonempty=True)

        upstream = skill.get("upstream")
        if not isinstance(upstream, dict):
            errors.append(f"{label}.upstream must be an object")
        else:
            repository = upstream.get("repository")
            if not isinstance(repository, str) or github_repository_slug(repository) is None:
                errors.append(f"{label}.upstream.repository must be a canonical GitHub HTTPS URL")
            pinned = upstream.get("pinnedCommit")
            if not isinstance(pinned, str) or not COMMIT_SHA.fullmatch(pinned):
                errors.append(f"{label}.upstream.pinnedCommit must be a full lowercase commit SHA")
            _require_string(upstream, "license", f"{label}.upstream", errors)
            tracking = upstream.get("tracking")
            if not isinstance(tracking, dict) or tracking.get("mode") not in TRACKING_MODES:
                errors.append(f"{label}.upstream.tracking is invalid")
            elif not isinstance(tracking.get("ref"), str) or not tracking["ref"].strip():
                errors.append(f"{label}.upstream.tracking.ref must not be empty")

        providers = skill.get("providers")
        if not isinstance(providers, dict) or set(providers) != set(PROVIDERS):
            errors.append(f"{label}.providers must declare every canonical provider")
        elif any(state not in PROVIDER_STATES for state in providers.values()):
            errors.append(f"{label}.providers contains an invalid support state")

        runtime = skill.get("runtime")
        if not isinstance(runtime, dict) or not isinstance(runtime.get("requirements"), list):
            errors.append(f"{label}.runtime is invalid")
        elif runtime.get("entrypoint") is not None and not isinstance(runtime.get("entrypoint"), str):
            errors.append(f"{label}.runtime.entrypoint must be a string or null")

        permissions = skill.get("permissions")
        if not isinstance(permissions, dict):
            errors.append(f"{label}.permissions must be an object")
        else:
            for field in ("network", "filesystem", "secrets"):
                _require_string_list(permissions, field, f"{label}.permissions", errors)
            if not isinstance(permissions.get("executesCommands"), bool):
                errors.append(f"{label}.permissions.executesCommands must be boolean")

        risk = skill.get("risk")
        if not isinstance(risk, dict) or risk.get("level") not in RISK_LEVELS:
            errors.append(f"{label}.risk is invalid")
        elif not isinstance(risk.get("notes"), list) or not risk["notes"]:
            errors.append(f"{label}.risk.notes must not be empty")

        deployment = skill.get("deployment")
        if not isinstance(deployment, dict):
            errors.append(f"{label}.deployment must be an object")
        else:
            package_kind = deployment.get("packageKind")
            if package_kind not in PACKAGE_KINDS:
                errors.append(f"{label}.deployment.packageKind is invalid")
            if deployment.get("installationScope") not in INSTALLATION_SCOPES:
                errors.append(f"{label}.deployment.installationScope is invalid")
            if deployment.get("invocationMode") not in INVOCATION_MODES:
                errors.append(f"{label}.deployment.invocationMode is invalid")
            conflict_group = deployment.get("conflictGroup")
            if conflict_group is not None and (
                not isinstance(conflict_group, str) or not IDENTIFIER.fullmatch(conflict_group)
            ):
                errors.append(f"{label}.deployment.conflictGroup is invalid")
            _require_string_list(
                deployment, "projectSurfaces", f"{label}.deployment", errors, nonempty=True
            )
            component_manifest = deployment.get("componentManifest")
            if component_manifest is not None and (
                not isinstance(component_manifest, str)
                or not re.fullmatch(
                    r"shared/external-skills/components/[a-z0-9]+(?:-[a-z0-9]+)*\.json",
                    component_manifest,
                )
            ):
                errors.append(f"{label}.deployment.componentManifest is invalid")
            if package_kind == "component-pack" and component_manifest is None:
                errors.append(f"{label}: component packs require a component manifest")
            if platform_root is not None and component_manifest is not None:
                component_path = platform_root / component_manifest
                if not component_path.is_file():
                    errors.append(f"{label}: component manifest is missing: {component_manifest}")
                else:
                    try:
                        component_value = json.loads(component_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as error:
                        errors.append(f"{label}: component manifest is invalid JSON: {error}")
                    else:
                        errors.extend(validate_external_skill_component_manifest(component_value, skill))
            if platform_root is not None and deployment.get("installationScope") == "project-profile":
                intake_path = platform_root / f"shared/external-skills/intake/{identifier}.json"
                if not intake_path.is_file():
                    errors.append(f"{label}: project-profile package intake review is missing")
                else:
                    try:
                        intake = json.loads(intake_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as error:
                        errors.append(f"{label}: intake review is invalid JSON: {error}")
                    else:
                        errors.extend(validate_external_skill_intake_review(intake, skill, platform_root))

        if platform_root is not None and lifecycle == "approved":
            skill_file = platform_root / "shared/skills" / identifier / "SKILL.md"
            if not skill_file.is_file():
                errors.append(f"approved external skill is missing canonical source: {identifier}")
            elif isinstance(upstream, dict) and isinstance(upstream.get("pinnedCommit"), str):
                match = re.search(
                    r"^\s*upstream_commit:\s*['\"]?([0-9a-f]{40})['\"]?\s*$",
                    skill_file.read_text(encoding="utf-8"),
                    re.MULTILINE,
                )
                if not match or match.group(1) != upstream["pinnedCommit"]:
                    errors.append(f"{identifier}: registry pinned commit differs from canonical SKILL.md")
            entrypoint = runtime.get("entrypoint") if isinstance(runtime, dict) else None
            if entrypoint is not None:
                entrypoint_path = platform_root / "shared/skills" / identifier / entrypoint
                if not entrypoint_path.is_file():
                    errors.append(
                        f"{identifier}: approved external skill runtime entrypoint is missing: {entrypoint}"
                    )
    return errors


def validate_external_skill_intake_review(
    value: object, skill: dict, platform_root: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    label = skill.get("id", "external-skill")
    expected = {
        "version", "skillId", "upstreamCommit", "reviewedAt", "decision",
        "reviewDocument", "scope", "checks",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return [f"{label}: intake review fields are invalid"]
    if value.get("version") != 1 or value.get("skillId") != label:
        errors.append(f"{label}: intake review identity is invalid")
    if value.get("upstreamCommit") != skill.get("upstream", {}).get("pinnedCommit"):
        errors.append(f"{label}: intake review does not match the pinned commit")
    if not isinstance(value.get("reviewedAt"), str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["reviewedAt"]
    ):
        errors.append(f"{label}: intake reviewedAt is invalid")
    if value.get("decision") not in INTAKE_DECISIONS:
        errors.append(f"{label}: intake decision is invalid")
    if value.get("scope") not in {"selected-components", "adapted-wrapper", "complete-package"}:
        errors.append(f"{label}: intake scope is invalid")
    review_document = value.get("reviewDocument")
    if not isinstance(review_document, str) or Path(review_document).is_absolute() or ".." in Path(review_document).parts:
        errors.append(f"{label}: intake review document is invalid")
    elif platform_root is not None and not (platform_root / review_document).is_file():
        errors.append(f"{label}: intake review document is missing")
    checks = value.get("checks")
    required_checks = {"license", "provenance", "staticAudit", "isolation"}
    if not isinstance(checks, dict) or set(checks) != required_checks:
        errors.append(f"{label}: intake checks are incomplete")
        return errors
    states = []
    for check_name, check in checks.items():
        if not isinstance(check, dict) or set(check) != {"status", "evidence"}:
            errors.append(f"{label}.{check_name}: intake check is invalid")
            continue
        state = check.get("status")
        if state not in INTAKE_CHECK_STATES:
            errors.append(f"{label}.{check_name}: intake status is invalid")
        else:
            states.append(state)
        evidence = check.get("evidence")
        if not isinstance(evidence, list) or not evidence or any(
            not isinstance(item, str) or not item.strip() for item in evidence
        ):
            errors.append(f"{label}.{check_name}: intake evidence is required")
    decision = value.get("decision")
    if decision == "approved" and any(state != "passed" for state in states):
        errors.append(f"{label}: approved intake requires every check to pass")
    if decision == "pilot" and "blocked" in states:
        errors.append(f"{label}: pilot intake cannot contain a blocked check")
    if decision != "blocked" and skill.get("lifecycle") == "blocked":
        errors.append(f"{label}: blocked package cannot have a publishable intake decision")
    return errors


def validate_external_skill_component_manifest(value: object, skill: dict) -> list[str]:
    errors: list[str] = []
    label = skill.get("id", "external-skill")
    if not isinstance(value, dict):
        return [f"{label}: component manifest root must be an object"]
    if value.get("version") != 1:
        errors.append(f"{label}: component manifest version must be 1")
    if value.get("skillId") != label:
        errors.append(f"{label}: component manifest skillId does not match")
    upstream = skill.get("upstream", {})
    if value.get("upstreamCommit") != upstream.get("pinnedCommit"):
        errors.append(f"{label}: component manifest upstreamCommit does not match")
    components = value.get("components")
    if not isinstance(components, list) or not components:
        errors.append(f"{label}: component manifest must contain components")
        return errors
    seen: set[str] = set()
    for index, component in enumerate(components):
        component_label = f"{label}.components[{index}]"
        if not isinstance(component, dict):
            errors.append(f"{component_label} must be an object")
            continue
        identifier = component.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            errors.append(f"{component_label}.id is invalid")
            continue
        if identifier in seen:
            errors.append(f"{label}: duplicate component id: {identifier}")
        seen.add(identifier)
        for field in ("displayName", "path", "group", "summary"):
            _require_string(component, field, component_label, errors)
        if component.get("lifecycle") not in {"candidate", "approved", "blocked"}:
            errors.append(f"{component_label}.lifecycle is invalid")
        if component.get("activation") not in {"foundation", "explicit", "side-effect"}:
            errors.append(f"{component_label}.activation is invalid")
        _require_string_list(component, "requires", component_label, errors)
        permissions = component.get("permissions")
        if not isinstance(permissions, dict) or set(permissions) != {
            "network", "projectWrite", "externalMutation"
        }:
            errors.append(f"{component_label}.permissions is invalid")
        elif any(not isinstance(item, bool) for item in permissions.values()):
            errors.append(f"{component_label}.permissions values must be boolean")
        if component.get("activation") == "side-effect" and not (
            isinstance(permissions, dict) and permissions.get("externalMutation") is True
        ):
            errors.append(f"{component_label}: side-effect activation requires externalMutation")
    return errors


def _require_string(value: dict, field: str, label: str, errors: list[str]) -> None:
    if not isinstance(value.get(field), str) or not value[field].strip():
        errors.append(f"{label}.{field} must not be empty")


def _require_string_list(
    value: dict,
    field: str,
    label: str,
    errors: list[str],
    *,
    nonempty: bool = False,
) -> None:
    items = value.get(field)
    if not isinstance(items, list) or (nonempty and not items) or any(
        not isinstance(item, str) or not item.strip() for item in items
    ):
        errors.append(f"{label}.{field} must be an array of non-empty strings")


def github_repository_slug(repository: str) -> str | None:
    parsed = urlparse(repository)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[1].endswith(".git"):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        return None
    return "/".join(parts)
