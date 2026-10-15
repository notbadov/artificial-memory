# Kimi adapter

This adapter installs the provider-neutral agent platform into Kimi Code CLI
and Kimi Desktop/Kimi Work. It installs shared Agent Skills, global
instructions, lifecycle hooks, semantic modes, and the shared `agentMemory`
HTTP MCP connection without replacing unrelated local settings.

Supported homes:

- Kimi Code CLI: `$KIMI_CODE_HOME` or `~/.kimi-code` on macOS, Linux, and Windows;
- Kimi Desktop on macOS: embedded `kimi-code/home` below
  `~/Library/Application Support/kimi-desktop`;
- Kimi Desktop on Windows: embedded `kimi-code/home` below
  `%APPDATA%\kimi-desktop` or `%LOCALAPPDATA%\kimi-desktop`.

Desktop discovery is bounded to those application-owned roots and ignores
caches and dependency trees. The existing embedded home is found
before Kimi is closed because Kimi may rebuild that runtime during shutdown. If
Desktop data exists but no runtime can be found, the installation fails explicitly
instead of reporting a CLI-only install as complete. The CLI home is always
installed.
Credentials stay machine-local and are never included in Git or a release. The CLI stores Agent Memory in `~/.kimi-code/mcp.json`. Kimi Desktop is
installed as a local managed `agent-platform` plugin because Desktop owns and
regenerates its embedded `mcp.json`; the generated plugin manifest therefore
holds the machine-local credential and remains outside the canonical repository.

The synchronization client runs `install.py` from the release it has verified. The
skills, policies and modes themselves are not in this repository: they arrive in the
signed release.

The installer reads `AGENT_MEMORY_TOKEN` first. It can also reuse the existing
machine-local bearer token from Codex or Claude configuration. Kimi Desktop has to be
closed while its plugin registry is changed. Start a new Kimi Code session after
installation.

Kimi Desktop receives one generated provider adapter plugin containing the
shared MCP declaration, skills, instructions, modes, and lifecycle hooks. Kimi's
own plugins and model/provider settings are preserved. Installing again
atomically refreshes only the managed `agent-platform` plugin.

Kimi Code CLI semantic modes preserve the currently selected model:

```bash
kimi-mode economy
kimi-mode balanced-economy
kimi-mode optimal
```

`economy` disables Thinking where the selected model allows it;
`balanced-economy` requests low-effort Thinking; `optimal` requests high-effort
Thinking. Models that always think may enforce their own supported effort.
