import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

import {
  CAPABILITY, COMMANDS, LAUNCH_VARIABLE, REMOVAL_REQUEST, commandLineEntry, describe, main, packageVersion, runtimeOrigin, usage,
} from "./launcher.mjs";
import {
  PROBE_SCRIPT, RuntimeError, builtAt, canonicalJson, checkArchive, current, ensureRuntime, extractArchive, fetchLimited, installVerified,
  isolatedEnvironment, platformKey, readPointer, runtimeBase, selectFromIndex, signingBytes, verifyEnvelope,
} from "./runtime.mjs";
import { TRUSTED_KEYS } from "./trust.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPOSITORY = path.resolve(HERE, "../../..");
// The envelope the desktop application's own tests verify, byte for byte: signed by the tool
// that builds the runtime, with a key made for the fixture.
const FIXTURE = JSON.parse(fs.readFileSync(path.join(HERE, "../fixtures/runtime-envelope.json"), "utf8"));
const FIXTURE_KEYS = { "test-key": FIXTURE.publicKey };
const ORIGIN = "https://runtime.test";
const WINDOWS_HOST = process.platform === "win32";

// What does not run on this computer, and why: a green run on Windows must not read as if the
// tests of a Mac's runtime had run there.
const notRun = [];
after(() => {
  if (notRun.length === 0) return;
  const reasons = new Map();
  for (const { reason } of notRun) reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
  console.log(`Not run on this computer: ${notRun.length} test(s).`);
  for (const [reason, count] of reasons) console.log(`  ${count} × ${reason}`);
});

/** A test that needs something only a Mac or Linux has; on Windows it is skipped with `reason`. */
function posix(name, reason, body) {
  if (WINDOWS_HOST) notRun.push({ name, reason });
  test(name, { skip: WINDOWS_HOST ? reason : false }, body);
}

function temporary(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `artificial-memory-${name}-`));
}

function code(action) {
  try {
    action();
  } catch (error) {
    assert.ok(error instanceof RuntimeError, String(error));
    return error.code;
  }
  return null;
}

function header(name, kind, size, link = "", mode = 0o755) {
  const block = Buffer.alloc(512);
  block.write(name, 0, 100, "utf8");
  block.write(mode.toString(8).padStart(7, "0") + "\0", 100, "latin1");
  block.write("0000000\0", 108, "latin1");
  block.write("0000000\0", 116, "latin1");
  block.write(size.toString(8).padStart(11, "0") + "\0", 124, "latin1");
  block.write("00000000000\0", 136, "latin1");
  block.write("        ", 148, "latin1");
  block.write(kind, 156, "latin1");
  block.write(link, 157, 100, "utf8");
  block.write("ustar\0" + "00", 257, "latin1");
  const sum = block.reduce((total, byte) => total + byte, 0);
  block.write(sum.toString(8).padStart(6, "0") + "\0 ", 148, "latin1");
  return block;
}

