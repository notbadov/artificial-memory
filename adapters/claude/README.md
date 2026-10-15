# Claude Code adapter

This adapter renders the platform's shared policy into Claude Code's user-level
configuration. It installs the skills and `CLAUDE.md` of a release, merges the platform's
hooks without removing the person's own, configures the memory service under the
provider-neutral `agentMemory` alias, and installs semantic mode launchers.

Claude Code supports user skills under `~/.claude/skills`, user instructions in
`~/.claude/CLAUDE.md`, hooks in `~/.claude/settings.json`, and user-scoped MCP
servers. Secrets remain machine-local.

The synchronization client starts `scripts/bootstrap_claude.py` with the release it has
verified; that script checks every checksum of the release again and runs `install.py` from
it. The skills, policies and modes themselves are not in this repository: they arrive in the
signed release.

The installer reads `AGENT_MEMORY_TOKEN` first. On a Mac already configured for
Codex, it may migrate the existing agentMemory bearer token from
`~/.codex/config.toml` into the machine-local `~/.claude.json`; the value is
never written to a repository or a release.

Start Claude with a shared semantic mode:

```bash
claude-mode balanced-economy
claude-mode economy
claude-mode optimal
```

The mappings are generated from the modes of a release into `modes/`:

- `optimal` -> Claude Opus, high effort;
- `balanced-economy` -> Claude Sonnet, medium effort;
- `economy` -> Claude Haiku with its default reasoning behavior (Haiku does not expose adaptive effort control).

The release and defect-learning quality gates are unchanged by a cheaper model.
