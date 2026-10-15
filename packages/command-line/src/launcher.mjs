// What `npx artificial-memory <command>` does: prepare the signed runtime, then hand the command
// to the command-line client inside it. This layer installs nothing but the runtime and decides
// nothing about connections; replacing npm with another way of delivery replaces only this.
// Asking what the package is (`--version`, `--help`, nothing at all) is answered here, without
// the network: a person who only looks must not wait for a 30 MB runtime to be downloaded.

import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { RuntimeError, ensureRuntime, isolatedEnvironment, runtimeBase } from "./runtime.mjs";
import { TRUSTED_KEYS } from "./trust.mjs";

export const DEFAULT_ORIGIN = "https://app.artificial-memory.com";
// The capability a runtime announces when it carries the command-line client.
export const CAPABILITY = "commandLine";
const ENTRY = "command_line.py";
// RUNTIME_REMOVAL_REQUEST and LAUNCH_VARIABLE in command_line.py.
export const REMOVAL_REQUEST = "remove-on-exit";
export const LAUNCH_VARIABLE = "ARTIFICIAL_MEMORY_LAUNCH";

// The commands of the client inside the runtime, named and described as build_parser() in
// command_line.py does; runtime.test.mjs compares the two. The options of each command are not
// repeated here: `<command> --help` is answered by the client that is installed.
export const COMMANDS = [
  ["connect", "connect this computer (a confirmation code is entered in the browser)"],
  ["sync", "synchronize now"],
  ["status", "what is connected, the last synchronization, the background schedule"],
  ["consents", "review skills with programs and servers that wait for your approval"],
  ["schedule", "turn the hourly background synchronization on or off"],
  ["disconnect", "end the connection: revoke access on the server and remove it here"],
  ["uninstall", "disconnect and remove everything this client keeps on the computer"],
];
const HELP_FLAGS = ["--help", "-h"];
const VERSION_FLAG = "--version";

const NOTICES = {
  download: "A newer Artificial Memory runtime is published but could not be downloaded; the installed one is used.",
  older: "The Artificial Memory runtime offered for download is older than the installed one and was not installed.",
};

const MESSAGES = {
  runtime_unsupported_platform:
    "Artificial Memory in the terminal works on macOS (Apple silicon and Intel) and on 64-bit Windows.",
  runtime_signature_invalid:
    "The Artificial Memory runtime offered for download is not signed with the Artificial Memory release key. Nothing was installed.",
  runtime_digest_mismatch:
    "The downloaded Artificial Memory runtime does not match its signed description. Nothing was installed.",
  runtime_archive_unsafe:
    "The downloaded Artificial Memory runtime contains files it must not contain. Nothing was installed.",
  runtime_manifest_invalid:
    "The description of the Artificial Memory runtime is not one this version understands. Nothing was installed. Try: npx artificial-memory@latest",
  runtime_index_invalid:
    "Artificial Memory offers no runtime this version understands for this computer. Nothing was installed. Try: npx artificial-memory@latest",
  runtime_download_failed:
    "The Artificial Memory runtime could not be downloaded. Check the network and try again.",
  runtime_probe_failed:
    "The downloaded Artificial Memory runtime does not start on this computer. Nothing was changed.",
  runtime_without_command_line:
    "The Artificial Memory runtime published now does not include the command-line client yet. Nothing was connected or installed into your AI apps.",
  origin_invalid:
    "AGENT_PLATFORM_CONSOLE_ORIGIN must be an https:// address without a path.",
};

export function describe(error) {
  const text = MESSAGES[error?.code];
  if (!text) return "Artificial Memory could not start: an unexpected error. Nothing was changed.";
  return error.message.includes(": ") ? `${text} (${error.message})` : text;
}

/** Where the runtime is published; the signature is checked whatever this says. */
export function runtimeOrigin(environment = process.env) {
  const value = environment.AGENT_PLATFORM_CONSOLE_ORIGIN ?? DEFAULT_ORIGIN;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new RuntimeError("origin_invalid");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new RuntimeError("origin_invalid");
  }
  return url.origin;
}