/** A tar.gz from [name, kind, content, link, mode] entries; names are written raw to model a hostile archive. */
function archive(entries) {
  const blocks = [];
  for (const [name, kind, content = "", link = "", mode = 0o755] of entries) {
    const data = Buffer.from(content);
    blocks.push(header(name, kind, data.length, link, mode), data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return gzipSync(Buffer.concat(blocks));
}

function pax(pathName) {
  const record = (key, value) => {
    const body = ` ${key}=${value}\n`;
    let length = body.length + 1;
    while (String(length).length + body.length !== length) length = String(length).length + body.length;
    return `${length}${body}`;
  };
  return record("path", pathName);
}

const HEALTHY = "#!/bin/sh\nexit 0\n";
const BROKEN = "#!/bin/sh\necho 'ModuleNotFoundError: cryptography' >&2\nexit 1\n";
const WINDOWS_HEALTHY = "MZ an interpreter that starts";
const WINDOWS_BROKEN = "MZ an interpreter without cryptography";

/**
 * The interpreter of a Windows runtime. A test cannot write a program Windows would start the
 * way it writes a shell script, so starting it is modelled: the file has to be where the
 * launcher says it is, and it is started exactly as the probe starts an interpreter.
 */
function windowsInterpreter(program, argv, options) {
  assert.deepEqual(argv, ["-E", "-s", "-c", PROBE_SCRIPT]);
  assert.equal(options.windowsHide, true);
  assert.deepEqual(Object.keys(options.env).filter((key) => key.toUpperCase().startsWith("PYTHON")), []);
  let content;
  try {
    content = fs.readFileSync(program, "utf8");
  } catch {
    return { status: null, error: { code: "ENOENT" } };
  }
  return content === WINDOWS_HEALTHY ? { status: 0, stderr: "" } : { status: 1, stderr: "ModuleNotFoundError: cryptography\r\n" };
}

/**
 * The two runtimes that are published, as far as the launcher can tell them apart: where the
 * interpreter is, what the archive may hold, and what starts the interpreter. A Mac's runtime is
 * a real shell script started for real, which needs a Mac or Linux; a Windows runtime runs on
 * any computer, and on Windows with the paths of Windows.
 */
const MAC = {
  label: "a Mac's runtime", platform: "darwin-aarch64", executable: "python/bin/python3",
  directories: ["python", "python/bin"], healthy: HEALTHY, broken: BROKEN, probe: spawnSync,
  needs: WINDOWS_HOST ? "the interpreter of a Mac's runtime is a shell script with an executable mode, which Windows cannot start" : null,
};
const WINDOWS = {
  label: "a Windows runtime", platform: "windows-x86_64", executable: "python/python.exe",
  directories: ["python"], healthy: WINDOWS_HEALTHY, broken: WINDOWS_BROKEN, probe: windowsInterpreter, needs: null,
};

/** `body(shape)` as one test for each runtime; the one this computer cannot run is skipped with its reason. */
function onEachRuntime(name, body) {
  for (const shape of [MAC, WINDOWS]) {
    const title = `${name} [${shape.label}]`;
    if (shape.needs) notRun.push({ name: title, reason: shape.needs });
    test(title, { skip: shape.needs ?? false }, () => body(shape));
  }
}

/** What starts programs in a launcher test: the probe as the runtime needs it, the client as `client` answers. */
function starter(shape, client) {
  return (program, argv, options) => (argv[2] === "-c" ? shape.probe(program, argv, options) : client(program, argv, options));
}

/** A runtime archive of the given shape and its envelope signed by `signer`. */
function published(signer, runtimeId, shape, { interpreter = shape.healthy, capabilities = ["schedulerCompanion", CAPABILITY] } = {}) {
  const { platform } = shape;
  const bytes = archive([
    ...shape.directories.map((directory) => [directory, "5"]), [shape.executable, "0", interpreter],
    ["bootstrap", "5"], ["bootstrap/companion_install.py", "0", "pass\n"],
    ["bootstrap/command_line.py", "0", "pass\n"],
  ]);
  const manifest = {
    runtimeId, platform, agentPlatformSha: "a".repeat(40), pythonVersion: "3.12.14", pythonSha256: "b".repeat(64),
    archive: `client-runtime-${runtimeId}-${platform}.tar.gz`, archiveSha256: createHash("sha256").update(bytes).digest("hex"),
    archiveSize: bytes.length, executable: shape.executable, installer: "bootstrap/companion_install.py",
    // Built on the day its id names, as the build tool makes it.
    capabilities, createdAt: `${runtimeId.slice(0, 4)}-${runtimeId.slice(4, 6)}-${runtimeId.slice(6, 8)}T12:00:00Z`,
  };
  const envelope = { schemaVersion: 1, keyId: "test-key", manifest };
  envelope.signature = sign(null, signingBytes(envelope), signer.privateKey).toString("base64");
  return { bytes, manifest, envelope };
}

function signer() {
  const pair = generateKeyPairSync("ed25519");
  const raw = pair.publicKey.export({ format: "der", type: "spki" }).subarray(-32).toString("base64");
  return { ...pair, keys: { "test-key": raw } };
}

/** A site that serves `latest.json` and the archives of the given runtimes. */
function site(items) {
  const requests = [];
  const latest = items[items.length - 1];
  const { platform } = latest.manifest;
  const fetcher = async (url, options) => {
    requests.push(url);
    assert.equal(options.redirect, "error");
    const reply = (body) => new Response(body, { status: 200 });
    if (url === `${ORIGIN}/downloads/client-runtime/latest.json`) {
      return reply(Buffer.from(JSON.stringify({ platforms: { [platform]: { envelope: latest.envelope, path: "https://evil.invalid/x" } } })));
    }
    const item = items.find((candidate) => url === `${ORIGIN}/downloads/client-runtime/${candidate.manifest.runtimeId}/${candidate.manifest.archive}`);
    return item ? reply(item.served ?? item.bytes) : { ok: false, status: 404, headers: new Headers() };
  };
  return { fetcher, requests };
}

const offline = async () => {
  throw new TypeError("fetch failed");
};

test("the bytes that are verified are the bytes Python signed and Desktop Companion verifies", () => {
  assert.deepEqual(signingBytes(FIXTURE.envelope), Buffer.from(FIXTURE.signingBytes, "base64"));
  const manifest = verifyEnvelope(FIXTURE.envelope, FIXTURE_KEYS, "darwin-aarch64");
  assert.equal(manifest.runtimeId, "20260930-4b3419da0740-py3.12.14");
});

test("a changed manifest, an unknown key and another platform are refused", () => {
  const tampered = structuredClone(FIXTURE.envelope);
  tampered.manifest.archiveSize = 1;
  assert.equal(code(() => verifyEnvelope(tampered, FIXTURE_KEYS, "darwin-aarch64")), "runtime_signature_invalid");
  // The fixture is not signed by the production key, and no key from the network is taken.
  assert.equal(code(() => verifyEnvelope(FIXTURE.envelope, TRUSTED_KEYS, "darwin-aarch64")), "runtime_signature_invalid");
  assert.equal(code(() => verifyEnvelope({ ...FIXTURE.envelope, publicKey: FIXTURE.publicKey }, TRUSTED_KEYS, "darwin-aarch64")), "runtime_index_invalid");
  assert.equal(code(() => verifyEnvelope(FIXTURE.envelope, { __proto__: FIXTURE_KEYS }, "darwin-aarch64")), "runtime_signature_invalid");
  assert.equal(code(() => verifyEnvelope(FIXTURE.envelope, FIXTURE_KEYS, "windows-x86_64")), "runtime_manifest_invalid");
});

test("the production release key is the one built in", () => {
  assert.deepEqual(Object.keys(TRUSTED_KEYS), ["home-server-2026-09"]);
  assert.equal(Buffer.from(TRUSTED_KEYS["home-server-2026-09"], "base64").length, 32);
  assert.ok(Object.isFrozen(TRUSTED_KEYS));
});

test("the download path comes from the signed manifest", () => {
  const index = { platforms: { "darwin-aarch64": { envelope: FIXTURE.envelope, path: "https://evil.invalid/runtime.tar.gz" } } };
  assert.equal(selectFromIndex(index, FIXTURE_KEYS, "darwin-aarch64").path,
    "/downloads/client-runtime/20260930-4b3419da0740-py3.12.14/client-runtime-20260930-4b3419da0740-py3.12.14-darwin-aarch64.tar.gz");
  assert.equal(code(() => selectFromIndex({ platforms: {} }, FIXTURE_KEYS, "darwin-aarch64")), "runtime_index_invalid");
});

test("canonical JSON refuses non-ASCII text and fractions", () => {
  assert.equal(canonicalJson({ b: [1, "x\"y\\"], a: null }), '{"a":null,"b":[1,"x\\"y\\\\"]}');
  assert.equal(code(() => canonicalJson({ note: "проверка" })), "runtime_manifest_invalid");
  assert.equal(code(() => canonicalJson({ size: 1.5 })), "runtime_manifest_invalid");
});

test("an archive that differs from its signed digest is refused", () => {
  const manifest = verifyEnvelope(FIXTURE.envelope, FIXTURE_KEYS, "darwin-aarch64");
  assert.equal(code(() => checkArchive(Buffer.from("not the archive"), manifest)), "runtime_digest_mismatch");
});

// How an archive is unpacked: for a Windows runtime on any computer, and on a Mac or Linux for
// a Mac's runtime as well. (Windows unpacks only Windows runtimes.)
const UNPACKING = WINDOWS_HOST ? [{ windows: true }] : [{ windows: false }, { windows: true }];

test("extraction refuses paths, links and entries that leave the directory", () => {
  for (const options of UNPACKING) {
    const directory = temporary("extract");
    for (const hostile of [
      archive([["../escape", "0", "x"]]),
      archive([["/etc/escape", "0", "x"]]),
      archive([["python/../../escape", "0", "x"]]),
      // The separators and the drives of Windows are never part of a name in a runtime.
      archive([["..\\escape", "0", "x"]]),
      archive([["python\\..\\..\\escape", "0", "x"]]),
      archive([["C:\\escape", "0", "x"]]),
      archive([["\\\\server\\share\\escape", "0", "x"]]),
      archive([["python/bin/link", "2", "", "../../../outside"]]),
      archive([["python/bin/link", "2", "", "/etc/passwd"]]),
      archive([["python/bin/link", "2", "", "..\\..\\..\\outside"]]),
      archive([["python/bin/hard", "1", "", "python/bin/python3"]]),
      archive([["python/device", "3"]]),
      archive([["PaxHeader", "x", pax("../../escape")], ["short", "0", "x"]]),
      Buffer.from("not gzip"),
    ]) {
      assert.equal(code(() => extractArchive(hostile, path.join(directory, "out"), options)), "runtime_archive_unsafe");
    }
    assert.deepEqual(fs.readdirSync(directory).filter((name) => name !== "out"), []);
    assert.ok(!fs.existsSync(path.join(os.tmpdir(), "escape")));
  }
});

posix("extraction keeps files, modes, relative links and long names",
  "file modes and symbolic links are what a Mac's runtime holds; Windows has no such modes and a Windows runtime no links", () => {
    const directory = temporary("extract-good");
    const long = "python/lib/" + "deep/".repeat(30) + "module.py";
    extractArchive(archive([
      ["python/bin/python3.12", "0", HEALTHY],
      ["python/bin/python3", "2", "", "python3.12"],
      ["python/lib/data.txt", "0", "data", "", 0o644],
      ["PaxHeader", "x", pax(long)], ["truncated", "0", "long"],
    ]), directory);
    assert.equal(fs.readlinkSync(path.join(directory, "python/bin/python3")), "python3.12");
    assert.equal(fs.statSync(path.join(directory, "python/bin/python3.12")).mode & 0o777, 0o755);
    assert.equal(fs.statSync(path.join(directory, "python/lib/data.txt")).mode & 0o777, 0o644);
    assert.equal(fs.readFileSync(path.join(directory, long), "utf8"), "long");
  });

test("a Windows runtime is unpacked with its files and long names, and without a single link", () => {
  const directory = temporary("extract-windows");
  const long = "python/Lib/" + "deep/".repeat(30) + "module.py";
  extractArchive(archive([
    ["python", "5"], ["python/python.exe", "0", WINDOWS_HEALTHY], ["python/python312.dll", "0", "MZ", "", 0o644],
    ["python/Lib/data.txt", "0", "data", "", 0o444],
    ["PaxHeader", "x", pax(long)], ["truncated", "0", "long"],
  ]), directory, { windows: true });
  assert.equal(fs.readFileSync(path.join(directory, "python", "python.exe"), "utf8"), WINDOWS_HEALTHY);
  assert.equal(fs.readFileSync(path.join(directory, ...long.split("/")), "utf8"), "long");
  assert.deepEqual(fs.readdirSync(path.join(directory, "python")).sort(), ["Lib", "python.exe", "python312.dll"]);
  // No mode of the archive is applied: a file the archive calls read-only can still be removed
  // with its runtime, which Windows refuses for a read-only file.
  fs.writeFileSync(path.join(directory, "python", "Lib", "data.txt"), "writable");
  fs.rmSync(directory, { recursive: true });
  // Windows runtimes carry no links at all.
  assert.equal(code(() => extractArchive(archive([["python/python3.exe", "2", "", "python.exe"]]), temporary("windows"), { windows: true })),
    "runtime_archive_unsafe");
  assert.equal(code(() => extractArchive(archive([["lib", "5"], ["lib/real", "5"], ["lib/alias", "2", "", "real"]]), temporary("windows"), { windows: true })),
    "runtime_archive_unsafe");
});

onEachRuntime("the runtime directory and pointer files are the ones Desktop Companion reads and writes", (shape) => {
  const home = temporary("home");
  const base = runtimeBase(home);
  assert.equal(base, path.join(home, ".agent-platform", "runtime"));
  const key = signer();
  const first = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const pointer = installVerified(base, first.manifest, first.bytes, { spawn: shape.probe });
  const written = JSON.parse(fs.readFileSync(path.join(base, "current.json"), "utf8"));
  // serde's camelCase RuntimePointer in runtime.rs, plus the build time, which 0.4.3 ignores.
  assert.deepEqual(Object.keys(written).sort(), ["capabilities", "createdAt", "executable", "installer", "root", "runtimeId"]);
  assert.deepEqual(written, pointer);
  assert.equal(written.executable, path.join(base, first.manifest.runtimeId, ...shape.executable.split("/")));
  assert.equal(written.installer, path.join(base, first.manifest.runtimeId, "bootstrap", "companion_install.py"));
  assert.equal(written.root, path.join(base, first.manifest.runtimeId));
  assert.equal(builtAt(written), Date.parse("2026-10-01T12:00:00Z"));
  // A pointer Desktop Companion 0.4.3 wrote (pretty JSON, no build time) is read back as it
  // is, and its day comes from the runtime id.
  const { createdAt: _, ...released } = written;
  fs.writeFileSync(path.join(base, "current.json"), JSON.stringify(released, null, 2));
  assert.deepEqual(current(base), released);
  assert.equal(builtAt(current(base)), Date.parse("2026-10-01T00:00:00Z"));
  assert.equal(builtAt({ runtimeId: "custom" }), null);
  fs.rmSync(written.installer);
  assert.equal(current(base), null, "a pointer to a runtime that lost its entry points is not used");
});

onEachRuntime("a newer runtime keeps the previous one, so a job that still starts it keeps working", (shape) => {
  const base = runtimeBase(temporary("home"));
  const key = signer();
  const [one, two, three] = ["20261001-aaaaaaaaaaaa", "20261002-bbbbbbbbbbbb", "20261003-cccccccccccc"]
    .map((id) => published(key, `${id}-py3.12.14`, shape));
  const first = installVerified(base, one.manifest, one.bytes, { spawn: shape.probe });
  installVerified(base, two.manifest, two.bytes, { spawn: shape.probe });
  assert.equal(current(base).runtimeId, two.manifest.runtimeId);
  assert.equal(readPointer(base, "previous.json").runtimeId, one.manifest.runtimeId);
  assert.ok(fs.existsSync(first.executable), "the interpreter the other tool may still be running stays");
  installVerified(base, three.manifest, three.bytes, { spawn: shape.probe });
  assert.deepEqual(fs.readdirSync(base).sort(), [three.manifest.runtimeId, two.manifest.runtimeId, "current.json", "previous.json"].sort());
  // Installing the current runtime again changes nothing.
  installVerified(base, three.manifest, three.bytes, { spawn: shape.probe });
  assert.equal(readPointer(base, "previous.json").runtimeId, two.manifest.runtimeId);
});

onEachRuntime("nothing switches when the new runtime does not start or was changed on the way", (shape) => {
  const base = runtimeBase(temporary("home"));
  const key = signer();
  const good = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  installVerified(base, good.manifest, good.bytes, { spawn: shape.probe });
  const broken = published(key, "20261002-bbbbbbbbbbbb-py3.12.14", shape, { interpreter: shape.broken });
  const failure = code(() => installVerified(base, broken.manifest, broken.bytes, { spawn: shape.probe }));
  assert.equal(failure, "runtime_probe_failed");
  assert.equal(current(base).runtimeId, good.manifest.runtimeId);
  assert.deepEqual(fs.readdirSync(base).sort(), [good.manifest.runtimeId, "current.json"]);
  const changed = Buffer.from(broken.bytes);
  changed[changed.length - 9] ^= 1;
  assert.equal(code(() => installVerified(base, broken.manifest, changed, { spawn: shape.probe })), "runtime_digest_mismatch");
  assert.equal(current(base).runtimeId, good.manifest.runtimeId);
});

onEachRuntime("the published runtime is installed once and found again without a download", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const item = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const served = site([item]);
  const options = { home, origin: ORIGIN, keys: key.keys, platform: shape.platform, fetcher: served.fetcher, spawn: shape.probe };
  const first = await ensureRuntime(options);
  assert.equal(first.pointer.runtimeId, item.manifest.runtimeId);
  assert.equal(served.requests.length, 2);
  const again = await ensureRuntime(options);
  assert.deepEqual(again.pointer, first.pointer);
  assert.equal(served.requests.length, 3, "only the index is read when the runtime is current");
  // Without the network the installed runtime is used; a computer without one cannot start.
  assert.deepEqual(await ensureRuntime({ ...options, fetcher: offline }), { pointer: first.pointer, offline: true, kept: null });
  await assert.rejects(ensureRuntime({ ...options, home: temporary("empty"), fetcher: offline }), { code: "runtime_download_failed" });
});

onEachRuntime("a published runtime that fails a check is an error even when one is installed", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const good = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const options = { home, origin: ORIGIN, keys: key.keys, platform: shape.platform, spawn: shape.probe };
  await ensureRuntime({ ...options, fetcher: site([good]).fetcher });
  const forged = published(signer(), "20261002-bbbbbbbbbbbb-py3.12.14", shape);
  await assert.rejects(ensureRuntime({ ...options, fetcher: site([forged]).fetcher }), { code: "runtime_signature_invalid" });
  const swapped = published(key, "20261003-cccccccccccc-py3.12.14", shape);
  swapped.served = Buffer.from(swapped.bytes);
  swapped.served[swapped.served.length - 9] ^= 1;
  await assert.rejects(ensureRuntime({ ...options, fetcher: site([swapped]).fetcher }), { code: "runtime_digest_mismatch" });
  assert.equal(current(runtimeBase(home)).runtimeId, good.manifest.runtimeId);
  await assert.rejects(ensureRuntime({ ...options, origin: "http://runtime.test", fetcher: site([good]).fetcher }),
    { code: "runtime_download_failed" });
});

