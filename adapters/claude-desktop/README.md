# Claude Desktop adapter

Claude Desktop does not load Claude Code's `~/.claude/skills` directory. It
accepts personal skills as ZIP uploads and loads local MCP servers from
`claude_desktop_config.json`.

This adapter keeps `shared/skills` canonical and produces deterministic Desktop
ZIP packages. Each archive contains one root skill directory and its `SKILL.md`.
Desktop's 200-character description limit is satisfied from the existing
`agents/openai.yaml` `short_description`; the full canonical skill instructions,
references, scripts, and assets are preserved.

The adapter also configures `agentMemory` through the local `mcp-remote` stdio
bridge. The bearer token is copied only between machine-local provider configs.
It is never written to the repository, packages, manifest, or release bundle.

Run the installer with the root of an unpacked release:

```powershell
python adapters/claude-desktop/install.py --platform-root <release>
```

The command builds and verifies packages under
`~/.agent-platform/claude-desktop/skills`, preserves unrelated Desktop MCP
servers, writes a backup before changing the Desktop config, and reports
`restart_required: true`.

Upload or update the generated ZIP files in Claude Desktop under
`Customize > Skills`. Personal skill upload is an account operation and is not
performed silently by the filesystem installer. Fully restart Claude Desktop
after an MCP configuration change.

Verification only:

```powershell
python adapters/claude-desktop/install.py --verify-only
```

## On a computer with the managed client

The managed client registers the Claude desktop app as its own agent
(«Claude (приложение: чат и вкладка Code) · <computer>», in English "Claude (desktop app: chat and the Code
tab) · <computer>") and writes `agentMemory`, `projectContext` and `agent-platform-collaboration` into
`claude_desktop_config.json` as local bridge entries (`command`/`args`), with no token. This adapter keeps such
an entry and needs no `AGENT_MEMORY_TOKEN`; restart Claude Desktop after a sync for the entries to take effect.

### The Code tab speaks as the desktop app

The app starts these servers once and gives them to its chat and to its Code tab. A Claude Code session in the
Code tab also starts Claude Code's own servers of the same names (`--provider claude`, from `~/.claude.json`),
and the desktop app's win. So a session in the Code tab is the participant `app-claude-desktop-<id>`, not
`app-claude-<id>`, and the bridge cannot tell the tab from the chat.

So:

- the desktop app carries out work on tasks like the other apps, with the provider `claude-desktop`;
- on one computer its agent and Claude Code's take each other's tasks. The service states which agents
  are interchangeable, and the run names the agent that did the work;
- a mode (`shared/modes`) cannot choose the desktop app's model or effort: the platform does not start the
  app, the person chooses both in it. Every mode says so (`[adapters.claude-desktop]`, `chosen-in-app`), and a
  run from the desktop app carries its mode for the quality gates only.

