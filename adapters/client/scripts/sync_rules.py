"""Standing instructions of the organization, of its projects and of the computer's owner.

The service sends them as finished blocks. This client only places them in the instruction
file of each app, after the platform's own rules, and takes out the blocks that are gone.
Everything else in such a file belongs to its owner and is left as it was.

This file is a part of `sync_client.py`, the entry point, which takes these functions from
here. Of the client it imports the messages; the installer of managed servers is imported where
it is used.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from sync_messages import text

RULE_BLOCK_ID = re.compile(r"rules-[a-z0-9][a-z0-9._-]{0,120}")
RULE_BLOCK = re.compile(
    r"\n*<!-- agent-platform:(rules-[a-z0-9][a-z0-9._-]{0,120}):start -->.*?<!-- agent-platform:\1:end -->\n?",
    re.DOTALL,
)
MAX_RULE_BLOCKS_BYTES = 128 * 1024


def instruction_rule_blocks(desired: dict) -> list[tuple[str, str]] | None:
    """The blocks to place, or None when the service sent no decision (an older service)."""

    delivered = desired.get("instructionRules")
    if not isinstance(delivered, dict) or not isinstance(delivered.get("blocks"), list):
        return None
    blocks: list[tuple[str, str]] = []
    size = 0
    for item in delivered["blocks"]:
        block_id, body = (item.get("id"), item.get("text")) if isinstance(item, dict) else (None, None)
        # A text that carried the marker could close its block early and plant text outside it.
        if (not isinstance(block_id, str) or not RULE_BLOCK_ID.fullmatch(block_id)
                or not isinstance(body, str) or not body.strip() or "agent-platform:" in body):
            raise ValueError(text("rules_malformed"))
        size += len(body.encode("utf-8"))
        blocks.append((block_id, body.strip()))
    if size > MAX_RULE_BLOCKS_BYTES or len({block_id for block_id, _ in blocks}) != len(blocks):
        raise ValueError(text("rules_malformed"))
    return blocks


def with_instruction_rule_blocks(content: str, blocks: list[tuple[str, str]]) -> str:
    """`content` with exactly these rule blocks at its end; everything else is left as it was."""

    kept = RULE_BLOCK.sub("\n", content).rstrip()
    rendered = "\n\n".join(
        f"<!-- agent-platform:{block_id}:start -->\n{body}\n<!-- agent-platform:{block_id}:end -->"
        for block_id, body in blocks
    )
    if not rendered:
        return kept + "\n" if kept else ""
    return f"{kept}\n\n{rendered}\n" if kept else rendered + "\n"


def instruction_files(args: argparse.Namespace, providers: list[str], home: Path | None = None) -> list[Path]:
    """The instruction file of every app this computer has the platform in."""

    home = home or Path.home()
    files: list[Path] = []
    if "claude" in providers:
        files.append((getattr(args, "claude_root", None) or home / ".claude") / "CLAUDE.md")
    if "codex" in providers:
        files.append((getattr(args, "codex_root", None) or home / ".codex") / "AGENTS.md")
    # Kimi Code and OpenCode: only where the platform already keeps an instruction file.
    kimi = os.environ.get("KIMI_CODE_HOME")
    kimi_home = Path(kimi).expanduser() if kimi and home == Path.home() else home / ".kimi-code"
    for candidate in (kimi_home / "AGENTS.md", home / ".config" / "opencode" / "agent-platform" / "AGENTS.md"):
        if candidate.is_file():
            files.append(candidate)
    return files


def apply_instruction_rules(args: argparse.Namespace, desired: dict, providers: list[str], reports: list[dict],
                            home: Path | None = None) -> None:
    """Place the blocks the service sent in every app. A failure is reported on the computer's
    first app, since the rules have no report of their own."""

    from install_managed_mcp_servers import atomic_write

    try:
        blocks = instruction_rule_blocks(desired)
        if blocks is None:
            return
        for path in instruction_files(args, providers, home):
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            updated = with_instruction_rule_blocks(current, blocks)
            if updated != current and (updated or path.is_file()):
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, updated)
    except (OSError, ValueError) as error:
        if reports:
            reports[0]["status"] = "failed"
            reports[0]["error"] = text("rules_not_placed", error=error)[:1000]


def remove_instruction_rules(args: argparse.Namespace, home: Path) -> bool:
    """Take the organization's standing instructions out of every app (disconnect)."""

    from install_managed_mcp_servers import atomic_write

    removed = False
    for path in instruction_files(args, ["claude", "codex"], home):
        if not path.is_file():
            continue
        try:
            current = path.read_text(encoding="utf-8")
            updated = with_instruction_rule_blocks(current, [])
            if updated != current:
                atomic_write(path, updated)
                removed = True
        except OSError:
            continue
    return removed