test("there is a runtime for the three desktop platforms and no guess for any other", () => {
  assert.equal(platformKey("darwin", "arm64"), "darwin-aarch64");
  assert.equal(platformKey("darwin", "x64"), "darwin-x86_64");
  assert.equal(platformKey("win32", "x64"), "windows-x86_64");
  for (const [platform, arch] of [["linux", "x64"], ["win32", "arm64"], ["win32", "ia32"]]) {
    assert.equal(platformKey(platform, arch), null);
  }
});

test("no PYTHON variable reaches the runtime interpreter", () => {
  assert.deepEqual(isolatedEnvironment({ PATH: "/bin", PYTHONPATH: "/evil", PythonHome: "x", HOME: "/h" }), { PATH: "/bin", HOME: "/h" });
});

onEachRuntime("the launcher runs the command-line client of the runtime and returns its exit code", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const item = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const started = [];
  const lines = [];
  const exit = await main(["status", "--json"], {
    home, keys: key.keys, platform: shape.platform, fetcher: site([item]).fetcher,
    environment: { AGENT_PLATFORM_CONSOLE_ORIGIN: ORIGIN, PYTHONPATH: "/evil", PATH: "/bin" },
    log: (line) => lines.push(line),
    spawn: starter(shape, (program, argv, options) => {
      started.push({ program, argv, options });
      return { status: 2 };
    }),
  });
  assert.equal(exit, 2);
  const root = path.join(runtimeBase(home), item.manifest.runtimeId);
  assert.equal(started.length, 1);
  assert.equal(started[0].program, path.join(root, ...shape.executable.split("/")));
  assert.deepEqual(started[0].argv, ["-E", "-s", path.join(root, "bootstrap", "command_line.py"), "status", "--json"]);
  assert.equal(started[0].options.env.PYTHONPATH, undefined);
  assert.equal(started[0].options.windowsHide, true);
  assert.match(lines[0], /Downloading the Artificial Memory runtime/);
});

