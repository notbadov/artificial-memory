# Codex adapter

This adapter renders the provider-neutral agent platform into Codex's local
surfaces: skills, `AGENTS.md`, hooks, mode profiles, and MCP configuration.

The synchronization client starts `scripts/install_codex_release.py` with the release it has
verified. The skills, policies and modes themselves are not in this repository: they arrive
in the signed release. Do not edit generated `~/.codex` files as the source of truth.

- `thread-modes.md`: the three mode profiles under `profiles/` and how to start Codex with one.
