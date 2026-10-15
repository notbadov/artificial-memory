# The synchronization client and the terminal client

The code that runs on a connected computer. It has one transport and one owner: the service
over HTTPS. `sync_client.py` requires an enrolled client ID, a protected client credential,
the address of the service and independently provisioned release trust. There is no SSH and
no local-bundle fallback.

The default endpoint is:

```text
https://app.artificial-memory.com/agent-api
```

Each synchronization acquires a per-user nonblocking lock, reads the desired state, validates
access to memory, tasks and project context, verifies the signed release and its checksums,
installs the adapters of the chosen AI apps and the project profiles, maintains the per-user
schedule, and submits bounded receipts. The service can ask a computer for one thing only: to
synchronize.

On Windows the client credential is protected with current-user DPAPI and a hidden Scheduled
Task starts the private launcher. On macOS the credential is a mode-0600 local file (a
connection made in the terminal keeps it in the Keychain instead, see below) and a per-user
LaunchAgent starts the launcher. Credentials and machine-local paths never enter Git or the
receipts sent to the service.

## What is where

| File | What it does |
|---|---|
| `scripts/sync_client.py` | The entry point of the synchronization client. Its parts are the `sync_*.py` files next to it; it declares them itself (`--runtime-files`). |
| `scripts/control_plane_sync.py` | The order of one synchronization, stage by stage. |
| `scripts/release_transport.py` | Verifies the signed release and its archive before anything is unpacked. |
| `scripts/sync_launch_guard.py` | What the hourly job starts before the client: checks that the client starts and puts the previous one back when it does not. |
| `scripts/sync_recovery.py` | Brings a computer back to the client of its approved release after such a restore. |
| `scripts/sync_consents.py` | What the owner of the computer agreed to, and the releases that were withdrawn. |
| `scripts/agent_identity.py`, `scripts/mcp_bridge.py` | The key of each AI app on this computer and the local bridge the app talks to the service through, so that no token is written into an app's settings. |
| `scripts/command_line.py` | The terminal client (`npx artificial-memory …`). |
| `scripts/companion_install.py` | Installs the synchronization client for the desktop application. |
| `scripts/client_status.py` | The local status the desktop application shows; it holds no credential. |
| `runtime/` | What the signed runtime is built from: the pinned Python builds and wheels. |

## The terminal client

`scripts/command_line.py` connects, synchronizes and disconnects a computer from the terminal
(the launcher is in `packages/command-line`). It ships in the signed client runtime next to
`companion_install.py` and starts the same synchronization client; it adds no second way of
installing anything.

A connection made there runs the client with `--schedule-layout terminal`, plus
`terminal.json` with the non-secret settings of the connection:

- The hourly job carries the product's name: `com.artificial-memory.sync.<id>` in launchd,
  `Artificial Memory Sync (<computer id>)` in Task Scheduler. Clients installed earlier keep
  the name they were installed with (`legacy` layout, the default); see `SCHEDULE_LAYOUTS` in
  `scripts/sync_launchers.py` for why they are not renamed in place.
- On macOS the credential is the Keychain item the desktop application keeps its own
  enrollment in (service `com.artificial-memory.agent-platform`, account `managed-client`,
  the same record). No credential file exists; the launcher reads the item with
  `/usr/bin/security`. The desktop application therefore finds the connection at start, asks
  the person to allow access to the item, and from its first run (`--scheduler companion`) the
  client removes the terminal's job, so a computer never keeps two schedules. Every run
  records who started it in `schedule-owner.json`; that is how the terminal client knows the
  application runs the connection now.
- On Windows the credential is `credential.dpapi`, and the desktop application takes the
  connection over from it.
- The Windows hourly script is the same text on every computer. What it runs is data in
  `sync-client.arguments.json` next to it: one JSON object with the interpreter and the
  command line as two strings. The command line is written by `subprocess.list2cmdline` and
  handed to the interpreter as it is, through .NET, so neither a PowerShell version nor a
  name with a quotation mark can change what the client receives. Nothing a person or the
  service chose is ever written into the source of a script. Before the client, every launcher
  starts `sync-client-guard.py`, a copy of the client's `sync_launch_guard.py` written with the
  launcher: under the run lock it checks that the client starts (ending a check that does not
  return), puts the previous client back when it does not, and tells the launcher what to
  start.

A computer the application connected is refused by every command that would change it.

`--scheduler manual` is the run mode of a terminal connection whose background schedule the
person turned off: no OS entry is installed and none is reported.

## Tests

From the top of the repository:

```bash
python -m pip install -r adapters/client/requirements-control-plane.txt
python tools/run_tests.py unittest discover -s tests -p 'test_*.py'
```

`docs/TESTING.md` says what these tests cover and what they cannot show.