onEachRuntime("the runtime is removed only when the client asks for it in its own file", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const item = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const options = (status, act = () => {}) => ({
    home, keys: key.keys, platform: shape.platform, fetcher: site([item]).fetcher,
    environment: { AGENT_PLATFORM_CONSOLE_ORIGIN: ORIGIN }, log: () => {},
    spawn: starter(shape, (program, argv, spawned) => {
      act(spawned.env[LAUNCH_VARIABLE]);
      return { status };
    }),
  });
  // The desktop application and the hourly job use this runtime. None of these may remove it:
  // a refused uninstall, any other command, and an uninstall that only printed its help.
  for (const [argv, status] of [[["uninstall"], 2], [["status"], 0], [["uninstall", "--help"], 0], [["uninstall"], 0]]) {
    assert.equal(await main(argv, options(status)), status);
    assert.ok(current(runtimeBase(home)), argv.join(" "));
  }
  // A request left by an earlier run (interrupted before the directory went) is not this
  // run's: `connect` must not lose the runtime its new hourly job starts. It is cleared.
  const requestFile = path.join(runtimeBase(home), REMOVAL_REQUEST);
  for (const stale of ["", "0123456789abcdef0123456789abcdef", "uninstall"]) {
    fs.writeFileSync(requestFile, stale + "\n");
    assert.equal(await main(["connect"], options(0)), 0);
    assert.ok(current(runtimeBase(home)), `a stale request ${JSON.stringify(stale)}`);
    assert.ok(!fs.existsSync(requestFile));
  }
  // Nor is a request written during this run under another run's name.
  assert.equal(await main(["uninstall"], options(0, () => fs.writeFileSync(requestFile, "someone-else\n"))), 0);
  assert.ok(current(runtimeBase(home)) && !fs.existsSync(requestFile));
  // An uninstall that finished and could not delete its own interpreter leaves the request,
  // naming the run the launcher started.
  const names = [];
  const request = (launch) => {
    names.push(launch);
    fs.writeFileSync(requestFile, launch + "\n");
  };
  assert.equal(await main(["uninstall"], options(0, request)), 0);
  assert.ok(!fs.existsSync(runtimeBase(home)));
  assert.match(names[0], /^[0-9a-f]{32}$/);
  const client = fs.readFileSync(path.join(REPOSITORY, "adapters/client/scripts/command_line.py"), "utf8");
  assert.ok(client.includes(`LAUNCH_VARIABLE = "${LAUNCH_VARIABLE}"`));
  assert.ok(client.includes(`RUNTIME_REMOVAL_REQUEST = "${REMOVAL_REQUEST}"`));
});

