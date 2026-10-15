# Cursor adapter

Cursor follows a computer's release like Kimi Code and OpenCode. The managed client writes
and removes everything the platform keeps in Cursor; nothing else in `~/.cursor` is touched.
Cursor keeps its user configuration in `~/.cursor` on macOS, Linux and Windows alike.

Nothing is written into Cursor unless the computer's owner chose it (when connecting, or later
in the web application): Cursor found on a computer is no choice. When it is no longer chosen,
its channels, the managed servers recorded there and the hooks are taken out again.

| What | Where | Written by |
|---|---|---|
| Channels of Cursor's own agent: `agent-platform-collaboration`, `agentMemory`, `projectContext` | `~/.cursor/mcp.json` → `mcpServers`, stdio entries `{command, args}` that start `mcp_bridge.py --provider cursor` with the client's interpreter | `tools/install_app_agent_channels.py --provider cursor --cursor-config ~/.cursor/mcp.json` |
| Managed MCP servers the organization assigns to Cursor | `~/.cursor/mcp.json` → `mcpServers`, remote entries `{url, headers}` | `tools/install_managed_mcp_servers.py --cursor-root ~/.cursor` |
| Work-session hooks | `~/.cursor/hooks.json`, events `sessionStart`, `beforeSubmitPrompt`, `stop`, `sessionEnd` | `adapters/cursor/install.py` |
| The hook script and the receipt | `~/.cursor/agent-platform/scripts/work_session_hook.py`, `~/.cursor/agent-platform/install.json` | `adapters/cursor/install.py` |

Formats: [MCP](https://cursor.com/docs/context/mcp), [hooks](https://cursor.com/docs/agent/hooks).

## What belongs to the person

- An MCP server in `mcp.json` is the platform's only when the client wrote it: a channel is a
  bridge entry started as `cursor`, a managed server is recorded per app in
  `~/.agent-platform/client/managed-mcp-state.json`. A server the person added under a name the
  organization delivers is a conflict: it stays as it is and is reported.
- A hook in `hooks.json` is the platform's only when its command names a script under
  `~/.cursor/agent-platform/scripts/`. Every other hook stays, in its place and order.
- A file that is not valid JSON, or a hooks file that is not version 1 with a `hooks` object,
  is left alone and the run fails for Cursor only: Codex, Claude Code and the other apps are
  configured and recorded as on any other run.
- Disconnecting the computer (and uninstalling, which disconnects first) takes out the
  platform's MCP entries, its hooks and `~/.cursor/agent-platform`: the hooks start the client's
  interpreter, which an uninstall deletes.

## How Cursor runs a hook

Cursor 3.24 runs a hook command with the system shell on macOS and Linux, and with PowerShell
on Windows (`pwsh`, else Windows PowerShell; `-NoProfile -NonInteractive -ExecutionPolicy
Bypass`), piping the input from a file. That was read from its extension host
(`ExtHostShellExec.$executeHookDirect`); the documentation says only that a command is "a shell
string". So the command is `'<python>' '<script>' --provider cursor` quoted for the POSIX shell,
and on Windows a PowerShell call: `& '<python>' '<script>' --provider cursor`, a quote inside a
path doubled. PowerShell may put a byte order mark before the input; the hook skips it.

A run that changes nothing writes nothing. Cursor reads `mcp.json` when it starts, so a change
there sets `restartRequired: true` on Cursor's receipt; `hooks.json` is reread when it is saved.

## Running it by hand

```sh
python adapters/cursor/install.py --platform-root <release> --python <the client's python>
python adapters/cursor/install.py --uninstall   # removes the hooks and ~/.cursor/agent-platform only
```

## Claude Code's hooks inside Cursor

Cursor can load Claude Code's hooks ([third-party hooks](https://cursor.com/docs/reference/third-party-hooks)).
Cursor puts `cursor_version`, `conversation_id` and `generation_id` into the input of every hook
it runs; Claude Code sends none of them. A Claude Code hook of the platform that receives one of
them records nothing and says so on stderr: the work-session hook does not count Cursor's
conversation as a Claude Code session, and the learning gate does not answer Cursor's `stop` with
Claude Code's `decision: block`.

## Not yet

Skills, the platform's rules (through `sessionStart` `additional_context`), skill and hook
inventories, modes and the per-tool approval of MCP servers are not installed into Cursor.
Cursor already reads `~/.claude/skills` and `~/.codex/skills` on a computer with those apps.
