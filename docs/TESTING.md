# Tests

Everything here runs in this repository alone, with no account and no network.

## Running them

You need Python 3.12 and Node.js 18 or newer.

```sh
python -m pip install -r adapters/client/requirements-control-plane.txt
python tools/run_tests.py unittest discover -s tests -p "test_*.py"
```

```sh
cd packages/command-line
node --test
```

The first command installs one package, `cryptography`, which the client uses to verify
signatures. The Python tests take a few minutes: many of them run a whole synchronization of
an installed client against a service made for the test.

`.github/workflows/tests.yml` runs the same commands on macOS, Windows and Linux, the
launcher's tests on Node.js 18, 22 and 24, and `ruff check .` with the rules in `ruff.toml`.

## They cannot touch your computer

The tests install clients, write launchers and schedules, and read keys. None of that reaches
the real computer:

- `tools/run_tests.py` gives the run a state directory of its own, so nothing is read from or
  written to `~/.agent-platform`. `tests/conftest.py` does the same for a direct `pytest` run.
- The tests that start the client as a process give it a disposable home
  (`sandboxed_environment` in `tools/test_isolation.py`), and
  `tests/test_client_without_posix.py` checks that every such start in the test modules does.
- `tools/process_guard.py` refuses, inside a test, to start a real scheduler (`launchctl`,
  `schtasks`), a real key store (`security`, `cmdkey`) or an interpreter outside the sandbox,
  whoever asks for it. A test that needs one of them gets a model of it instead.

## What they cover

| Tests | What they hold |
|---|---|
| `test_release_transport`, `test_safe_tar`, `test_withdrawn_releases` | A release is installed only when its signature, size, checksums and layout are right, an archive cannot write outside its directory, and a withdrawn release is refused. |
| `test_verifying_page` | The scripts in [VERIFYING.md](VERIFYING.md) are run against a signed runtime and a signed release and agree with the client's own checks. |
| `test_control_plane_sync`, `test_managed_client_runtime`, `test_client_module_boundaries` | The order of a synchronization, how a release replaces the installed client, and that the client's parts fit together. |
| `test_client_recovery`, `test_client_launch_guard`, `test_client_busy_files`, `test_sync_guard` | A client that does not start is put back, the computer returns to its approved release, a file another program holds does not break an update, and two runs never overlap. |
| `test_client_launcher_golden` | Every file the client writes for the hourly job (the shell and PowerShell scripts, the job definition, the arguments file) is the recorded one to the byte, so a change of a launcher is never an accident. |
| `test_client_consents`, `test_client_instruction_rules`, `test_client_follower_apps`, `test_app_agent_channels`, `test_mcp_bridge`, `test_work_session_hook` | What is written into the AI apps: skills with programs wait for approval, rules are placed without touching the rest of a file, each app gets its own key and no token in its settings. |
| `test_client_disconnect` | Disconnecting removes what was added and nothing else. |
| `test_client_output_encoding`, `test_client_without_posix` | The client on Windows: its output is UTF-8 everywhere, and every file imports on an interpreter that has nothing of POSIX. |
| `packages/command-line/src/runtime.test.mjs` | The launcher: the signature and digest of a runtime, unpacking without leaving its directory, refusing an older runtime, and the commands it passes on. |

## What a run here does not show

- **Tests that need an earlier release of the client.** A few tests of
  `test_managed_client_runtime` and `test_client_recovery` install the client of a past
  release and update it. They read that client from the history of the source repository.
  This repository is published without history, so those tests are skipped here and say so.
- **The other system.** Tests of the Mac launcher (a shell script) are skipped on Windows, and
  the tests that need a real Windows (a file held open by another program, PowerShell running
  the hourly script) are skipped elsewhere. Each skipped test names its reason; the launcher's
  tests end with `Not run on this computer` and the reasons.
- **A real connection.** No test here talks to the service.

## Tests that are not published

Some tests of this code need what is not in this repository, and stay with it:

- The tests of the terminal client's commands (`connect`, `sync`, `disconnect`, `uninstall`,
  taking a connection over from the desktop application and back) start the service itself
  inside the test process. Publishing them would mean publishing the service.
- The checks that the client, the desktop application, the web application and the service
  name the same stages, reasons and files read the source of all four.
- The checks of what a release must contain read the skills and rules, which are delivered in
  a release and are not part of this code.
- The runs in which the client of every past release installs the release of this code, and
  this client theirs, with nothing of the client replaced. They install the skills and rules
  of a whole release into the apps and read the past releases from the history.

So `adapters/client/scripts/command_line.py` is covered here only by the tests that import it
and by the launcher's tests; its commands are tested where the service is.