posix("a link that leaves only through another link is refused",
  "makes symbolic links, which a Windows runtime never holds (every link is refused there) and a Windows user may not be allowed to make", () => {
    // Each link passes a check of its own text: l1 -> .. stays inside d, and l2 -> l1/.. reads
    // as d again. Followed for real, l2 is the parent of the destination.
    const directory = temporary("chain");
    const destination = path.join(directory, "out");
    const hostile = archive([
      ["d", "5"], ["d/l1", "2", "", ".."], ["d/l2", "2", "", "l1/.."], ["d/l2/escaped", "0", "x"],
    ]);
    assert.equal(code(() => extractArchive(hostile, destination)), "runtime_archive_unsafe");
    assert.ok(!fs.existsSync(path.join(directory, "escaped")));
    assert.deepEqual(fs.readdirSync(directory), ["out"]);
    // A link that is inside when it is made and leads out once a later link appears: it reads as
    // d (b/c/../../.. from d) until d/b -> .. turns it into the parent of the destination.
    const later = path.join(directory, "later");
    assert.equal(code(() => extractArchive(archive([
      ["d", "5"], ["d/a", "2", "", "b/c/../../.."], ["d/b", "2", "", ".."],
    ]), later)), "runtime_archive_unsafe");
    // A file is never written through such a link either: the directory it goes into is resolved
    // for real, one component at a time.
    const through = path.join(directory, "through");
    assert.equal(code(() => extractArchive(archive([
      ["d", "5"], ["d/a", "2", "", "b/c/../../.."], ["d/b", "2", "", ".."], ["d/a/escaped", "0", "x"],
    ]), through)), "runtime_archive_unsafe");
    assert.ok(!fs.existsSync(path.join(directory, "escaped")) && !fs.existsSync(path.join(os.tmpdir(), "escaped")));
    // Writing through a link to a file is refused too.
    assert.equal(code(() => extractArchive(archive([["a", "0", "first"], ["link", "2", "", "a"], ["link", "0", "second"]]), temporary("overwrite"))),
      "runtime_archive_unsafe");
    // A link to a sibling directory inside, and a file through it, are what real runtimes hold.
    const good = temporary("links");
    extractArchive(archive([["lib", "5"], ["lib/real", "5"], ["lib/alias", "2", "", "real"], ["lib/alias/module.py", "0", "ok"]]), good);
    assert.equal(fs.readFileSync(path.join(good, "lib/real/module.py"), "utf8"), "ok");
  });

