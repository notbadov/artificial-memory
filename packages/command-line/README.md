# artificial-memory

Artificial Memory in the terminal: connect a computer, synchronize it and disconnect it without
the desktop application. An alternative to the application, not a replacement.

```
npx artificial-memory connect       # shows a 6-digit code; you confirm it in the browser
npx artificial-memory status
npx artificial-memory sync
npx artificial-memory sync --repair # also retry a release whose client did not start here
npx artificial-memory consents      # skills with programs that wait for your approval
npx artificial-memory schedule off  # or: on
npx artificial-memory disconnect
npx artificial-memory uninstall
```

Needs Node.js 18 or newer on macOS (Apple silicon or Intel) or 64-bit Windows. Nothing else
has to be installed.

## What this package is

Only a launcher, with no dependencies and no install scripts:

1. It reads `https://app.artificial-memory.com/downloads/client-runtime/latest.json`, the same
   index Desktop Companion reads.
2. It verifies the runtime's signed description with the release key built into
   `src/trust.mjs` (ed25519, key id `home-server-2026-09`), downloads the archive, checks its
   size and SHA-256 against the signed description, unpacks it, and starts the interpreter once
   to see that it works. Unpacking creates every directory itself and follows no link out of
   `~/.agent-platform/runtime/`: a link is accepted only when the place it really leads to,
   through the links unpacked before it, is inside. A runtime that fails any of these is not
   installed and nothing is run. A runtime built before the installed one is not installed
   either, however validly it is signed.
3. It runs `bootstrap/command_line.py` from that runtime with the runtime's own Python and
   passes the command on.

Everything that touches a connection or an AI app lives in the signed runtime and in the signed
release the runtime installs (`command_line.py`, `sync_client.py`). Their source is in the same
repository as this launcher: https://github.com/notbadov/artificial-memory

The runtime directory `~/.agent-platform/runtime/` is shared with the desktop application: when
both are installed they use one runtime and one copy of everything.

## What it changes on your computer

- `~/.agent-platform/`: the runtime, the synchronization client, the release cache and the
  record of this connection.
- The access key of the connection: in the macOS Keychain, or in a file protected for your
  Windows account (DPAPI). It is never printed and never passed on a command line.
- The settings of the AI apps you chose (Codex, Claude Code, Kimi, OpenCode): the connection to
  memory, tasks and project context, and the skills and rules of your space.
- One background job that synchronizes once an hour: a launchd job on macOS, a Scheduled Task
  on Windows. `schedule off` removes it, `schedule on` puts it back.

On Windows the task is registered as "Interactive only": it runs only while you are signed in
to Windows, and after you sign out synchronization waits until you sign in again. The command
never asks for your Windows password and does not store it.

`disconnect` ends the connection at the service and removes the key and the job. `uninstall`
also removes what this client keeps in `~/.agent-platform/`.

## With the desktop application

The command and the application can be installed on one computer. One of them holds the
connection: when the application is connected it keeps its own connection, ends the one made
in the terminal and says so in its window; the command says the same at its next start.

## Checking what you downloaded

The launcher refuses a runtime that is not signed with the release key in `src/trust.mjs`
(ed25519, key id `home-server-2026-09`, public key
`0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8=`). The signed description names the archive, its
size and its SHA-256, so a changed or replaced archive is not installed and nothing from it is
run.

## More

- What the command does, step by step: https://app.artificial-memory.com/cli
- Source and issues: https://github.com/notbadov/artificial-memory
- The desktop application: https://app.artificial-memory.com/download
