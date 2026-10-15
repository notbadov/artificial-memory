#!/usr/bin/env python3
"""Compile canonical agent/team contracts for one provider installation."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path


PROVIDERS = ("codex", "claude", "kimi", "opencode")


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return value


def compile_provider_catalog(platform_root: Path, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise SystemExit(f"unsupported provider: {provider}")
    agents_source = read_object(platform_root / "shared/agents/catalog.json")
    teams_source = read_object(platform_root / "shared/teams/catalog.json")
    lifecycle = read_object(platform_root / "shared/runtime/lifecycle.json")

    agents = [
        agent
        for agent in agents_source.get("agents", [])
        if provider in agent.get("allowed_providers", [])
    ]
    agent_ids = {agent.get("id") for agent in agents}
    teams = [
        team
        for team in teams_source.get("teams", [])
        if team.get("coordinator") in agent_ids
        and all(stage.get("agent") in agent_ids for stage in team.get("stages", []))
    ]
    return {
        "version": 1,
        "provider": provider,
        "agents": agents,
        "teams": teams,
        "runtime": lifecycle,
    }


def write_compiled_catalog(platform_root: Path, provider: str, target: Path) -> None:
    rendered = json.dumps(
        compile_provider_catalog(platform_root, provider),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent) as handle:
        handle.write(rendered)
        staged = Path(handle.name)
    staged.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    compiled = compile_provider_catalog(args.platform_root.resolve(), args.provider)
    if args.output:
        write_compiled_catalog(args.platform_root.resolve(), args.provider, args.output.resolve())
    else:
        print(json.dumps(compiled, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