test("nothing already unpacked is written over, and nothing is written under a file", () => {
  for (const options of UNPACKING) {
    for (const entries of [
      [["a", "0", "first"], ["a", "0", "second"]],
      [["a", "0", "file"], ["a/b", "0", "under a file"]],
      [["a", "5"], ["a", "0", "a file where a directory is"]],
    ]) {
      const directory = temporary("overwrite");
      assert.equal(code(() => extractArchive(archive(entries), directory, options)), "runtime_archive_unsafe");
      assert.equal(fs.statSync(path.join(directory, "a")).isDirectory(), entries[0][1] === "5");
    }
  }
});

onEachRuntime("another process's staging directory is left alone, and an abandoned one is removed", (shape) => {
  const base = runtimeBase(temporary("home"));
  const key = signer();
  const item = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  fs.mkdirSync(path.join(base, "20261002-bbbbbbbbbbbb-py3.12.14.partial-4242"), { recursive: true });
  const abandoned = path.join(base, "20260901-cccccccccccc-py3.12.14.partial");
  fs.mkdirSync(abandoned);
  const longAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000);
  fs.utimesSync(abandoned, longAgo, longAgo);
  installVerified(base, item.manifest, item.bytes, { spawn: shape.probe });
  assert.deepEqual(fs.readdirSync(base).sort(), ["20261001-aaaaaaaaaaaa-py3.12.14", "20261002-bbbbbbbbbbbb-py3.12.14.partial-4242", "current.json"]);
});

