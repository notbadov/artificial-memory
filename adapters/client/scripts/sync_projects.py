"""The projects of this computer: which folder is which project, and what a project brings.

A person maps a folder to a project of the organization (project-mappings.json). The service
then says which profiles the project has and which context passports were published for it.
This file reads the mapping, the receipt of each profile installation and the passports that
were delivered, checks a passport before it is written, and says when either is out of date.
The installation of a profile itself is the release's own tool, started by the entry point.

This file is a part of `sync_client.py`, the entry point. Of the client it imports the base.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from sync_base import _write_json_atomic

CONTEXT_PASSPORT_FIELDS = {
    "schemaVersion", "organizationId", "workspaceId", "projectId", "consumer",
    "targetClient", "authority", "tokenBudget", "status", "items",
    "assemblyDigest", "deliveryReceipt",
}
CONTEXT_PASSPORT_ITEM_FIELDS = {
    "sourceType", "sourceRef", "revision", "provenance", "scope", "validity",
    "sensitivity", "allowedActions", "estimatedTokens", "targetClients",
    "priority", "freshness", "availability", "memoryReviewStatus",
    "effectiveScope", "effectiveAllowedActions", "decision",
}


def load_project_mappings(path: Path, desired_assignments: list[dict] | None = None) -> list[dict]:
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("projects"), list):
        raise SystemExit("project mapping file is invalid")
    project_id_pattern = re.compile(r"(?:[a-z0-9]|[a-z0-9][a-z0-9._-]{0,62}[a-z0-9])")
    remote = {}
    if desired_assignments is not None:
        if not isinstance(desired_assignments, list) or len(desired_assignments) > 100:
            raise SystemExit("project assignments from Control Plane are invalid")
        for assignment in desired_assignments:
            if (not isinstance(assignment, dict) or assignment.get("version") != 1
                    or not isinstance(assignment.get("projectId"), str)
                    or not project_id_pattern.fullmatch(assignment["projectId"])
                    or not isinstance(assignment.get("profiles"), list)
                    or assignment["projectId"] in remote):
                raise SystemExit("project assignments from Control Plane are invalid")
            remote[assignment["projectId"]] = assignment
    normalized = []
    mapped: set[str] = set()
    for item in value["projects"]:
        if (not isinstance(item, dict) or set(item) != {"projectId", "localPath"}
                or not isinstance(item.get("projectId"), str)
                or not project_id_pattern.fullmatch(item["projectId"])
                or item["projectId"] in mapped):
            raise SystemExit("project mapping entry is invalid")
        mapped.add(item["projectId"])
        project_root = Path(item["localPath"]).resolve()
        manifest = project_root / ".agent-platform/project-profiles.json"
        assignment = remote.get(item["projectId"])
        if desired_assignments is not None and assignment is None:
            raise SystemExit(f"project is not registered in Control Plane: {item['projectId']}")
        if assignment is None:
            if not manifest.is_file():
                raise SystemExit(f"project profile assignment is missing: {manifest}")
            assignment = json.loads(manifest.read_text(encoding="utf-8"))
        if assignment.get("projectId") != item["projectId"]:
            raise SystemExit(f"project mapping id differs from assignment: {item['projectId']}")
        normalized.append({"projectId": item["projectId"], "projectRoot": project_root, "assignment": assignment})
    return normalized


def project_mapping_id(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]


def project_assignment_digest(project: dict) -> str:
    value = project["assignment"]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def project_receipt_path(project: dict, provider: str) -> Path:
    return Path.home() / ".agent-platform/projects" / project["projectId"] / project_mapping_id(project["projectRoot"]) / f"{provider}.json"


def read_project_receipt(project: dict, provider: str) -> dict:
    path = project_receipt_path(project, provider)
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def project_profiles_need_update(projects: list[dict], providers: list[str]) -> bool:
    return any(
        read_project_receipt(project, provider).get("assignmentDigest") != project_assignment_digest(project)
        for project in projects for provider in providers
    )


def project_profile_report(receipt: dict, *, status: str = "current", error: str | None = None) -> dict:
    return {
        "projectId": receipt.get("projectId"),
        "mappingId": receipt.get("mappingId"),
        "assignmentDigest": receipt.get("assignmentDigest"),
        "catalogDigest": receipt.get("catalogDigest"),
        "profileCount": len(receipt.get("profiles", [])),
        "componentCount": len(receipt.get("components", [])),
        **({"grantDigest": hashlib.sha256(json.dumps(
            receipt["grants"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()} if "grants" in receipt else {}),
        "status": status,
        "error": error,
    }


def project_receipt_changed(before: dict, after: dict) -> bool:
    fields = ("assignmentDigest", "catalogDigest", "profiles", "components", "grants")
    return any(before.get(field) != after.get(field) for field in fields)


def context_passport_root() -> Path:
    return Path.home() / ".agent-platform/context-passports"


def _context_passport_index(provider: str) -> Path:
    return context_passport_root() / f"index-{provider}.json"


def _context_passport_path(project_id: str, provider: str) -> Path:
    return context_passport_root() / project_id / f"{provider}.json"


def _context_publications(desired: dict, provider: str) -> list[dict]:
    values = desired.get("contextPassports", [])
    if not isinstance(values, list) or len(values) > 100:
        raise SystemExit("Control Plane returned invalid Context Passport desired state")
    result = []
    seen: set[str] = set()
    for value in values:
        if (not isinstance(value, dict)
                or set(value) != {"version", "projectId", "provider", "publicationRevision", "passportDigest", "publishedAt", "passport"}
                or value.get("version") != 1
                or value.get("provider") != provider
                or not isinstance(value.get("projectId"), str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", value["projectId"])
                or value["projectId"] in seen
                or type(value.get("publicationRevision")) is not int
                or value["publicationRevision"] < 1
                or not isinstance(value.get("passportDigest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["passportDigest"])
                or not isinstance(value.get("publishedAt"), str)
                or not 1 <= len(value["publishedAt"]) <= 40):
            if isinstance(value, dict) and value.get("provider") != provider:
                continue
            raise SystemExit("Control Plane returned invalid Context Passport desired state")
        seen.add(value["projectId"])
        _validate_context_passport(value["passport"], value)
        result.append(value)
    return sorted(result, key=lambda item: item["projectId"])


def _validate_context_passport(passport: object, publication: dict) -> None:
    if (not isinstance(passport, dict) or set(passport) != CONTEXT_PASSPORT_FIELDS
            or passport.get("schemaVersion") != 1
            or passport.get("projectId") != publication["projectId"]
            or passport.get("targetClient") != publication["provider"]
            or passport.get("assemblyDigest") != publication["passportDigest"]
            or not isinstance(passport.get("items"), list)
            or not 1 <= len(passport["items"]) <= 500):
        raise SystemExit("Control Plane returned an invalid Context Passport")
    for item in passport["items"]:
        if not isinstance(item, dict) or set(item) != CONTEXT_PASSPORT_ITEM_FIELDS:
            raise SystemExit("Control Plane returned an invalid Context Passport item")
    core = {key: passport[key] for key in passport if key not in {"assemblyDigest", "deliveryReceipt"}}
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if passport["assemblyDigest"] != expected_digest:
        raise SystemExit("Control Plane returned a Context Passport with an invalid digest")
    delivery = passport.get("deliveryReceipt")
    if (not isinstance(delivery, dict) or set(delivery) != {
            "status", "targetClient", "passportDigest", "installedRevision", "deliveredAt", "failureCode",
        }
            or delivery.get("status") != "not_requested"
            or delivery.get("targetClient") != publication["provider"]
            or delivery.get("passportDigest") != publication["passportDigest"]
            or any(delivery.get(field) is not None for field in ("installedRevision", "deliveredAt", "failureCode"))):
        raise SystemExit("Control Plane returned an invalid Context Passport delivery envelope")


def read_context_passport_reports(provider: str) -> list[dict]:
    index_path = _context_passport_index(provider)
    if not index_path.is_file():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("projects"), list):
        return []
    reports = []
    for project_id in index["projects"][:100]:
        if not isinstance(project_id, str):
            continue
        path = _context_passport_path(project_id, provider)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        passport = envelope.get("passport") if isinstance(envelope, dict) else None
        delivery = passport.get("deliveryReceipt") if isinstance(passport, dict) else None
        publication = envelope.get("publication") if isinstance(envelope, dict) else None
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "publication", "passport"}
                or envelope.get("version") != 1 or not isinstance(delivery, dict)
                or set(delivery) != {"status", "targetClient", "passportDigest", "installedRevision", "deliveredAt", "failureCode"}
                or not isinstance(publication, dict) or set(publication) != {"revision", "publishedAt"}
                or type(publication.get("revision")) is not int or publication["revision"] < 1
                or delivery.get("status") != "delivered"
                or delivery.get("targetClient") != provider
                or not isinstance(delivery.get("passportDigest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", delivery["passportDigest"])
                or not isinstance(delivery.get("installedRevision"), str)
                or not isinstance(delivery.get("deliveredAt"), str)
                or delivery.get("failureCode") is not None):
            continue
        canonical = json.loads(json.dumps(passport, ensure_ascii=False))
        canonical["deliveryReceipt"] = {
            "status": "not_requested", "targetClient": provider,
            "passportDigest": delivery["passportDigest"], "installedRevision": None,
            "deliveredAt": None, "failureCode": None,
        }
        try:
            _validate_context_passport(canonical, {
                "projectId": project_id, "provider": provider,
                "passportDigest": delivery["passportDigest"],
            })
        except SystemExit:
            continue
        reports.append({
            "version": 1, "projectId": project_id,
            "passportDigest": delivery.get("passportDigest"),
            "publicationRevision": publication.get("revision"),
            "status": "delivered", "installedRevision": delivery.get("installedRevision"),
            "deliveredAt": delivery.get("deliveredAt"), "failureCode": None,
        })
    return reports


def context_passports_need_update(desired: dict, providers: list[str]) -> bool:
    for provider in providers:
        wanted = {
            item["projectId"]: (item["passportDigest"], item["publicationRevision"])
            for item in _context_publications(desired, provider)
        }
        current = {
            item["projectId"]: (item["passportDigest"], item["publicationRevision"])
            for item in read_context_passport_reports(provider)
        }
        if wanted != current:
            return True
    return False


def install_context_passports(desired: dict, provider: str) -> list[dict]:
    publications = _context_publications(desired, provider)
    delivered_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    root = context_passport_root()
    try:
        previous_index = json.loads(_context_passport_index(provider).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_index = {}
    previous = {
        item for item in previous_index.get("projects", [])
        if isinstance(item, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", item)
    } if isinstance(previous_index, dict) and previous_index.get("version") == 1 else set()
    reports = []
    for publication in publications:
        passport = json.loads(json.dumps(publication["passport"], ensure_ascii=False))
        passport["deliveryReceipt"] = {
            "status": "delivered", "targetClient": provider,
            "passportDigest": publication["passportDigest"],
            "installedRevision": f"publication-{publication['publicationRevision']}",
            "deliveredAt": delivered_at, "failureCode": None,
        }
        path = _context_passport_path(publication["projectId"], provider)
        _write_json_atomic(path, {
            "version": 1,
            "publication": {
                "revision": publication["publicationRevision"],
                "publishedAt": publication["publishedAt"],
            },
            "passport": passport,
        })
        try:
            path.chmod(0o600)
        except OSError:
            pass
        reports.append({
            "version": 1, "projectId": publication["projectId"],
            "passportDigest": publication["passportDigest"],
            "publicationRevision": publication["publicationRevision"],
            "status": "delivered",
            "installedRevision": f"publication-{publication['publicationRevision']}",
            "deliveredAt": delivered_at, "failureCode": None,
        })
    wanted = {item["projectId"] for item in publications}
    for project_id in sorted(previous - wanted):
        path = _context_passport_path(project_id, provider)
        if path.is_file():
            path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
    root.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_context_passport_index(provider), {
        "version": 1, "provider": provider, "projects": sorted(wanted), "updatedAt": delivered_at,
    })
    return reports