/** The command-line client inside a runtime, or an error when the runtime has none. */
export function commandLineEntry(pointer) {
  const entry = path.join(path.dirname(pointer.installer), ENTRY);
  if (!pointer.capabilities.includes(CAPABILITY) || !fs.existsSync(entry)) {
    throw new RuntimeError("runtime_without_command_line");
  }
  return entry;
}

/** The version of this package: package.json is part of every published copy of it. */
export function packageVersion() {
  return JSON.parse(fs.readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;
}

/** What `--help` prints: the commands, and how to learn more without starting anything. */
export function usage() {
  const width = Math.max(...COMMANDS.map(([name]) => name.length));
  return [
    "usage: npx artificial-memory <command> [options]",
    "",
    "Artificial Memory in the terminal: connect this computer, synchronize it, disconnect it.",
    "",
    "commands:",
    ...COMMANDS.map(([name, summary]) => `  ${name.padEnd(width)}  ${summary}`),
    "",
    "A command first installs or updates the signed Artificial Memory runtime and checks its signature.",
    "  npx artificial-memory <command> --help   the options of a command",
    "  npx artificial-memory --version          the version of this package",
  ].join("\n");
}

/** The exit code when the question is answered here, or null when the client has to answer it. */
function answerLocally(argv, out, log) {
  if (argv.length === 0) {
    // As the client answers a missing command: the usage on stderr, and the exit code of argparse.
    log(usage());
    return 2;
  }
  if (HELP_FLAGS.includes(argv[0])) {
    out(usage());
    return 0;
  }
  if (argv[0] === VERSION_FLAG) {
    out(packageVersion());
    return 0;
  }
  return null;
}

export async function main(argv, {
  home = os.homedir(), environment = process.env, keys = TRUSTED_KEYS, fetcher, platform,
  spawn = spawnSync, log = (line) => process.stderr.write(line + "\n"),
  out = (line) => process.stdout.write(line + "\n"),
} = {}) {
  const answered = answerLocally(argv, out, log);
  if (answered !== null) return answered;
  let pointer;
  try {
    let kept;
    ({ pointer, kept } = await ensureRuntime({
      home, origin: runtimeOrigin(environment), keys, fetcher, spawn, ...(platform ? { platform } : {}),
      onDownload: (manifest) => log(
        `Downloading the Artificial Memory runtime (${Math.round(manifest.archiveSize / 1048576)} MB) and checking its signature…`),
    }));
    if (kept) log(NOTICES[kept]);
    const entry = commandLineEntry(pointer);
    const request = path.join(runtimeBase(home), REMOVAL_REQUEST);
    // A request an earlier run left (it was interrupted before the directory went) is not this
    // run's: acted on now, it would delete the runtime after a `connect`, under the new hourly job.
    fs.rmSync(request, { force: true });
    const launch = randomBytes(16).toString("hex");
    // Isolated like every run Desktop Companion starts: no user site packages, no PYTHON* variables.
    const result = spawn(pointer.executable, ["-E", "-s", entry, ...argv], {
      stdio: "inherit", env: { ...isolatedEnvironment(environment), [LAUNCH_VARIABLE]: launch }, windowsHide: true,
    });
    if (result.error) throw new RuntimeError("runtime_probe_failed", result.error.code ?? "start");
    // The client asks for this, in a file, when it finished an uninstall and the OS would not
    // let it delete the interpreter it was running on (Windows). Nothing else removes the
    // runtime: not an exit code, not the name of the command (`uninstall --help` exits 0 too),
    // and not a request that does not name this very run. The runtime is shared with the
    // desktop application and the hourly job.
    let asked = "";
    try {
      asked = fs.readFileSync(request, "utf8").trim();
    } catch {
      // No request: the usual case.
    }
    if (asked === launch) fs.rmSync(runtimeBase(home), { recursive: true, force: true });
    else fs.rmSync(request, { force: true });
    return result.status ?? 130;
  } catch (error) {
    log(describe(error));
    return 1;
  }
}