onEachRuntime("a newer runtime that cannot be downloaded leaves the installed one in use", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const installed = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const options = { home, origin: ORIGIN, keys: key.keys, platform: shape.platform, spawn: shape.probe };
  const first = await ensureRuntime({ ...options, fetcher: site([installed]).fetcher });
  // The index names a runtime whose archive is not there (404), or the transfer breaks.
  const missing = published(key, "20261005-bbbbbbbbbbbb-py3.12.14", shape);
  const index = site([missing]).fetcher;
  for (const fetcher of [
    async (url, init) => (url.endsWith("latest.json") ? index(url, init) : { ok: false, status: 404, headers: new Headers() }),
    async (url, init) => (url.endsWith("latest.json") ? index(url, init) : offline()),
  ]) {
    assert.deepEqual(await ensureRuntime({ ...options, fetcher }), { pointer: first.pointer, offline: false, kept: "download" });
  }
  // The launcher says so and still runs the command.
  const lines = [];
  const exit = await main(["status"], {
    home, keys: key.keys, platform: shape.platform, environment: { AGENT_PLATFORM_CONSOLE_ORIGIN: ORIGIN },
    fetcher: async (url, init) => (url.endsWith("latest.json") ? index(url, init) : { ok: false, status: 404, headers: new Headers() }),
    log: (line) => lines.push(line), spawn: starter(shape, () => ({ status: 0 })),
  });
  assert.equal(exit, 0);
  assert.match(lines.join("\n"), /could not be downloaded; the installed one is used/);
  // Without an installed runtime the same failure is an error.
  await assert.rejects(ensureRuntime({ ...options, home: temporary("empty"), fetcher: async (url, init) => (
    url.endsWith("latest.json") ? index(url, init) : { ok: false, status: 404, headers: new Headers() }) }),
  { code: "runtime_download_failed" });
});

onEachRuntime("a validly signed older runtime does not replace a newer one", async (shape) => {
  const home = temporary("home");
  const key = signer();
  const newer = published(key, "20261005-bbbbbbbbbbbb-py3.12.14", shape);
  const older = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape);
  const options = { home, origin: ORIGIN, keys: key.keys, platform: shape.platform, spawn: shape.probe };
  const first = await ensureRuntime({ ...options, fetcher: site([newer]).fetcher });
  const served = site([older]);
  assert.deepEqual(await ensureRuntime({ ...options, fetcher: served.fetcher }), { pointer: first.pointer, offline: false, kept: "older" });
  assert.equal(served.requests.length, 1, "the older archive is not even downloaded");
  assert.equal(current(runtimeBase(home)).runtimeId, newer.manifest.runtimeId);
  // The same holds for a pointer Desktop Companion 0.4.3 rewrote without the build time.
  const { createdAt: _, ...released } = first.pointer;
  fs.writeFileSync(path.join(runtimeBase(home), "current.json"), JSON.stringify(released, null, 2));
  assert.equal((await ensureRuntime({ ...options, fetcher: site([older]).fetcher })).kept, "older");
  // A newer one is installed as before.
  const newest = published(key, "20261009-cccccccccccc-py3.12.14", shape);
  assert.equal((await ensureRuntime({ ...options, fetcher: site([newest]).fetcher })).pointer.runtimeId, newest.manifest.runtimeId);
  // A manifest without a readable build time cannot be ordered and is refused.
  const undated = published(key, "20261010-dddddddddddd-py3.12.14", shape);
  undated.manifest.createdAt = "yesterday";
  undated.envelope.signature = sign(null, signingBytes(undated.envelope), key.privateKey).toString("base64");
  await assert.rejects(ensureRuntime({ ...options, fetcher: site([undated]).fetcher }), { code: "runtime_manifest_invalid" });
});

onEachRuntime("a runtime without the command-line client is named as such and nothing is started", async (shape) => {
  const key = signer();
  const item = published(key, "20261001-aaaaaaaaaaaa-py3.12.14", shape, { capabilities: ["schedulerCompanion"] });
  const lines = [];
  const exit = await main(["connect"], {
    home: temporary("home"), keys: key.keys, platform: shape.platform, fetcher: site([item]).fetcher,
    environment: { AGENT_PLATFORM_CONSOLE_ORIGIN: ORIGIN }, log: (line) => lines.push(line),
    spawn: starter(shape, () => assert.fail("nothing but the probe may be started")),
  });
  assert.equal(exit, 1);
  assert.match(lines.at(-1), /does not include the command-line client yet/);
  assert.throws(() => commandLineEntry({ installer: "/nowhere/bootstrap/companion_install.py", capabilities: [CAPABILITY] }),
    { code: "runtime_without_command_line" });
});

test("every refusal names Artificial Memory and says that nothing was installed", () => {
  for (const failure of ["runtime_signature_invalid", "runtime_digest_mismatch", "runtime_archive_unsafe", "runtime_unsupported_platform",
    "runtime_download_failed", "runtime_probe_failed", "runtime_manifest_invalid", "runtime_index_invalid"]) {
    assert.match(describe(new RuntimeError(failure)), /Artificial Memory/);
  }
  assert.match(describe(new Error("boom")), /Artificial Memory could not start/);
  assert.doesNotMatch(describe(new RuntimeError("runtime_signature_invalid")), /Control Plane|sync_client/);
});

test("the runtime is taken from an https origin only", () => {
  assert.equal(runtimeOrigin({}), "https://app.artificial-memory.com");
  assert.equal(runtimeOrigin({ AGENT_PLATFORM_CONSOLE_ORIGIN: "https://staging.example/" }), "https://staging.example");
  for (const value of ["http://app.artificial-memory.com", "https://user:pw@host", "https://host/path", "https://host/?a=1", "file:///x", "nonsense"]) {
    assert.throws(() => runtimeOrigin({ AGENT_PLATFORM_CONSOLE_ORIGIN: value }), { code: "origin_invalid" });
  }
});

test("the package is published in public and ships only the launcher", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(HERE, "../package.json"), "utf8"));
  // Published by hand with a one-time code, where no provenance attestation can be made, so
  // none is asked for.
  assert.deepEqual(manifest.publishConfig, { access: "public" });
  assert.equal(manifest.name, "artificial-memory");
  assert.deepEqual(manifest.dependencies ?? {}, {});
  assert.ok(manifest.files.every((file) => !file.includes("test")));
  assert.deepEqual(Object.keys(manifest.scripts), ["test"], "no install scripts: npx runs nothing but the launcher");
});

