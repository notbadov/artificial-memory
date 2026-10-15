"""Validated project-profile catalog and deterministic project-local installation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PARAMETER_ID = re.compile(r"^[a-z][A-Za-z0-9]*$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
PROVIDERS = {"codex", "claude"}
LIFECYCLES = {"approved", "pilot", "blocked", "archived"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def https_origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        port_number = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return None
    port = f":{port_number}" if port_number and port_number != 443 else ""
    return f"https://{parsed.hostname.lower()}{port}"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_project_profile_catalog(platform_root: Path) -> dict:
    path = platform_root / "shared/project-profiles/catalog.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_project_profile_catalog(value, platform_root)
    errors.extend(validate_profile_components_against_registry(value, platform_root))
    if errors:
        raise ValueError("invalid project profile catalog: " + "; ".join(errors))
    return value


def validate_profile_components_against_registry(catalog: dict, platform_root: Path) -> list[str]:
    """Keep profile selection inside the separately reviewed external-skill inventory."""

    from .external_skills import (
        load_external_skill_component_manifests,
        load_external_skill_registry,
    )

    errors: list[str] = []
    registry = load_external_skill_registry(platform_root)
    packages = {item["id"]: item for item in registry["skills"]}
    manifests = load_external_skill_component_manifests(platform_root, registry)
    for profile in catalog.get("profiles", []):
        if not isinstance(profile, dict):
            continue
        for component in profile.get("components", []):
            if not isinstance(component, dict):
                continue
            package_id = component.get("packageId")
            package = packages.get(package_id)
            label = f"{profile.get('id')}.{component.get('componentId')}"
            if package is None:
                errors.append(f"{label}: external package is not registered")
                continue
            if package["deployment"]["installationScope"] != "project-profile":
                errors.append(f"{label}: external package is not project-profile scoped")
            if component.get("upstreamCommit") != package["upstream"]["pinnedCommit"]:
                errors.append(f"{label}: upstream commit differs from the external registry")
            if component.get("license") != package["upstream"]["license"]:
                errors.append(f"{label}: license differs from the external registry")
            if package["deployment"]["packageKind"] == "adapted-wrapper":
                if profile.get("lifecycle") != "pilot":
                    errors.append(f"{label}: adapted wrapper must remain a pilot profile")
                expected_source = f"shared/external-skills/adapters/{package_id}"
                if component.get("source") != expected_source:
                    errors.append(f"{label}: adapted wrapper source must be {expected_source}")
                continue
            manifest = manifests.get(package_id)
            selected = next(
                (item for item in (manifest or {}).get("components", [])
                 if item.get("id") == component.get("componentId")),
                None,
            )
            if selected is None:
                errors.append(f"{label}: component is absent from the reviewed manifest")
            elif selected["lifecycle"] != "approved":
                errors.append(f"{label}: component is not approved")
            elif selected["permissions"]["externalMutation"]:
                errors.append(f"{label}: externally mutating component cannot enter a profile")
    return errors


def validate_project_profile_catalog(value: object, platform_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or value.get("version") != 1:
        return ["project profile catalog version must be 1"]
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        return ["project profile catalog must contain profiles"]
    seen: set[str] = set()
    for index, profile in enumerate(profiles):
        label = f"profiles[{index}]"
        if not isinstance(profile, dict):
            errors.append(f"{label} must be an object")
            continue
        identifier = profile.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            errors.append(f"{label}.id is invalid")
            continue
        label = identifier
        if identifier in seen:
            errors.append(f"duplicate project profile id: {identifier}")
        seen.add(identifier)
        revision = profile.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            errors.append(f"{label}.revision must be a positive integer")
        for field in ("displayName", "summary"):
            if not isinstance(profile.get(field), str) or not profile[field].strip():
                errors.append(f"{label}.{field} must not be empty")
        if profile.get("lifecycle") not in LIFECYCLES:
            errors.append(f"{label}.lifecycle is invalid")
        if profile.get("invocationMode") not in {"implicit-or-explicit", "explicit-only"}:
            errors.append(f"{label}.invocationMode is invalid")
        permissions = profile.get("permissions")
        if not isinstance(permissions, dict) or set(permissions) != {
            "network", "projectWrite", "externalMutation"
        } or any(not isinstance(item, bool) for item in permissions.values()):
            errors.append(f"{label}.permissions is invalid")
        elif permissions["externalMutation"]:
            errors.append(f"{label}: project profiles cannot authorize external mutations")
        parameter_fields = profile.get("parameterFields")
        if not isinstance(parameter_fields, list):
            errors.append(f"{label}.parameterFields must be an array")
            parameter_fields = []
        parameter_ids: set[str] = set()
        for field_index, field in enumerate(parameter_fields):
            field_label = f"{label}.parameterFields[{field_index}]"
            if not isinstance(field, dict) or set(field) != {
                "id", "label", "description", "type", "requiredWhenEnabled"
            }:
                errors.append(f"{field_label} has invalid fields")
                continue
            field_id = field.get("id")
            if not isinstance(field_id, str) or not PARAMETER_ID.fullmatch(field_id):
                errors.append(f"{field_label}.id is invalid")
            elif field_id in parameter_ids:
                errors.append(f"{label}: duplicate parameter field {field_id}")
            else:
                parameter_ids.add(field_id)
            if field.get("type") not in {"text", "https-url"}:
                errors.append(f"{field_label}.type is invalid")
            if not isinstance(field.get("requiredWhenEnabled"), bool):
                errors.append(f"{field_label}.requiredWhenEnabled must be boolean")
            for text_field in ("label", "description"):
                if not isinstance(field.get(text_field), str) or not field[text_field].strip():
                    errors.append(f"{field_label}.{text_field} must not be empty")
        grant_policy = profile.get("grantPolicy")
        network_policy = grant_policy.get("network") if isinstance(grant_policy, dict) else None
        if not isinstance(grant_policy, dict) or set(grant_policy) != {"network"} or not isinstance(network_policy, dict):
            errors.append(f"{label}.grantPolicy is invalid")
        elif isinstance(permissions, dict) and permissions.get("network"):
            if set(network_policy) != {"mode", "parameterId"} or network_policy.get("mode") != "origin-parameter":
                errors.append(f"{label}.grantPolicy.network must bind one origin parameter")
            else:
                field_id = network_policy.get("parameterId")
                field = next((item for item in parameter_fields if isinstance(item, dict) and item.get("id") == field_id), None)
                if field is None or field.get("type") != "https-url" or field.get("requiredWhenEnabled") is not True:
                    errors.append(f"{label}.grantPolicy.network parameter must be a required HTTPS URL")
        elif set(network_policy) != {"mode"} or network_policy.get("mode") != "deny":
            errors.append(f"{label}.grantPolicy.network must deny network access")
        prerequisites = profile.get("prerequisites")
        if not isinstance(prerequisites, list) or not prerequisites or any(
            not isinstance(item, str) or not item.strip() for item in prerequisites
        ):
            errors.append(f"{label}.prerequisites must contain non-empty strings")
        components = profile.get("components")
        if not isinstance(components, list) or not components:
            errors.append(f"{label}.components must not be empty")
            continue
        component_ids: set[str] = set()
        for component_index, component in enumerate(components):
            component_label = f"{label}.components[{component_index}]"
            if not isinstance(component, dict):
                errors.append(f"{component_label} must be an object")
                continue
            if set(component) != {"packageId", "componentId", "source", "upstreamCommit", "license"}:
                errors.append(f"{component_label} has invalid fields")
            for field in ("packageId", "componentId"):
                item = component.get(field)
                if not isinstance(item, str) or not IDENTIFIER.fullmatch(item):
                    errors.append(f"{component_label}.{field} is invalid")
            component_id = component.get("componentId")
            if component_id in component_ids:
                errors.append(f"{label}: duplicate component {component_id}")
            if isinstance(component_id, str):
                component_ids.add(component_id)
            source = component.get("source")
            if not isinstance(source, str) or source.startswith("/") or ".." in Path(source).parts:
                errors.append(f"{component_label}.source is invalid")
            elif platform_root is not None:
                source_root = platform_root / source
                if not (source_root / "SKILL.md").is_file():
                    errors.append(f"{component_label}.source has no SKILL.md: {source}")
            commit = component.get("upstreamCommit")
            if not isinstance(commit, str) or not COMMIT_SHA.fullmatch(commit):
                errors.append(f"{component_label}.upstreamCommit is invalid")
            if not isinstance(component.get("license"), str) or not component["license"].strip():
                errors.append(f"{component_label}.license must not be empty")
    return errors


def _assignment_catalog(value: object, catalog: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return catalog, errors
    custom_profiles = value.get("customProfiles", [])
    if not isinstance(custom_profiles, list) or len(custom_profiles) > 50:
        return catalog, ["customProfiles must be an array with at most 50 items"]
    if not custom_profiles:
        return catalog, errors
    component_registry: dict[tuple[str, str], dict] = {}
    for profile in catalog.get("profiles", []):
        for component in profile.get("components", []):
            key = (component.get("packageId"), component.get("componentId"))
            if all(isinstance(item, str) for item in key):
                component_registry.setdefault(key, component)
    system_ids = {profile.get("id") for profile in catalog.get("profiles", [])}
    custom_ids: set[str] = set()
    resolved = []
    expected_fields = {
        "id", "revision", "displayName", "summary", "lifecycle", "invocationMode",
        "permissions", "grantPolicy", "parameterFields", "components",
    }
    for index, raw in enumerate(custom_profiles):
        label = f"customProfiles[{index}]"
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            errors.append(f"{label} has invalid fields")
            continue
        profile_id = raw.get("id")
        if (not isinstance(profile_id, str) or not IDENTIFIER.fullmatch(profile_id)
                or profile_id in system_ids or profile_id in custom_ids):
            errors.append(f"{label}.id is invalid or duplicated")
            continue
        custom_ids.add(profile_id)
        if raw.get("lifecycle") not in {"approved", "archived"}:
            errors.append(f"{profile_id}.lifecycle is invalid")
        permissions = raw.get("permissions")
        if (not isinstance(permissions, dict)
                or set(permissions) != {"network", "projectWrite", "externalMutation"}
                or type(permissions.get("projectWrite")) is not bool
                or permissions.get("network") is not False
                or permissions.get("externalMutation") is not False):
            errors.append(f"{profile_id}.permissions exceed organization bundle policy")
        if raw.get("grantPolicy") != {"network": {"mode": "deny"}}:
            errors.append(f"{profile_id}.grantPolicy must deny network")
        if raw.get("parameterFields") != []:
            errors.append(f"{profile_id}.parameterFields must be empty")
        component_refs = raw.get("components")
        if not isinstance(component_refs, list) or not component_refs:
            errors.append(f"{profile_id}.components must not be empty")
            continue
        resolved_components = []
        seen_components: set[tuple[str, str]] = set()
        for component_index, component in enumerate(component_refs):
            if not isinstance(component, dict) or set(component) != {"packageId", "componentId"}:
                errors.append(f"{profile_id}.components[{component_index}] has invalid fields")
                continue
            key = (component.get("packageId"), component.get("componentId"))
            source = component_registry.get(key)
            if source is None:
                errors.append(f"{profile_id}.components[{component_index}] is absent from the signed catalog")
                continue
            if key in seen_components:
                errors.append(f"{profile_id} contains a duplicate component")
                continue
            seen_components.add(key)
            resolved_components.append(dict(source))
        resolved.append({
            **raw,
            "prerequisites": ["Composed from approved signed catalog components."],
            "components": resolved_components,
        })
    effective = {"version": 1, "profiles": [*catalog.get("profiles", []), *resolved]}
    errors.extend(validate_project_profile_catalog(effective))
    return effective, errors


def validate_project_assignment(value: object, catalog: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or value.get("version") != 1:
        return ["project profile assignment version must be 1"]
    allowed = {"version", "projectId", "profiles", "customProfiles"}
    if set(value) - allowed:
        errors.append("project profile assignment has unknown fields")
    project_id = value.get("projectId")
    if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
        errors.append("projectId is invalid")
    assignments = value.get("profiles")
    if not isinstance(assignments, list):
        errors.append("profiles must be an array")
        return errors
    effective_catalog, custom_errors = _assignment_catalog(value, catalog)
    errors.extend(custom_errors)
    by_id = {profile["id"]: profile for profile in effective_catalog["profiles"]}
    seen: set[str] = set()
    for index, assignment in enumerate(assignments):
        label = f"profiles[{index}]"
        if not isinstance(assignment, dict) or set(assignment) != {
            "profileId", "expectedRevision", "desiredState", "parameters"
        }:
            errors.append(f"{label} has invalid fields")
            continue
        profile_id = assignment.get("profileId")
        if profile_id in seen:
            errors.append(f"duplicate assignment: {profile_id}")
        if isinstance(profile_id, str):
            seen.add(profile_id)
        profile = by_id.get(profile_id)
        if profile is None:
            errors.append(f"{label}.profileId is unknown")
            continue
        if assignment.get("expectedRevision") != profile["revision"]:
            errors.append(f"{profile_id}: expectedRevision does not match catalog")
        if assignment.get("desiredState") not in {"enabled", "disabled"}:
            errors.append(f"{profile_id}: desiredState is invalid")
        parameters = assignment.get("parameters")
        if not isinstance(parameters, dict):
            errors.append(f"{profile_id}: parameters must be an object")
        if isinstance(parameters, dict):
            parameter_fields = {field["id"]: field for field in profile.get("parameterFields", [])}
            unknown = sorted(set(parameters) - set(parameter_fields))
            if unknown:
                errors.append(f"{profile_id}: unknown parameters: {', '.join(unknown)}")
            for field_id, field in parameter_fields.items():
                parameter = parameters.get(field_id)
                if (assignment.get("desiredState") == "enabled" and field["requiredWhenEnabled"]
                        and (not isinstance(parameter, str) or not parameter.strip())):
                    errors.append(f"{profile_id} requires {field_id}")
                if parameter is not None and field["type"] == "text" and not isinstance(parameter, str):
                    errors.append(f"{profile_id}.{field_id} must be text")
                if parameter is not None and field["type"] == "https-url" and https_origin(parameter) is None:
                    errors.append(f"{profile_id}.{field_id} must be an HTTPS URL")
    return errors


def project_profile_grant(
    *, project_id: str, profile: dict, parameters: dict,
) -> dict:
    network_origins = []
    network_policy = profile["grantPolicy"]["network"]
    if network_policy["mode"] == "origin-parameter":
        allowed_origin = https_origin(parameters.get(network_policy["parameterId"]))
        if allowed_origin:
            network_origins.append(allowed_origin)
    grant = {
        "version": 1,
        "projectId": project_id,
        "profileId": profile["id"],
        "profileRevision": profile["revision"],
        "permissions": profile["permissions"],
        "networkPolicy": {
            "mode": "origin-allowlist" if network_origins else "deny",
            "origins": network_origins,
        },
        "parameters": parameters,
    }
    return {**grant, "grantDigest": canonical_json_digest(grant)}


def install_project_profiles(
    *, platform_root: Path, project_root: Path, provider: str, state_root: Path,
    assignment: dict | None = None,
) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported project-profile provider: {provider}")
    catalog = load_project_profile_catalog(platform_root)
    if assignment is None:
        assignment_path = project_root / ".agent-platform/project-profiles.json"
        assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    errors = validate_project_assignment(assignment, catalog)
    if errors:
        raise ValueError("invalid project profile assignment: " + "; ".join(errors))
    catalog, _ = _assignment_catalog(assignment, catalog)

    project_id = assignment["projectId"]
    mapping_id = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    receipt_dir = state_root / project_id / mapping_id
    receipt_path = receipt_dir / f"{provider}.json"
    previous = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else None
    skills_root = project_root / (".agents/skills" if provider == "codex" else ".claude/skills")
    profiles = {profile["id"]: profile for profile in catalog["profiles"]}
    enabled = [item for item in assignment["profiles"] if item["desiredState"] == "enabled"]
    desired: dict[str, dict] = {}
    grants: dict[str, dict] = {}
    for item in enabled:
        profile = profiles[item["profileId"]]
        if profile["lifecycle"] not in {"approved", "pilot"}:
            raise ValueError(f"project profile is not installable: {profile['id']}")
        grant = project_profile_grant(
            project_id=project_id, profile=profile,
            parameters=item["parameters"],
        )
        grants[profile["id"]] = grant
        for component in profile["components"]:
            destination_name = f"agent-platform--{component['componentId']}"
            source = platform_root / component["source"]
            desired[destination_name] = {
                "profileId": profile["id"],
                "profileRevision": profile["revision"],
                "componentId": component["componentId"],
                "packageId": component["packageId"],
                "source": component["source"],
                "sourceDigest": tree_digest(source),
                "upstreamCommit": component["upstreamCommit"],
                "license": component["license"],
                "grant": grant,
            }

    managed_previous = {
        item["destination"]: item for item in (previous or {}).get("components", [])
        if isinstance(item, dict) and isinstance(item.get("destination"), str)
    }
    for destination_name in sorted(desired):
        destination = skills_root / destination_name
        if not destination.exists():
            continue
        current = tree_digest(destination) if destination.is_dir() else None
        previous_digest = managed_previous.get(destination_name, {}).get("installedDigest")
        if previous_digest is None or current != previous_digest:
            raise ValueError(f"refusing to overwrite unmanaged or changed project skill: {destination_name}")
    grant_root = project_root / ".agent-platform/profile-grants"
    for old_grant in (previous or {}).get("grants", []):
        old_profile_id = old_grant.get("profileId") if isinstance(old_grant, dict) else None
        if isinstance(old_profile_id, str) and old_profile_id not in grants:
            old_path = grant_root / f"{old_profile_id}.json"
            if old_path.is_file():
                old_path.unlink()
    if grants:
        grant_root.mkdir(parents=True, exist_ok=True)
        for profile_id, grant in sorted(grants.items()):
            grant_path = grant_root / f"{profile_id}.json"
            grant_path.write_text(
                json.dumps(grant, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            grant_path.chmod(0o600)
    for destination_name, old in managed_previous.items():
        if destination_name in desired:
            continue
        destination = skills_root / destination_name
        if destination.is_dir() and tree_digest(destination) == old.get("installedDigest"):
            shutil.rmtree(destination)

    installed: list[dict] = []
    skills_root.mkdir(parents=True, exist_ok=True)
    for destination_name, metadata in sorted(desired.items()):
        source = platform_root / metadata["source"]
        destination = skills_root / destination_name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        public_metadata = {key: value for key, value in metadata.items() if key != "grant"}
        installed.append({
            **public_metadata,
            "destination": destination_name,
            "grantDigest": metadata["grant"]["grantDigest"],
            "installedDigest": tree_digest(destination),
        })

    receipt = {
        "version": 1,
        "projectId": project_id,
        "mappingId": mapping_id,
        "provider": provider,
        "assignmentDigest": canonical_json_digest(assignment),
        "catalogDigest": canonical_json_digest(catalog),
        "profiles": [
            {"profileId": item["profileId"], "revision": profiles[item["profileId"]]["revision"],
             "grantDigest": grants[item["profileId"]]["grantDigest"]}
            for item in enabled
        ],
        "grants": [grants[item["profileId"]] for item in enabled],
        "components": installed,
        "installedAt": utc_now(),
    }
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    _exclude_managed_project_skills(project_root, provider)
    return receipt


def _exclude_managed_project_skills(project_root: Path, provider: str) -> None:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return
    exclude = git_dir / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    pattern = "/.agents/skills/agent-platform--*/" if provider == "codex" else "/.claude/skills/agent-platform--*/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    patterns = [pattern, "/.agent-platform/profile-grants/"]
    missing = [item for item in patterns if item not in existing.splitlines()]
    if missing:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(existing + separator + "\n".join(missing) + "\n", encoding="utf-8")