/** A launch in which nothing may reach the network or start a program. */
function withoutNetwork(home) {
  const lines = { out: [], log: [] };
  return {
    lines,
    options: {
      home, environment: { AGENT_PLATFORM_CONSOLE_ORIGIN: ORIGIN },
      fetcher: async () => assert.fail("the network is not used to say what the package is"),
      spawn: () => assert.fail("no program is started to say what the package is"),
      out: (line) => lines.out.push(line), log: (line) => lines.log.push(line),
    },
  };
}

test("--version and --help are answered on a fresh computer without downloading the runtime", async () => {
  const home = temporary("home");
  const manifest = JSON.parse(fs.readFileSync(path.join(HERE, "../package.json"), "utf8"));
  const version = withoutNetwork(home);
  assert.equal(await main(["--version"], version.options), 0);
  assert.deepEqual(version.lines.out, [manifest.version]);
  assert.equal(packageVersion(), manifest.version);
  for (const flag of ["--help", "-h"]) {
    const help = withoutNetwork(home);
    assert.equal(await main([flag], help.options), 0);
    assert.deepEqual(help.lines.out, [usage()]);
    assert.deepEqual(help.lines.log, []);
  }
  // Nothing at all is a missing command, as the client says it: the usage on stderr, exit 2.
  const bare = withoutNetwork(home);
  assert.equal(await main([], bare.options), 2);
  assert.deepEqual(bare.lines.log, [usage()]);
  assert.deepEqual(bare.lines.out, []);
  assert.ok(!fs.existsSync(runtimeBase(home)), "no runtime directory is made");
  for (const [name] of COMMANDS) assert.match(usage(), new RegExp(`^  ${name} `, "m"));
});

test("the help of a command is still the client's, so it goes to the runtime", async () => {
  const { options } = withoutNetwork(temporary("home"));
  // The runtime is asked for (and here refused for want of a network): the launcher does not
  // answer for the options of a command.
  const asked = [];
  // A platform the runtime is built for: on any other the launcher refuses before it asks.
  const exit = await main(["connect", "--help"], { ...options, platform: "darwin-aarch64", fetcher: async (url) => {
    asked.push(url);
    throw new TypeError("fetch failed");
  } });
  assert.equal(exit, 1);
  assert.deepEqual(asked, [`${ORIGIN}/downloads/client-runtime/latest.json`]);
});

test("the commands the launcher lists are the commands of the terminal client, with the same descriptions", () => {
  const client = fs.readFileSync(path.join(REPOSITORY, "adapters/client/scripts/command_line.py"), "utf8");
  const declared = [...client.matchAll(/commands\.add_parser\("([a-z-]+)", help="([^"]*)"\)/g)].map((match) => [match[1], match[2]]);
  assert.ok(declared.length > 0, "build_parser() in command_line.py is read");
  assert.deepEqual(COMMANDS, declared);
  const dispatched = [...client.match(/^COMMANDS = \{([^}]*)\}/m)[1].matchAll(/"([a-z-]+)":/g)].map((match) => match[1]);
  assert.deepEqual(COMMANDS.map(([name]) => name).sort(), dispatched.sort());
});

/** An answer without Content-Length whose body arrives in `chunks` pieces of `size` bytes. */
function streamed(chunks, size) {
  const sent = { chunks: 0, cancelled: false };
  const body = new ReadableStream({
    pull(controller) {
      if (sent.chunks === chunks) return controller.close();
      sent.chunks += 1;
      controller.enqueue(new Uint8Array(size));
    },
    cancel() {
      sent.cancelled = true;
    },
  });
  return { sent, response: new Response(body, { status: 200 }) };
}

test("an answer without Content-Length is cut off at the first chunk over the limit", async () => {
  const limit = 1000;
  const { sent, response } = streamed(1_000_000, 300);
  assert.equal(response.headers.get("content-length"), null);
  await assert.rejects(fetchLimited(`${ORIGIN}/big`, limit, async () => response),
    { code: "runtime_download_failed", message: /too large/ });
  // 4 × 300 is the first total over 1000: the transfer ends there, long before the 300 MB the
  // server would send. (A stream may ask for one chunk ahead of the reader.)
  assert.ok(sent.chunks <= 5, `${sent.chunks} chunks were pulled`);
  assert.ok(sent.cancelled);
  // Within the limit the body arrives whole, in whatever pieces it was sent; exactly the limit is allowed.
  assert.equal((await fetchLimited(`${ORIGIN}/small`, limit, async () => streamed(3, 300).response)).length, 900);
  assert.equal((await fetchLimited(`${ORIGIN}/exact`, 900, async () => streamed(3, 300).response)).length, 900);
});

test("a transfer that breaks in the middle is an interrupted download, not a refusal", async () => {
  let sent = 0;
  const body = new ReadableStream({
    pull(controller) {
      sent += 1;
      if (sent > 2) return controller.error(new TypeError("terminated"));
      controller.enqueue(new Uint8Array(10));
    },
  });
  await assert.rejects(fetchLimited(`${ORIGIN}/broken`, 1000, async () => new Response(body, { status: 200 })),
    (error) => error.code === "runtime_download_failed" && /interrupted/.test(error.message));
});
